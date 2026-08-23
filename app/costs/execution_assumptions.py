"""Versioned execution assumptions.

V1 default is fully taker-oriented and stop exits carry an extra stress
component - no maker fill is ever assumed unless explicitly configured.
"""

from __future__ import annotations

from app.config import CostSettings
from app.costs.enums import ExecutionMode
from app.costs.models import ExecutionAssumptions


def build_execution_assumptions(settings: CostSettings) -> ExecutionAssumptions:
    return ExecutionAssumptions(
        version=settings.execution_assumption_version,
        entry=ExecutionMode(settings.entry_execution_mode),
        target=ExecutionMode(settings.target_execution_mode),
        invalidation=ExecutionMode(settings.invalidation_execution_mode),
    )


def stress_bps_for(settings: CostSettings, leg: str, mode: ExecutionMode) -> float:
    """Additional conservative stress buffer (bps) for one leg."""
    if leg == "entry":
        return settings.entry_slippage_stress_bps
    if leg == "target":
        return settings.target_slippage_stress_bps
    stress = settings.invalidation_slippage_stress_bps
    if mode is ExecutionMode.STOP_STRESS_TAKER:
        return stress
    # plain stop taker: still conservative, but without the full stress add-on
    return min(stress, settings.entry_slippage_stress_bps)
