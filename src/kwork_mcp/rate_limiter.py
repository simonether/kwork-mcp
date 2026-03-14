from __future__ import annotations

import asyncio
import random
import time
from collections import deque


class InProcessRateLimiter:
    """Sliding window rate limiter using in-memory deque.

    No Redis needed — MCP server is single-process.
    """

    def __init__(self, rps: int = 2, burst: int = 5) -> None:
        self._rps = rps
        self._burst = burst
        self._timestamps: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self) -> bool:
        async with self._lock:
            now = time.monotonic()
            window_start = now - 1.0
            while self._timestamps and self._timestamps[0] < window_start:
                self._timestamps.popleft()
            if len(self._timestamps) < self._burst:
                self._timestamps.append(now)
                return True
            return False

    async def wait_and_acquire(self) -> None:
        base_delay = 1.0 / self._rps if self._rps > 0 else 0.5
        for attempt in range(20):
            if await self.acquire():
                return
            delay = min(base_delay * (2**attempt), 5.0)
            jitter = random.uniform(0, delay * 0.25)
            await asyncio.sleep(delay + jitter)
        msg = "Rate limiter: sustained overload, попробуйте позже"
        raise RuntimeError(msg)
