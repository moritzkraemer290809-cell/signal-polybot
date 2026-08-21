"""Strategy engine: hard gates, candidate classes, scoring gates, dedupe.

All scenarios use clearly synthetic candles from tests/strategy_helpers.
Research artifacts only - assertions also verify that no trade execution
parameters exist anywhere on the produced objects.
"""

from __future__ import annotations

import dataclasses
from datetime import timedelta

from tests.strategy_helpers import (
    build_context,
    build_series,
    default_series_set,
    make_candle,
    ranging_candles,
    trending_candles,
)

from app.config import StrategySettings
from app.domain.enums import Timeframe
from app.strategy.enums import (
    CandidateDirection,
    CandidateState,
    CandidateType,
    RejectionCode,
    StrategyRegime,
)
from app.strategy.feature_pipeline import build_feature_bundle
from app.strategy.models import Candle
from app.strategy.regime import classify_regime
from app.strategy.setup_rules import RuleOutcome, continuation_rule
from app.strategy.strategy_engine import evaluate_context

SETTINGS = StrategySettings(_env_file=None)


def _shift(candle: Candle, delta: float) -> Candle:
    return Candle(
        open_time=candle.open_time,
        open=candle.open + delta,
        high=candle.high + delta,
        low=candle.low + delta,
        close=candle.close + delta,
        volume=candle.volume,
        trade_count=candle.trade_count,
        timeframe=candle.timeframe,
    )


def _codes(outcome) -> set[RejectionCode]:
    return {rejection.primary_code for rejection in outcome.rejections}


# --------------------------------------------------------------- hard gates


def test_gate_not_on_active_watchlist() -> None:
    series, as_of = default_series_set(trend_up=True, settings=SETTINGS)
    context = build_context(series, as_of, SETTINGS, watchlist_active=False)
    outcome = evaluate_context(context, SETTINGS)
    assert outcome.candidates == ()
    assert outcome.rejections[0].primary_code is RejectionCode.NOT_ON_ACTIVE_WATCHLIST
    assert outcome.regime is None  # gate fires before any feature computation


def test_gate_bot_paused() -> None:
    series, as_of = default_series_set(trend_up=True, settings=SETTINGS)
    context = build_context(series, as_of, SETTINGS, bot_paused=True)
    outcome = evaluate_context(context, SETTINGS)
    assert outcome.candidates == ()
    assert outcome.rejections[0].primary_code is RejectionCode.BOT_PAUSED


def test_gate_session_not_allowed() -> None:
    series, as_of = default_series_set(trend_up=True, settings=SETTINGS)
    context = build_context(series, as_of, SETTINGS, session_allowed=False, session_state="CLOSED")
    outcome = evaluate_context(context, SETTINGS)
    assert outcome.rejections[0].primary_code is RejectionCode.SESSION_NOT_ALLOWED
    assert "CLOSED" in outcome.rejections[0].detail


def test_gate_data_quality() -> None:
    series, as_of = default_series_set(trend_up=True, settings=SETTINGS)
    context = build_context(
        series, as_of, SETTINGS, data_quality_ok=False, data_quality_status="STALE"
    )
    outcome = evaluate_context(context, SETTINGS)
    assert outcome.rejections[0].primary_code is RejectionCode.DATA_QUALITY_NOT_SUFFICIENT


def test_gate_orderbook_not_fresh() -> None:
    series, as_of = default_series_set(trend_up=True, settings=SETTINGS)
    context = build_context(series, as_of, SETTINGS, orderbook_ok=False)
    outcome = evaluate_context(context, SETTINGS)
    assert outcome.rejections[0].primary_code is RejectionCode.ORDERBOOK_NOT_FRESH
    # gate is configuration-driven
    relaxed = StrategySettings(_env_file=None, require_fresh_orderbook=False)
    context = build_context(series, as_of, relaxed, orderbook_ok=False)
    outcome = evaluate_context(context, relaxed)
    assert RejectionCode.ORDERBOOK_NOT_FRESH not in _codes(outcome)


def test_gate_missing_required_timeframe() -> None:
    series, as_of = default_series_set(trend_up=True, settings=SETTINGS)
    partial = {key: value for key, value in series.items() if key != "15m"}
    context = build_context(partial, as_of, SETTINGS)
    outcome = evaluate_context(context, SETTINGS)
    assert outcome.rejections[0].primary_code is RejectionCode.INSUFFICIENT_CANDLE_HISTORY
    assert "15m" in outcome.rejections[0].detail


