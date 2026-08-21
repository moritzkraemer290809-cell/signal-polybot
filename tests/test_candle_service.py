"""Candle gap detection."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.data.candle_service import find_gaps
from app.domain.enums import Timeframe
from app.domain.models import CandleData


def candle(minute: int) -> CandleData:
    return CandleData(
        timeframe=Timeframe.M1,
        open_time=datetime(2026, 8, 21, 12, minute, tzinfo=UTC),
        open=Decimal("1"),
        high=Decimal("1"),
        low=Decimal("1"),
        close=Decimal("1"),
        volume=Decimal("0"),
    )


def test_contiguous_series_has_no_gaps() -> None:
    assert find_gaps([candle(0), candle(1), candle(2)], Timeframe.M1) == []


def test_missing_buckets_are_detected() -> None:
    gaps = find_gaps([candle(0), candle(3)], Timeframe.M1)
    assert len(gaps) == 1
    assert gaps[0].start == datetime(2026, 8, 21, 12, 1, tzinfo=UTC)
    assert gaps[0].end == datetime(2026, 8, 21, 12, 3, tzinfo=UTC)
    assert gaps[0].missing_buckets == 2


def test_bounds_extend_gap_detection() -> None:
    start = datetime(2026, 8, 21, 11, 58, tzinfo=UTC)
    end = datetime(2026, 8, 21, 12, 3, tzinfo=UTC)
    gaps = find_gaps([candle(0), candle(1)], Timeframe.M1, start=start, end=end)
    assert [(gap.start, gap.end) for gap in gaps] == [
        (start, datetime(2026, 8, 21, 12, 0, tzinfo=UTC)),
        (datetime(2026, 8, 21, 12, 2, tzinfo=UTC), end),
    ]


def test_empty_series_with_bounds_is_one_gap() -> None:
    start = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
    end = start + timedelta(minutes=5)
    gaps = find_gaps([], Timeframe.M1, start=start, end=end)
    assert len(gaps) == 1 and gaps[0].missing_buckets == 5
