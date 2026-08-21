"""Per-chat rate limiting for sends and edits."""

from __future__ import annotations

import pytest
from tests.conftest import FakeClock

from app.config import TelegramSettings
from app.telegram.rate_limiter import TelegramRateLimiter

CHAT = -100_123


def make(clock: FakeClock, **overrides) -> TelegramRateLimiter:
    settings = TelegramSettings(
        _env_file=None,
        group_messages_per_minute=3,
        group_min_interval_seconds=1.0,
        edit_min_interval_seconds=5.0,
        **overrides,
    )
    return TelegramRateLimiter(settings, clock=clock)


def test_min_interval_between_sends() -> None:
    clock = FakeClock()
    limiter = make(clock)
    assert limiter.send_wait_seconds(CHAT) == 0.0
    limiter.record_send(CHAT)
    assert limiter.send_wait_seconds(CHAT) == pytest.approx(1.0)
    clock.advance(0.4)
    assert limiter.send_wait_seconds(CHAT) == pytest.approx(0.6)
    clock.advance(0.6)
    assert limiter.send_wait_seconds(CHAT) == 0.0


def test_messages_per_minute_window() -> None:
    clock = FakeClock()
    limiter = make(clock)
    for _ in range(3):
        limiter.record_send(CHAT)
        clock.advance(2.0)
    # 3 sends within the last minute -> wait until the first leaves the window
    wait = limiter.send_wait_seconds(CHAT)
    assert wait == pytest.approx(60.0 - 6.0)
    clock.advance(wait)
    assert limiter.send_wait_seconds(CHAT) == 0.0


def test_edit_interval_is_independent_and_conservative() -> None:
    clock = FakeClock()
    limiter = make(clock)
    assert limiter.edit_wait_seconds(CHAT) == 0.0
    limiter.record_edit(CHAT)
    assert limiter.edit_wait_seconds(CHAT) == pytest.approx(5.0)
    # sends are unaffected by edit interval
    assert limiter.send_wait_seconds(CHAT) == 0.0
    clock.advance(5.0)
    assert limiter.edit_wait_seconds(CHAT) == 0.0


def test_chats_are_isolated() -> None:
    clock = FakeClock()
    limiter = make(clock)
    limiter.record_send(CHAT)
    assert limiter.send_wait_seconds(999) == 0.0


def test_wait_accounting() -> None:
    limiter = make(FakeClock())
    limiter.record_wait(1.5)
    limiter.record_wait(0.0)
    assert limiter.total_wait_seconds == pytest.approx(1.5)
