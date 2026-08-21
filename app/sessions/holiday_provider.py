"""Holiday provider abstraction over the local, versioned calendar data."""

from __future__ import annotations

from datetime import date
from typing import Protocol


class HolidayProvider(Protocol):
    @property
    def version(self) -> str: ...

    def covers(self, day: date) -> bool: ...

    def holiday_name(self, day: date) -> str | None: ...


class StaticUsEquityHolidayProvider:
    def __init__(self) -> None:
        from app.sessions import us_equity_calendar_data as data

        self._version = data.VERSION
        self._min_year = data.COVERAGE_MIN_YEAR
        self._max_year = data.COVERAGE_MAX_YEAR
        self._holidays = data.FULL_HOLIDAYS

    @property
    def version(self) -> str:
        return self._version

    @property
    def coverage(self) -> tuple[int, int]:
        return self._min_year, self._max_year

    def covers(self, day: date) -> bool:
        return self._min_year <= day.year <= self._max_year

    def holiday_name(self, day: date) -> str | None:
        return self._holidays.get(day)
