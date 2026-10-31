"""
api/app/services/ratelimit.py

In-memory sliding-window rate limiter and daily LLM call cap service for Incident Copilot.

Architecture & Concurrency Notes:
  - This in-memory limiter relies on a single uvicorn worker process. Process-local state
    is stored in deques and dictionaries protected by asyncio.Lock.
  - Limits are GLOBAL across the application, not per-IP, because the FastAPI API container
    is addressed only by the Streamlit UI container over the private Docker network.
  - The daily LLM call counter tracks real LLM invocations and resets at 00:00 UTC.
    Cache hits and 'no_context' early exits bypass the daily counter completely.
  - Logging never records prompt questions, incident IDs, or request payloads; only the
    endpoint identifier and limit event are logged.
"""

from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime, timedelta, timezone
import logging
import math
import time
from typing import Dict, Optional

from fastapi import HTTPException, status

from app.config import get_settings
from app.services.cache import get_redis_client, is_circuit_open, record_redis_failure

logger = logging.getLogger(__name__)


class SlidingWindowLimiter:
    """
    In-memory sliding-window rate limiter tracking request timestamps over a 60-second window.

    Relies on the single uvicorn worker. Protected by an asyncio.Lock for task-safe concurrency.
    """

    def __init__(self, window_s: float = 60.0):
        self._window_s = window_s
        self._timestamps: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def check(self, limit_per_min: int, endpoint_name: str) -> None:
        """
        Validates whether a request can proceed under the per-minute limit.

        Raises HTTP 429 with a Retry-After header if the request exceeds the limit.
        """
        async with self._lock:
            now = time.monotonic()
            cutoff = now - self._window_s

            # Purge timestamps older than 60 seconds
            while self._timestamps and self._timestamps[0] <= cutoff:
                self._timestamps.popleft()

            if len(self._timestamps) >= limit_per_min:
                logger.warning("Rate limit exceeded for endpoint '%s'", endpoint_name)
                oldest = self._timestamps[0]
                retry_after = max(1, int(math.ceil(self._window_s - (now - oldest))))
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail="Rate limit exceeded. Please try again shortly.",
                    headers={"Retry-After": str(retry_after)},
                )

            self._timestamps.append(now)

    def reset(self) -> None:
        """Resets the sliding window timestamps (for testing)."""
        self._timestamps.clear()


