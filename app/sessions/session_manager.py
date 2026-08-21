"""Timezone-aware session managers for equity and crypto perps.

Equity sessions are computed strictly in America/New_York from the versioned
local calendar; DST is handled by the IANA timezone.  Unknown/uncovered dates
or a disabled/mismatching calendar never silently become a regular session -
they yield ``calendar_available=False`` (CALENDAR_UNAVAILABLE downstream).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from app.config import CryptoSessionSettings, EquitySessionSettings
from app.sessions.equity_calendar import EquityCalendar
from app.sessions.models import (
    CryptoSessionSnapshot,
    CryptoSessionState,
    EquitySessionSnapshot,
    EquitySessionState,
)
from app.sessions.timezone import NEW_YORK, parse_hhmm, to_new_york


class EquitySessionManager:
    def __init__(self, settings: EquitySessionSettings, calendar: EquityCalendar) -> None:
        self._settings = settings
        self._calendar = calendar

    @property
    def calendar(self) -> EquityCalendar:
        return self._calendar

    def evaluate(self, now_utc: datetime | None = None) -> EquitySessionSnapshot:
        now_utc = now_utc or datetime.now(tz=UTC)
        if now_utc.tzinfo is None:
            now_utc = now_utc.replace(tzinfo=UTC)

        if not self._settings.calendar_enabled:
            return EquitySessionSnapshot(
                state=EquitySessionState.EQUITY_CLOSED,
                evaluated_at=now_utc,
                calendar_available=False,
                calendar_version=None,
                analysis_allowed=False,
                detail="equity calendar disabled by configuration",
            )
        if not self._calendar.version_matches_config:
            return EquitySessionSnapshot(
                state=EquitySessionState.EQUITY_CLOSED,
                evaluated_at=now_utc,
                calendar_available=False,
                calendar_version=self._calendar.version,
                analysis_allowed=False,
                detail=(
                    "calendar version mismatch: "
                    f"data={self._calendar.version} config={self._settings.calendar_version}"
                ),
            )

        local = to_new_york(now_utc)
        info = self._calendar.classify(local.date())
        if not info.covered:
            return EquitySessionSnapshot(
                state=EquitySessionState.EQUITY_CLOSED,
                evaluated_at=now_utc,
                calendar_available=False,
                calendar_version=self._calendar.version,
                analysis_allowed=False,
                detail=f"date {local.date()} outside calendar coverage",
            )

        version = self._calendar.version
        common: dict[str, Any] = {
            "evaluated_at": now_utc,
            "calendar_available": True,
            "calendar_version": version,
        }

        if info.is_weekend:
            return EquitySessionSnapshot(
                state=EquitySessionState.EQUITY_WEEKEND,
                analysis_allowed=False,
                next_transition_at=self._next_open_after(local),
                next_transition_state=EquitySessionState.EQUITY_REGULAR,
                **common,
            )
        if info.holiday_name is not None:
            return EquitySessionSnapshot(
                state=EquitySessionState.EQUITY_HOLIDAY,
                analysis_allowed=False,
                holiday_name=info.holiday_name,
                next_transition_at=self._next_open_after(local),
                next_transition_state=EquitySessionState.EQUITY_REGULAR,
                **common,
            )

        open_dt = local.replace(
            hour=info.open_time.hour, minute=info.open_time.minute, second=0, microsecond=0
        )
        close_dt = local.replace(
            hour=info.close_time.hour, minute=info.close_time.minute, second=0, microsecond=0
        )
        premarket_start = local.replace(hour=4, minute=0, second=0, microsecond=0)
        after_hours_end = local.replace(hour=20, minute=0, second=0, microsecond=0)

        if local < open_dt:
            if self._settings.premarket_enabled and local >= premarket_start:
                state = EquitySessionState.EQUITY_PREMARKET
            else:
                state = EquitySessionState.EQUITY_CLOSED
            return EquitySessionSnapshot(
                state=state,
                analysis_allowed=(
                    state is EquitySessionState.EQUITY_PREMARKET
                    and self._settings.premarket_analysis_enabled
                ),
                early_close_reason=info.early_close_reason,
                next_transition_at=open_dt.astimezone(UTC),
                next_transition_state=(
                    EquitySessionState.EQUITY_EARLY_CLOSE
                    if info.is_early_close
                    else EquitySessionState.EQUITY_REGULAR
                ),
                **common,
            )
        if local < close_dt:
            state = (
                EquitySessionState.EQUITY_EARLY_CLOSE
                if info.is_early_close
                else EquitySessionState.EQUITY_REGULAR
            )
            return EquitySessionSnapshot(
                state=state,
                analysis_allowed=True,
                early_close_reason=info.early_close_reason,
                next_transition_at=close_dt.astimezone(UTC),
                next_transition_state=(
                    EquitySessionState.EQUITY_AFTER_HOURS
                    if self._settings.after_hours_enabled
                    else EquitySessionState.EQUITY_CLOSED
                ),
                **common,
            )
        if self._settings.after_hours_enabled and local < after_hours_end:
            return EquitySessionSnapshot(
                state=EquitySessionState.EQUITY_AFTER_HOURS,
                analysis_allowed=self._settings.after_hours_analysis_enabled,
                early_close_reason=info.early_close_reason,
                next_transition_at=after_hours_end.astimezone(UTC),
                next_transition_state=EquitySessionState.EQUITY_CLOSED,
                **common,
            )
        return EquitySessionSnapshot(
            state=EquitySessionState.EQUITY_CLOSED,
            analysis_allowed=False,
            early_close_reason=info.early_close_reason,
            next_transition_at=self._next_open_after(local),
            next_transition_state=EquitySessionState.EQUITY_REGULAR,
            **common,
        )

    def _next_open_after(self, local_now: datetime) -> datetime | None:
        """UTC timestamp of the next regular session open (searches 14 days)."""
        open_time = parse_hhmm(self._settings.regular_session_open)
        for offset in range(0, 14):
            day = (local_now + timedelta(days=offset)).date()
            info = self._calendar.classify(day)
            if not info.covered:
                return None
            if not info.is_trading_day:
                continue
            candidate = datetime(
                day.year, day.month, day.day, open_time.hour, open_time.minute, tzinfo=NEW_YORK
            )
            if candidate > local_now:
                return candidate.astimezone(UTC)
        return None


class CryptoSessionManager:
    def __init__(self, settings: CryptoSessionSettings) -> None:
        self._settings = settings

    def evaluate(self, now_utc: datetime | None = None) -> CryptoSessionSnapshot:
        now_utc = now_utc or datetime.now(tz=UTC)
        if now_utc.tzinfo is None:
            now_utc = now_utc.replace(tzinfo=UTC)
        if not self._settings.session_enabled:
            return CryptoSessionSnapshot(
                state=CryptoSessionState.CRYPTO_DISABLED,
                evaluated_at=now_utc,
                analysis_allowed=False,
            )
        window_detail = self._active_thin_window(now_utc)
        if window_detail is not None and self._settings.thin_liquidity_policy == "LIMITED_SESSION":
            return CryptoSessionSnapshot(
                state=CryptoSessionState.CRYPTO_THIN_LIQUIDITY,
                evaluated_at=now_utc,
                analysis_allowed=True,  # not blocking - stricter quality applies
                limited=True,
                window_detail=window_detail,
            )
        return CryptoSessionSnapshot(
            state=CryptoSessionState.CRYPTO_24_7,
            evaluated_at=now_utc,
            analysis_allowed=True,
        )

    def _active_thin_window(self, now_utc: datetime) -> str | None:
        for window in self._settings.thin_liquidity_windows_json:
            days = window.get("days", list(range(7)))
            start = parse_hhmm(str(window["start"]))
            end = parse_hhmm(str(window["end"]))
            now_time = now_utc.time().replace(second=0, microsecond=0)
            weekday = now_utc.weekday()
            if start <= end:
                inside = weekday in days and start <= now_time < end
            else:  # window spans midnight UTC
                inside = (weekday in days and now_time >= start) or (
                    (weekday - 1) % 7 in days and now_time < end
                )
            if inside:
                return f"{window['start']}-{window['end']} UTC days={days}"
        return None
