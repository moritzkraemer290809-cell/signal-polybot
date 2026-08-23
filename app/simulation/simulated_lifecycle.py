"""Mapping of phase-10 lifecycle events to modelled simulation exits.

The simulation never invents exit rules: every hypothetical exit is
triggered by an observed lifecycle event or update from phase 10.  This
module only translates that vocabulary - it holds no thresholds and no
market logic of its own.
"""

from __future__ import annotations

from app.simulation.enums import SimulationExitReason

#: phase-10 lifecycle EVENT types that close a hypothetical simulation
_EVENT_EXIT_REASONS = {
    "TARGET_1_REACHED": SimulationExitReason.TARGET_1_OBSERVED,
    "TARGET_2_REACHED": SimulationExitReason.TARGET_2_OBSERVED,
    "TECHNICAL_EXIT": SimulationExitReason.TECHNICAL_EXIT_CONDITION,
    "INVALIDATED": SimulationExitReason.INVALIDATION_CONDITION,
    "EXPIRED": SimulationExitReason.EXPIRED,
    "SUPERSEDED": SimulationExitReason.SUPERSEDED,
    "DATA_INVALID": SimulationExitReason.DATA_INVALID,
    "PAUSED": SimulationExitReason.SESSION_POLICY_END,
}

#: phase-10 lifecycle UPDATE types that may close a simulation
_UPDATE_EXIT_REASONS = {
    "TARGET_1_OBSERVED": SimulationExitReason.TARGET_1_OBSERVED,
    "TARGET_2_OBSERVED": SimulationExitReason.TARGET_2_OBSERVED,
    "TECHNICAL_EXIT_CONDITION": SimulationExitReason.TECHNICAL_EXIT_CONDITION,
    "INVALIDATION_CONDITION": SimulationExitReason.INVALIDATION_CONDITION,
    "SESSION_TRANSITION": SimulationExitReason.SESSION_POLICY_END,
}

#: terminal phase-10 states (a simulation may never outlive its lifecycle)
TERMINAL_LIFECYCLE_STATES = frozenset(
    {
        "TECHNICAL_EXIT",
        "INVALIDATED",
        "EXPIRED",
        "SUPERSEDED",
        "REJECTED",
        "DATA_INVALID",
    }
)

#: lifecycle states from which a shadow simulation may be admitted
ADMISSIBLE_LIFECYCLE_STATES = frozenset({"ENTRY_CONFIRMED", "ACTIVE_RESEARCH"})

#: exit reasons whose modelled quantity is the full reference quantity
FULL_EXIT_REASONS = frozenset(SimulationExitReason)


def exit_reason_for_event(event_type: str) -> SimulationExitReason | None:
    """Translate a phase-10 event type into a modelled exit reason."""
    return _EVENT_EXIT_REASONS.get(event_type)


def exit_reason_for_update(update_type: str) -> SimulationExitReason | None:
    return _UPDATE_EXIT_REASONS.get(update_type)


def exit_reason_for_state(state: str) -> SimulationExitReason | None:
    """Fallback when only the terminal state is known."""
    if state not in TERMINAL_LIFECYCLE_STATES:
        return None
    return _EVENT_EXIT_REASONS.get(state, SimulationExitReason.LIFECYCLE_TERMINAL)


def is_admissible_state(state: str) -> bool:
    return state in ADMISSIBLE_LIFECYCLE_STATES


def is_terminal_state(state: str) -> bool:
    return state in TERMINAL_LIFECYCLE_STATES
