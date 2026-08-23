"""Cost engine orchestration: one conservative CostEstimate per plan.

Pure computation over immutable snapshots - no I/O, no network, no account
data.  Raises :class:`CostModelError` (with a rejection-code name) whenever a
conservative estimate is impossible; it never falls back to optimistic
assumptions.
"""

from __future__ import annotations

from datetime import datetime

from app.config import CostSettings
from app.costs.execution_assumptions import build_execution_assumptions
from app.costs.funding_projection import project_funding
from app.costs.models import (
    CostEstimate,
    CostModelError,
    ExecutionAssumptions,
    FeeScheduleSnapshot,
    FundingSnapshot,
    OrderbookDepthSnapshot,
    TargetCostBreakdown,
)
from app.costs.net_expectancy import invalidation_economics, target_economics
from app.costs.slippage import estimate_leg
from app.costs.version import cost_configuration_hash


def estimate_costs(
    *,
    bullish: bool,
    entry_reference_price: float,
    invalidation_price: float,
    target_prices: list[float],
    quantity: float,
    book: OrderbookDepthSnapshot,
    funding: FundingSnapshot,
    fee_schedule: FeeScheduleSnapshot,
    settings: CostSettings,
    as_of: datetime,
    hold_minutes: int | None = None,
    assumptions: ExecutionAssumptions | None = None,
) -> CostEstimate:
    """Full conservative cost picture for one hypothetical reference plan."""
    if not settings.engine_enabled:
        raise CostModelError("CONFIGURATION_INVALID", "cost engine disabled")
    if quantity <= 0 or entry_reference_price <= 0 or invalidation_price <= 0:
        raise CostModelError("CONFIGURATION_INVALID", "non-positive cost engine inputs")
    if not target_prices:
        raise CostModelError("REFERENCE_TARGET_UNAVAILABLE", "no target prices supplied")

    assumptions = assumptions or build_execution_assumptions(settings)
    hold = hold_minutes if hold_minutes is not None else settings.reference_hold_minutes
    warnings: list[str] = []

    entry = estimate_leg(
        leg="entry",
        mode=assumptions.entry,
        bullish=bullish,
        reference_price=entry_reference_price,
        quantity=quantity,
        book=book,
        schedule=fee_schedule,
        settings=settings,
    )
    invalidation = estimate_leg(
        leg="invalidation",
        mode=assumptions.invalidation,
        bullish=bullish,
        reference_price=invalidation_price,
        quantity=quantity,
        book=book,
        schedule=fee_schedule,
        settings=settings,
    )
    funding_projection = project_funding(
        funding,
        bullish=bullish,
        hold_minutes=hold,
        notional=entry.notional,
        as_of=as_of,
        settings=settings,
    )
    if funding_projection.buffer_cost > 0:
        warnings.append("funding modeled with conservative thin-data buffer")

    cost_buffer = entry.notional * settings.conservative_cost_buffer_bps / 10_000
    gross_loss, net_loss = invalidation_economics(
        bullish=bullish,
        entry=entry,
        invalidation=invalidation,
        quantity=quantity,
        funding=funding_projection,
        cost_buffer=cost_buffer,
    )
    if gross_loss <= 0:
        raise CostModelError(
            "INVALIDATION_WRONG_SIDE",
            "invalidation is not on the adverse side of the reference entry",
        )

    targets: list[TargetCostBreakdown] = []
    for index, target_price in enumerate(target_prices):
        target_leg = estimate_leg(
            leg="target",
            mode=assumptions.target,
            bullish=bullish,
            reference_price=target_price,
            quantity=quantity,
            book=book,
            schedule=fee_schedule,
            settings=settings,
        )
        targets.append(
            target_economics(
                bullish=bullish,
                target_index=index,
                target_price=target_price,
                target_leg=target_leg,
                entry=entry,
                quantity=quantity,
                funding=funding_projection,
                cost_buffer=cost_buffer,
                gross_invalidation_loss=gross_loss,
                net_invalidation_loss=net_loss,
            )
        )

    frictions = (
        entry.fee_cost
        + invalidation.fee_cost
        + entry.slippage_cost
        + invalidation.slippage_cost
        + funding_projection.expected_cost
        + cost_buffer
    )
    cost_to_risk_pct = frictions / gross_loss * 100.0

    slippage_total = entry.slippage_cost + invalidation.slippage_cost
    if gross_loss > 0 and slippage_total / gross_loss * 100.0 > settings.max_slippage_to_risk_pct:
        raise CostModelError(
            "SLIPPAGE_EXCESSIVE",
            f"entry+invalidation slippage consumes "
            f"{slippage_total / gross_loss * 100.0:.1f}% of the technical risk "
            f"(max {settings.max_slippage_to_risk_pct:.1f}%)",
        )

    return CostEstimate(
        cost_model_name=settings.model_name,
        cost_model_version=settings.model_version,
        cost_config_hash=cost_configuration_hash(settings),
        fee_schedule=fee_schedule,
        execution_assumptions=assumptions,
        entry=entry,
        invalidation=invalidation,
        targets=tuple(targets),
        funding=funding_projection,
        gross_invalidation_loss=gross_loss,
        net_invalidation_loss=net_loss,
        total_entry_cost=entry.fee_cost + entry.slippage_cost,
        total_invalidation_path_cost=frictions,
        cost_buffer=cost_buffer,
        cost_to_risk_pct=cost_to_risk_pct,
        warnings=tuple(warnings),
        detail={
            "hold_minutes": hold,
            "spread_bps": book.spread_bps,
            "funding_basis": funding_projection.basis,
        },
    )
