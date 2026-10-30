"""
api/tests/test_endpoints.py

Integration tests for Step 5 endpoints:
  - POST /incidents/{id}/resolve
  - POST /incidents/{id}/postmortem/publish
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from app.db.session import get_session
from app.main import app
from app.models.orm import DocTypeEnum, Incident, Message, RoleEnum, SeverityEnum, StatusEnum
from app.models.schemas import DocumentUploadResponse
from app.services.llm import LLMResult, LLMUnavailableError
from app.services.postmortem import acquire_resolution_lock, release_resolution_lock


def test_resolve_and_publish_endpoints():
    async def _test():
        inc_id = uuid.uuid4()
        now = datetime.now(timezone.utc)

        fake_incident = Incident(
            id=inc_id,
            title="Database Connection Pool Exhaustion",
            description="Connections exhausted on primary replica",
            severity=SeverityEnum.high,
            status=StatusEnum.active,
            created_at=now,
            updated_at=now,
            resolved_at=None,
            postmortem_md=None,
        )

        fake_user_msg = Message(
            id=uuid.uuid4(),
            incident_id=inc_id,
            role=RoleEnum.user,
            content="Pool exhausted, seeing 503s",
            created_at=now,
        )
        fake_asst_msg = Message(
            id=uuid.uuid4(),
            incident_id=inc_id,
            role=RoleEnum.assistant,
            content="Run psql to check pool",
            meta={"cache_status": "miss", "guardrail": "pass", "sources": []},
            created_at=now,
        )

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:

            # ── 1. 404 when incident does not exist ──────────────────────────
            missing_id = uuid.uuid4()
            with patch("app.routers.incidents.async_session_factory") as mock_factory:
                mock_session = AsyncMock()
                mock_factory.return_value.__aenter__.return_value = mock_session
                # Return None for incident
                mock_incident_res = MagicMock()
                mock_incident_res.scalar_one_or_none.return_value = None
                mock_session.execute.return_value = mock_incident_res

                resp = await client.post(
                    f"/incidents/{missing_id}/resolve",
                    json={"generate_postmortem": False},
                )
                assert resp.status_code == 404
                assert resp.json()["detail"] == "Incident not found"

            # ── 2. 409 when incident is already resolved ─────────────────────
            resolved_incident = Incident(
                id=inc_id,
                title="Resolved Inc",
                severity=SeverityEnum.low,
                status=StatusEnum.resolved,
                created_at=now,
                updated_at=now,
                resolved_at=now,
                postmortem_md="Already resolved",
            )
            with patch("app.routers.incidents.async_session_factory") as mock_factory:
                mock_session = AsyncMock()
                mock_factory.return_value.__aenter__.return_value = mock_session
                mock_res = MagicMock()
                mock_res.scalar_one_or_none.return_value = resolved_incident
                mock_session.execute.return_value = mock_res

                resp = await client.post(
                    f"/incidents/{inc_id}/resolve",
                    json={"generate_postmortem": False},
                )
                assert resp.status_code == 409
                assert resp.json()["detail"] == "Incident is already resolved"

            # ── 3. 409 when resolution already in progress ───────────────────
            await acquire_resolution_lock(inc_id)
            try:
                with patch("app.routers.incidents.async_session_factory") as mock_factory:
                    mock_session = AsyncMock()
                    mock_factory.return_value.__aenter__.return_value = mock_session
                    mock_res = MagicMock()
                    mock_res.scalar_one_or_none.return_value = fake_incident
                    mock_msgs_res = MagicMock()
                    mock_msgs_res.scalars.return_value.all.return_value = [fake_user_msg]
                    mock_session.execute.side_effect = [mock_res, mock_msgs_res]

                    resp = await client.post(
                        f"/incidents/{inc_id}/resolve",
                        json={"generate_postmortem": False},
                    )
                    assert resp.status_code == 409
                    assert resp.json()["detail"] == "Resolution already in progress"
            finally:
                await release_resolution_lock(inc_id)

            # ── 4. 422 when generate_postmortem=True and no user msg and no summary ──
            with patch("app.routers.incidents.async_session_factory") as mock_factory:
                mock_session = AsyncMock()
                mock_factory.return_value.__aenter__.return_value = mock_session
                mock_res = MagicMock()
                mock_res.scalar_one_or_none.return_value = fake_incident
                mock_msgs_res = MagicMock()
                # Empty message list
                mock_msgs_res.scalars.return_value.all.return_value = []
                mock_session.execute.side_effect = [mock_res, mock_msgs_res]

                resp = await client.post(
                    f"/incidents/{inc_id}/resolve",
                    json={"generate_postmortem": True, "resolution_summary": None},
                )
                assert resp.status_code == 422
                assert resp.json()["detail"] == "Nothing to summarize"

            # ── 5. 200 Success resolving with postmortem ─────────────────────
            mock_postmortem = """# Post-mortem: Database Connection Pool Exhaustion

