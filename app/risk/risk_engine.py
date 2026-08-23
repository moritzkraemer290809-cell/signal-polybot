"""Risk engine core: one candidate in, one plan or one rejection out.

Pure, deterministic computation over an immutable RiskEvaluationContext -
no I/O, no clock access beyond the provided as_of, no account data.  Hard
gates always run before any modelling; a good score can never rescue a
failed gate, and missing data always blocks instead of being estimated.
"""

from __future__ import annotations

from typing import Any

from app.config import CostSettings, RiskSettings
from app.costs.cost_engine import estimate_costs
from app.costs.models import CostModelError
from app.risk.enums import PlanRejectionCode
from app.risk.leverage_suitability import determine_leverage_suitability
from app.risk.models import (
    RiskEngineError,
    RiskEvaluationContext,
    RiskEvaluationOutcome,
)
from app.risk.plan_builder import build_plan, compute_eligibility_score
from app.risk.plan_rejection import build_rejection
from app.risk.reference_entry import derive_entry_zone
from app.risk.reference_position_sizing import size_reference_position
from app.risk.reference_targets import derive_targets
from app.risk.risk_distance import compute_risk_distance, validate_risk_distance
from app.risk.risk_validation import validate_cost_estimate
from app.risk.technical_invalidation import derive_invalidation

#: settings keys a symbol/asset-class override may adjust - hard data,
#: session and instrument safety gates can never be bypassed this way
_OVERRIDABLE_KEYS = {
    "min_net_rr",
    "max_cost_to_risk_pct",
    "min_distance_bps",
    "max_distance_bps",
    "reference_risk_per_plan_pct",
    "max_reference_leverage",
    "eligibility_min_score",
}


def effective_risk_settings(settings: RiskSettings, symbol: str, asset_class: str) -> RiskSettings:
    """Apply whitelisted per-symbol/per-asset-class numeric overrides."""
    updates: dict[str, Any] = {}
    for source in (
        settings.asset_class_overrides_json.get(asset_class, {}),
        settings.symbol_overrides_json.get(symbol, {}),
    ):
        for key, value in source.items():
            if key in _OVERRIDABLE_KEYS:
                updates[key] = value
    if not updates:
        return settings
    # the V1 hard leverage cap survives every override
    if "max_reference_leverage" in updates:
        updates["max_reference_leverage"] = min(3.0, float(updates["max_reference_leverage"]))
    return settings.model_copy(update=updates)


def _map_cost_error(error: CostModelError) -> PlanRejectionCode:
    try:
        return PlanRejectionCode(error.code)
    except ValueError:
        return PlanRejectionCode.EVALUATION_ERROR


