"""Liquidity pools, sweeps, reclaim, rejection and retest."""

from __future__ import annotations

from tests.strategy_helpers import make_candle, trending_candles

from app.domain.enums import Timeframe
from app.strategy.enums import LiquidityLevelType, RetestStatus, SweepDirection
from app.strategy.liquidity_pools import detect_liquidity_levels
from app.strategy.liquidity_sweeps import detect_sweeps
from app.strategy.models import CandleSeries, LiquidityLevel
from app.strategy.reclaim_rejection import detect_reclaim, detect_rejection
from app.strategy.retest import evaluate_retest
from app.strategy.swing_detection import detect_swings


def build_series(candles, timeframe=Timeframe.M5):
    return CandleSeries.build(
        timeframe, candles, candles[-1].close_time, min_candles=1, max_gap_multiplier=1.5
    )


def rows(series_rows, timeframe=Timeframe.M5):
    return [
        make_candle(timeframe, index, o, h, low, c)
        for index, (o, h, low, c) in enumerate(series_rows)
    ]


def level(price: float, level_type=LiquidityLevelType.SWING_LOW) -> LiquidityLevel:
    return LiquidityLevel(
        level_type=level_type,
        timeframe=Timeframe.M5,
        price=price,
        relevance=1.0,
        touches=1,
        is_major=True,
        detail="test level",
    )


def test_liquidity_pools_from_swings_and_range() -> None:
    series = build_series(trending_candles(Timeframe.M15, 60), Timeframe.M15)
    swings = detect_swings(series, left_bars=2, right_bars=2, atr_multiplier=0.0, atr_period=5)
    levels = detect_liquidity_levels(series, swings, lookback=50, equal_tolerance_bps=5)
    types = {item.level_type for item in levels}
    assert LiquidityLevelType.SWING_HIGH in types
    assert LiquidityLevelType.SWING_LOW in types
    assert LiquidityLevelType.RANGE_HIGH in types
    assert LiquidityLevelType.RANGE_LOW in types


def test_sweep_below_with_reaction_same_candle() -> None:
    candles = rows(
        [
            (100, 101, 99.5, 100.5),
            (100.5, 101, 99.8, 100.2),
            (100.2, 100.8, 98.8, 100.4),  # pierces 99.5 level, closes back above
            (100.4, 101, 100, 100.8),
        ]
    )
    series = build_series(candles)
    sweeps = detect_sweeps(series, [level(99.5)], min_overshoot_bps=3.0)
    assert len(sweeps) == 1
    sweep = sweeps[0]
    assert sweep.direction is SweepDirection.BELOW
    assert sweep.confirmed is True
    assert sweep.confidence == 0.9
    assert sweep.overshoot_bps > 3.0
    assert "candle-approximated" in sweep.detail  # documented uncertainty


def test_sweep_without_reaction_has_reduced_confidence() -> None:
    candles = rows(
        [
            (100, 101, 99.5, 100.5),
            (100.2, 100.8, 98.8, 99.0),  # pierces and stays below
            (99.0, 99.3, 98.5, 98.7),
        ]
    )
    series = build_series(candles)
    sweeps = detect_sweeps(series, [level(99.5)], min_overshoot_bps=3.0)
    assert len(sweeps) == 1
    assert sweeps[0].confirmed is False
    assert sweeps[0].confidence == 0.3


def test_sweep_overshoot_below_minimum_is_ignored() -> None:
    candles = rows([(100, 101, 99.49, 100.5)])  # only ~1bp beyond 99.5
    series = build_series(candles)
    assert detect_sweeps(series, [level(99.5)], min_overshoot_bps=5.0) == []


def test_reclaim_requires_close_beyond_level() -> None:
    candles = rows(
        [
            (100, 101, 98.8, 99.2),  # sweep candle, closes below level
            (99.2, 99.6, 98.9, 99.4),  # still below
            (99.4, 100.4, 99.3, 100.1),  # close back above 99.5 + tolerance
        ]
    )
    series = build_series(candles)
    sweeps = detect_sweeps(series, [level(99.5)], min_overshoot_bps=3.0)
    reclaim = detect_reclaim(series, sweeps[0], tolerance_bps=2.0)
    assert reclaim is not None
    assert reclaim.close_price == 100.1
    assert reclaim.confirmed_at == series.candles[2].close_time


def test_rejection_needs_wick_close_and_follow_through() -> None:
    # bearish rejection at 105: wick into level, close away, next candle lower
    candles = rows(
        [
            (100, 105.5, 99.8, 100.4),  # wick to 105.5, closes far below
            (100.4, 100.6, 99.0, 99.3),  # follow-through down
        ]
    )
    series = build_series(candles)
    rejection = detect_rejection(series, 105.0, direction_bearish=True)
    assert rejection is not None
    assert rejection.followed_through is True

    # wick only, no follow-through -> no rejection
    no_follow = rows(
        [
            (100, 105.5, 99.8, 100.4),
            (100.4, 101.5, 100.2, 101.2),  # closes higher instead
        ]
    )
    assert detect_rejection(build_series(no_follow), 105.0, direction_bearish=True) is None


def test_retest_statuses() -> None:
    base = rows(
        [
            (100, 100.5, 99.5, 100.2),
            (100.2, 101.2, 100, 101),  # break above 100.5
        ]
    )
    break_time = base[-1].close_time

    # confirmed: comes back into band, holds, moves away
    confirmed = [
        *base,
        make_candle(Timeframe.M5, 2, 101, 101.2, 100.45, 100.9),
        make_candle(Timeframe.M5, 3, 100.9, 102, 100.8, 101.8),
    ]
    result = evaluate_retest(
        build_series(confirmed),
        100.5,
        break_time,
        bullish=True,
        tolerance_bps=10,
        max_candles=6,
    )
    assert result.status is RetestStatus.CONFIRMED

    # failed: closes back through the level
    failed = [*base, make_candle(Timeframe.M5, 2, 101, 101.1, 99.8, 100.0)]
    result = evaluate_retest(
        build_series(failed),
        100.5,
        break_time,
        bullish=True,
        tolerance_bps=10,
        max_candles=6,
    )
    assert result.status is RetestStatus.FAILED

    # pending: no touch yet, window still open
    pending = [*base, make_candle(Timeframe.M5, 2, 101, 102, 100.9, 101.8)]
    result = evaluate_retest(
        build_series(pending),
        100.5,
        break_time,
        bullish=True,
        tolerance_bps=10,
        max_candles=6,
    )
    assert result.status is RetestStatus.PENDING

    # not present: window elapsed without any touch
    far = base + [
        make_candle(Timeframe.M5, 2 + i, 101 + i, 102 + i, 100.9 + i, 101.8 + i) for i in range(4)
    ]
    result = evaluate_retest(
        build_series(far),
        100.5,
        break_time,
        bullish=True,
        tolerance_bps=10,
        max_candles=4,
    )
    assert result.status is RetestStatus.NOT_PRESENT


def test_overlapping_levels_each_get_their_own_sweep() -> None:
    candles = rows(
        [
            (100, 101, 99.5, 100.5),
            (100.2, 100.8, 98.5, 100.4),  # pierces both 99.5 and 99.0
        ]
    )
    series = build_series(candles)
    sweeps = detect_sweeps(series, [level(99.5), level(99.0)], min_overshoot_bps=3.0)
    assert {sweep.level.price for sweep in sweeps} == {99.5, 99.0}
