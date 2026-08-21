"""Time-series integrity: anti-look-ahead, gaps, revisions, as_of discipline."""

from __future__ import annotations

from datetime import timedelta

import pytest
from tests.strategy_helpers import make_candle, trending_candles

from app.domain.enums import Timeframe
from app.strategy.enums import RejectionCode
from app.strategy.models import CandleSeries, CandleSeriesError


def build(candles, as_of, *, min_candles=5, gap=1.5):
    return CandleSeries.build(
        Timeframe.M5, candles, as_of, min_candles=min_candles, max_gap_multiplier=gap
    )


def five(count):
    return trending_candles(Timeframe.M5, count)


def test_only_closed_candles_are_included() -> None:
    candles = five(10)
    # as_of exactly at close of candle index 7 -> candles 8, 9 are open/future
    as_of = candles[7].close_time
    series = build(candles, as_of)
    assert len(series) == 8
    assert series.last.open_time == candles[7].open_time
    assert all(candle.close_time <= as_of for candle in series.candles)


def test_open_candle_is_ignored_mid_bar() -> None:
    candles = five(10)
    as_of = candles[9].open_time + timedelta(minutes=2)  # candle 9 still open
    series = build(candles, as_of)
    assert series.last.open_time == candles[8].open_time


def test_as_of_boundary_is_inclusive_on_close() -> None:
    candles = five(10)
    as_of = candles[9].close_time
    series = build(candles, as_of)
    assert series.last.open_time == candles[9].open_time


def test_insufficient_history_rejects() -> None:
    candles = five(4)
    with pytest.raises(CandleSeriesError) as excinfo:
        build(candles, candles[-1].close_time, min_candles=10)
    assert excinfo.value.code is RejectionCode.INSUFFICIENT_CANDLE_HISTORY


def test_candle_gap_rejects() -> None:
    candles = five(10)
    gapped = candles[:5] + candles[7:]  # missing candles 5 and 6
    with pytest.raises(CandleSeriesError) as excinfo:
        build(gapped, candles[-1].close_time)
    assert excinfo.value.code is RejectionCode.CANDLE_GAP


def test_stale_last_candle_rejects() -> None:
    candles = five(10)
    as_of = candles[-1].close_time + timedelta(minutes=30)  # far beyond last close
    with pytest.raises(CandleSeriesError) as excinfo:
        build(candles, as_of)
    assert excinfo.value.code is RejectionCode.CANDLE_GAP


def test_only_open_candles_yields_open_candle_code() -> None:
    candles = five(3)
    as_of = candles[0].open_time + timedelta(minutes=1)  # nothing closed yet
    with pytest.raises(CandleSeriesError) as excinfo:
        build(candles, as_of, min_candles=1)
    assert excinfo.value.code is RejectionCode.OPEN_CANDLE_ONLY


def test_out_of_order_input_is_sorted_deterministically() -> None:
    candles = five(10)
    shuffled = [candles[3], candles[0], candles[7], *candles[1:3], *candles[4:7], *candles[8:]]
    series = build(shuffled, candles[-1].close_time)
    opens = [candle.open_time for candle in series.candles]
    assert opens == sorted(opens)


def test_candle_revision_last_wins_before_close() -> None:
    candles = five(10)
    revised = make_candle(
        Timeframe.M5,
        9,
        candles[9].open,
        candles[9].high + 1,
        candles[9].low,
        candles[9].close + 0.5,
    )
    series = build([*candles, revised], candles[-1].close_time)
    assert len(series) == 10
    assert series.last.close == revised.close  # latest revision wins


def test_no_lookahead_in_rolling_features() -> None:
    """A feature computed at as_of=t must not change when future candles are
    appended to the raw input (they are excluded by the series builder)."""
    from app.strategy.candle_features import atr, rolling_high

    candles = five(30)
    as_of = candles[19].close_time
    series_short = build(candles[:20], as_of, min_candles=5)
    series_with_future = build(candles, as_of, min_candles=5)
    assert atr(series_short, 5) == atr(series_with_future, 5)
    assert rolling_high(series_short, 10) == rolling_high(series_with_future, 10)


def test_window_metadata_records_used_candles() -> None:
    candles = five(10)
    series = build(candles, candles[-1].close_time)
    meta = series.window_metadata()
    assert meta["count"] == 10
    assert meta["first_open_time"] == candles[0].open_time.isoformat()
    assert meta["last_close_time"] == candles[-1].close_time.isoformat()
