"""Early-close provider: strictly calendar-based, never a generic rule.

Whether a specific day is an early close comes only from the versioned
calendar data; the (usually 13:00 ET) closing time itself is configurable via
``EQUITY_EARLY_CLOSE_DEFAULT``.
"""

from __future__ import annotations

from datetime import date
from typing import Protocol


class EarlyCloseProvider(Protocol):
    def early_close_reason(self, day: date) -> str | None: ...


class StaticUsEquityEarlyCloseProvider:
    def __init__(self) -> None:
        from app.sessions import us_equity_calendar_data as data

        self._early_closes = data.EARLY_CLOSES

    def early_close_reason(self, day: date) -> str | None:
        return self._early_closes.get(day)
