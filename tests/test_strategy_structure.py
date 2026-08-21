"""Swing detection and market structure: confirmation discipline, HH/HL,
equal levels, BOS, ChoCh, no wick-only breaks."""

from __future__ import annotations

from tests.strategy_helpers import make_candle, trending_candles

from app.domain.enums import Timeframe
from app.strategy.enums import StructureEventType, StructureState, SwingType
from app.strategy.market_structure import analyze_structure
from app.strategy.models import CandleSeries
from app.strategy.swing_detection import detect_swings


def build_series(candles):
    return CandleSeries.build(
        Timeframe.M15, candles, candles[-1].close_time, min_candles=1, max_gap_multiplier=1.5
    )


def rows_to_candles(rows):
    return [
        make_candle(Timeframe.M15, index, o, h, low, c) for index, (o, h, low, c) in enumerate(rows)
    ]


def swings_for(series, *, left=2, right=2, atr_mult=0.0):
    return detect_swings(
        series, left_bars=left, right_bars=right, atr_multiplier=atr_mult, atr_period=5
    )


def test_confirmed_pivot_high_and_low() -> None:
    # clear pivot high at index 3 and pivot low at index 7
    rows = [
        (100, 101, 99, 100.5),
        (100.5, 102, 100, 101.5),
        (101.5, 103, 101, 102.5),
        (102.5, 105, 102, 104),  # pivot high (105)
        (104, 104.5, 102.5, 103),
        (103, 103.5, 101.5, 102),
        (102, 102.5, 100.5, 101),
        (101, 101.5, 99, 99.5),  # pivot low (99)
        (99.5, 101, 99.2, 100.5),
        (100.5, 102, 100, 101.5),
    ]
    series = build_series(rows_to_candles(rows))
    swings = swings_for(series)
    highs = [swing for swing in swings if swing.swing_type is SwingType.HIGH]
    lows = [swing for swing in swings if swing.swing_type is SwingType.LOW]
    assert [swing.price for swing in highs] == [105]
    assert [swing.price for swing in lows] == [99]
    # confirmation = close time of the 2nd candle after the pivot
    assert highs[0].confirmed_at == series.candles[5].close_time


def test_unconfirmed_pivot_at_series_end_is_not_emitted() -> None:
    rows = [
        (100, 101, 99, 100.5),
        (100.5, 102, 100, 101.5),
        (101.5, 105, 101, 104),  # would-be pivot high, but only 1 candle follows
        (104, 104.5, 102.5, 103),
    ]
    series = build_series(rows_to_candles(rows))
    assert swings_for(series) == []


def test_atr_filter_drops_weak_pivots() -> None:
    rows = [
        (100, 100.6, 99.4, 100.2),
        (100.2, 100.7, 99.8, 100.4),
        (100.4, 100.9, 100.0, 100.6),  # micro pivot high
        (100.6, 100.8, 100.1, 100.3),
        (100.3, 100.6, 99.9, 100.1),
        (100.1, 100.5, 99.8, 100.2),
    ]
    series = build_series(rows_to_candles(rows))
    assert swings_for(series, atr_mult=3.0) == []  # prominence < 3*ATR filtered


def test_uptrend_structure_is_bullish_with_hh_hl() -> None:
    series = build_series(trending_candles(Timeframe.M15, 40, up=True))
    swings = swings_for(series)
    analysis = analyze_structure(series, swings, equal_tolerance_bps=5, confirmation_closes=1)
    assert analysis.state is StructureState.BULLISH
    event_types = {event.event_type for event in analysis.events}
    assert StructureEventType.HIGHER_HIGH in event_types
    assert StructureEventType.HIGHER_LOW in event_types


def test_downtrend_structure_is_bearish_with_lh_ll() -> None:
    series = build_series(trending_candles(Timeframe.M15, 40, up=False))
    swings = swings_for(series)
    analysis = analyze_structure(series, swings, equal_tolerance_bps=5, confirmation_closes=1)
    assert analysis.state is StructureState.BEARISH
    event_types = {event.event_type for event in analysis.events}
    assert StructureEventType.LOWER_HIGH in event_types
    assert StructureEventType.LOWER_LOW in event_types


