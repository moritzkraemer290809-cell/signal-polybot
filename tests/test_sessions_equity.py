"""Equity sessions: calendar, DST, holidays, early closes, unavailability."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.config import EquitySessionSettings
from app.sessions.early_close_provider import StaticUsEquityEarlyCloseProvider
from app.sessions.equity_calendar import EquityCalendar
from app.sessions.holiday_provider import StaticUsEquityHolidayProvider
from app.sessions.models import EquitySessionState
from app.sessions.session_manager import EquitySessionManager
from app.sessions.timezone import to_new_york


def make_manager(**overrides) -> EquitySessionManager:
    settings = EquitySessionSettings(_env_file=None, **overrides)
    calendar = EquityCalendar(
        settings, StaticUsEquityHolidayProvider(), StaticUsEquityEarlyCloseProvider()
    )
    return EquitySessionManager(settings, calendar)


def at(y: int, m: int, d: int, hh: int, mm: int = 0) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=UTC)


manager = make_manager()

# 2026-08-21 is a regular Friday; NY is UTC-4 (EDT) in August.


def test_regular_day_before_open_is_premarket() -> None:
    snapshot = manager.evaluate(at(2026, 8, 21, 12, 0))  # 08:00 NY
    assert snapshot.state is EquitySessionState.EQUITY_PREMARKET
    assert snapshot.analysis_allowed is False  # premarket analysis disabled in V1
    assert snapshot.next_transition_at == at(2026, 8, 21, 13, 30)


def test_regular_day_during_session() -> None:
    snapshot = manager.evaluate(at(2026, 8, 21, 15, 0))  # 11:00 NY
    assert snapshot.state is EquitySessionState.EQUITY_REGULAR
    assert snapshot.analysis_allowed is True
    assert snapshot.next_transition_at == at(2026, 8, 21, 20, 0)  # 16:00 NY close


def test_regular_day_after_close_is_after_hours_then_closed() -> None:
    after_hours = manager.evaluate(at(2026, 8, 21, 21, 0))  # 17:00 NY
    assert after_hours.state is EquitySessionState.EQUITY_AFTER_HOURS
    assert after_hours.analysis_allowed is False
    closed = manager.evaluate(at(2026, 8, 22, 1, 0))  # 21:00 NY Friday
    assert closed.state is EquitySessionState.EQUITY_CLOSED


def test_early_morning_before_premarket_is_closed() -> None:
    snapshot = manager.evaluate(at(2026, 8, 21, 6, 0))  # 02:00 NY
    assert snapshot.state is EquitySessionState.EQUITY_CLOSED


def test_weekend() -> None:
    snapshot = manager.evaluate(at(2026, 8, 22, 15, 0))  # Saturday
    assert snapshot.state is EquitySessionState.EQUITY_WEEKEND
    assert snapshot.analysis_allowed is False
    # next open is Monday 09:30 NY = 13:30 UTC
    assert snapshot.next_transition_at == at(2026, 8, 24, 13, 30)


def test_full_holiday_thanksgiving() -> None:
    snapshot = manager.evaluate(at(2026, 11, 26, 16, 0))
    assert snapshot.state is EquitySessionState.EQUITY_HOLIDAY
    assert snapshot.holiday_name == "Thanksgiving Day"
    assert snapshot.analysis_allowed is False


def test_early_close_day_phases() -> None:
    # 2026-11-27 (day after Thanksgiving), NY is UTC-5 (EST) in November.
    before = manager.evaluate(at(2026, 11, 27, 14, 0))  # 09:00 NY
    assert before.state is EquitySessionState.EQUITY_PREMARKET
    assert before.next_transition_state is EquitySessionState.EQUITY_EARLY_CLOSE

    during = manager.evaluate(at(2026, 11, 27, 17, 0))  # 12:00 NY
    assert during.state is EquitySessionState.EQUITY_EARLY_CLOSE
    assert during.analysis_allowed is True
    assert during.next_transition_at == at(2026, 11, 27, 18, 0)  # 13:00 NY

    after = manager.evaluate(at(2026, 11, 27, 18, 30))  # 13:30 NY
    assert after.state is EquitySessionState.EQUITY_AFTER_HOURS
    assert after.analysis_allowed is False
    assert after.early_close_reason == "Day after Thanksgiving"


def test_dst_spring_forward_shifts_utc_open() -> None:
    # DST starts 2026-03-08: Friday 2026-03-06 is EST (UTC-5), Monday 03-09 EDT (UTC-4)
    est_open = manager.evaluate(at(2026, 3, 6, 14, 30))  # 09:30 EST
    assert est_open.state is EquitySessionState.EQUITY_REGULAR
    est_before = manager.evaluate(at(2026, 3, 6, 13, 45))  # 08:45 EST
    assert est_before.state is EquitySessionState.EQUITY_PREMARKET

    edt_open = manager.evaluate(at(2026, 3, 9, 13, 30))  # 09:30 EDT
    assert edt_open.state is EquitySessionState.EQUITY_REGULAR
    edt_would_be_early = manager.evaluate(at(2026, 3, 9, 14, 30))  # 10:30 EDT: still open
    assert edt_would_be_early.state is EquitySessionState.EQUITY_REGULAR


def test_dst_fall_back_shifts_utc_open() -> None:
    # DST ends 2026-11-01: Friday 10-30 EDT (UTC-4), Monday 11-02 EST (UTC-5)
    assert manager.evaluate(at(2026, 10, 30, 13, 30)).state is EquitySessionState.EQUITY_REGULAR
    assert manager.evaluate(at(2026, 11, 2, 13, 30)).state is EquitySessionState.EQUITY_PREMARKET
    assert manager.evaluate(at(2026, 11, 2, 14, 30)).state is EquitySessionState.EQUITY_REGULAR


def test_utc_to_new_york_conversion() -> None:
    summer = to_new_york(at(2026, 8, 21, 15, 0))
    assert (summer.hour, summer.minute) == (11, 0)
    winter = to_new_york(at(2026, 12, 21, 15, 0))
    assert (winter.hour, winter.minute) == (10, 0)


def test_date_outside_coverage_is_calendar_unavailable() -> None:
    snapshot = manager.evaluate(at(2029, 6, 1, 15, 0))
    assert snapshot.calendar_available is False
    assert snapshot.analysis_allowed is False


def test_calendar_version_mismatch_is_unavailable() -> None:
    mismatched = make_manager(calendar_version="us-equity-9999.9")
    snapshot = mismatched.evaluate(at(2026, 8, 21, 15, 0))
    assert snapshot.calendar_available is False
    assert "version mismatch" in (snapshot.detail or "")


def test_calendar_disabled_blocks_equity() -> None:
    disabled = make_manager(calendar_enabled=False)
    snapshot = disabled.evaluate(at(2026, 8, 21, 15, 0))
    assert snapshot.calendar_available is False
    assert snapshot.analysis_allowed is False


def test_no_fixed_german_times_in_session_code() -> None:
    from pathlib import Path

    sessions_dir = Path(__file__).resolve().parent.parent / "app" / "sessions"
    for path in sessions_dir.rglob("*.py"):
        source = path.read_text()
        assert "Europe/Berlin" not in source
        assert "Europe/" not in source


def test_invalid_session_time_config_fails_loudly() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        EquitySessionSettings(_env_file=None, regular_session_open="25:99")
