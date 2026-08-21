"""Timezone helpers.  Equity sessions are computed strictly in
America/New_York; storage is always UTC; the display timezone is independent.
No fixed German (or any other local) wall-clock times exist in this codebase.
"""

from __future__ import annotations

from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

NEW_YORK = ZoneInfo("America/New_York")


def to_new_york(ts_utc: datetime) -> datetime:
    if ts_utc.tzinfo is None:
        ts_utc = ts_utc.replace(tzinfo=UTC)
    return ts_utc.astimezone(NEW_YORK)


def parse_hhmm(value: str) -> time:
    hour, _, minute = value.partition(":")
    return time(int(hour), int(minute))
