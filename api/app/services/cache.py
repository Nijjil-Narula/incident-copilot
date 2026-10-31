"""
api/app/services/cache.py

Redis caching client with circuit breaker, single-flight locking, and
knowledge base version management for Incident Copilot.

Key components:
  1. Redis fail-open cache with a 30-second circuit breaker.
  2. In-memory KB version calculation (SHA-256 hash of sorted document records).
  3. Single-flight per-key asyncio.Lock with waiter reference-counting
     (relies on a single uvicorn worker process per API container).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator, Dict, List, Optional, Tuple, Union

import redis.asyncio as aioredis
from sqlalchemy import select

from app.config import get_settings
from app.models.orm import Document

logger = logging.getLogger(__name__)

# ── Redis Client & Circuit Breaker ──────────────────────────────────────────
_redis_client: Optional[aioredis.Redis] = None
_last_failure_time: float = float("-inf")
CIRCUIT_BREAKER_COOLDOWN_S: float = 30.0


def get_redis_client() -> aioredis.Redis:
    """Returns or lazily creates the Redis async client singleton."""
    global _redis_client
    if _redis_client is None:
        settings = get_settings()
        _redis_client = aioredis.from_url(
            settings.redis_url,
            socket_timeout=2.0,
            decode_responses=True,
        )
    return _redis_client


async def close_redis_client() -> None:
    """Closes the Redis async connection cleanly on application shutdown."""
    global _redis_client
    if _redis_client is not None:
        try:
            await _redis_client.aclose()
        except Exception as exc:
            logger.warning("Error closing Redis client: %s", type(exc).__name__)
        finally:
            _redis_client = None


def record_redis_failure() -> None:
    """Trips the 30-second Redis failure circuit breaker."""
    global _last_failure_time
    _last_failure_time = time.monotonic()


def is_circuit_open() -> bool:
    """Checks whether the 30-second Redis failure circuit breaker is active."""
    return (time.monotonic() - _last_failure_time) < CIRCUIT_BREAKER_COOLDOWN_S


async def get_cached_answer(cache_key: str) -> Union[dict, str, None]:
    """
    Retrieves a cached answer dictionary from Redis.

    Returns:
      - dict: On cache hit.
      - None: On cache miss (key not present).
      - "bypass": If circuit breaker is tripped or Redis operation fails.
    """
    global _last_failure_time

    if is_circuit_open():
        return "bypass"

    try:
        client = get_redis_client()
        raw = await client.get(cache_key)
        if raw is None:
            return None
        return json.loads(raw)
    except (aioredis.RedisError, asyncio.TimeoutError, TimeoutError, Exception) as exc:
        _last_failure_time = time.monotonic()
        logger.warning(
            "Redis read failure for key '%s': %s. Circuit breaker tripped for %ss.",
            cache_key,
            type(exc).__name__,
            CIRCUIT_BREAKER_COOLDOWN_S,
        )
        return "bypass"


async def set_cached_answer(cache_key: str, data: dict, ttl_s: int) -> None:
    """
    Writes an answer dictionary to Redis with the specified TTL.

    Fails open: logs a warning and trips the circuit breaker on failure without
    raising an exception to the caller.
    """
    global _last_failure_time

    if is_circuit_open():
        return

    try:
        client = get_redis_client()
        await client.set(cache_key, json.dumps(data), ex=ttl_s)
    except (aioredis.RedisError, asyncio.TimeoutError, TimeoutError, Exception) as exc:
        _last_failure_time = time.monotonic()
        logger.warning(
            "Redis write failure for key '%s': %s. Circuit breaker tripped for %ss.",
            cache_key,
            type(exc).__name__,
            CIRCUIT_BREAKER_COOLDOWN_S,
        )


# ── Knowledge Base Version (kb_version) ─────────────────────────────────────
_current_kb_version: str = ""
_kb_lock = asyncio.Lock()


async def recompute_kb_version(session_factory) -> str:
    """
    Recomputes the knowledge base version hash from PostgreSQL documents.

    Queries all documents: doc_type, filename, sha256.
    Formats lines as: '{doc_type}:{filename}:{sha256}', sorted alphabetically.
    Returns the first 16 characters of the SHA-256 hash.
    Maintained in memory and updated atomically under _kb_lock.
    """
    global _current_kb_version

    async with _kb_lock:
        async with session_factory() as session:
            stmt = select(Document.doc_type, Document.filename, Document.sha256)
            res = await session.execute(stmt)
            rows = res.all()

        lines: List[str] = []
        for doc_type, filename, sha256 in rows:
            dt_str = doc_type.value if hasattr(doc_type, "value") else str(doc_type)
            lines.append(f"{dt_str}:{filename}:{sha256}")

        lines.sort()
        payload = "\n".join(lines).encode("utf-8") if lines else b""
        new_version = hashlib.sha256(payload).hexdigest()[:16]

        _current_kb_version = new_version
        logger.info("Knowledge base version recomputed: %s (%d docs)", new_version, len(lines))
        return _current_kb_version


def get_kb_version() -> str:
    """
    Returns the current in-memory knowledge base version hash.
    Falls back to the hash of empty input if not yet initialized.
    """
    if not _current_kb_version:
        return hashlib.sha256(b"").hexdigest()[:16]
    return _current_kb_version


# ── Single-Flight Lock with Waiter Reference Counting ────────────────────────
# Maps cache_key -> [asyncio.Lock, waiter_count]
# NOTE: This in-memory lock relies on a single uvicorn worker process.
# In a multi-worker cluster, a distributed Redis mutex would be used instead.
_single_flight_locks: Dict[str, List[Any]] = {}
_flight_master_lock = asyncio.Lock()


@asynccontextmanager
async def single_flight_lock(key: str) -> AsyncGenerator[None, None]:
    """
    Async context manager implementing single-flight request coalescing.

    Ensures that identical concurrent requests wait on a shared lock rather than
    initiating duplicate LLM generations. Locks are reference-counted and evicted
    from memory as soon as the last waiter exits.
    """
    async with _flight_master_lock:
        if key not in _single_flight_locks:
            _single_flight_locks[key] = [asyncio.Lock(), 0]
        entry = _single_flight_locks[key]
        entry[1] += 1
        lock: asyncio.Lock = entry[0]

    acquired = False
    try:
        await lock.acquire()
        acquired = True
        yield
    finally:
        if acquired:
            lock.release()
        async with _flight_master_lock:
            entry[1] -= 1
            if entry[1] <= 0:
                _single_flight_locks.pop(key, None)


__all__ = [
    "get_redis_client",
    "close_redis_client",
    "record_redis_failure",
    "is_circuit_open",
    "get_cached_answer",
    "set_cached_answer",
    "recompute_kb_version",
    "get_kb_version",
    "single_flight_lock",
]
