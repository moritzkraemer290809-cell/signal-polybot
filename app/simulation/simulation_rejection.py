"""SimulationRejection construction (structured, aggregated audit)."""

from __future__ import annotations

import uuid
from datetime import datetime

from app.simulation.enums import SimulationRejectionCode
from app.simulation.models import SimulationInputs, SimulationRejection


def build_rejection(
    inputs: SimulationInputs,
    code: SimulationRejectionCode,
    detail: str,
    *,
    simulation_model_version: str,
    simulation_config_hash: str,
    as_of: datetime | None = None,
) -> SimulationRejection:
    return SimulationRejection(
        rejection_id=uuid.uuid4(),
        run_id=inputs.run_id,
        lifecycle_signal_id=inputs.lifecycle_signal_id,
        plan_id=inputs.plan.plan_id,
        candidate_id=inputs.plan.candidate_id,
        symbol=inputs.plan.symbol,
        as_of=as_of or inputs.as_of,
        primary_code=code,
        codes=(code,),
        detail=detail,
        simulation_model_version=simulation_model_version,
        simulation_config_hash=simulation_config_hash,
    )


def build_run_rejection(
    *,
    run_id: uuid.UUID | None,
    symbol: str,
    code: SimulationRejectionCode,
    detail: str,
    simulation_model_version: str,
    simulation_config_hash: str,
    as_of: datetime,
) -> SimulationRejection:
    """Rejection without a lifecycle context (run/manifest/data level)."""
    return SimulationRejection(
        rejection_id=uuid.uuid4(),
        run_id=run_id,
        lifecycle_signal_id=None,
        plan_id=None,
        candidate_id=None,
        symbol=symbol,
        as_of=as_of,
        primary_code=code,
        codes=(code,),
        detail=detail,
        simulation_model_version=simulation_model_version,
        simulation_config_hash=simulation_config_hash,
    )
