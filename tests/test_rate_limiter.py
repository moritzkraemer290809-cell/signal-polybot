"""Weighted REST rate limiter behaviour."""

from __future__ import annotations

import pytest
from tests.conftest import FakeClock

from app.adapters.rate_limiter import WeightedRateLimiter


async def test_acquire_within_budget_does_not_sleep() -> None:
    clock = FakeClock()
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock.advance(seconds)

    limiter = WeightedRateLimiter(600, clock=clock, sleep=fake_sleep)
    for _ in range(10):
        await limiter.acquire(20)
    assert sleeps == []
    assert limiter.available_tokens == pytest.approx(400.0)


async def test_acquire_blocks_until_refill() -> None:
    clock = FakeClock()
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock.advance(seconds)

    limiter = WeightedRateLimiter(600, clock=clock, sleep=fake_sleep)  # 10 tokens/sec
    await limiter.acquire(600)  # drain everything
    await limiter.acquire(50)  # needs 5 seconds of refill
    assert sum(sleeps) == pytest.approx(5.0)


async def test_weight_larger_than_budget_rejected() -> None:
    limiter = WeightedRateLimiter(100, clock=FakeClock())
    with pytest.raises(ValueError):
        await limiter.acquire(101)
    with pytest.raises(ValueError):
        await limiter.acquire(0)


async def test_tokens_refill_continuously() -> None:
    clock = FakeClock()

    async def fake_sleep(seconds: float) -> None:
        clock.advance(seconds)

    limiter = WeightedRateLimiter(600, clock=clock, sleep=fake_sleep)
    await limiter.acquire(600)
    clock.advance(30)  # half a minute -> 300 tokens back
    assert limiter.available_tokens == pytest.approx(300.0)