def test_gate_order_watchlist_before_paused() -> None:
    series, as_of = default_series_set(trend_up=True, settings=SETTINGS)
    context = build_context(series, as_of, SETTINGS, watchlist_active=False, bot_paused=True)
    outcome = evaluate_context(context, SETTINGS)
    assert outcome.rejections[0].primary_code is RejectionCode.NOT_ON_ACTIVE_WATCHLIST


# ------------------------------------------------------- candidate classes


def test_bullish_sweep_reversal_candidate() -> None:
    series, as_of = default_series_set(trend_up=True, settings=SETTINGS)
    outcome = evaluate_context(build_context(series, as_of, SETTINGS), SETTINGS)
    assert outcome.regime is not None
    assert outcome.regime.regime is StrategyRegime.TREND_UP
    types = {candidate.candidate_type for candidate in outcome.candidates}
    assert CandidateType.BULLISH_SWEEP_REVERSAL in types
    candidate = next(
        item
        for item in outcome.candidates
        if item.candidate_type is CandidateType.BULLISH_SWEEP_REVERSAL
    )
    assert candidate.direction is CandidateDirection.BULLISH
    assert candidate.setup_score >= SETTINGS.min_setup_score
    assert candidate.state in (CandidateState.DETECTED, CandidateState.CONFIRMED)
    assert candidate.reason_summary
    assert candidate.invalidation_conditions
    assert candidate.score_components


def test_bearish_sweep_reversal_candidate() -> None:
    series, as_of = default_series_set(trend_up=False, settings=SETTINGS)
    outcome = evaluate_context(build_context(series, as_of, SETTINGS), SETTINGS)
    assert outcome.regime is not None
    assert outcome.regime.regime is StrategyRegime.TREND_DOWN
    types = {candidate.candidate_type for candidate in outcome.candidates}
    assert CandidateType.BEARISH_SWEEP_REVERSAL in types
    candidate = next(
        item
        for item in outcome.candidates
        if item.candidate_type is CandidateType.BEARISH_SWEEP_REVERSAL
    )
    assert candidate.direction is CandidateDirection.BEARISH


def _series_with_15m_bos(settings: StrategySettings):
    """Trend-up set whose 15m series ends two candles into a fresh impulse:
    the last close confirms a break of structure above the last swing high."""
    series, as_of = default_series_set(trend_up=True, settings=settings)
    count_15m = 62  # 62 % 6 == 2 -> ends mid-impulse right after a BOS close
    m15 = trending_candles(
        Timeframe.M15,
        count_15m,
        up=True,
        step=0.2,
        start_price=100.0,
        start=as_of - timedelta(minutes=15 * count_15m),
    )
    series = dict(series)
    series["15m"] = build_series(Timeframe.M15, m15, as_of, settings)
    # 5m shifted far above every 15m level: no BELOW sweep -> the bullish
    # slot is decided by the continuation rule alone
    raised = [_shift(candle, +30.0) for candle in series["5m"].candles]
    series["5m"] = build_series(Timeframe.M5, list(raised), as_of, settings)
    return series, as_of


def test_bullish_reclaim_continuation_candidate() -> None:
    series, as_of = _series_with_15m_bos(SETTINGS)
    outcome = evaluate_context(build_context(series, as_of, SETTINGS), SETTINGS)
    types = {candidate.candidate_type for candidate in outcome.candidates}
    assert CandidateType.BULLISH_RECLAIM_CONTINUATION in types


