"""Equity trading-day classification built on the calendar providers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, time

from app.config import EquitySessionSettings
from app.sessions.early_close_provider import EarlyCloseProvider
from app.sessions.holiday_provider import HolidayProvider
from app.sessions.timezone import parse_hhmm


@dataclass(frozen=True)
class TradingDayInfo:
    day: date
    covered: bool
    is_weekend: bool
    holiday_name: str | None
    early_close_reason: str | None
    open_time: time
    close_time: time

    @property
    def is_trading_day(self) -> bool:
        return self.covered and not self.is_weekend and self.holiday_name is None

    @property
    def is_early_close(self) -> bool:
        return self.is_trading_day and self.early_close_reason is not None


class EquityCalendar:
    def __init__(
        self,
        settings: EquitySessionSettings,
        holidays: HolidayProvider,
        early_closes: EarlyCloseProvider,
    ) -> None:
        self._settings = settings
        self._holidays = holidays
        self._early_closes = early_closes
        self._open = parse_hhmm(settings.regular_session_open)
        self._close = parse_hhmm(settings.regular_session_close)
        self._early_close = parse_hhmm(settings.early_close_default)

    @property
    def version(self) -> str:
        return self._holidays.version

    @property
    def version_matches_config(self) -> bool:
        return self._holidays.version == self._settings.calendar_version

    def classify(self, day: date) -> TradingDayInfo:
        covered = (
            self._holidays.covers(day)
            and self._settings.calendar_min_year <= day.year <= self._settings.calendar_max_year
        )
        early_close_reason = self._early_closes.early_close_reason(day)
        return TradingDayInfo(
            day=day,
            covered=covered,
            is_weekend=day.weekday() >= 5,
            holiday_name=self._holidays.holiday_name(day),
            early_close_reason=early_close_reason,
            open_time=self._open,
            close_time=self._early_close if early_close_reason else self._close,
        )
