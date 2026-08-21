"""SetupCandidate construction (dedupe key, invalidation conditions).

Candidates are internal research hypotheses.  They carry no trade
execution parameters of any kind - only structure classifications and
technical invalidation conditions.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import timedelta

from app.config import StrategySettings
from app.strategy.enums import CandidateState
from app.strategy.explainability import summarize_candidate
from app.strategy.feature_pipeline import FeatureBundle
from app.strategy.models import (
    EvaluationContext,
    RegimeResult,
    ScoreBreakdown,
    SetupCandidate,
)
from app.strategy.setup_rules import RuleOutcome


def dedupe_key_for(context: EvaluationContext, outcome: RuleOutcome, level_price: float) -> str:
    raw = "|".join(
        [
            str(context.instrument_id),
            outcome.candidate_type.value,
            outcome.direction.value,
            f"{level_price:.6g}",
            "15m",
            context.strategy_version,
        ]
    )
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def build_candidate(
    context: EvaluationContext,
    bundle: FeatureBundle,
    regime: RegimeResult,
    outcome: RuleOutcome,
    score: int,
    components: list[ScoreBreakdown],
    settings: StrategySettings,
    feature_snapshot_id: uuid.UUID | None,
) -> SetupCandidate:
    level_price = float(outcome.referenced_levels[0]["price"]) if outcome.referenced_levels else 0.0
    expiry_at = context.as_of + timedelta(minutes=5 * settings.expire_after_5m_candles)
    confidence = round(0.6 * score + 0.4 * regime.confidence)
    candle_meta = {
        timeframe: series.window_metadata() for timeframe, series in context.series.items()
    }
    return SetupCandidate(
        candidate_id=uuid.uuid4(),
        candidate_type=outcome.candidate_type,
        direction=outcome.direction,
        state=CandidateState.CONFIRMED if outcome.confirmed else CandidateState.DETECTED,
        instrument_pk=context.instrument_pk,
        instrument_id=context.instrument_id,
        symbol=context.symbol,
        asset_class=context.asset_class,
        as_of=context.as_of,
        expiry_at=expiry_at,
        strategy_name=context.strategy_name,
        strategy_version=context.strategy_version,
        feature_schema_version=context.feature_schema_version,
        config_hash=context.config_hash,
        ruleset_hash=context.ruleset_hash,
        session_state=context.session_state,
        market_quality_score=context.market_quality_score,
        data_quality_status=context.data_quality_status,
        primary_regime=regime.regime,
        regime_confidence=regime.confidence,
        higher_timeframe_structure=bundle.structure["1h"].state,
        local_structure=bundle.structure["5m"].state,
        referenced_levels=tuple(outcome.referenced_levels),
        structure_events=tuple(outcome.structure_events),
        liquidity_events=tuple(outcome.liquidity_events),
        reclaim_status=outcome.reclaim_status,
        rejection_status=outcome.rejection_status,
        retest_status=outcome.retest_status,
        setup_score=score,
        score_components=tuple(components),
        confidence=min(95, confidence),
        reason_summary=summarize_candidate(
            outcome.candidate_type.value,
            outcome.direction.value,
            regime.regime.value,
            bundle.structure["1h"].state.value,
            outcome.evidence,
            score,
        ),
        reason_codes=tuple(item.component for item in components if item.awarded > 0),
        invalidation_conditions=tuple(outcome.invalidation_conditions),
        dedupe_key=dedupe_key_for(context, outcome, level_price),
        feature_snapshot_id=feature_snapshot_id,
        candle_window_metadata=candle_meta,
        features={
            "momentum_direction": bundle.momentum_5m.direction,
            "relative_volume": bundle.volume_5m.relative_volume,
            "atr_percentile_1h": bundle.volatility_1h.atr_percentile,
            "spread_bps": bundle.orderbook.spread_bps,
        },
    )
