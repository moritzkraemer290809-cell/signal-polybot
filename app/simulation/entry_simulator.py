"""Modelled entry of a hypothetical delayed follower.

The modelled entry happens at the DELAYED reference instant, never at the
lifecycle event price: a follower who reacts late trades a different book.
Missing, stale or too thin public data rejects the simulation instead of
inventing an idealised price.  Nothing here is a fill or an execution.
"""

from __future__ import annotations

from app.config import CostSettings, ShadowSimulationSettings
from app.costs.enums import ExecutionMode
from app.costs.models import FeeScheduleSnapshot
from app.simulation.enums import SimulationLegKind, SimulationRejectionCode
from app.simulation.models import (
    DelayRealization,
    MarketReferenceSnapshot,
    ModelledExecution,
    SimulationPlanReference,
)
from app.simulation.simulated_execution import ExecutionModelError, model_execution


def model_entry(
    *,
    plan: SimulationPlanReference,
    delay: DelayRealization,
    market: MarketReferenceSnapshot | None,
    schedule: FeeScheduleSnapshot | None,
    cost_settings: CostSettings,
    shadow_settings: ShadowSimulationSettings,
) -> ModelledExecution:
    """Model the delayed entry leg or raise :class:`ExecutionModelError`."""
    if schedule is None:
        raise ExecutionModelError(
            SimulationRejectionCode.FEE_SCHEDULE_UNAVAILABLE,
            "no administered fee schedule for the modelled entry instant",
        )
    if market is None:
        raise ExecutionModelError(
            SimulationRejectionCode.ENTRY_DELAY_DATA_UNAVAILABLE,
            f"no public market reference at the delayed instant "
            f"{delay.scheduled_entry_at.isoformat()}",
        )
    if not market.data_quality_ok:
        raise ExecutionModelError(
            SimulationRejectionCode.ENTRY_BOOK_STALE,
            f"data quality {market.data_quality_status} at the delayed entry instant",
        )

    execution = model_execution(
        leg=SimulationLegKind.MODELLED_ENTRY,
        entering=True,
        bullish=plan.bullish,
        quantity=plan.reference_quantity,
        market=market,
        schedule=schedule,
        mode=ExecutionMode(cost_settings.entry_execution_mode),
        cost_settings=cost_settings,
        shadow_settings=shadow_settings,
        max_staleness_seconds=shadow_settings.delay_max_entry_staleness_seconds,
        stale_code=SimulationRejectionCode.ENTRY_BOOK_STALE,
        depth_code=SimulationRejectionCode.ENTRY_BOOK_INSUFFICIENT_DEPTH,
    )

    # a delayed follower may never assume an unlimited adverse entry: the
    # modelled slippage is capped against the plan's technical risk
    risk_per_unit = plan.risk_per_unit
    if risk_per_unit > 0:
        adverse_move = abs(execution.modelled_price - plan.entry_reference_price)
        slippage_to_risk_pct = adverse_move / risk_per_unit * 100.0
        limit = cost_settings.max_slippage_to_risk_pct
        worse_than_reference = (
            execution.modelled_price > plan.entry_reference_price
            if plan.bullish
            else execution.modelled_price < plan.entry_reference_price
        )
        if worse_than_reference and slippage_to_risk_pct > limit:
            raise ExecutionModelError(
                SimulationRejectionCode.ENTRY_SLIPPAGE_EXCESSIVE,
                f"modelled delayed entry {execution.modelled_price:.6g} deviates "
                f"{slippage_to_risk_pct:.1f}% of the technical risk distance from the "
                f"reference {plan.entry_reference_price:.6g} (limit {limit:.1f}%)",
            )
    return execution
