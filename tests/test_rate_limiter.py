from __future__ import annotations

import asyncio

import pytest

from kwork_mcp.rate_limiter import InProcessRateLimiter


@pytest.mark.asyncio
async def test_acquire_within_burst() -> None:
    limiter = InProcessRateLimiter(rps=2, burst=3)
    results = [await limiter.acquire() for _ in range(3)]
    assert results == [True, True, True]


@pytest.mark.asyncio
async def test_acquire_exceeds_burst() -> None:
    limiter = InProcessRateLimiter(rps=2, burst=2)
    assert await limiter.acquire() is True
    assert await limiter.acquire() is True
    assert await limiter.acquire() is False


@pytest.mark.asyncio
async def test_wait_and_acquire_succeeds() -> None:
    limiter = InProcessRateLimiter(rps=10, burst=1)
    await limiter.wait_and_acquire()
    # After window passes, should succeed again
    await asyncio.sleep(1.1)
    await limiter.wait_and_acquire()


@pytest.mark.asyncio
async def test_acquire_refills_after_window() -> None:
    limiter = InProcessRateLimiter(rps=2, burst=1)
    assert await limiter.acquire() is True
    assert await limiter.acquire() is False
    await asyncio.sleep(1.1)
    assert await limiter.acquire() is True
