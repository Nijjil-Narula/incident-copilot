"""
api/app/services/ratelimit_selftest.py

Standalone verification test suite for SlidingWindowLimiter, DailyLLMCapLimiter,
and RequestSizeLimitMiddleware abuse prevention.

Runnable inside the Docker container with:
  python -m app.services.ratelimit_selftest

Covers mandated test scenarios:
  1. Allows N requests then blocks request N+1
  2. Retry-After header >= 1
  3. Window expires and allows subsequent requests
  4. Daily cap blocks at cap with Retry-After to midnight
  5. Day rollover resets counter at 00:00 UTC
  6. RequestSizeLimitMiddleware allows empty body without Content-Length (200),
     rejects Transfer-Encoding chunked without Content-Length (411),
     rejects body > 1 MB (413), and allows multipart
  7. needed=2 pre-check blocks at cap-1 and allows at cap-2
  8. In-memory fallback when Redis is unavailable
  9. Rollback on refusal: 5 attempts with cap=2 end at count=2 and last 3 raise 429
"""

from __future__ import annotations

import asyncio
import logging
import sys
from datetime import datetime, timezone
from typing import List

from fastapi import HTTPException

from app.main import RequestSizeLimitMiddleware
from app.services.cache import record_redis_failure
from app.services.ratelimit import DailyLLMCapLimiter, SlidingWindowLimiter