def test_bearish_rejection_continuation_rule() -> None:
    """Rule-level: a wick rejection at the last 15m swing high with
    follow-through yields the BEARISH_REJECTION_CONTINUATION outcome.
    (Engine-level the bearish slot is organically won by the higher-scored
    sweep reversal - best-candidate-per-direction is tested separately.)"""
    series, as_of = default_series_set(trend_up=False, settings=SETTINGS)
    context0 = build_context(series, as_of, SETTINGS)
    bundle0 = build_feature_bundle(context0, SETTINGS)
    highs = [swing for swing in bundle0.structure["15m"].swings if swing.swing_type.value == "HIGH"]
    level = highs[-1].price
    m5 = list(series["5m"].candles)
    last3 = m5[-3:]
    poke = level * (1 + 0.0001)  # ~1bps above: touches, below 3bps sweep bound
    open_ = level - 0.20
    reject_candle = Candle(
        open_time=last3[0].open_time,
        open=open_,
        high=poke,
        low=open_ - 0.02,
        close=open_ + 0.02,
        volume=200.0,
        trade_count=20,
        timeframe=Timeframe.M5,
    )
    follow1 = Candle(
        open_time=last3[1].open_time,
        open=reject_candle.close,
        high=reject_candle.close + 0.02,
        low=reject_candle.close - 0.4,
        close=reject_candle.close - 0.35,
        volume=200.0,
        trade_count=20,
        timeframe=Timeframe.M5,
    )
    follow2 = Candle(
        open_time=last3[2].open_time,
        open=follow1.close,
        high=follow1.close + 0.02,
        low=follow1.close - 0.4,
        close=follow1.close - 0.3,
        volume=200.0,
        trade_count=20,
        timeframe=Timeframe.M5,
    )
    series = dict(series)
    series["5m"] = build_series(
        Timeframe.M5, [*m5[:-3], reject_candle, follow1, follow2], as_of, SETTINGS
    )
    context = build_context(series, as_of, SETTINGS)
    bundle = build_feature_bundle(context, SETTINGS)
    regime = classify_regime(
        series["1h"],
        bundle.structure["1h"],
        bundle.volatility_1h,
        SETTINGS,
        as_of=as_of,
        low_liquidity=False,
        data_degraded=False,
    )
    result = continuation_rule(context, bundle, regime, SETTINGS, bullish=False)
    assert isinstance(result, RuleOutcome)
    assert result.candidate_type is CandidateType.BEARISH_REJECTION_CONTINUATION
    assert result.direction is CandidateDirection.BEARISH
    assert result.rejection_status == "CONFIRMED"


def _range_breakout_series(up: bool, settings: StrategySettings):
    series, as_of = default_series_set(trend_up=None, settings=settings)
    m5 = ranging_candles(Timeframe.M5, 80, start=as_of - timedelta(minutes=5 * 80))
    if up:
        closes = [100.4, 100.8, 101.15, 101.35, 101.25, 101.45]
    else:
        closes = [99.6, 99.2, 98.85, 98.65, 98.75, 98.55]
    prev = m5[73].close
    tail = []
    for offset, close in enumerate(closes):
        open_ = prev
        tail.append(
            make_candle(
                Timeframe.M5,
                74 + offset,
                open_,
                max(open_, close) + 0.05,
                min(open_, close) - 0.05,
                close,
                volume=300.0,
                start=as_of - timedelta(minutes=5 * 80),
            )
        )
        prev = close
    series = dict(series)
    series["5m"] = build_series(Timeframe.M5, m5[:74] + tail, as_of, settings)
    return series, as_of


def test_range_breakout_up_candidate() -> None:
    settings = StrategySettings(_env_file=None, min_setup_score=55)
    series, as_of = _range_breakout_series(True, settings)
    outcome = evaluate_context(build_context(series, as_of, settings), settings)
    assert outcome.regime is not None
    assert outcome.regime.regime in (StrategyRegime.RANGE, StrategyRegime.BREAKOUT)
    types = {candidate.candidate_type for candidate in outcome.candidates}
    assert CandidateType.RANGE_BREAKOUT_UP in types


def test_range_breakout_down_candidate() -> None:
    settings = StrategySettings(_env_file=None, min_setup_score=55)
    series, as_of = _range_breakout_series(False, settings)
    outcome = evaluate_context(build_context(series, as_of, settings), settings)
    types = {candidate.candidate_type for candidate in outcome.candidates}
    assert CandidateType.RANGE_BREAKOUT_DOWN in types
    candidate = next(iter(outcome.candidates))
    assert candidate.direction is CandidateDirection.BEARISH


# --------------------------------------------------------- scoring gates