class DailyLLMCapLimiter:
    """
    In-memory daily LLM call budget manager.

    Maintains a daily counter that resets at 00:00 UTC. Check-and-increment operations
    are executed atomically inside a single asyncio.Lock at the moment a real LLM call
    is attempted.
    """

    def __init__(self):
        self._current_day_utc: str = ""
        self._call_count: int = 0
        self._lock = asyncio.Lock()

    def _rollover_if_needed(self, now_utc: datetime) -> None:
        """Resets the daily counter if the current UTC day has advanced."""
        today_str = now_utc.strftime("%Y-%m-%d")
        if today_str != self._current_day_utc:
            self._current_day_utc = today_str
            self._call_count = 0

    def _get_seconds_until_midnight_utc(self, now_utc: datetime) -> int:
        """Calculates integer seconds remaining until 00:00:00 UTC tomorrow."""
        tomorrow_utc = (now_utc + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        return max(1, int((tomorrow_utc - now_utc).total_seconds()))

    async def check_capacity(
        self,
        endpoint_name: str,
        needed: int = 1,
        cap: Optional[int] = None,
    ) -> None:
        """
        Non-mutating check verifying if daily LLM capacity remains for `needed` calls.

        Raises HTTP 429 unless (effective_cap - current_count) >= needed.
        Used by endpoints like POST /resolve (with needed=2) to fail fast before
        acquiring in-progress locks or performing database writes.
        """
        async with self._lock:
            now_utc = datetime.now(timezone.utc)
            self._rollover_if_needed(now_utc)
            effective_cap = cap if cap is not None else get_settings().daily_llm_call_cap

            redis_val: Optional[int] = None
            if not is_circuit_open():
                try:
                    client = get_redis_client()
                    key = f"llmcap:{self._current_day_utc}"
                    raw = await client.get(key)
                    if raw is not None:
                        redis_val = int(raw)
                    else:
                        redis_val = 0
                except Exception:
                    record_redis_failure()
                    logger.warning("daily cap using in-memory fallback")
                    redis_val = None
            else:
                logger.warning("daily cap using in-memory fallback")

            # Effective count = max(redis value, in-memory value)
            if redis_val is not None:
                current_count = max(redis_val, self._call_count)
            else:
                current_count = self._call_count

            if (effective_cap - current_count) < needed:
                logger.warning("Daily LLM call cap exceeded for endpoint '%s'", endpoint_name)
                retry_after = self._get_seconds_until_midnight_utc(now_utc)
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail="Daily LLM request limit reached. Please try again tomorrow.",
                    headers={"Retry-After": str(retry_after)},
                )

    async def acquire_call(self, endpoint_name: str, cap: Optional[int] = None) -> None:
        """
        Atomically increments the daily LLM call counter and checks budget.

        Persists the count in Redis at 'llmcap:YYYY-MM-DD' with TTL 172800 seconds.
        Fails open to the in-memory counter if Redis is unavailable or circuit is open.
        Raises HTTP 429 if the post-increment count exceeds effective_cap.
        """
        async with self._lock:
            now_utc = datetime.now(timezone.utc)
            self._rollover_if_needed(now_utc)
            effective_cap = cap if cap is not None else get_settings().daily_llm_call_cap

            # Increment in-memory counter
            self._call_count += 1

            redis_val: Optional[int] = None
            if not is_circuit_open():
                try:
                    client = get_redis_client()
                    key = f"llmcap:{self._current_day_utc}"
                    redis_val = await client.incr(key)
                    if redis_val == 1:
                        await client.expire(key, 172800)
                except Exception:
                    record_redis_failure()
                    logger.warning("daily cap using in-memory fallback")
                    redis_val = None
            else:
                logger.warning("daily cap using in-memory fallback")

            # Effective count = max(redis value, in-memory value)
            if redis_val is not None:
                effective_count = max(redis_val, self._call_count)
                self._call_count = effective_count
            else:
                effective_count = self._call_count

            if effective_count > effective_cap:
                # Undo the increment before raising 429: refused requests must never raise stored count
                self._call_count -= 1
                if redis_val is not None:
                    try:
                        client = get_redis_client()
                        key = f"llmcap:{self._current_day_utc}"
                        await client.decr(key)
                    except Exception:
                        record_redis_failure()
                        logger.warning("daily cap using in-memory fallback")

                logger.warning("Daily LLM call cap exceeded for endpoint '%s'", endpoint_name)
                retry_after = self._get_seconds_until_midnight_utc(now_utc)
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail="Daily LLM request limit reached. Please try again tomorrow.",
                    headers={"Retry-After": str(retry_after)},
                )

    def get_count(self) -> int:
        """Returns the current day's call count."""
        return self._call_count

    def reset(self, count: int = 0, day_str: Optional[str] = None) -> None:
        """Manually resets the limiter state (for testing)."""
        self._call_count = count
        self._current_day_utc = day_str or datetime.now(timezone.utc).strftime("%Y-%m-%d")


# Global limiter singletons (relying on single uvicorn worker)
ask_limiter = SlidingWindowLimiter(window_s=60.0)
resolve_limiter = SlidingWindowLimiter(window_s=60.0)
upload_limiter = SlidingWindowLimiter(window_s=60.0)
daily_llm_limiter = DailyLLMCapLimiter()

__all__ = [
    "SlidingWindowLimiter",
    "DailyLLMCapLimiter",
    "ask_limiter",
    "resolve_limiter",
    "upload_limiter",
    "daily_llm_limiter",
]
