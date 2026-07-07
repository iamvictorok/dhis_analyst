"""Rate limiting tests."""
import pytest

from backend.app.ratelimit import InMemoryRateLimiter


@pytest.mark.asyncio
async def test_in_memory_limiter_allows_up_to_rpm_then_blocks():
    limiter = InMemoryRateLimiter(rpm=3)
    results = [await limiter.is_allowed("user-a") for _ in range(5)]
    # First 3 allowed, the rest blocked within the window.
    assert results == [True, True, True, False, False]


@pytest.mark.asyncio
async def test_in_memory_limiter_is_per_key():
    limiter = InMemoryRateLimiter(rpm=1)
    assert await limiter.is_allowed("user-a") is True
    assert await limiter.is_allowed("user-b") is True  # different key, own bucket
    assert await limiter.is_allowed("user-a") is False


@pytest.mark.asyncio
async def test_in_memory_limiter_prunes_expired_keys():
    # Tiny window so entries expire immediately; the sweep must drop stale keys
    # rather than growing the store unbounded.
    limiter = InMemoryRateLimiter(rpm=5, window=0.0)
    await limiter.is_allowed("ephemeral")
    # Next call is past the window → sweep runs and removes the stale key.
    await limiter.is_allowed("other")
    assert "ephemeral" not in limiter._store