async def run_async_tests() -> bool:
    all_passed = True

    def record_result(name: str, passed: bool, detail: str = ""):
        nonlocal all_passed
        status = "PASS" if passed else "FAIL"
        if not passed:
            all_passed = False
        print(f"[{status}] {name}{f': {detail}' if detail else ''}")

    print("Running Rate Limit & Abuse Prevention Self-Test Suite...\n" + "=" * 60)

    # ── Test 1: Allows N then blocks N+1 ──────────────────────────────────────
    try:
        limiter = SlidingWindowLimiter(window_s=60.0)
        n = 5
        # First N should succeed
        for i in range(n):
            await limiter.check(limit_per_min=n, endpoint_name="TEST_EP")

        # N+1 should raise 429
        blocked = False
        try:
            await limiter.check(limit_per_min=n, endpoint_name="TEST_EP")
        except HTTPException as exc:
            if exc.status_code == 429:
                blocked = True

        passed = blocked
        record_result("Test 1: Allows N requests then blocks request N+1", passed)
    except Exception as exc:
        record_result("Test 1: Allows N requests then blocks request N+1", False, str(exc))

    # ── Test 2: Retry-After header >= 1 ───────────────────────────────────────
    try:
        limiter = SlidingWindowLimiter(window_s=60.0)
        await limiter.check(limit_per_min=1, endpoint_name="TEST_EP")
        retry_after_val = 0
        try:
            await limiter.check(limit_per_min=1, endpoint_name="TEST_EP")
        except HTTPException as exc:
            if exc.status_code == 429 and "Retry-After" in exc.headers:
                retry_after_val = int(exc.headers["Retry-After"])

        passed = retry_after_val >= 1
        record_result(f"Test 2: Retry-After header >= 1 (got {retry_after_val})", passed)
    except Exception as exc:
        record_result("Test 2: Retry-After header >= 1", False, str(exc))

    # ── Test 3: Window expires ───────────────────────────────────────────────
    try:
        # 100 ms window
        fast_limiter = SlidingWindowLimiter(window_s=0.1)
        await fast_limiter.check(limit_per_min=1, endpoint_name="TEST_FAST")

        # Immediate second call is blocked
        blocked = False
        try:
            await fast_limiter.check(limit_per_min=1, endpoint_name="TEST_FAST")
        except HTTPException as exc:
            if exc.status_code == 429:
                blocked = True

        # Wait for the 100 ms window to expire
        await asyncio.sleep(0.15)

        # Call after window expiration succeeds
        unblocked_after_expiry = False
        try:
            await fast_limiter.check(limit_per_min=1, endpoint_name="TEST_FAST")
            unblocked_after_expiry = True
        except HTTPException:
            unblocked_after_expiry = False

        passed = blocked and unblocked_after_expiry
        record_result("Test 3: Window expires and allows subsequent requests", passed)
    except Exception as exc:
        record_result("Test 3: Window expires and allows subsequent requests", False, str(exc))

    # ── Test 4: Daily cap blocks at cap ───────────────────────────────────────
    try:
        # Ensure fallback mode for predictable in-memory cap test
        record_redis_failure()
        daily_limiter = DailyLLMCapLimiter()
        daily_limiter.reset(count=0)
        cap = 3

        # Acquire up to cap
        for i in range(cap):
            await daily_limiter.acquire_call(endpoint_name="TEST_DAILY", cap=cap)

        # Non-mutating check_capacity raises 429 when capacity is exhausted
        check_blocked = False
        try:
            await daily_limiter.check_capacity(endpoint_name="TEST_DAILY", cap=cap)
        except HTTPException as exc:
            if exc.status_code == 429 and int(exc.headers.get("Retry-After", 0)) >= 1:
                check_blocked = True

        # acquire_call at cap raises 429
        acquire_blocked = False
        try:
            await daily_limiter.acquire_call(endpoint_name="TEST_DAILY", cap=cap)
        except HTTPException as exc:
            if exc.status_code == 429 and "Daily" in exc.detail:
                acquire_blocked = True

        passed = check_blocked and acquire_blocked
        record_result("Test 4: Daily cap blocks at cap with Retry-After to midnight", passed)
    except Exception as exc:
        record_result("Test 4: Daily cap blocks at cap with Retry-After to midnight", False, str(exc))

    # ── Test 5: Day rollover resets ───────────────────────────────────────────
    try:
        record_redis_failure()
        daily_limiter = DailyLLMCapLimiter()
        # Simulate yesterday's exhausted quota
        yesterday_str = "2000-01-01"
        daily_limiter.reset(count=100, day_str=yesterday_str)

        # acquire_call today detects day rollover and resets counter
        await daily_limiter.acquire_call(endpoint_name="TEST_ROLLOVER", cap=5)
        passed = (daily_limiter.get_count() == 1)

        record_result("Test 5: Day rollover resets counter at 00:00 UTC", passed)
    except Exception as exc:
        record_result("Test 5: Day rollover resets counter at 00:00 UTC", False, str(exc))

    # ── Test 6: Request size middleware (pure ASGI) ──────────────────────────
    try:
        async def dummy_app(scope, receive, send):
            await send({
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/plain")],
            })
            await send({
                "type": "http.response.body",
                "body": b"OK",
            })

        middleware = RequestSizeLimitMiddleware(dummy_app)

        async def call_mw(headers: list[tuple[bytes, bytes]], method: str = "POST"):
            scope = {
                "type": "http",
                "method": method,
                "headers": headers,
            }
            response_status = None
            async def mock_receive():
                return {"type": "http.request", "body": b""}
            async def mock_send(msg):
                nonlocal response_status
                if msg["type"] == "http.response.start":
                    response_status = msg["status"]

            await middleware(scope, mock_receive, mock_send)
            return response_status

        # 1. Missing Content-Length AND no Transfer-Encoding on JSON POST -> allowed (200)
        status_200_empty = await call_mw([
            (b"content-type", b"application/json"),
        ])

        # 2. Transfer-Encoding present on JSON POST without Content-Length -> 411
        status_411_chunked = await call_mw([
            (b"content-type", b"application/json"),
            (b"transfer-encoding", b"chunked"),
        ])

        # 3. Body > 1 MB -> 413
        status_413 = await call_mw([
            (b"content-type", b"application/json"),
            (b"content-length", str(2 * 1024 * 1024).encode("latin1")),
        ])

        # 4. Normal JSON POST <= 1 MB -> 200
        status_200_json = await call_mw([
            (b"content-type", b"application/json"),
            (b"content-length", b"128"),
        ])

        # 5. Multipart upload missing Content-Length -> exempt (200)
        status_200_multipart = await call_mw([
            (b"content-type", b"multipart/form-data; boundary=xyz"),
        ])

        # 6. Malformed Content-Length -> 400
        status_400 = await call_mw([
            (b"content-type", b"application/json"),
            (b"content-length", b"not-a-number"),
        ])

        passed = (
            status_200_empty == 200
            and status_411_chunked == 411
            and status_413 == 413
            and status_200_json == 200
            and status_200_multipart == 200
            and status_400 == 400
        )
        record_result(
            "Test 6: RequestSizeLimitMiddleware (empty=200, chunked=411, >1MB=413, multipart=200, invalid=400)",
            passed,
        )
    except Exception as exc:
        record_result("Test 6: RequestSizeLimitMiddleware", False, str(exc))

    # ── Test 7: needed=2 pre-check (blocks at cap-1, allows at cap-2) ────────
    try:
        record_redis_failure()
        limiter = DailyLLMCapLimiter()
        cap = 10

        # At cap - 2 (count=8): (10 - 8) = 2 >= 2 -> ALLOWS
        limiter.reset(count=8)
        allowed_at_cap_minus_2 = False
        try:
            await limiter.check_capacity(endpoint_name="TEST_RESOLVE", needed=2, cap=cap)
            allowed_at_cap_minus_2 = True
        except HTTPException:
            allowed_at_cap_minus_2 = False

        # At cap - 1 (count=9): (10 - 9) = 1 < 2 -> BLOCKS WITH 429
        limiter.reset(count=9)
        blocked_at_cap_minus_1 = False
        try:
            await limiter.check_capacity(endpoint_name="TEST_RESOLVE", needed=2, cap=cap)
        except HTTPException as exc:
            if exc.status_code == 429 and "Retry-After" in exc.headers:
                blocked_at_cap_minus_1 = True

        passed = allowed_at_cap_minus_2 and blocked_at_cap_minus_1
        record_result(
            "Test 7: check_capacity with needed=2 blocks at cap-1 and allows at cap-2",
            passed,
        )
    except Exception as exc:
        record_result("Test 7: check_capacity with needed=2", False, str(exc))

    # ── Test 8: In-memory fallback when Redis is unavailable ─────────────────
    try:
        # Trip Redis failure circuit breaker to simulate Redis down / open breaker
        record_redis_failure()
        fallback_limiter = DailyLLMCapLimiter()
        fallback_limiter.reset(count=0)
        cap = 2

        # In-memory operations must succeed without Redis errors breaking the request
        await fallback_limiter.acquire_call("TEST_FALLBACK", cap=cap)
        await fallback_limiter.acquire_call("TEST_FALLBACK", cap=cap)

        # 3rd call exceeds cap=2 -> blocks with 429 and rolls back to 2
        fallback_blocked = False
        try:
            await fallback_limiter.acquire_call("TEST_FALLBACK", cap=cap)
        except HTTPException as exc:
            if exc.status_code == 429:
                fallback_blocked = True

        passed = (fallback_limiter.get_count() == 2) and fallback_blocked
        record_result(
            "Test 8: In-memory fallback functions seamlessly when Redis is unavailable (count rolled back to 2)",
            passed,
        )
    except Exception as exc:
        record_result("Test 8: In-memory fallback", False, str(exc))

    # ── Test 9: Rollback on refusal (5 attempts, cap=2, ending count=2) ───────
    try:
        record_redis_failure()
        limiter = DailyLLMCapLimiter()
        limiter.reset(count=0)
        cap = 2

        succeeded = 0
        blocked = 0
        for i in range(5):
            try:
                await limiter.acquire_call("TEST_ROLLBACK_5", cap=cap)
                succeeded += 1
            except HTTPException as exc:
                if exc.status_code == 429:
                    blocked += 1

        passed = (succeeded == 2) and (blocked == 3) and (limiter.get_count() == 2)
        record_result(
            "Test 9: 5 acquire_call attempts with cap=2 end at count=2 and last 3 raise 429",
            passed,
        )
    except Exception as exc:
        record_result("Test 9: 5 acquire_call attempts with rollback", False, str(exc))

    print("=" * 60)
    if all_passed:
        print("ALL TESTS PASSED.")
        return True
    else:
        print("SOME TESTS FAILED.")
        return False


def main() -> int:
    success = asyncio.run(run_async_tests())
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
