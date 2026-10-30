"""
api/tests/test_step5.py

Comprehensive test suite for Step 5:
  - Post-mortem report helpers: slugify_title, format_duration, build_header_table,
    build_timeline_table, format_transcript, build_user_prompt, build_audit_footer
  - Heading validation and retry behavior
  - Concurrency lock
  - Post-mortem generation with guardrail verification
  - Pydantic schemas (ResolveIncidentRequest, PublishPostmortemResponse, DocumentUploadResponse)
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch
import uuid

import pytest
from pydantic import ValidationError

from app.models.orm import DocTypeEnum, Incident, Message, RoleEnum, SeverityEnum, StatusEnum
from app.models.schemas import (
    DocumentUploadResponse,
    IncidentDetail,
    IncidentOut,
    PublishPostmortemResponse,
    ResolveIncidentRequest,
)
from app.services.llm import LLMProvider, LLMResult, LLMUnavailableError
from app.services.postmortem import (
    MANDATORY_HEADINGS,
    acquire_resolution_lock,
    build_audit_footer,
    build_header_table,
    build_timeline_table,
    build_user_prompt,
    format_duration,
    format_transcript,
    generate_postmortem_report,
    has_all_headings,
    release_resolution_lock,
    slugify_title,
)


# ── 1. Schema Validation Tests ───────────────────────────────────────────────

def test_resolve_incident_request_validation():
    # Default values
    req = ResolveIncidentRequest()
    assert req.resolution_summary is None
    assert req.generate_postmortem is True

    # Strips whitespace
    req2 = ResolveIncidentRequest(resolution_summary="   fixed issue   ")
    assert req2.resolution_summary == "fixed issue"

    # Empty string becomes None
    req3 = ResolveIncidentRequest(resolution_summary="   ")
    assert req3.resolution_summary is None

    # Max length 2000
    with pytest.raises(ValidationError):
        ResolveIncidentRequest(resolution_summary="x" * 2001)


def test_publish_postmortem_response():
    doc_id = uuid.uuid4()
    resp = PublishPostmortemResponse(
        document_id=doc_id,
        filename="postmortem-12345678-test.md",
        doc_type="postmortem",
        chunk_count=3,
        replaced=False,
    )
    assert resp.document_id == doc_id
    assert resp.filename == "postmortem-12345678-test.md"
    assert resp.doc_type == "postmortem"
    assert resp.chunk_count == 3
    assert resp.replaced is False


def test_document_upload_response_replaced_field():
    doc_id = uuid.uuid4()
    resp = DocumentUploadResponse(
        id=doc_id,
        filename="runbook.md",
        doc_type=DocTypeEnum.runbook,
        chunk_count=2,
        char_count=500,
        replaced=True,
    )
    assert resp.replaced is True


# ── 2. Helper Functions Tests ────────────────────────────────────────────────

def test_slugify_title():
    assert slugify_title("Primary Database Connection Pool Exhaustion!") == "primary-database-connection-pool-exhaustion"
    assert slugify_title("  High CPU usage on web-01  ") == "high-cpu-usage-on-web-01"
    assert slugify_title("---___---") == "incident"
    long_title = "a" * 100
    assert len(slugify_title(long_title, max_chars=60)) == 60


def test_format_duration():
    start = datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
    # 45 seconds
    end1 = start + timedelta(seconds=45)
    assert format_duration(start, end1) == "45s"

    # 42m 10s
    end2 = start + timedelta(minutes=42, seconds=10)
    assert format_duration(start, end2) == "42m 10s"

    # 1h 24m
    end3 = start + timedelta(hours=1, minutes=24, seconds=5)
    assert format_duration(start, end3) == "1h 24m"

    # Exactly 2 hours
    end4 = start + timedelta(hours=2)
    assert format_duration(start, end4) == "2h"


def test_build_header_table():
    incident = MagicMock(spec=Incident)
    incident.title = "Payment API Latency Spike"
    incident.severity = SeverityEnum.critical
    incident.created_at = datetime(2026, 3, 15, 14, 0, 0, tzinfo=timezone.utc)

    resolved_at = datetime(2026, 3, 15, 15, 30, 0, tzinfo=timezone.utc)
    messages = [MagicMock(spec=Message), MagicMock(spec=Message)]

    header = build_header_table(incident, messages, resolved_at)
    assert "# Post-mortem: Payment API Latency Spike" in header
    assert "| **Severity** | CRITICAL |" in header
    assert "| **Opened (UTC)** | 2026-03-15 14:00:00 |" in header
    assert "| **Resolved (UTC)** | 2026-03-15 15:30:00 |" in header
    assert "| **Duration** | 1h 30m |" in header
    assert "| **Messages** | 2 |" in header


def test_build_timeline_table():
    msg1 = MagicMock(spec=Message)
    msg1.role = RoleEnum.user
    msg1.created_at = datetime(2026, 3, 15, 14, 1, 0, tzinfo=timezone.utc)
    msg1.content = "Checked database connections with ```SELECT * FROM pg_stat_activity``` and noticed pool full | alert"

    msg2 = MagicMock(spec=Message)
    msg2.role = RoleEnum.assistant
    msg2.created_at = datetime(2026, 3, 15, 14, 2, 0, tzinfo=timezone.utc)
    msg2.content = "Here is what to run"
    msg2.meta = {
        "cache_status": "miss",
        "guardrail": "pass",
        "sources": [{"chunk_id": 10}, {"chunk_id": 11}],
    }

    resolved_at = datetime(2026, 3, 15, 14, 10, 0, tzinfo=timezone.utc)
    timeline = build_timeline_table(
        messages=[msg1, msg2],
        resolution_summary="Scaled connection pool max_size to 200",
        resolved_at=resolved_at,
    )

    assert "## Timeline (UTC)" in timeline
    assert "| 2026-03-15 14:01:00 | Engineer | Checked database connections with and noticed pool full \\| alert |" in timeline
    assert "| 2026-03-15 14:02:00 | Copilot | Answered (miss), 2 source(s) |" in timeline
    assert "| 2026-03-15 14:10:00 | Engineer | Resolved: Scaled connection pool max_size to 200 |" in timeline


def test_build_timeline_table_cap_60():
    messages = []
    base_time = datetime(2026, 3, 15, 12, 0, 0, tzinfo=timezone.utc)
    for i in range(70):
        m = MagicMock(spec=Message)
        m.role = RoleEnum.user
        m.created_at = base_time + timedelta(minutes=i)
        m.content = f"Step {i}"
        messages.append(m)

    resolved_at = base_time + timedelta(minutes=75)
    timeline = build_timeline_table(messages, "Resolved", resolved_at)

    assert "*(11 earlier timeline entries omitted)*" in timeline
    # 60 retained rows
    data_lines = [line for line in timeline.splitlines() if line.startswith("| 2026-03-15")]
    assert len(data_lines) == 60


def test_format_transcript_and_neutralization():
    msg1 = MagicMock(spec=Message)
    msg1.role = RoleEnum.user
    msg1.created_at = datetime(2026, 3, 15, 14, 0, 0, tzinfo=timezone.utc)
    msg1.content = "Delimiters test: <<< and >>> breakout"

    msg2 = MagicMock(spec=Message)
    msg2.role = RoleEnum.assistant
    msg2.created_at = datetime(2026, 3, 15, 14, 1, 0, tzinfo=timezone.utc)
    msg2.content = "Safe answer"

    transcript = format_transcript([msg1, msg2])
    assert "<<<" not in transcript
    assert ">>>" not in transcript
    assert "< < <" in transcript
    assert "> > >" in transcript
    assert "[14:00:00 UTC] ENGINEER: Delimiters test: < < < and > > > breakout" in transcript
    assert "[14:01:00 UTC] COPILOT: Safe answer" in transcript


def test_has_all_headings():
    complete_report = """