def test_score_threshold_rejects_weak_setup() -> None:
    """The same breakout scenario is a candidate at threshold 55 and a
    SETUP_SCORE_BELOW_THRESHOLD rejection at the default threshold."""
    series, as_of = _range_breakout_series(True, SETTINGS)
    outcome = evaluate_context(build_context(series, as_of, SETTINGS), SETTINGS)
    assert not any(
        candidate.candidate_type is CandidateType.RANGE_BREAKOUT_UP
        for candidate in outcome.candidates
    )
    below = [
        rejection
        for rejection in outcome.rejections
        if rejection.primary_code is RejectionCode.SETUP_SCORE_BELOW_THRESHOLD
    ]
    assert below
    assert below[0].candidate_type is CandidateType.RANGE_BREAKOUT_UP
    assert str(SETTINGS.min_setup_score) in below[0].detail


def test_momentum_confirmation_is_mandatory() -> None:
    """Flat 5m tail -> neutral momentum -> MOMENTUM_INSUFFICIENT, never a
    candidate with reduced score."""
    series, as_of = _series_with_15m_bos(SETTINGS)
    flat_level = series["5m"].candles[-1].close
    candles = list(series["5m"].candles)
    tail = [
        Candle(
            open_time=candle.open_time,
            open=flat_level,
            high=flat_level + 0.02,
            low=flat_level - 0.02,
            close=flat_level,
            volume=candle.volume,
            trade_count=candle.trade_count,
            timeframe=candle.timeframe,
        )
        for candle in candles[-12:]
    ]
    series = dict(series)
    series["5m"] = build_series(Timeframe.M5, candles[:-12] + tail, as_of, SETTINGS)
    outcome = evaluate_context(build_context(series, as_of, SETTINGS), SETTINGS)
    assert outcome.candidates == ()
    assert RejectionCode.MOMENTUM_INSUFFICIENT in _codes(outcome)


def test_volume_confirmation_gate_when_enabled() -> None:
    settings = StrategySettings(_env_file=None, require_volume_confirmation=True)
    series, as_of = default_series_set(trend_up=True, settings=settings)
    # uniform volume -> relative volume 1.0 < min_relative_volume
    outcome = evaluate_context(build_context(series, as_of, settings), settings)
    assert outcome.candidates == ()
    assert RejectionCode.VOLUME_INSUFFICIENT in _codes(outcome)
    # same scenario without the gate produces the candidate
    outcome = evaluate_context(build_context(series, as_of, SETTINGS), SETTINGS)
    assert outcome.candidates


def test_higher_timeframe_conflict_kills_candidate() -> None:
    """A clean bullish 5m sweep+reclaim inside a bearish 1h/15m context is
    rejected with HIGHER_TIMEFRAME_CONFLICT."""
    series, as_of = default_series_set(trend_up=False, settings=SETTINGS)
    context0 = build_context(series, as_of, SETTINGS)
    bundle0 = build_feature_bundle(context0, SETTINGS)
    lows = [swing for swing in bundle0.structure["15m"].swings if swing.swing_type.value == "LOW"]
    level = lows[-1].price
    m5 = list(series["5m"].candles)
    tail_slots = m5[-12:]
    crafted: list[Candle] = []
    # drift just above the level, then sweep it and rally (bullish momentum)
    for offset, slot in enumerate(tail_slots[:9]):
        close = level + 0.30 - offset * 0.02
        crafted.append(
            Candle(
                open_time=slot.open_time,
                open=close + 0.01,
                high=close + 0.04,
                low=close - 0.02,
                close=close,
                volume=200.0,
                trade_count=20,
                timeframe=Timeframe.M5,
            )
        )
    sweep_slot, rally1, rally2 = tail_slots[9:]
    sweep_candle = Candle(
        open_time=sweep_slot.open_time,
        open=level + 0.12,
        high=level + 0.14,
        low=level - level * 0.0010,  # 10bps overshoot below the level
        close=level + 0.05,  # closes back above -> confirmed sweep + reclaim
        volume=250.0,
        trade_count=25,
        timeframe=Timeframe.M5,
    )
    rally_a = Candle(
        open_time=rally1.open_time,
        open=sweep_candle.close,
        high=level + 0.45,
        low=sweep_candle.close - 0.02,
        close=level + 0.40,
        volume=250.0,
        trade_count=25,
        timeframe=Timeframe.M5,
    )
    rally_b = Candle(
        open_time=rally2.open_time,
        open=rally_a.close,
        high=level + 0.85,
        low=rally_a.close - 0.02,
        close=level + 0.80,
        volume=250.0,
        trade_count=25,
        timeframe=Timeframe.M5,
    )
    series = dict(series)
    series["5m"] = build_series(
        Timeframe.M5, [*m5[:-12], *crafted, sweep_candle, rally_a, rally_b], as_of, SETTINGS
    )
    outcome = evaluate_context(build_context(series, as_of, SETTINGS), SETTINGS)
    conflicts = [
        rejection
        for rejection in outcome.rejections
        if rejection.primary_code is RejectionCode.HIGHER_TIMEFRAME_CONFLICT
    ]
    assert conflicts
    assert conflicts[0].candidate_type is CandidateType.BULLISH_SWEEP_REVERSAL
    assert not any(
        candidate.direction is CandidateDirection.BULLISH for candidate in outcome.candidates
    )


