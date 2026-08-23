"""Eligibility scoring and SignalEligibilityPlan assembly.

The eligibility score is a research quality measure of the plan itself - it
is NOT a win probability and can never override a hard rejection gate.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import TYPE_CHECKING

from app.config import RiskSettings
from app.costs.enums import FundingModelState
from app.costs.models import CostEstimate
from app.risk.enums import PlanStatus
from app.risk.models import (
    LeverageSuitabilityRange,
    LiquidationBufferResult,
    MarginModelResult,
    ReferenceEntryZone,
    ReferencePosition,
    ReferenceTarget,
    RiskDistance,
    ScoreComponent,
    SignalEligibilityPlan,
    TechnicalInvalidation,
)
from app.risk.version import plan_dedupe_key

if TYPE_CHECKING:
    from app.risk.models import RiskEvaluationContext


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def compute_eligibility_score(
    *,
    invalidation: TechnicalInvalidation,
    distance: RiskDistance,
    targets: tuple[ReferenceTarget, ...],
    costs: CostEstimate,
    liquidation_buffer: LiquidationBufferResult,
    margin: MarginModelResult,
    settings: RiskSettings,
) -> tuple[int, list[ScoreComponent], list[str]]:
    weights = settings.eligibility_score_weights_json
    components: list[ScoreComponent] = []
    reasons: list[str] = []

    sweet = (
        1.0 if distance.atr_multiple is not None and 1.0 <= distance.atr_multiple <= 2.5 else 0.7
    )
    fractions: dict[str, tuple[float, str]] = {
        "invalidation_quality": (
            _clamp(invalidation.confidence * sweet),
            f"structural basis {invalidation.reference_level_type.value}, "
            f"distance {distance.atr_multiple:.2f}xATR"
            if distance.atr_multiple is not None
            else "structural basis without ATR context",
        )
    }

    primary_relevance = targets[0].relevance if targets else 0.0
    fractions["target_quality"] = (
        _clamp(primary_relevance + 0.1 * max(0, len(targets) - 1)),
        f"{len(targets)} technical target level(s), primary relevance {primary_relevance:.2f}",
    )

    primary = costs.primary_target
    net_rr = primary.net_rr if primary and primary.net_rr is not None else 0.0
    fractions["net_rr"] = (
        _clamp(net_rr / settings.min_net_rr - 0.5),
        f"net R:R {net_rr:.2f} vs minimum {settings.min_net_rr:g} "
        "(hypothetical model value, no win probability)",
    )

    slip_pct = (
        (costs.entry.slippage_cost + costs.invalidation.slippage_cost)
        / costs.gross_invalidation_loss
        * 100.0
        if costs.gross_invalidation_loss > 0
        else 100.0
    )
    fractions["executability"] = (
        _clamp(1.0 - slip_pct / settings.max_cost_to_risk_pct),
        f"entry+invalidation slippage {slip_pct:.1f}% of technical risk, "
        f"{costs.entry.vwap.levels_consumed} entry level(s) consumed",
    )

    fractions["cost_quality"] = (
        _clamp(1.0 - costs.cost_to_risk_pct / settings.max_cost_to_risk_pct),
        f"total costs {costs.cost_to_risk_pct:.1f}% of technical risk "
        f"(max {settings.max_cost_to_risk_pct:g}%)",
    )

    buffer_fraction = 0.0
    if liquidation_buffer.buffer_bps is not None and settings.min_liquidation_buffer_bps > 0:
        buffer_fraction = _clamp(
            liquidation_buffer.buffer_bps / (2 * settings.min_liquidation_buffer_bps)
        )
    fractions["margin_buffer"] = (
        buffer_fraction,
        f"liquidation buffer {liquidation_buffer.buffer_bps:.0f}bps"
        if liquidation_buffer.buffer_bps is not None
        else "no buffer available",
    )

    confidence = 1.0
    if margin.approximated:
        confidence -= 0.4
        reasons.append("margin model APPROXIMATED (opt-in) - reduced data confidence")
    if costs.funding.state is not FundingModelState.MODELED:
        confidence -= 0.3
        reasons.append("funding modeled via conservative buffer only")
    confidence -= 0.1 * len(costs.warnings)
    fractions["data_confidence"] = (
        _clamp(confidence),
        "model/data confidence after approximation and warning deductions",
    )

    total = 0.0
    for component, max_points in weights.items():
        fraction, reason = fractions.get(component, (0.0, "component missing"))
        awarded = round(fraction * max_points, 2)
        total += awarded
        components.append(
            ScoreComponent(
                component=component, max_points=max_points, awarded=awarded, reason=reason
            )
        )
    return round(total), components, reasons


def build_plan(
    context: RiskEvaluationContext,
    *,
    entry_zone: ReferenceEntryZone,
    invalidation: TechnicalInvalidation,
    targets: tuple[ReferenceTarget, ...],
    distance: RiskDistance,
    position: ReferencePosition,
    leverage: LeverageSuitabilityRange,
    margin: MarginModelResult,
    liquidation_buffer: LiquidationBufferResult,
    costs: CostEstimate,
    eligibility_score: int,
    score_components: list[ScoreComponent],
    eligibility_reasons: list[str],
    warnings: list[str],
    settings: RiskSettings,
) -> SignalEligibilityPlan:
    candidate = context.candidate
    expiry = min(
        context.as_of + timedelta(seconds=settings.plan_expiry_seconds),
        candidate.expiry_at,
    )
    dedupe = plan_dedupe_key(
        candidate_id=str(candidate.candidate_id),
        risk_model_version=context.risk_model_version,
        cost_model_version=context.cost_model_version,
        risk_config_hash=context.risk_config_hash,
        cost_config_hash=context.cost_config_hash,
        fee_schedule_version=costs.fee_schedule.version,
        instrument_snapshot_version=context.instrument_snapshot_version,
        invalidation_price=invalidation.invalidation_price,
        entry_basis=entry_zone.entry_basis.value,
        first_target_price=targets[0].price,
    )
    return SignalEligibilityPlan(
        plan_id=uuid.uuid4(),
        status=PlanStatus.ELIGIBLE,
        candidate_id=candidate.candidate_id,
        candidate_type=candidate.candidate_type,
        direction=candidate.direction,
        instrument_pk=candidate.instrument_pk,
        instrument_id=candidate.instrument_id,
        symbol=candidate.symbol,
        asset_class=candidate.asset_class,
        instrument_snapshot=context.instrument,
        entry_zone=entry_zone,
        invalidation=invalidation,
        targets=targets,
        risk_distance=distance,
        position=position,
        leverage=leverage,
        margin=margin,
        liquidation_buffer=liquidation_buffer,
        costs=costs,
        eligibility_score=eligibility_score,
        score_components=tuple(score_components),
        eligibility_reasons=tuple(eligibility_reasons),
        warnings=tuple(warnings),
        dedupe_key=dedupe,
        risk_model_name=context.risk_model_name,
        risk_model_version=context.risk_model_version,
        risk_config_hash=context.risk_config_hash,
        cost_model_name=context.cost_model_name,
        cost_model_version=context.cost_model_version,
        cost_config_hash=context.cost_config_hash,
        fee_schedule_version=costs.fee_schedule.version,
        execution_assumption_version=costs.execution_assumptions.version,
        instrument_snapshot_version=context.instrument_snapshot_version,
        strategy_name=candidate.strategy_name,
        strategy_version=candidate.strategy_version,
        strategy_config_hash=candidate.strategy_config_hash,
        as_of=context.as_of,
        expiry_at=expiry,
        data_timestamps={
            "bbo_at": context.bbo_at.isoformat() if context.bbo_at else "n/a",
            "book_at": (
                context.book.snapshot_at.isoformat() if context.book.snapshot_at else "n/a"
            ),
            "instrument_snapshot_at": context.instrument.as_of.isoformat(),
            "candidate_as_of": candidate.as_of.isoformat(),
        },
    )
