"""Locally maintained, versioned NYSE/Nasdaq-compatible US equity calendar.

VERSION / COVERAGE
    Version:  us-equity-2026.1
    Coverage: 2026-01-01 .. 2028-12-31 (current year + two years)
    Source:   NYSE published holiday schedule (nyse.com/markets/hours-calendars),
              transcribed manually - no runtime network dependency.

UPDATE PROCESS (documented in docs/operations.md)
    1. Check the official NYSE holiday schedule for the new year.
    2. Append full-closure dates to FULL_HOLIDAYS and 13:00-ET early closes
       to EARLY_CLOSES below.
    3. Bump VERSION (e.g. us-equity-2027.1), COVERAGE_MAX_YEAR, and the
       matching EQUITY_CALENDAR_VERSION / EQUITY_CALENDAR_MAX_YEAR settings.
    4. Run the calendar test suite.

Dates outside the coverage window are treated as CALENDAR_UNAVAILABLE by the
session manager - never silently assumed to be regular sessions.

NYSE observance rules reflected here:
- Holiday on Saturday -> observed the preceding Friday
  (exception: New Year's Day on Saturday is NOT observed, e.g. 2028-01-01).
- Holiday on Sunday -> observed the following Monday.
- Early closes (13:00 ET): day after Thanksgiving; July 3 when it is a
  weekday and July 4 is not observed that day; Christmas Eve when it is a
  weekday and not itself the observed holiday.
"""

from __future__ import annotations

from datetime import date

VERSION = "us-equity-2026.1"
COVERAGE_MIN_YEAR = 2026
COVERAGE_MAX_YEAR = 2028

#: full market closures: date -> holiday name
FULL_HOLIDAYS: dict[date, str] = {
    # --- 2026 ---
    date(2026, 1, 1): "New Year's Day",
    date(2026, 1, 19): "Martin Luther King, Jr. Day",
    date(2026, 2, 16): "Washington's Birthday",
    date(2026, 4, 3): "Good Friday",
    date(2026, 5, 25): "Memorial Day",
    date(2026, 6, 19): "Juneteenth National Independence Day",
    date(2026, 7, 3): "Independence Day (observed)",
    date(2026, 9, 7): "Labor Day",
    date(2026, 11, 26): "Thanksgiving Day",
    date(2026, 12, 25): "Christmas Day",
    # --- 2027 ---
    date(2027, 1, 1): "New Year's Day",
    date(2027, 1, 18): "Martin Luther King, Jr. Day",
    date(2027, 2, 15): "Washington's Birthday",
    date(2027, 3, 26): "Good Friday",
    date(2027, 5, 31): "Memorial Day",
    date(2027, 6, 18): "Juneteenth (observed)",
    date(2027, 7, 5): "Independence Day (observed)",
    date(2027, 9, 6): "Labor Day",
    date(2027, 11, 25): "Thanksgiving Day",
    date(2027, 12, 24): "Christmas Day (observed)",
    # --- 2028 ---
    # New Year's Day 2028 falls on Saturday and is not observed (NYSE rule).
    date(2028, 1, 17): "Martin Luther King, Jr. Day",
    date(2028, 2, 21): "Washington's Birthday",
    date(2028, 4, 14): "Good Friday",
    date(2028, 5, 29): "Memorial Day",
    date(2028, 6, 19): "Juneteenth National Independence Day",
    date(2028, 7, 4): "Independence Day",
    date(2028, 9, 4): "Labor Day",
    date(2028, 11, 23): "Thanksgiving Day",
    date(2028, 12, 25): "Christmas Day",
}

#: 13:00-ET early closes: date -> description
EARLY_CLOSES: dict[date, str] = {
    # --- 2026 --- (July 3 is a full observed holiday, so no July early close)
    date(2026, 11, 27): "Day after Thanksgiving",
    date(2026, 12, 24): "Christmas Eve",
    # --- 2027 --- (Dec 24 is the observed Christmas holiday; July 3 is a Saturday)
    date(2027, 11, 26): "Day after Thanksgiving",
    # --- 2028 --- (Dec 24 is a Sunday)
    date(2028, 7, 3): "Day before Independence Day",
    date(2028, 11, 24): "Day after Thanksgiving",
}
