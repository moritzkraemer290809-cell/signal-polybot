"""Deterministic signal state machine - pure domain logic, no I/O.

The transition map is explicit and closed: anything not listed is invalid.
Terminal states are never silently reactivated - continuing research always
requires a NEW signal with a new ID.  ``PAUSED`` is not terminal, but it is
a dormant end-of-monitoring state: the only ways out are the terminal
housekeeping transitions (EXPIRED / SUPERSEDED / DATA_INVALID); resuming
research requires a new signal (documented extension of the base map).
"""

from __future__ import annotations

from app.signals.enums import SignalEventType, SignalState

TERMINAL_STATES: frozenset[SignalState] = frozenset(
    {
        SignalState.TECHNICAL_EXIT,
        SignalState.INVALIDATED,
        SignalState.EXPIRED,
        SignalState.SUPERSEDED,
        SignalState.REJECTED,
        SignalState.DATA_INVALID,
    }
)

#: states in which the entry has already been technically confirmed
POST_ENTRY_STATES: frozenset[SignalState] = frozenset(
    {
        SignalState.ENTRY_CONFIRMED,
        SignalState.ACTIVE_RESEARCH,
        SignalState.TARGET_1_REACHED,
        SignalState.TARGET_2_REACHED,
        SignalState.TRAILING_RESEARCH,
    }
)

TRANSITIONS: dict[SignalState, frozenset[SignalState]] = {
    SignalState.DRAFT: frozenset({SignalState.PENDING_ADMISSION, SignalState.REJECTED}),
    SignalState.PENDING_ADMISSION: frozenset(
        {
            SignalState.WATCHING_ENTRY,
            SignalState.REJECTED,
            SignalState.EXPIRED,
            SignalState.PAUSED,
            SignalState.DATA_INVALID,
        }
    ),
    SignalState.WATCHING_ENTRY: frozenset(
        {
            SignalState.ENTRY_CONFIRMED,
            SignalState.INVALIDATED,
            SignalState.EXPIRED,
            SignalState.SUPERSEDED,
            SignalState.PAUSED,
            SignalState.DATA_INVALID,
            SignalState.REJECTED,
        }
    ),
    SignalState.ENTRY_CONFIRMED: frozenset(
        {
            SignalState.ACTIVE_RESEARCH,
            SignalState.INVALIDATED,
            SignalState.EXPIRED,
            SignalState.SUPERSEDED,
            SignalState.PAUSED,
            SignalState.DATA_INVALID,
        }
    ),
    SignalState.ACTIVE_RESEARCH: frozenset(
        {
            SignalState.TARGET_1_REACHED,
            SignalState.TARGET_2_REACHED,
            SignalState.TRAILING_RESEARCH,
            SignalState.TECHNICAL_EXIT,
            SignalState.INVALIDATED,
            SignalState.EXPIRED,
            SignalState.SUPERSEDED,
            SignalState.PAUSED,
            SignalState.DATA_INVALID,
        }
    ),
    SignalState.TARGET_1_REACHED: frozenset(
        {
            SignalState.TARGET_2_REACHED,
            SignalState.TRAILING_RESEARCH,
            SignalState.TECHNICAL_EXIT,
            SignalState.INVALIDATED,
            SignalState.EXPIRED,
            SignalState.SUPERSEDED,
            SignalState.PAUSED,
            SignalState.DATA_INVALID,
        }
    ),
    SignalState.TARGET_2_REACHED: frozenset(
        {
            SignalState.TRAILING_RESEARCH,
            SignalState.TECHNICAL_EXIT,
            SignalState.EXPIRED,
            SignalState.SUPERSEDED,
            SignalState.PAUSED,
            SignalState.DATA_INVALID,
        }
    ),
    SignalState.TRAILING_RESEARCH: frozenset(
        {
            SignalState.TECHNICAL_EXIT,
            SignalState.EXPIRED,
            SignalState.SUPERSEDED,
            SignalState.PAUSED,
            SignalState.DATA_INVALID,
        }
    ),
    #: documented extension: paused signals may only be closed out
    SignalState.PAUSED: frozenset(
        {SignalState.EXPIRED, SignalState.SUPERSEDED, SignalState.DATA_INVALID}
    ),
    SignalState.TECHNICAL_EXIT: frozenset(),
    SignalState.INVALIDATED: frozenset(),
    SignalState.EXPIRED: frozenset(),
    SignalState.SUPERSEDED: frozenset(),
    SignalState.REJECTED: frozenset(),
    SignalState.DATA_INVALID: frozenset(),
}

#: event emitted when entering a state
EVENT_FOR_STATE: dict[SignalState, SignalEventType] = {
    SignalState.PENDING_ADMISSION: SignalEventType.ADMITTED,
    SignalState.WATCHING_ENTRY: SignalEventType.WATCHING_ENTRY,
    SignalState.ENTRY_CONFIRMED: SignalEventType.ENTRY_CONFIRMED,
    SignalState.ACTIVE_RESEARCH: SignalEventType.ACTIVE_RESEARCH,
    SignalState.TARGET_1_REACHED: SignalEventType.TARGET_1_REACHED,
    SignalState.TARGET_2_REACHED: SignalEventType.TARGET_2_REACHED,
    SignalState.TRAILING_RESEARCH: SignalEventType.TRAILING_RESEARCH,
    SignalState.TECHNICAL_EXIT: SignalEventType.TECHNICAL_EXIT,
    SignalState.INVALIDATED: SignalEventType.INVALIDATED,
    SignalState.EXPIRED: SignalEventType.EXPIRED,
    SignalState.SUPERSEDED: SignalEventType.SUPERSEDED,
    SignalState.PAUSED: SignalEventType.PAUSED,
    SignalState.REJECTED: SignalEventType.REJECTED,
    SignalState.DATA_INVALID: SignalEventType.DATA_INVALID,
}


def is_terminal(state: SignalState) -> bool:
    return state in TERMINAL_STATES


def is_post_entry(state: SignalState) -> bool:
    return state in POST_ENTRY_STATES


def can_transition(from_state: SignalState, to_state: SignalState) -> bool:
    return to_state in TRANSITIONS.get(from_state, frozenset())


class InvalidTransitionError(Exception):
    def __init__(self, from_state: SignalState, to_state: SignalState) -> None:
        super().__init__(f"invalid transition {from_state.value} -> {to_state.value}")
        self.from_state = from_state
        self.to_state = to_state


def validate_transition(from_state: SignalState, to_state: SignalState) -> None:
    if not can_transition(from_state, to_state):
        raise InvalidTransitionError(from_state, to_state)


def transition_map_document() -> dict[str, list[str]]:
    """Serializable transition map for the local dashboard."""
    return {
        state.value: sorted(target.value for target in targets)
        for state, targets in TRANSITIONS.items()
    }
