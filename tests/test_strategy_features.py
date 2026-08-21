"""Feature functions: candle math, volatility, momentum, volume, book/context."""

from __future__ import annotations

import math
from datetime import UTC, datetime

import pytest
from tests.strategy_helpers import make_candle, market_snapshot, trending_candles

from app.domain.enums import Timeframe
from app.strategy import candle_features as cf
from app.strategy.market_context import compute_market_context
from app.strategy.models import CandleSeries
from app.strategy.momentum import compute_momentum
from app.strategy.orderbook_features import compute_orderbook_features
from app.strategy.volatility import compute_volatility
from app.strategy.volume_confirmation import compute_volume

AS_OF = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)


def series_from(rows: list[tuple[float, float, float, float]], volume: float = 100.0):
    candles = [
        make_candle(Timeframe.M5, index, o, h, low, c, volume=volume)
        for index, (o, h, low, c) in enumerate(rows)
    ]
    return CandleSeries.build(
        Timeframe.M5, candles, candles[-1].close_time, min_candles=1, max_gap_multiplier=1.5
    )


def test_true_range_uses_previous_close() -> None:
    series = series_from([(100, 101, 99, 100.5), (103, 104, 102.5, 103.5)])
    ranges = cf.true_range_series(series)
    assert ranges[0] == pytest.approx(2.0)  # first: high-low
    # second: max(1.5, |104-100.5|=3.5, |102.5-100.5|=2.0) = 3.5 (gap up)
    assert ranges[1] == pytest.approx(3.5)


def test_atr_wilder_smoothing() -> None:
    rows = [(100 + i * 0.0, 101 + i * 0.0, 99, 100) for i in range(20)]
    series = series_from(rows)
    assert cf.atr(series, 5) == pytest.approx(2.0)  # constant TR -> ATR = TR
    assert cf.atr_bps(series, 5) == pytest.approx(2.0 / 100 * 10_000)


def test_atr_insufficient_history_is_none() -> None:
    series = series_from([(100, 101, 99, 100)] * 3)
    assert cf.atr(series, 5) is None


def test_returns_and_log_returns() -> None:
    series = series_from([(100, 101, 99, 100), (100, 103, 99, 102)])
    assert cf.simple_returns(series, 1) == pytest.approx(0.02)
    assert cf.log_return(series, 1) == pytest.approx(math.log(102 / 100))
    assert cf.simple_returns(series, 5) is None  # window exceeds history


def test_realized_volatility_zero_for_flat_series() -> None:
    series = series_from([(100, 100.5, 99.5, 100)] * 12)
    assert cf.realized_volatility(series, 10) == pytest.approx(0.0)


def test_body_wick_and_close_location() -> None:
    series = series_from([(100, 106, 99, 101)])
    candle = series.last
    assert cf.body_to_range_ratio(candle) == pytest.approx(1 / 7)
    assert cf.upper_wick_ratio(candle) == pytest.approx(5 / 7)
    assert cf.lower_wick_ratio(candle) == pytest.approx(1 / 7)
    # CLV: ((101-99)-(106-101))/7 = -3/7
    assert cf.close_location_value(candle) == pytest.approx(-3 / 7)


def test_zero_range_candle_features_are_none_not_guessed() -> None:
    series = series_from([(100, 100, 100, 100)])
    candle = series.last
    assert cf.body_to_range_ratio(candle) is None
    assert cf.close_location_value(candle) is None


def test_rolling_high_low_and_distance() -> None:
    series = series_from([(100, 105, 95, 100), (100, 110, 98, 102), (102, 108, 101, 103)])
    assert cf.rolling_high(series, 3) == 110
    assert cf.rolling_low(series, 3) == 95
    assert cf.rolling_high(series, 2, exclude_last=True) == pytest.approx(110)
    assert cf.distance_bps(103, 110) == pytest.approx((103 - 110) / 110 * 10_000)


def test_compression_expansion_ratio() -> None:
    rows = [(100, 104, 96, 100)] * 15 + [(100, 100.5, 99.5, 100)] * 5
    series = series_from(rows)
    ratio = cf.range_compression_ratio(series, 5, 20)
    assert ratio is not None and ratio < 0.5  # recent ranges compressed


def test_efficiency_ratio_bounds() -> None:
    monotone = series_from([(100 + i, 100.5 + i, 99.5 + i, 101 + i) for i in range(10)])
    assert cf.efficiency_ratio(monotone, 8) == pytest.approx(1.0)
    choppy = series_from([(100 + (i % 2), 101, 99, 100 + ((i + 1) % 2)) for i in range(10)])
    assert cf.efficiency_ratio(choppy, 8) <= 0.2


def test_volatility_percentile_midrank_on_uniform_data() -> None:
    series = series_from([(100, 101, 99, 100)] * 30)
    features = compute_volatility(series, atr_period=5, lookback=20, high_percentile=90)
    assert features.atr_percentile == pytest.approx(50.0)  # uniform -> midrank
    assert features.is_high_volatility is False


def test_high_volatility_detection() -> None:
    rows = [(100, 100.6, 99.4, 100)] * 28 + [(100, 108, 92, 101), (101, 110, 93, 100)]
    series = series_from(rows)
    features = compute_volatility(series, atr_period=5, lookback=25, high_percentile=90)
    assert features.atr_percentile is not None and features.atr_percentile >= 90
    assert features.is_high_volatility is True


def test_momentum_direction_and_continuation() -> None:
    up = series_from(
        [(100 + i * 0.5, 100.6 + i * 0.5, 99.9 + i * 0.5, 100.5 + i * 0.5) for i in range(20)]
    )
    features = compute_momentum(up, lookback=5)
    assert features.direction == 1
    assert features.continuation_count >= 5
    assert features.rsi is not None and features.rsi > 50


def test_relative_volume_and_missing_trade_count() -> None:
    candles = trending_candles(Timeframe.M5, 25, volume=100.0)
    spike = make_candle(Timeframe.M5, 25, 100, 101, 99, 100.5, volume=300.0, trade_count=0)
    series = CandleSeries.build(
        Timeframe.M5, [*candles, spike], spike.close_time, min_candles=5, max_gap_multiplier=1.5
    )
    features = compute_volume(series, lookback=20)
    assert features.relative_volume == pytest.approx(3.0)
    assert features.volume_available is True


def test_volume_baseline_missing_is_flagged_not_guessed() -> None:
    series = series_from([(100, 101, 99, 100)] * 5, volume=0.0)
    features = compute_volume(series, lookback=20)
    assert features.volume_available is False
    assert features.relative_volume is None


def test_orderbook_features_invalid_when_stale() -> None:
    stale = market_snapshot(AS_OF, book_fresh=False)
    features = compute_orderbook_features(stale)
    assert features.valid is False
    assert features.spread_bps is None
    assert features.depth_imbalance is None


def test_orderbook_imbalance() -> None:
    snapshot = market_snapshot(AS_OF, bid_depth_pusd=30_000.0, ask_depth_pusd=10_000.0)
    features = compute_orderbook_features(snapshot)
    assert features.valid is True
    assert features.depth_imbalance == pytest.approx(0.5)


def test_market_context_deviations_and_missing_prices() -> None:
    snapshot = market_snapshot(AS_OF, price=100.0)
    features = compute_market_context(snapshot)
    assert features.valid is True
    assert features.mark_to_index_bps == pytest.approx(-2.0, abs=0.1)
    missing = market_snapshot(AS_OF, index_price=None, mid_price=None)
    features = compute_market_context(missing)
    assert features.valid is False
    assert features.mark_to_index_bps is None  # never estimated