def evaluate_candidate(
    context: RiskEvaluationContext,
    risk_settings: RiskSettings,
    cost_settings: CostSettings,
) -> RiskEvaluationOutcome:
    candidate = context.candidate

    def rejected(code: PlanRejectionCode, detail: str) -> RiskEvaluationOutcome:
        return RiskEvaluationOutcome(
            candidate_id=candidate.candidate_id,
            symbol=candidate.symbol,
            as_of=context.as_of,
            plan=None,
            rejection=build_rejection(context, code, detail),
        )

    settings = effective_risk_settings(risk_settings, candidate.symbol, candidate.asset_class)
    warnings: list[str] = []

    # ------------------------------------------------------------ hard gates
    if context.bot_paused:
        return rejected(
            PlanRejectionCode.BOT_PAUSED, "global paused mode - no new eligibility plans"
        )
    if not context.watchlist_active:
        return rejected(
            PlanRejectionCode.INSTRUMENT_INACTIVE,
            "instrument is no longer on the active watchlist",
        )
    if not context.session_allowed:
        return rejected(
            PlanRejectionCode.SESSION_NOT_ALLOWED,
            f"session {context.session_state} not allowed (or unknown) for new plans",
        )
    if candidate.state == "EXPIRED" or candidate.expiry_at <= context.as_of:
        return rejected(PlanRejectionCode.CANDIDATE_EXPIRED, "research candidate expired")
    if candidate.state == "SUPERSEDED":
        return rejected(PlanRejectionCode.CANDIDATE_SUPERSEDED, "research candidate superseded")
    if settings.require_confirmed_candidate and candidate.state != "CONFIRMED":
        return rejected(
            PlanRejectionCode.CANDIDATE_NOT_CONFIRMED,
            f"candidate state {candidate.state} - only CONFIRMED candidates are evaluated",
        )
    if context.instrument.status != "ACTIVE":
        return rejected(
            PlanRejectionCode.INSTRUMENT_INACTIVE,
            f"instrument status {context.instrument.status}",
        )
    if not context.instrument.data_quality_ok:
        return rejected(
            PlanRejectionCode.DATA_QUALITY_INSUFFICIENT,
            f"data quality {context.instrument.data_quality_status}",
        )
    if settings.require_fresh_orderbook and not (
        context.instrument.orderbook_fresh and context.book.fresh
    ):
        return rejected(PlanRejectionCode.ORDERBOOK_NOT_FRESH, "order book not fresh/reliable")
    if context.instrument.reference_price is None:
        return rejected(
            PlanRejectionCode.INSTRUMENT_RISK_DATA_MISSING,
            "no mark/mid price available for the risk reference",
        )
    if context.instrument.min_notional is None:
        return rejected(
            PlanRejectionCode.INSTRUMENT_RISK_DATA_MISSING, "instrument min_notional missing"
        )
    if context.instrument.max_leverage is None or context.instrument.max_leverage < 1:
        return rejected(
            PlanRejectionCode.MAX_LEVERAGE_UNAVAILABLE, "instrument max leverage missing"
        )
    if context.fee_schedule is None or not context.fee_schedule.active:
        return rejected(
            PlanRejectionCode.FEE_SCHEDULE_UNAVAILABLE, "no active fee schedule available"
        )
    if (
        candidate.asset_class == "EQUITY"
        and cost_settings.equity_overnight_blocked
        and context.equity_overnight_risk
    ):
        return rejected(
            PlanRejectionCode.EQUITY_OVERNIGHT_BLOCKED,
            "equity research hold would cross the session close (overnight blocked)",
        )

    # ----------------------------------------------------- technical framing
    try:
        entry_zone = derive_entry_zone(context, settings)
        invalidation = derive_invalidation(context, entry_zone.entry_reference_price, settings)
        distance = compute_risk_distance(
            entry_zone.entry_reference_price,
            invalidation.invalidation_price,
            context.atr_5m,
        )
        validate_risk_distance(distance, settings)
        targets = derive_targets(context, entry_zone.entry_reference_price, settings)
        position = size_reference_position(
            context.instrument, entry_zone.entry_reference_price, distance, settings
        )
        atr_percentile = _float_or_none(candidate.features.get("atr_percentile_1h"))
        funding_thin = context.funding.current_rate is None or len(context.funding.history) < 3
        leverage, margin, liquidation_buffer = determine_leverage_suitability(
            context.instrument,
            entry_reference_price=entry_zone.entry_reference_price,
            invalidation_price=invalidation.invalidation_price,
            notional=position.reference_notional,
            bullish=candidate.bullish,
            atr=context.atr_5m,
            atr_percentile=atr_percentile,
            spread_bps=context.book.spread_bps,
            funding_thin=funding_thin,
            settings=settings,
        )
    except RiskEngineError as error:
        return rejected(error.code, error.detail)

    # ------------------------------------------------------------ cost model
    try:
        costs = estimate_costs(
            bullish=candidate.bullish,
            entry_reference_price=entry_zone.entry_reference_price,
            invalidation_price=invalidation.invalidation_price,
            target_prices=[target.price for target in targets],
            quantity=position.reference_quantity,
            book=context.book,
            funding=context.funding,
            fee_schedule=context.fee_schedule,
            settings=cost_settings,
            as_of=context.as_of,
            assumptions=context.execution_assumptions,
        )
    except CostModelError as error:
        return rejected(_map_cost_error(error), error.detail)

    try:
        validate_cost_estimate(costs, settings)
    except RiskEngineError as error:
        return rejected(error.code, error.detail)
    warnings.extend(costs.warnings)
    if margin.approximated:
        warnings.append("margin model APPROXIMATED via explicit opt-in - no liquidation guarantee")

    # ------------------------------------------------------------- scoring
    score, components, score_reasons = compute_eligibility_score(
        invalidation=invalidation,
        distance=distance,
        targets=targets,
        costs=costs,
        liquidation_buffer=liquidation_buffer,
        margin=margin,
        settings=settings,
    )
    if score < settings.eligibility_min_score:
        return rejected(
            PlanRejectionCode.ELIGIBILITY_SCORE_BELOW_THRESHOLD,
            f"eligibility score {score} < {settings.eligibility_min_score} "
            "(research quality measure, not a win probability)",
        )

    plan = build_plan(
        context,
        entry_zone=entry_zone,
        invalidation=invalidation,
        targets=targets,
        distance=distance,
        position=position,
        leverage=leverage,
        margin=margin,
        liquidation_buffer=liquidation_buffer,
        costs=costs,
        eligibility_score=score,
        score_components=components,
        eligibility_reasons=[
            f"net R:R {costs.primary_target.net_rr:.2f} over minimum {settings.min_net_rr:g}"
            if costs.primary_target and costs.primary_target.net_rr is not None
            else "net R:R available",
            f"costs {costs.cost_to_risk_pct:.1f}% of technical risk",
            *score_reasons,
        ],
        warnings=warnings,
        settings=settings,
    )
    return RiskEvaluationOutcome(
        candidate_id=candidate.candidate_id,
        symbol=candidate.symbol,
        as_of=context.as_of,
        plan=plan,
        rejection=None,
        warnings=tuple(warnings),
    )


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