## Summary
The system experienced latency.

## What happened
High database load occurred.

## What was tried
Restarted replica.

## Root cause
Connection leak.

## Resolution
Patched connection release logic.

## Follow-ups
Add pool telemetry.
"""
    assert has_all_headings(complete_report) is True

    incomplete_report = """
## Summary
Text
## What happened
Text
## What was tried
Text
## Root cause
Text
## Resolution
Text
"""
    assert has_all_headings(incomplete_report) is False


def test_build_audit_footer():
    footer1 = build_audit_footer(10, 0)
    assert "*Generated by Incident Copilot from 10 messages. Review before sharing.*" in footer1
    assert "removed by safety guardrail" not in footer1

    footer2 = build_audit_footer(10, 2)
    assert "*2 command(s) removed by safety guardrail because they were not found in runbooks.*" in footer2


# ── 3. Concurrency Lock Tests ────────────────────────────────────────────────

def test_concurrency_lock():
    async def _test():
        inc_id = uuid.uuid4()
        assert await acquire_resolution_lock(inc_id) is True
        # Second acquisition fails
        assert await acquire_resolution_lock(inc_id) is False

        await release_resolution_lock(inc_id)
        # Now can acquire again
        assert await acquire_resolution_lock(inc_id) is True
        await release_resolution_lock(inc_id)

    asyncio.run(_test())


# ── 4. Post-Mortem Generation Tests ──────────────────────────────────────────

def test_generate_postmortem_success():
    async def _test():
        incident = MagicMock(spec=Incident)
        incident.id = uuid.uuid4()
        incident.title = "Redis Cache Eviction Surge"
        incident.severity = SeverityEnum.high
        incident.description = "Keys expired simultaneously"
        incident.created_at = datetime(2026, 4, 1, 10, 0, 0, tzinfo=timezone.utc)

        msg1 = MagicMock(spec=Message)
        msg1.role = RoleEnum.user
        msg1.created_at = datetime(2026, 4, 1, 10, 5, 0, tzinfo=timezone.utc)
        msg1.content = "Redis memory reached maxmemory limit"

        msg2 = MagicMock(spec=Message)
        msg2.role = RoleEnum.assistant
        msg2.created_at = datetime(2026, 4, 1, 10, 6, 0, tzinfo=timezone.utc)
        msg2.content = "Check key eviction stats with redis-cli info"
        msg2.meta = {
            "cache_status": "miss",
            "guardrail": "pass",
            "sources": [{"chunk_id": 1}],
        }

        runbook_chunk = MagicMock()
        runbook_chunk.doc_type = DocTypeEnum.runbook
        runbook_chunk.text = "Run: `redis-cli info stats` to check evictions."

        resolved_at = datetime(2026, 4, 1, 10, 30, 0, tzinfo=timezone.utc)

        mock_llm_text = """
