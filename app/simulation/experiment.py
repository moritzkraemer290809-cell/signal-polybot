"""Experiment lifecycle helpers (pure, no I/O).

An experiment groups hypothetical runs that answer one research question.
Statuses move forward only; a completed experiment is never edited in
place - new insight means a new run under a new manifest.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from app.simulation.enums import (
    BacktestRunStatus,
    ExperimentStatus,
    RunType,
    SimulationRunStatus,
)
from app.simulation.experiment_manifest import Experiment

_EXPERIMENT_TRANSITIONS: dict[ExperimentStatus, frozenset[ExperimentStatus]] = {
    ExperimentStatus.DRAFT: frozenset(
        {ExperimentStatus.VALIDATING, ExperimentStatus.REJECTED, ExperimentStatus.CANCELLED}
    ),
    ExperimentStatus.VALIDATING: frozenset(
        {ExperimentStatus.RUNNING, ExperimentStatus.REJECTED, ExperimentStatus.CANCELLED}
    ),
    ExperimentStatus.RUNNING: frozenset(
        {
            ExperimentStatus.COMPLETED,
            ExperimentStatus.COMPLETED_WITH_GAPS,
            ExperimentStatus.FAILED,
            ExperimentStatus.CANCELLED,
        }
    ),
    ExperimentStatus.COMPLETED: frozenset(),
    ExperimentStatus.COMPLETED_WITH_GAPS: frozenset(),
    ExperimentStatus.REJECTED: frozenset(),
    ExperimentStatus.FAILED: frozenset(),
    ExperimentStatus.CANCELLED: frozenset(),
}

_TERMINAL_RUN_STATUSES = frozenset(
    {
        SimulationRunStatus.COMPLETED,
        SimulationRunStatus.INCOMPLETE,
        SimulationRunStatus.REJECTED,
        SimulationRunStatus.FAILED,
        SimulationRunStatus.CANCELLED,
    }
)

_TERMINAL_BACKTEST_STATUSES = frozenset(
    {
        BacktestRunStatus.COMPLETED,
        BacktestRunStatus.COMPLETED_WITH_GAPS,
        BacktestRunStatus.REJECTED,
        BacktestRunStatus.FAILED,
        BacktestRunStatus.CANCELLED,
    }
)


def can_transition(current: ExperimentStatus, target: ExperimentStatus) -> bool:
    return target in _EXPERIMENT_TRANSITIONS.get(current, frozenset())


def is_terminal_run(status: SimulationRunStatus | BacktestRunStatus) -> bool:
    if isinstance(status, SimulationRunStatus):
        return status in _TERMINAL_RUN_STATUSES
    return status in _TERMINAL_BACKTEST_STATUSES


def new_experiment(
    *,
    name: str,
    description: str,
    created_at: datetime,
    created_by: str = "system",
    tags: tuple[str, ...] = (),
) -> Experiment:
    return Experiment(
        experiment_id=uuid.uuid4(),
        name=name,
        description=description,
        status=ExperimentStatus.DRAFT.value,
        created_at=created_at,
        created_by=created_by,
        tags=tags,
    )


def experiment_status_for_runs(
    statuses: tuple[BacktestRunStatus, ...],
) -> ExperimentStatus:
    """Aggregate run outcomes into the experiment's terminal status."""
    if not statuses:
        return ExperimentStatus.DRAFT
    if any(status is BacktestRunStatus.RUNNING for status in statuses):
        return ExperimentStatus.RUNNING
    if all(status is BacktestRunStatus.CANCELLED for status in statuses):
        return ExperimentStatus.CANCELLED
    if any(status is BacktestRunStatus.FAILED for status in statuses):
        return ExperimentStatus.FAILED
    if any(status is BacktestRunStatus.REJECTED for status in statuses):
        return ExperimentStatus.REJECTED
    if any(status is BacktestRunStatus.COMPLETED_WITH_GAPS for status in statuses):
        return ExperimentStatus.COMPLETED_WITH_GAPS
    return ExperimentStatus.COMPLETED


def run_type_of(manifest_payload: dict[str, object]) -> RunType:
    return RunType(str(manifest_payload.get("run_type", RunType.BACKTEST.value)))
