"""Pure strategy evaluation over an immutable EvaluationContext.

``evaluate_context`` performs: hard gates -> feature pipeline -> regime ->
candidate rules -> scoring -> SetupCandidate(s) / SetupRejection(s).  It has
no I/O, no clock access beyond the provided as_of, and is fully
deterministic for a given context + settings.
"""

from __future__ import annotations

import uuid

from app.config import StrategySettings
from app.strategy.enums import RejectionCode, StrategyRegime
from app.strategy.feature_pipeline import build_feature_bundle
from app.strategy.models import (
    EvaluationContext,
    EvaluationOutcome,
    SetupCandidate,
    SetupRejection,
)
from app.strategy.regime import classify_regime
from app.strategy.scoring import compute_score
from app.strategy.setup_candidate import build_candidate
from app.strategy.setup_rejection import build_rejection
from app.strategy.setup_rules import RuleFailure, RuleOutcome, evaluate_rules

_REGIME_BLOCKS = {
    StrategyRegime.NO_TRADE: RejectionCode.REGIME_NO_TRADE,
    StrategyRegime.HIGH_VOLATILITY: RejectionCode.REGIME_HIGH_VOLATILITY,
    StrategyRegime.LOW_LIQUIDITY: RejectionCode.REGIME_LOW_LIQUIDITY,
    StrategyRegime.INSUFFICIENT_DATA: RejectionCode.REGIME_INSUFFICIENT_DATA,
    StrategyRegime.EVENT_RISK: RejectionCode.REGIME_NO_TRADE,
}


def evaluate_context(
    context: EvaluationContext,
    settings: StrategySettings,
    *,
    feature_snapshot_id: uuid.UUID | None = None,
) -> EvaluationOutcome:
    rejections: list[SetupRejection] = []
    candidates: list[SetupCandidate] = []

    def rejected(code: RejectionCode, detail: str) -> EvaluationOutcome:
        rejections.append(build_rejection(context, code, detail))
        return EvaluationOutcome(
            context_symbol=context.symbol,
            as_of=context.as_of,
            regime=None,
            candidates=(),
            rejections=tuple(rejections),
        )

    # ---------------------------------------------------------- hard gates
    if settings.require_active_watchlist and not context.watchlist_active:
        return rejected(RejectionCode.NOT_ON_ACTIVE_WATCHLIST, "instrument not on active watchlist")
    if context.bot_paused:
        return rejected(RejectionCode.BOT_PAUSED, "global paused mode - no new setup evaluations")
    if not context.session_allowed:
        return rejected(
            RejectionCode.SESSION_NOT_ALLOWED, f"session {context.session_state} not allowed"
        )
    if not context.data_quality_ok:
        return rejected(
            RejectionCode.DATA_QUALITY_NOT_SUFFICIENT,
            f"data quality {context.data_quality_status}",
        )
    if settings.require_fresh_orderbook and not context.orderbook_ok:
        return rejected(RejectionCode.ORDERBOOK_NOT_FRESH, "orderbook not fresh/reliable")
    for timeframe in settings.required_timeframes:
        if timeframe == "1m" and settings.allow_1m_optional:
            continue
        if timeframe not in context.series:
            return rejected(
                RejectionCode.INSUFFICIENT_CANDLE_HISTORY,
                f"required timeframe {timeframe} missing",
            )

    # ------------------------------------------------- features and regime
    bundle = build_feature_bundle(context, settings)
    regime = classify_regime(
        context.series["1h"],
        bundle.structure["1h"],
        bundle.volatility_1h,
        settings,
        as_of=context.as_of,
        low_liquidity=context.low_liquidity,
        data_degraded=not context.data_quality_ok,
    )

    base = EvaluationOutcome(
        context_symbol=context.symbol,
        as_of=context.as_of,
        regime=regime,
        candidates=(),
        rejections=(),
        feature_values=bundle.feature_values,
        feature_validity=bundle.feature_validity,
        warnings=bundle.warnings,
        candle_window_metadata={
            timeframe: series.window_metadata() for timeframe, series in context.series.items()
        },
        structure_by_timeframe=bundle.structure,
    )

    block = _REGIME_BLOCKS.get(regime.regime)
    if block is not None:
        rejections.append(
            build_rejection(context, block, f"regime {regime.regime.value}: {regime.reasons[-1]}")
        )
        return _with(base, candidates, rejections)

    # ------------------------------------------------------ candidate rules
    for result in evaluate_rules(context, bundle, regime, settings):
        if result is None:
            continue
        if isinstance(result, RuleFailure):
            rejections.append(
                build_rejection(
                    context, result.code, result.detail, candidate_type=result.candidate_type
                )
            )
            continue
        outcome: RuleOutcome = result
        # mandatory confirmations (configuration-driven hard gates)
        momentum_fraction = outcome.fractions.get("momentum", (0.0, ""))[0]
        if settings.require_momentum_confirmation and momentum_fraction < 0.6:
            rejections.append(
                build_rejection(
                    context,
                    RejectionCode.MOMENTUM_INSUFFICIENT,
                    f"momentum not aligned for {outcome.candidate_type.value}",
                    candidate_type=outcome.candidate_type,
                )
            )
            continue
        volume_fraction = outcome.fractions.get("volume", (0.0, ""))[0]
        if settings.require_volume_confirmation and volume_fraction < 0.7:
            rejections.append(
                build_rejection(
                    context,
                    RejectionCode.VOLUME_INSUFFICIENT,
                    f"volume confirmation missing for {outcome.candidate_type.value}",
                    candidate_type=outcome.candidate_type,
                )
            )
            continue
        # both HTF and 15m structure hard-conflicting kills the candidate
        if (
            outcome.fractions.get("htf_bias", (0.0, ""))[0] == 0.0
            and outcome.fractions.get("structure_15m", (0.0, ""))[0] == 0.0
        ):
            rejections.append(
                build_rejection(
                    context,
                    RejectionCode.HIGHER_TIMEFRAME_CONFLICT,
                    f"structure conflicts on 1h and 15m for {outcome.candidate_type.value}",
                    candidate_type=outcome.candidate_type,
                )
            )
            continue
        score, components = compute_score(outcome.fractions, settings)
        if score < settings.min_setup_score:
            rejections.append(
                build_rejection(
                    context,
                    RejectionCode.SETUP_SCORE_BELOW_THRESHOLD,
                    f"{outcome.candidate_type.value}: score {score} < {settings.min_setup_score}",
                    candidate_type=outcome.candidate_type,
                )
            )
            continue
        candidates.append(
            build_candidate(
                context,
                bundle,
                regime,
                outcome,
                score,
                components,
                settings,
                feature_snapshot_id,
            )
        )

    # keep only the best candidate per direction (deterministic ordering)
    best: dict[str, SetupCandidate] = {}
    for candidate in sorted(
        candidates, key=lambda item: (-item.setup_score, item.candidate_type.value)
    ):
        best.setdefault(candidate.direction.value, candidate)
    return _with(base, list(best.values()), rejections)


def _with(
    base: EvaluationOutcome,
    candidates: list[SetupCandidate],
    rejections: list[SetupRejection],
) -> EvaluationOutcome:
    return EvaluationOutcome(
        context_symbol=base.context_symbol,
        as_of=base.as_of,
        regime=base.regime,
        candidates=tuple(candidates),
        rejections=tuple(rejections),
        feature_values=base.feature_values,
        feature_validity=base.feature_validity,
        warnings=base.warnings,
        candle_window_metadata=base.candle_window_metadata,
        structure_by_timeframe=base.structure_by_timeframe,
    )