## Summary
Redis cache evictions increased due to high load.

## What happened
Memory reached limit causing evictions.

## What was tried
Checked evictions using `redis-cli info stats`.

## Root cause
TTL expired simultaneously.

## Resolution
Increased memory and jittered TTL.

## Follow-ups
Not stated.
"""
        mock_llm_result = LLMResult(
            text=mock_llm_text,
            tokens_in=100,
            tokens_out=80,
            model="gemini-3.5-flash",
        )

        mock_provider = AsyncMock(spec=LLMProvider)
        mock_provider.generate.return_value = mock_llm_result

        with patch("app.services.postmortem.get_llm_provider", return_value=mock_provider):
            report = await generate_postmortem_report(
                incident=incident,
                messages=[msg1, msg2],
                resolution_summary="Jittered TTL expirations",
                runbook_chunks=[runbook_chunk],
                resolved_at=resolved_at,
            )

        assert "# Post-mortem: Redis Cache Eviction Surge" in report
        assert "## Summary" in report
        assert "## What happened" in report
        assert "## What was tried" in report
        assert "## Root cause" in report
        assert "## Resolution" in report
        assert "## Follow-ups" in report
        assert "## Timeline (UTC)" in report
        assert "*Generated by Incident Copilot from 2 messages. Review before sharing.*" in report
        # The verified command was retained
        assert "`redis-cli info stats`" in report

    asyncio.run(_test())


def test_generate_postmortem_heading_retry_and_fail():
    async def _test():
        incident = MagicMock(spec=Incident)
        incident.id = uuid.uuid4()
        incident.title = "Test Incident"
        incident.severity = SeverityEnum.low
        incident.description = "Test"
        incident.created_at = datetime(2026, 4, 1, 10, 0, 0, tzinfo=timezone.utc)

        msg = MagicMock(spec=Message)
        msg.role = RoleEnum.user
        msg.created_at = datetime(2026, 4, 1, 10, 1, 0, tzinfo=timezone.utc)
        msg.content = "Test message"

        # Missing ## Follow-ups heading in both attempts
        bad_llm_result = LLMResult(
            text="## Summary\nBad\n## What happened\nBad\n## What was tried\nBad\n## Root cause\nBad\n## Resolution\nBad",
            tokens_in=50,
            tokens_out=40,
            model="gemini-3.5-flash",
        )

        mock_provider = AsyncMock(spec=LLMProvider)
        mock_provider.generate.return_value = bad_llm_result

        with patch("app.services.postmortem.get_llm_provider", return_value=mock_provider):
            with pytest.raises(LLMUnavailableError, match="required sections"):
                await generate_postmortem_report(
                    incident=incident,
                    messages=[msg],
                    resolution_summary="Done",
                    runbook_chunks=[],
                    resolved_at=datetime.now(timezone.utc),
                )

        # Should have retried exactly once (2 calls total)
        assert mock_provider.generate.call_count == 2

    asyncio.run(_test())


# ── 5. Ingest Document Bytes Validation Tests ────────────────────────────────

def test_ingest_document_bytes_validation():
    from fastapi import HTTPException
    from app.services.knowledge import ingest_document_bytes

    async def _test():
        mock_session = AsyncMock()

        # Filename empty or too long
        with pytest.raises(HTTPException) as exc_info:
            await ingest_document_bytes("", DocTypeEnum.runbook, b"content", mock_session)
        assert exc_info.value.status_code == 422

        # Unsupported extension
        with pytest.raises(HTTPException) as exc_info:
            await ingest_document_bytes("test.exe", DocTypeEnum.runbook, b"content", mock_session)
        assert exc_info.value.status_code == 422
        assert "Unsupported file type" in exc_info.value.detail

        # Empty content
        with pytest.raises(HTTPException) as exc_info:
            await ingest_document_bytes("test.md", DocTypeEnum.runbook, b"", mock_session)
        assert exc_info.value.status_code == 422
        assert "Uploaded file is empty" in exc_info.value.detail

    asyncio.run(_test())
