"""
api/app/services/answer.py

Incident triage RAG answer pipeline with Redis caching, single-flight locking,
LangSmith observability, and whole-command guardrail verification.

Privacy & Observability Notice:
  Traces and logs intentionally exclude sensitive data:
  - Application logging only outputs timings, metrics, and incident IDs.
  - Question text, document chunks, and raw completions are never written to logs.
  - LangSmith tracing sends queries and prompt chunks to the configured project;
    tracing is disabled by default (LANGSMITH_TRACING=false).
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
import uuid
from typing import Any, List, Optional, Tuple

from app.config import get_settings
from app.db.session import async_session_factory
from app.models.orm import DocTypeEnum
from app.models.schemas import SearchHit, SearchQueryRequest
from app.services.cache import (
    get_cached_answer,
    get_kb_version,
    set_cached_answer,
    single_flight_lock,
)
from app.services.guardrail import GuardrailResult, verify_guardrail
from app.services.knowledge import search_knowledge
from app.services.llm import LLMResult, get_llm_provider
from app.services.prompts import PROMPT_VERSION, SYSTEM_PROMPT, format_user_prompt
from app.services.ratelimit import daily_llm_limiter

# LangSmith tracing integration (fails safe if langsmith is not configured or throws)
try:
    from langsmith import traceable
except ImportError:
    def traceable(name: Optional[str] = None, run_type: Optional[str] = None, **kwargs):
        def decorator(func):
            return func
        return decorator

logger = logging.getLogger(__name__)


@traceable(name="retrieval", run_type="retriever")
async def _retrieve_context(
    question: str,
    retrieval_k: int,
) -> Tuple[List[SearchHit], float]:
    """
    Retrieves document chunks using semantic search:
      - Union of top k overall chunks and top 3 chunks with doc_type=runbook.
      - Deduplicated by chunk_id.
      - Ordered descending by similarity score.
      - Total text characters capped at 12,000.

    CRITICAL: Opens and closes an isolated read session immediately. No database
    connection is retained during the subsequent LLM generation.
    """
    start_time = time.perf_counter()

    async with async_session_factory() as session:
        res_all = await search_knowledge(
            SearchQueryRequest(query=question, k=retrieval_k),
            session=session,
        )
        res_runbooks = await search_knowledge(
            SearchQueryRequest(query=question, k=3, doc_type=DocTypeEnum.runbook),
            session=session,
        )

    # Union and deduplicate by chunk_id
    seen_ids = set()
    combined: List[SearchHit] = []
    for hit in res_all.hits + res_runbooks.hits:
        if hit.chunk_id not in seen_ids:
            seen_ids.add(hit.chunk_id)
            combined.append(hit)

    # Order by similarity score descending
    combined.sort(key=lambda h: h.score, reverse=True)

    # Cap total character budget at 12,000
    capped: List[SearchHit] = []
    char_count = 0
    for hit in combined:
        hit_len = len(hit.text)
        if char_count + hit_len > 12000 and capped:
            break
        capped.append(hit)
        char_count += hit_len

    retrieval_ms = round((time.perf_counter() - start_time) * 1000, 2)
    return capped, retrieval_ms


@traceable(name="llm_generate", run_type="llm")
async def _call_llm(system: str, user: str) -> Tuple[LLMResult, float]:
    """Invokes the configured LLM provider and measures latency."""
    start_time = time.perf_counter()
    provider = get_llm_provider()
    result = await provider.generate(system=system, user=user)
    llm_ms = round((time.perf_counter() - start_time) * 1000, 2)
    return result, llm_ms


@traceable(name="guardrail_verify", run_type="tool")
def _run_guardrail(answer: str, chunks: List[SearchHit]) -> GuardrailResult:
    """Applies whole-command verification against retrieved runbook chunks."""
    return verify_guardrail(answer, chunks)


@traceable(name="ask_pipeline", run_type="chain")
async def process_question(
    incident_id: uuid.UUID,
    question: str,
) -> Tuple[str, dict]:
    """
    Orchestrates the full incident question-answering pipeline.

    Steps:
      1. Normalizes question for deterministic caching.
      2. Computes cache key: SHA-256 of kb_version|provider|model|prompt_version|norm_q.
      3. Acquires single-flight lock for this cache key:
         - Checks Redis cache: returns immediately on hit.
         - On miss: retrieves chunks, verifies relevance threshold, formats prompt,
           calls LLM, verifies commands via guardrail, saves to cache.
      4. Logs only metrics and timing (no sensitive text).

    Returns:
      (answer_text, meta_dict)
    """
    settings = get_settings()
    start_time = time.perf_counter()

    # 1. Normalize question
    norm_q = re.sub(r"\s+", " ", question.strip().lower())
    kb_version = get_kb_version()

    # 2. Compute cache key
    cache_payload = f"{kb_version}|{settings.llm_provider}|{settings.gemini_model}|{PROMPT_VERSION}|{norm_q}"
    cache_key = "answer:" + hashlib.sha256(cache_payload.encode("utf-8")).hexdigest()

    # 3. Single-flight lock around cache check and generation
    async with single_flight_lock(cache_key):
        cached = await get_cached_answer(cache_key)

        # Cache Hit
        if isinstance(cached, dict):
            content = cached.get("content", "")
            meta = dict(cached.get("meta", {}))
            meta["cache_status"] = "hit"
            meta["tokens_in"] = 0
            meta["tokens_out"] = 0
            meta["llm_ms"] = 0.0
            meta["response_ms"] = round((time.perf_counter() - start_time) * 1000, 2)

            logger.info(
                "Answer cache HIT for incident=%s [total_ms=%.2f]",
                incident_id,
                meta["response_ms"],
            )
            return content, meta

        cache_status = "bypass" if cached == "bypass" else "miss"

        # Cache Miss — Retrieve context chunks
        hits, ret_ms = await _retrieve_context(question, settings.retrieval_k)

        # Re-check relevance threshold
        best_score = hits[0].score if hits else 0.0
        if best_score < settings.retrieval_min_score:
            total_ms = round((time.perf_counter() - start_time) * 1000, 2)
            meta = {
                "cache_status": cache_status,
                "guardrail": "no_context",
                "response_ms": total_ms,
                "retrieval_ms": ret_ms,
                "llm_ms": 0.0,
                "provider": settings.llm_provider,
                "model": settings.gemini_model,
                "kb_version": kb_version,
                "tokens_in": 0,
                "tokens_out": 0,
                "sources": [],
                "unverified_commands": [],
                "raw_answer": None,
                "destructive_commands": [],
                "truncated": False,
            }
            logger.info(
                "Answer NO CONTEXT for incident=%s [best_score=%.4f < %.2f, total_ms=%.2f]",
                incident_id,
                best_score,
                settings.retrieval_min_score,
                total_ms,
            )
            return "I could not find this in the documentation.", meta

        # Format user prompt with neutralized delimiters
        user_prompt = format_user_prompt(hits, question)

        # Call LLM provider (atomically counts real LLM call against daily budget)
        await daily_llm_limiter.acquire_call("POST /incidents/{id}/ask")
        llm_result, llm_ms = await _call_llm(SYSTEM_PROMPT, user_prompt)

        # Run guardrail verification
        guardrail_result = _run_guardrail(llm_result.text, hits)

        # Build citation metadata
        sources = [
            {
                "n": idx,
                "chunk_id": h.chunk_id,
                "filename": h.filename,
                "doc_type": h.doc_type.value if hasattr(h.doc_type, "value") else str(h.doc_type),
                "section": h.section or "General",
                "score": round(h.score, 4),
            }
            for idx, h in enumerate(hits, start=1)
        ]

        total_ms = round((time.perf_counter() - start_time) * 1000, 2)

        meta = {
            "cache_status": cache_status,
            "guardrail": guardrail_result.guardrail,
            "response_ms": total_ms,
            "retrieval_ms": ret_ms,
            "llm_ms": llm_ms,
            "provider": settings.llm_provider,
            "model": settings.gemini_model,
            "kb_version": kb_version,
            "tokens_in": llm_result.tokens_in,
            "tokens_out": llm_result.tokens_out,
            "sources": sources,
            "unverified_commands": guardrail_result.unverified_commands,
            "raw_answer": guardrail_result.raw_answer,
            "destructive_commands": guardrail_result.destructive_commands,
            "truncated": llm_result.truncated,
        }

        # Save to Redis cache
        await set_cached_answer(
            cache_key,
            {"content": guardrail_result.text, "meta": meta},
            settings.answer_cache_ttl_s,
        )

        logger.info(
            "Answer generated for incident=%s: status=%s guardrail=%s total_ms=%.2f ret_ms=%.2f llm_ms=%.2f in=%d out=%d",
            incident_id,
            cache_status,
            guardrail_result.guardrail,
            total_ms,
            ret_ms,
            llm_ms,
            llm_result.tokens_in,
            llm_result.tokens_out,
        )

        return guardrail_result.text, meta


__all__ = ["process_question"]
