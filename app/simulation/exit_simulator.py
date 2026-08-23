"""Modelled exit of a hypothetical simulation.

The exit INSTANT comes from phase-10 lifecycle events - this module never
invents its own exit rules.  Only the modelled execution price is derived
here, via a conservative book walk on the opposite (adverse) side.  When
the public book cannot carry the modelled quantity the simulation is
rejected or flagged UNMODELED per policy; it is never completed with an
idealised mid price.
"""

from __future__ import annotations

from app.config import CostSettings, ShadowSimulationSettings
from app.costs.enums import ExecutionMode
from app.costs.models import FeeScheduleSnapshot
from app.simulation.enums import (
    SimulationExitReason,
    SimulationLegKind,
    SimulationRejectionCode,
)
from app.simulation.models import (
    MarketReferenceSnapshot,
    ModelledExecution,
    SimulationPlanReference,
)
from app.simulation.simulated_execution import ExecutionModelError, model_execution

#: exit reasons whose execution assumption carries the stop stress buffer
_STRESSED_REASONS = frozenset(
    {
        SimulationExitReason.INVALIDATION_CONDITION,
        SimulationExitReason.TECHNICAL_EXIT_CONDITION,
        SimulationExitReason.DATA_INVALID,
    }
)


def execution_mode_for(reason: SimulationExitReason, cost_settings: CostSettings) -> ExecutionMode:
    """Conservative execution assumption for one modelled exit reason."""
    if reason in _STRESSED_REASONS:
        return ExecutionMode(cost_settings.invalidation_execution_mode)
    if reason in (
        SimulationExitReason.TARGET_1_OBSERVED,
        SimulationExitReason.TARGET_2_OBSERVED,
    ):
        return ExecutionMode(cost_settings.target_execution_mode)
    # expiry/supersede/session ends: neutral technical close-out assumption
    return ExecutionMode(cost_settings.target_execution_mode)


def model_exit(
    *,
    plan: SimulationPlanReference,
    reason: SimulationExitReason,
    market: MarketReferenceSnapshot | None,
    schedule: FeeScheduleSnapshot | None,
    quantity: float,
    cost_settings: CostSettings,
    shadow_settings: ShadowSimulationSettings,
) -> ModelledExecution:
    """Model the exit leg or raise :class:`ExecutionModelError`."""
    if schedule is None:
        raise ExecutionModelError(
            SimulationRejectionCode.FEE_SCHEDULE_UNAVAILABLE,
            "no administered fee schedule for the modelled exit instant",
        )
    if market is None:
        raise ExecutionModelError(
            SimulationRejectionCode.EXIT_BOOK_STALE,
            f"no public market reference at the modelled exit instant ({reason.value})",
        )
    if not market.data_quality_ok:
        raise ExecutionModelError(
            SimulationRejectionCode.EXIT_BOOK_STALE,
            f"data quality {market.data_quality_status} at the modelled exit instant",
        )
    return model_execution(
        leg=SimulationLegKind.MODELLED_EXIT,
        entering=False,
        bullish=plan.bullish,
        quantity=quantity,
        market=market,
        schedule=schedule,
        mode=execution_mode_for(reason, cost_settings),
        cost_settings=cost_settings,
        shadow_settings=shadow_settings,
        max_staleness_seconds=shadow_settings.exit_max_book_staleness_seconds,
        stale_code=SimulationRejectionCode.EXIT_BOOK_STALE,
        depth_code=SimulationRejectionCode.EXIT_BOOK_INSUFFICIENT_DEPTH,
    )


def model_partial_reduction(
    *,
    plan: SimulationPlanReference,
    market: MarketReferenceSnapshot | None,
    schedule: FeeScheduleSnapshot | None,
    cost_settings: CostSettings,
    shadow_settings: ShadowSimulationSettings,
) -> ModelledExecution | None:
    """Optional modelled partial reduction at reference target 1.

    Disabled by default (V1).  When enabled the reduced share is a pure
    simulation assumption and stays marked as such.
    """
    if not shadow_settings.simulation_allow_partial_target_simulation:
        return None
    share = shadow_settings.simulation_partial_target_pct / 100.0
    quantity = plan.reference_quantity * share
    if quantity <= 0:
        return None
    if schedule is None or market is None:
        raise ExecutionModelError(
            SimulationRejectionCode.EXIT_BOOK_STALE,
            "no public market reference for the modelled partial reduction",
        )
    execution = model_execution(
        leg=SimulationLegKind.MODELLED_PARTIAL_REDUCTION,
        entering=False,
        bullish=plan.bullish,
        quantity=quantity,
        market=market,
        schedule=schedule,
        mode=ExecutionMode(cost_settings.target_execution_mode),
        cost_settings=cost_settings,
        shadow_settings=shadow_settings,
        max_staleness_seconds=shadow_settings.exit_max_book_staleness_seconds,
        stale_code=SimulationRejectionCode.EXIT_BOOK_STALE,
        depth_code=SimulationRejectionCode.EXIT_BOOK_INSUFFICIENT_DEPTH,
    )
    return execution