| Metric | Value |
|---|---|
| **Severity** | HIGH |

## Summary
Pool exhausted.

## What happened
503 errors.

## What was tried
Restarted pool.

## Root cause
Connection leak.

## Resolution
Increased max connections.

## Follow-ups
Not stated.

## Timeline (UTC)
| Time (UTC) | Actor | Event |

---
*Generated by Incident Copilot from 2 messages. Review before sharing.*
"""
            updated_incident = Incident(
                id=inc_id,
                title="Database Connection Pool Exhaustion",
                description="Connections exhausted on primary replica",
                severity=SeverityEnum.high,
                status=StatusEnum.resolved,
                created_at=now,
                updated_at=now,
                resolved_at=now,
                postmortem_md=mock_postmortem,
            )

            with patch("app.routers.incidents.async_session_factory") as mock_factory, \
                 patch("app.routers.incidents.generate_postmortem_report", return_value=mock_postmortem) as mock_gen:
                mock_read_session = AsyncMock()
                mock_write_session = AsyncMock()
                mock_factory.return_value.__aenter__.side_effect = [
                    mock_read_session,
                    mock_write_session,
                ]

                # Read session setup
                mock_res1 = MagicMock()
                mock_res1.scalar_one_or_none.return_value = fake_incident
                mock_msgs_res1 = MagicMock()
                mock_msgs_res1.scalars.return_value.all.return_value = [fake_user_msg, fake_asst_msg]
                mock_read_session.execute.side_effect = [mock_res1, mock_msgs_res1]

                # Write session setup
                active_incident_for_write = Incident(
                    id=inc_id,
                    title="Database Connection Pool Exhaustion",
                    description="Connections exhausted on primary replica",
                    severity=SeverityEnum.high,
                    status=StatusEnum.active,
                    created_at=now,
                    updated_at=now,
                    resolved_at=None,
                    postmortem_md=None,
                )
                mock_res2 = MagicMock()
                mock_res2.scalar_one_or_none.return_value = active_incident_for_write
                mock_msgs_res2 = MagicMock()
                mock_msgs_res2.scalars.return_value.all.return_value = [fake_user_msg, fake_asst_msg]
                mock_write_session.execute.side_effect = [mock_res2, mock_msgs_res2]

                resp = await client.post(
                    f"/incidents/{inc_id}/resolve",
                    json={
                        "resolution_summary": "Replaced read replica and flushed connections",
                        "generate_postmortem": True,
                    },
                )
                assert resp.status_code == 200
                data = resp.json()
                assert data["status"] == "resolved"
                assert data["resolved_at"] is not None
                assert "## Summary" in data["postmortem_md"]
                assert len(data["messages"]) == 2

            # ── 6. Publish postmortem: 409 if not resolved ───────────────────
            mock_sess = AsyncMock()
            async def override_get_session():
                yield mock_sess

            app.dependency_overrides[get_session] = override_get_session
            try:
                mock_sess.get.return_value = fake_incident  # active incident

                resp = await client.post(f"/incidents/{inc_id}/postmortem/publish")
                assert resp.status_code == 409
                assert resp.json()["detail"] == "Incident is not resolved or post-mortem is empty"

                # ── 7. Publish postmortem: 201 Success ───────────────────────────
                doc_uuid = uuid.uuid4()
                fake_upload_resp = DocumentUploadResponse(
                    id=doc_uuid,
                    filename="postmortem-12345678-database-connection-pool-exhaustion.md",
                    doc_type=DocTypeEnum.postmortem,
                    chunk_count=3,
                    char_count=1200,
                    replaced=False,
                )

                with patch("app.routers.incidents.ingest_document_bytes", return_value=fake_upload_resp) as mock_ingest:
                    mock_sess.get.return_value = updated_incident

                    resp = await client.post(f"/incidents/{inc_id}/postmortem/publish")
                    assert resp.status_code == 201
                    pub_data = resp.json()
                    assert pub_data["document_id"] == str(doc_uuid)
                    assert pub_data["doc_type"] == "postmortem"
                    assert pub_data["chunk_count"] == 3
                    assert pub_data["replaced"] is False
                    assert mock_ingest.called
            finally:
                app.dependency_overrides.clear()

    asyncio.run(_test())
