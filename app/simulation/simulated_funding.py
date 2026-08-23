"""Modelled funding cost over a hypothetical holding window.

Reuses the phase-9 funding projection (adverse percentile, favourable
rates floored at zero) but applies it to the ACTUAL modelled holding
duration instead of the plan's hold assumption.  Missing funding data is
never silently treated as zero: per policy the simulation is rejected or
marked incomplete with an explicit warning.
"""

from __future__ import annotations

import math
from datetime import datetime

from app.config import CostSettings, ShadowSimulationSettings
from app.costs.funding_projection import project_funding
from app.costs.models import CostModelError, FundingSnapshot
from app.simulation.enums import SimulationRejectionCode
from app.simulation.models import ModelledFunding, SimulationPlanReference
from app.simulation.simulated_execution import ExecutionModelError


def model_funding(
    *,
    plan: SimulationPlanReference,
    funding: FundingSnapshot | None,
    notional: float,
    hold_seconds: float,
    entry_at: datetime,
    cost_settings: CostSettings,
    shadow_settings: ShadowSimulationSettings,
) -> ModelledFunding:
    """Model funding over the hypothetical holding window."""
    hold_minutes = max(1, math.ceil(max(0.0, hold_seconds) / 60.0))
    if funding is None:
        if shadow_settings.simulation_require_funding_data:
            raise ExecutionModelError(
                SimulationRejectionCode.FUNDING_UNAVAILABLE,
                "no public funding context for the modelled holding window and "
                "policy requires funding data",
            )
        return ModelledFunding(
            funding_cost=0.0,
            intervals_modelled=0,
            hours_modelled=hold_minutes / 60.0,
            rate_basis="unavailable",
            data_available=False,
            detail=(
                "no funding data for the modelled holding window - result marked "
                "incomplete rather than assuming zero funding"
            ),
            warnings=("FUNDING_DATA_UNAVAILABLE",),
        )
    try:
        projection = project_funding(
            funding,
            bullish=plan.bullish,
            hold_minutes=hold_minutes,
            notional=notional,
            as_of=entry_at,
            settings=cost_settings,
        )
    except CostModelError as error:
        if shadow_settings.simulation_require_funding_data:
            raise ExecutionModelError(
                SimulationRejectionCode.FUNDING_UNAVAILABLE, error.detail
            ) from error
        return ModelledFunding(
            funding_cost=0.0,
            intervals_modelled=0,
            hours_modelled=hold_minutes / 60.0,
            rate_basis="unavailable",
            data_available=False,
            detail=f"funding model unavailable: {error.detail}",
            warnings=("FUNDING_DATA_UNAVAILABLE",),
        )
    warnings: tuple[str, ...] = ()
    if projection.state.value != "MODELED":
        warnings = ("FUNDING_BUFFER_ONLY",)
    return ModelledFunding(
        funding_cost=projection.expected_cost,
        intervals_modelled=projection.intervals_charged,
        hours_modelled=hold_minutes / 60.0,
        rate_basis=projection.basis,
        data_available=projection.state.value == "MODELED",
        detail=(
            f"modelled funding over {hold_minutes} minute(s) of hypothetical holding "
            f"({projection.detail})"
        ),
        warnings=warnings,
    )
