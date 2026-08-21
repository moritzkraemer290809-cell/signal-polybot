"""Weighted async token-bucket rate limiter for the Polymarket REST API.

Polymarket enforces a weighted budget of 1000 tokens per minute per IP.  The
limiter refills continuously and blocks callers until enough tokens are
available.  Clock and sleep are injectable for deterministic tests.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable


class WeightedRateLimiter:
    def __init__(
        self,
        budget_per_minute: int,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if budget_per_minute <= 0:
            raise ValueError("budget_per_minute must be positive")
        self._capacity = float(budget_per_minute)
        self._refill_per_second = budget_per_minute / 60.0
        self._tokens = self._capacity
        self._clock = clock
        self._sleep = sleep
        self._last_refill = clock()
        self._lock = asyncio.Lock()

    def _refill(self) -> None:
        now = self._clock()
        elapsed = max(0.0, now - self._last_refill)
        self._last_refill = now
        self._tokens = min(self._capacity, self._tokens + elapsed * self._refill_per_second)

    @property
    def available_tokens(self) -> float:
        self._refill()
        return self._tokens

    async def acquire(self, weight: int) -> None:
        """Block until ``weight`` tokens are available, then consume them."""
        if weight <= 0:
            raise ValueError("weight must be positive")
        if weight > self._capacity:
            raise ValueError(f"weight {weight} exceeds total budget {self._capacity:.0f}")
        async with self._lock:
            while True:
                self._refill()
                if self._tokens >= weight:
                    self._tokens -= weight
                    return
                deficit = weight - self._tokens
                await self._sleep(deficit / self._refill_per_second)