def test_best_candidate_per_direction() -> None:
    series, as_of = default_series_set(trend_up=True, settings=SETTINGS)
    outcome = evaluate_context(build_context(series, as_of, SETTINGS), SETTINGS)
    directions = [candidate.direction for candidate in outcome.candidates]
    assert len(directions) == len(set(directions))


# --------------------------------------------- determinism, dedupe, hygiene


def test_evaluation_is_deterministic_and_dedupe_key_stable() -> None:
    series, as_of = default_series_set(trend_up=True, settings=SETTINGS)
    context = build_context(series, as_of, SETTINGS)
    first = evaluate_context(context, SETTINGS)
    second = evaluate_context(context, SETTINGS)
    assert [c.candidate_type for c in first.candidates] == [
        c.candidate_type for c in second.candidates
    ]
    assert [c.setup_score for c in first.candidates] == [c.setup_score for c in second.candidates]
    assert [c.dedupe_key for c in first.candidates] == [c.dedupe_key for c in second.candidates]
    # candidate_id is a fresh uuid per evaluation, dedupe_key is not
    assert first.candidates[0].candidate_id != second.candidates[0].candidate_id


def test_dedupe_key_varies_by_instrument_and_version() -> None:
    series, as_of = default_series_set(trend_up=True, settings=SETTINGS)
    base = evaluate_context(build_context(series, as_of, SETTINGS), SETTINGS)
    other_instrument = evaluate_context(
        build_context(series, as_of, SETTINGS, instrument_id=999), SETTINGS
    )
    other_version = evaluate_context(
        build_context(series, as_of, SETTINGS, strategy_version="9.9.9"), SETTINGS
    )
    assert base.candidates[0].dedupe_key != other_instrument.candidates[0].dedupe_key
    assert base.candidates[0].dedupe_key != other_version.candidates[0].dedupe_key


def test_candidates_carry_no_trade_parameters() -> None:
    series, as_of = default_series_set(trend_up=True, settings=SETTINGS)
    outcome = evaluate_context(build_context(series, as_of, SETTINGS), SETTINGS)
    assert outcome.candidates
    banned = (
        "entry",
        "stop",
        "target",
        "take_profit",
        "leverage",
        "position",
        "order",
        "size",
        "notional",
    )
    for candidate in outcome.candidates:
        field_names = {field.name for field in dataclasses.fields(candidate)}
        for name in field_names:
            assert not any(term in name for term in banned), name
        # nested feature payloads stay clean as well
        for key in candidate.features:
            assert not any(term in key for term in banned), key
    for rejection in outcome.rejections:
        field_names = {field.name for field in dataclasses.fields(rejection)}
        for name in field_names:
            assert not any(term in name for term in banned), name


def test_candidates_are_versioned_and_explainable() -> None:
    series, as_of = default_series_set(trend_up=True, settings=SETTINGS)
    context = build_context(series, as_of, SETTINGS)
    outcome = evaluate_context(context, SETTINGS)
    for candidate in outcome.candidates:
        assert candidate.strategy_name == SETTINGS.name
        assert candidate.strategy_version == SETTINGS.version
        assert candidate.config_hash == context.config_hash
        assert candidate.ruleset_hash == context.ruleset_hash
        assert candidate.candle_window_metadata  # reproducibility anchor
        total = sum(component.awarded for component in candidate.score_components)
        assert total == candidate.setup_score
