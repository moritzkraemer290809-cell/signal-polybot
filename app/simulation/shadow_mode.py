"""Shadow-mode orchestration helpers (pure decisions, no I/O).

Decides WHICH persisted lifecycles a shadow run should consider and how a
simulation outcome maps onto run bookkeeping.  All heavy lifting stays in
the pure simulation engine; persistence stays in the job.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from app.simulation.enums import SimulatedPositionState, SimulationEventType
from app.simulation.models import SimulatedPositionResult
from app.simulation.simulated_lifecycle import (
    ADMISSIBLE_LIFECYCLE_STATES,
    is_terminal_state,
)

#: lifecycle states a shadow run may pick up (entry already confirmed)
SHADOW_SOURCE_STATES = frozenset(
    ADMISSIBLE_LIFECYCLE_STATES | {"TARGET_1_REACHED", "TARGET_2_REACHED", "TRAILING_RESEARCH"}
)


@dataclass(frozen=True)
class ShadowCandidate:
    """One persisted lifecycle a shadow run may simulate."""

    signal_id: uuid.UUID
    state: str
    entry_confirmed_at: datetime | None
    terminal_at: datetime | None

    @property
    def finished(self) -> bool:
        return is_terminal_state(self.state) or self.state in (
            "TARGET_1_REACHED",
            "TARGET_2_REACHED",
        )


def is_simulatable(state: str, entry_confirmed_at: datetime | None) -> bool:
    """Only lifecycles with a confirmed entry are ever simulated."""
    return entry_confirmed_at is not None and (
        state in SHADOW_SOURCE_STATES or is_terminal_state(state)
    )


def event_for_result(result: SimulatedPositionResult) -> SimulationEventType:
    """Audit event describing one simulation outcome."""
    if result.state is SimulatedPositionState.CLOSED_SIMULATION:
        return SimulationEventType.SIMULATION_COMPLETED
    if result.state is SimulatedPositionState.UNMODELED_EXIT:
        return SimulationEventType.EXIT_UNMODELED
    if result.state is SimulatedPositionState.OPEN_SIMULATION:
        return SimulationEventType.ENTRY_MODELLED
    return SimulationEventType.SIMULATION_INCOMPLETE


def run_counts(results: list[SimulatedPositionResult], rejected: int) -> dict[str, int]:
    completed = sum(1 for item in results if item.state is SimulatedPositionState.CLOSED_SIMULATION)
    incomplete = len(results) - completed
    return {
        "total": len(results) + rejected,
        "completed": completed,
        "incomplete": incomplete,
        "rejected": rejected,
    }
