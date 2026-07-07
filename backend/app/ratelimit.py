"""Per-key rate limiting.

Uses a Redis-backed sliding window when ``REDIS_URL`` is configured (so the
limit is shared across all uvicorn workers/replicas), and falls back to an
in-memory limiter otherwise. Both expose the same async ``is_allowed`` API.

The in-memory limiter is process-local, so with multiple workers the effective
limit is (workers × rpm) — configure ``REDIS_URL`` for a true global limit.
"""
from __future__ import annotations

import logging
import time
import uuid
from typing import Protocol

logger = logging.getLogger("dhis2_analyst.ratelimit")

_WINDOW_SECONDS = 60.0


class RateLimiter(Protocol):
    async def is_allowed(self, key: str) -> bool: ...
    async def aclose(self) -> None: ...


class InMemoryRateLimiter:
    """Process-local sliding-window limiter with periodic pruning."""

    def __init__(self, rpm: int, window: float = _WINDOW_SECONDS) -> None:
        self._rpm = rpm
        self._window = window
        self._store: dict[str, list[float]] = {}
        self._last_sweep = time.monotonic()

    async def is_allowed(self, key: str) -> bool:
        now = time.monotonic()
        cutoff = now - self._window
        self._maybe_sweep(now, cutoff)
        bucket = [t for t in self._store.get(key, ()) if t > cutoff]
        if len(bucket) >= self._rpm:
            self._store[key] = bucket
            return False
        bucket.append(now)
        self._store[key] = bucket
        return True

    def _maybe_sweep(self, now: float, cutoff: float) -> None:
        # Bound memory: drop keys whose newest hit has expired. Runs at most
        # once per window so it stays O(1) amortised per request.
        if now - self._last_sweep < self._window:
            return
        self._last_sweep = now
        stale = [k for k, ts in self._store.items() if not ts or ts[-1] <= cutoff]
        for k in stale:
            del self._store[k]

    async def aclose(self) -> None:
        self._store.clear()


class RedisRateLimiter:
    """Distributed sliding-window limiter backed by a Redis sorted set.

    Fails OPEN (allows the request) if Redis is unreachable at call time, so a
    Redis outage degrades to unlimited rather than locking everyone out.
    """

    # Atomic: expire old entries, count, and add-if-under-limit in one round trip.
    _LUA = """
    local key = KEYS[1]
    local now = tonumber(ARGV[1])
    local window = tonumber(ARGV[2])
    local limit = tonumber(ARGV[3])
    local member = ARGV[4]
    redis.call('ZREMRANGEBYSCORE', key, 0, now - window)
    local count = redis.call('ZCARD', key)
    if count < limit then
        redis.call('ZADD', key, now, member)
        redis.call('EXPIRE', key, math.ceil(window))
        return 1
    end
    return 0
    """

    def __init__(self, client, rpm: int, window: float = _WINDOW_SECONDS) -> None:
        self._redis = client
        self._rpm = rpm
        self._window = window

    async def is_allowed(self, key: str) -> bool:
        now = time.time()
        member = f"{now}:{uuid.uuid4().hex}"
        try:
            allowed = await self._redis.eval(
                self._LUA, 1, f"rl:{key}", now, self._window, self._rpm, member
            )
            return bool(int(allowed))
        except Exception as exc:
            logger.warning("redis_ratelimit_error_failing_open", extra={"error": str(exc)})
            return True

    async def aclose(self) -> None:
        try:
            await self._redis.aclose()
        except Exception:
            pass


async def create_rate_limiter(settings) -> RateLimiter:
    """Build the appropriate limiter for the current configuration."""
    if getattr(settings, "redis_url", ""):
        try:
            import redis.asyncio as aioredis

            client = aioredis.from_url(
                settings.redis_url, encoding="utf-8", decode_responses=True
            )
            await client.ping()
            logger.info("ratelimit_backend_selected", extra={"backend": "redis"})
            return RedisRateLimiter(client, settings.rate_limit_rpm)
        except Exception as exc:
            logger.warning(
                "redis_unavailable_fallback_in_memory",
                extra={"error": str(exc), "redis_url_configured": True},
            )
    logger.info("ratelimit_backend_selected", extra={"backend": "in_memory"})
    return InMemoryRateLimiter(settings.rate_limit_rpm)
