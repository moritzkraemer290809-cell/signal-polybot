"""Deterministic candidate-class rules.

Each rule inspects the feature bundle and either yields a RuleOutcome
(component fractions + evidence + referenced levels) or explains why it does
not apply / fails a mandatory confirmation.  No rule ever produces trade
parameters - only structure/research classifications.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.config import StrategySettings
from app.strategy.enums import (
    CandidateDirection,
    CandidateType,
    RejectionCode,
    RetestStatus,
    StrategyRegime,
    StructureEventType,
    StructureState,
    SweepDirection,
)
from app.strategy.feature_pipeline import FeatureBundle
from app.strategy.models import EvaluationContext, RegimeResult, RetestResult
from app.strategy.reclaim_rejection import detect_reclaim, detect_rejection
from app.strategy.retest import evaluate_retest


@dataclass(frozen=True)
class RuleOutcome:
    candidate_type: CandidateType
    direction: CandidateDirection
    fractions: dict[str, tuple[float, str]]
    evidence: list[str]
    referenced_levels: list[dict[str, Any]]
    structure_events: list[str]
    liquidity_events: list[str]
    reclaim_status: str
    rejection_status: str
    retest_status: RetestStatus
    confirmed: bool
    invalidation_conditions: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class RuleFailure:
    candidate_type: CandidateType
    code: RejectionCode
    detail: str


RuleResult = RuleOutcome | RuleFailure | None


def _htf_fraction(
    direction: CandidateDirection, structure: StructureState, regime: StrategyRegime
) -> tuple[float, str]:
    aligned_structure = (
        StructureState.BULLISH
        if direction is CandidateDirection.BULLISH
        else StructureState.BEARISH
    )
    aligned_regime = (
        StrategyRegime.TREND_UP
        if direction is CandidateDirection.BULLISH
        else StrategyRegime.TREND_DOWN
    )
    if structure is aligned_structure and regime is aligned_regime:
        return 1.0, f"1h structure {structure.value} and regime {regime.value} aligned"
    if structure is aligned_structure or regime is aligned_regime:
        return 0.8, f"partial alignment ({structure.value}/{regime.value})"
    if structure in (StructureState.NEUTRAL, StructureState.INSUFFICIENT_STRUCTURE):
        return 0.5, f"neutral 1h structure ({structure.value})"
    if structure is StructureState.TRANSITIONAL:
        return 0.4, "transitional 1h structure"
    return 0.0, f"1h structure {structure.value} against candidate"


def _momentum_fraction(bundle: FeatureBundle, direction: CandidateDirection) -> tuple[float, str]:
    momentum = bundle.momentum_5m
    wanted = 1 if direction is CandidateDirection.BULLISH else -1
    if momentum.direction == wanted:
        bonus = min(0.4, momentum.continuation_count * 0.1)
        return 0.6 + bonus, f"5m momentum aligned (continuation={momentum.continuation_count})"
    if momentum.direction == 0:
        return 0.4, "5m momentum neutral"
    return 0.0, "5m momentum against candidate"


def _volume_fraction(bundle: FeatureBundle, settings: StrategySettings) -> tuple[float, str]:
    volume = bundle.volume_5m
    if not volume.volume_available or volume.relative_volume is None:
        return 0.0, "volume unavailable"
    ratio = volume.relative_volume
    if ratio >= settings.min_relative_volume * 2:
        return 1.0, f"relative volume {ratio:.2f} strong"
    if ratio >= settings.min_relative_volume:
        return 0.7, f"relative volume {ratio:.2f} sufficient"
    return 0.3, f"relative volume {ratio:.2f} below {settings.min_relative_volume}"


def _market_context_fraction(
    bundle: FeatureBundle, direction: CandidateDirection
) -> tuple[float, str]:
    if not bundle.orderbook.valid:
        return 0.0, "orderbook features invalid"
    imbalance = bundle.orderbook.depth_imbalance
    if imbalance is None:
        return 0.6, "spread valid, depth imbalance unavailable"
    against = imbalance < -0.5 if direction is CandidateDirection.BULLISH else imbalance > 0.5
    if against:
        return 0.3, f"depth imbalance {imbalance:.2f} against candidate"
    return 1.0, f"book context neutral/supportive (imbalance {imbalance:.2f})"


def _volatility_fraction(bundle: FeatureBundle) -> tuple[float, str]:
    volatility = bundle.volatility_1h
    if volatility.is_high_volatility:
        return 0.0, "high volatility state"
    if volatility.atr_percentile is not None and volatility.atr_percentile >= 75:
        return 0.6, f"elevated volatility (pctl {volatility.atr_percentile:.0f})"
    return 1.0, "volatility suitable"


def _common_fractions(
    bundle: FeatureBundle,
    settings: StrategySettings,
    direction: CandidateDirection,
    regime: RegimeResult,
) -> dict[str, tuple[float, str]]:
    return {
        "htf_bias": _htf_fraction(direction, bundle.structure["1h"].state, regime.regime),
        "momentum": _momentum_fraction(bundle, direction),
        "volume": _volume_fraction(bundle, settings),
        "market_context": _market_context_fraction(bundle, direction),
        "volatility_fit": _volatility_fraction(bundle),
    }


def _structure_15m_fraction(
    bundle: FeatureBundle, direction: CandidateDirection
) -> tuple[float, str]:
    state = bundle.structure["15m"].state
    aligned = (
        StructureState.BULLISH
        if direction is CandidateDirection.BULLISH
        else StructureState.BEARISH
    )
    if state is aligned:
        return 1.0, f"15m structure {state.value}"
    if state in (StructureState.NEUTRAL, StructureState.TRANSITIONAL):
        return 0.5, f"15m structure {state.value}"
    if state is StructureState.INSUFFICIENT_STRUCTURE:
        return 0.3, "15m structure insufficient"
    return 0.0, f"15m structure {state.value} against candidate"


def sweep_reversal_rule(
    context: EvaluationContext,
    bundle: FeatureBundle,
    regime: RegimeResult,
    settings: StrategySettings,
    *,
    bullish: bool,
) -> RuleResult:
    direction = CandidateDirection.BULLISH if bullish else CandidateDirection.BEARISH
    candidate_type = (
        CandidateType.BULLISH_SWEEP_REVERSAL if bullish else CandidateType.BEARISH_SWEEP_REVERSAL
    )
    wanted = SweepDirection.BELOW if bullish else SweepDirection.ABOVE
    sweeps = [sweep for sweep in bundle.sweeps_5m if sweep.direction is wanted]
    if not sweeps:
        return None  # rule does not apply - no rejection spam
    sweep = max(sweeps, key=lambda item: (item.confidence, item.level.relevance))
    if not sweep.confirmed:
        return RuleFailure(candidate_type, RejectionCode.SWEEP_UNCONFIRMED, sweep.detail)

    series_5m = context.series["5m"]
    reclaim = detect_reclaim(series_5m, sweep, tolerance_bps=settings.reclaim_tolerance_bps)
    if reclaim is None:
        return RuleFailure(
            candidate_type,
            RejectionCode.RECLAIM_UNCONFIRMED,
            f"no close-confirmed reclaim of {sweep.level.price:.6g}",
        )
    retest = evaluate_retest(
        series_5m,
        sweep.level.price,
        reclaim.confirmed_at,
        bullish=bullish,
        tolerance_bps=settings.retest_tolerance_bps,
        max_candles=settings.retest_max_5m_candles,
    )
    if retest.status is RetestStatus.FAILED:
        return RuleFailure(
            candidate_type, RejectionCode.RECLAIM_UNCONFIRMED, "retest failed after reclaim"
        )

    fractions = _common_fractions(bundle, settings, direction, regime)
    fractions["structure_15m"] = _structure_15m_fraction(bundle, direction)
    fractions["liquidity_event"] = (
        sweep.confidence,
        f"confirmed sweep ({sweep.detail})",
    )
    local = reclaim.confidence
    if retest.status is RetestStatus.CONFIRMED:
        local = min(1.0, local + 0.2)
    fractions["local_confirmation_5m"] = (
        local,
        f"close-confirmed reclaim; retest {retest.status.value}",
    )
    level = sweep.level
    return RuleOutcome(
        candidate_type=candidate_type,
        direction=direction,
        fractions=fractions,
        evidence=[
            f"sweep of {level.level_type.value}@{level.price:.6g}",
            f"reclaim close at {reclaim.close_price:.6g}",
            f"retest {retest.status.value}",
        ],
        referenced_levels=[
            {"type": level.level_type.value, "price": level.price, "timeframe": "15m"}
        ],
        structure_events=[event.event_type.value for event in bundle.structure["15m"].events[-3:]],
        liquidity_events=[sweep.detail],
        reclaim_status="CONFIRMED",
        rejection_status="NOT_PRESENT",
        retest_status=retest.status,
        confirmed=retest.status is RetestStatus.CONFIRMED,
        invalidation_conditions=[
            f"5m close back {'below' if bullish else 'above'} swept level {level.price:.6g}",
            "15m structure turns against candidate direction",
            "data quality no longer sufficient",
            f"not confirmed within {settings.expire_after_5m_candles} closed 5m candles",
        ],
    )


def continuation_rule(
    context: EvaluationContext,
    bundle: FeatureBundle,
    regime: RegimeResult,
    settings: StrategySettings,
    *,
    bullish: bool,
) -> RuleResult:
    direction = CandidateDirection.BULLISH if bullish else CandidateDirection.BEARISH
    candidate_type = (
        CandidateType.BULLISH_RECLAIM_CONTINUATION
        if bullish
        else CandidateType.BEARISH_REJECTION_CONTINUATION
    )
    aligned_structure = StructureState.BULLISH if bullish else StructureState.BEARISH
    structure_15m = bundle.structure["15m"]
    structure_1h = bundle.structure["1h"]
    aligned_regime = StrategyRegime.TREND_UP if bullish else StrategyRegime.TREND_DOWN
    htf_ok = structure_1h.state is aligned_structure or regime.regime is aligned_regime
    if not htf_ok or structure_15m.state is not aligned_structure:
        return None  # continuation setups require aligned trend context

    if bullish:
        bos_events = [
            event
            for event in structure_15m.events
            if event.event_type is StructureEventType.BREAK_OF_STRUCTURE and "above" in event.detail
        ]
        if not bos_events:
            return RuleFailure(
                candidate_type,
                RejectionCode.STRUCTURE_NOT_CONFIRMED,
                "no confirmed 15m break of structure above",
            )
        bos = bos_events[-1]
        retest = evaluate_retest(
            context.series["5m"],
            bos.price,
            bos.confirm_close_time,
            bullish=True,
            tolerance_bps=settings.retest_tolerance_bps,
            max_candles=settings.retest_max_5m_candles,
        )
        if retest.status is RetestStatus.FAILED:
            return RuleFailure(
                candidate_type, RejectionCode.STRUCTURE_NOT_CONFIRMED, "5m retest failed"
            )
        local_confidence = 0.8 if retest.status is RetestStatus.CONFIRMED else 0.6
        local_detail = f"BOS reclaim context; retest {retest.status.value}"
        reclaim_status, rejection_status = "CONFIRMED", "NOT_PRESENT"
        level_price = bos.price
        evidence = [f"15m BOS at {bos.price:.6g}", f"retest {retest.status.value}"]
        events = [bos.event_type.value]
    else:
        # bearish continuation: rejection at the most recent 15m swing high
        highs = [swing for swing in structure_15m.swings if swing.swing_type.value == "HIGH"]
        if not highs:
            return RuleFailure(
                candidate_type,
                RejectionCode.STRUCTURE_NOT_CONFIRMED,
                "no 15m swing high available for rejection",
            )
        level_price = highs[-1].price
        rejection = detect_rejection(context.series["5m"], level_price, direction_bearish=True)
        if rejection is None:
            return RuleFailure(
                candidate_type,
                RejectionCode.REJECTION_UNCONFIRMED,
                f"no close-confirmed rejection at {level_price:.6g}",
            )
        retest = RetestResult(RetestStatus.NOT_PRESENT, level_price, "n/a for rejection")
        local_confidence = rejection.confidence
        local_detail = f"rejection confirmed ({rejection.detail})"
        reclaim_status, rejection_status = "NOT_PRESENT", "CONFIRMED"
        evidence = [f"rejection at {level_price:.6g}", rejection.detail]
        events = ["REJECTION"]

    fractions = _common_fractions(bundle, settings, direction, regime)
    fractions["structure_15m"] = _structure_15m_fraction(bundle, direction)
    fractions["liquidity_event"] = (
        0.6,
        "continuation off structural level (no fresh sweep required)",
    )
    fractions["local_confirmation_5m"] = (local_confidence, local_detail)
    return RuleOutcome(
        candidate_type=candidate_type,
        direction=direction,
        fractions=fractions,
        evidence=evidence,
        referenced_levels=[{"type": "STRUCTURE_LEVEL", "price": level_price, "timeframe": "15m"}],
        structure_events=events,
        liquidity_events=[],
        reclaim_status=reclaim_status,
        rejection_status=rejection_status,
        retest_status=retest.status,
        confirmed=retest.status is RetestStatus.CONFIRMED or not bullish,
        invalidation_conditions=[
            "15m structure turns against candidate direction",
            f"5m close back {'below' if bullish else 'above'} {level_price:.6g}",
            "data quality no longer sufficient",
            f"not confirmed within {settings.expire_after_5m_candles} closed 5m candles",
        ],
    )


def range_breakout_rule(
    context: EvaluationContext,
    bundle: FeatureBundle,
    regime: RegimeResult,
    settings: StrategySettings,
    *,
    up: bool,
) -> RuleResult:
    direction = CandidateDirection.BULLISH if up else CandidateDirection.BEARISH
    candidate_type = CandidateType.RANGE_BREAKOUT_UP if up else CandidateType.RANGE_BREAKOUT_DOWN
    if regime.regime not in (StrategyRegime.RANGE, StrategyRegime.BREAKOUT):
        return None
    wanted_type = "RANGE_HIGH" if up else "RANGE_LOW"
    range_levels = [
        level for level in bundle.liquidity_levels_15m if level.level_type.value == wanted_type
    ]
    if not range_levels:
        return None
    level = range_levels[0]
    series_5m = context.series["5m"]
    last = series_5m.last
    broke = last.close > level.price if up else last.close < level.price
    if not broke:
        # also accept a confirmed break within the recent closed candles
        recent = series_5m.candles[-settings.retest_max_5m_candles :]
        broke_at = next(
            (
                candle
                for candle in recent
                if (candle.close > level.price if up else candle.close < level.price)
            ),
            None,
        )
        if broke_at is None:
            return RuleFailure(
                candidate_type,
                RejectionCode.STRUCTURE_NOT_CONFIRMED,
                f"no confirmed close {'above' if up else 'below'} range level {level.price:.6g}",
            )
        break_time = broke_at.close_time
    else:
        break_time = last.close_time
    retest = evaluate_retest(
        series_5m,
        level.price,
        break_time,
        bullish=up,
        tolerance_bps=settings.retest_tolerance_bps,
        max_candles=settings.retest_max_5m_candles,
    )
    if retest.status is RetestStatus.FAILED:
        return RuleFailure(
            candidate_type, RejectionCode.STRUCTURE_NOT_CONFIRMED, "breakout retest failed"
        )
    fractions = _common_fractions(bundle, settings, direction, regime)
    fractions["structure_15m"] = _structure_15m_fraction(bundle, direction)
    fractions["liquidity_event"] = (
        min(0.9, 0.5 + level.relevance * 0.4),
        f"range boundary {level.detail}",
    )
    local = 0.7 if retest.status is RetestStatus.CONFIRMED else 0.55
    fractions["local_confirmation_5m"] = (
        local,
        f"close-confirmed breakout; retest {retest.status.value}",
    )
    return RuleOutcome(
        candidate_type=candidate_type,
        direction=direction,
        fractions=fractions,
        evidence=[
            f"confirmed close {'above' if up else 'below'} {level.price:.6g}",
            f"retest {retest.status.value}",
        ],
        referenced_levels=[{"type": wanted_type, "price": level.price, "timeframe": "15m"}],
        structure_events=[],
        liquidity_events=[f"range boundary at {level.price:.6g}"],
        reclaim_status="NOT_PRESENT",
        rejection_status="NOT_PRESENT",
        retest_status=retest.status,
        confirmed=retest.status is RetestStatus.CONFIRMED,
        invalidation_conditions=[
            f"5m close back inside the prior range ({level.price:.6g})",
            "15m structure turns against candidate direction",
            "data quality no longer sufficient",
            f"not confirmed within {settings.expire_after_5m_candles} closed 5m candles",
        ],
    )


def evaluate_rules(
    context: EvaluationContext,
    bundle: FeatureBundle,
    regime: RegimeResult,
    settings: StrategySettings,
) -> list[RuleResult]:
    return [
        sweep_reversal_rule(context, bundle, regime, settings, bullish=True),
        sweep_reversal_rule(context, bundle, regime, settings, bullish=False),
        continuation_rule(context, bundle, regime, settings, bullish=True),
        continuation_rule(context, bundle, regime, settings, bullish=False),
        range_breakout_rule(context, bundle, regime, settings, up=True),
        range_breakout_rule(context, bundle, regime, settings, up=False),
    ]
