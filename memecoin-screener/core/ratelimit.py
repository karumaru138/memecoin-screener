"""A tiny async rate limiter (token bucket) shared by the API clients.

DexScreener's 60 req/min is the binding constraint of the whole system, so
every DexScreener call must pass through one shared limiter. Helius gets its
own. This is a simple, dependency-free token bucket.
"""
from __future__ import annotations

import asyncio
import time


class AsyncRateLimiter:
    def __init__(self, rate_per_sec: float, burst: float | None = None):
        self.rate = rate_per_sec
        self.capacity = burst if burst is not None else max(1.0, rate_per_sec)
        self._tokens = self.capacity
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()

    @classmethod
    def per_minute(cls, rpm: float) -> "AsyncRateLimiter":
        return cls(rate_per_sec=rpm / 60.0, burst=max(1.0, rpm / 6.0))

    async def acquire(self, cost: float = 1.0) -> None:
        while True:
            async with self._lock:
                now = time.monotonic()
                elapsed = now - self._updated
                self._updated = now
                self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
                if self._tokens >= cost:
                    self._tokens -= cost
                    return
                deficit = cost - self._tokens
                wait = deficit / self.rate
            await asyncio.sleep(wait)