def test_equal_levels_within_tolerance() -> None:
    rows = [
        (99.5, 100.2, 99, 100),
        (100, 101, 99.5, 100.5),
        (100.5, 104.99, 100, 103),  # pivot high ~105 (index 2)
        (103, 103.5, 101, 102),
        (102, 102.5, 100.5, 101),
        (101, 105.01, 100.8, 103.5),  # second pivot high within tolerance
        (103.5, 104, 102, 102.5),
        (102.5, 103, 101.5, 102),
    ]
    series = build_series(rows_to_candles(rows))
    swings = swings_for(series)
    analysis = analyze_structure(series, swings, equal_tolerance_bps=10, confirmation_closes=1)
    assert StructureEventType.EQUAL_HIGH in {event.event_type for event in analysis.events}


def test_bos_requires_close_not_wick() -> None:
    """A wick above the last swing high must NOT create a BOS; a close must."""
    base = [
        (99.5, 100.2, 99, 100),
        (100, 101, 99.5, 100.5),
        (100.5, 105, 100, 104),  # pivot high 105 (index 2: two candles each side)
        (104, 104.5, 102, 103),
        (103, 103.5, 101, 102),
    ]
    wick_only = [*base, (102, 106, 101.5, 102.5)]  # wick above 105, close below
    series = build_series(rows_to_candles(wick_only))
    analysis = analyze_structure(
        series, swings_for(series), equal_tolerance_bps=5, confirmation_closes=1
    )
    assert StructureEventType.BREAK_OF_STRUCTURE not in {
        event.event_type for event in analysis.events
    }

    with_close = [*base, (102, 106.5, 101.5, 106)]  # close above 105
    series = build_series(rows_to_candles(with_close))
    analysis = analyze_structure(
        series, swings_for(series), equal_tolerance_bps=5, confirmation_closes=1
    )
    bos = [
        event
        for event in analysis.events
        if event.event_type is StructureEventType.BREAK_OF_STRUCTURE
    ]
    assert len(bos) == 1
    assert bos[0].price == 105
    assert "above" in bos[0].detail


def test_bos_confirmation_closes_configurable() -> None:
    base = [
        (99.5, 100.2, 99, 100),
        (100, 101, 99.5, 100.5),
        (100.5, 105, 100, 104),
        (104, 104.5, 102, 103),
        (103, 103.5, 101, 102),
        (102, 106.5, 101.5, 106),  # first close above
    ]
    series = build_series(rows_to_candles(base))
    analysis = analyze_structure(
        series, swings_for(series), equal_tolerance_bps=5, confirmation_closes=2
    )
    # only one confirming close -> no BOS yet with confirmation_closes=2
    assert StructureEventType.BREAK_OF_STRUCTURE not in {
        event.event_type for event in analysis.events
    }
    two_closes = [*base, (106, 107, 105.5, 106.5)]
    series = build_series(rows_to_candles(two_closes))
    analysis = analyze_structure(
        series, swings_for(series), equal_tolerance_bps=5, confirmation_closes=2
    )
    assert StructureEventType.BREAK_OF_STRUCTURE in {event.event_type for event in analysis.events}


def test_change_of_character_in_downtrend() -> None:
    """In a bearish structure, confirmed closes above the last swing high
    produce a ChoCh (not a plain BOS) and the state turns TRANSITIONAL."""
    down = trending_candles(Timeframe.M15, 30, up=False, start_price=200.0)
    last_close = down[-1].close
    recovery = [
        make_candle(
            Timeframe.M15,
            30 + index,
            last_close + index * 2,
            last_close + index * 2 + 2.4,
            last_close + index * 2 - 0.2,
            last_close + (index + 1) * 2,
        )
        for index in range(6)
    ]
    series = build_series([*down, *recovery])
    swings = swings_for(series)
    analysis = analyze_structure(series, swings, equal_tolerance_bps=5, confirmation_closes=1)
    assert StructureEventType.CHANGE_OF_CHARACTER in {event.event_type for event in analysis.events}
    assert analysis.state is StructureState.TRANSITIONAL


def test_insufficient_structure_with_too_few_swings() -> None:
    rows = [(100, 100.5, 99.5, 100)] * 6
    series = build_series(rows_to_candles(rows))
    analysis = analyze_structure(
        series, swings_for(series), equal_tolerance_bps=5, confirmation_closes=1
    )
    assert analysis.state is StructureState.INSUFFICIENT_STRUCTURE


def test_parameter_change_changes_swing_identity() -> None:
    series = build_series(trending_candles(Timeframe.M15, 40, up=True))
    swings_22 = swings_for(series, left=2, right=2)
    swings_33 = swings_for(series, left=3, right=3)
    assert len(swings_33) <= len(swings_22)
    assert all(swing.params == {"left": 3, "right": 3} for swing in swings_33)
