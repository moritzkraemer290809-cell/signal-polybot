"""Signal lifecycle state machine definition.

Only the *definition* of the state machine lives here (data model level).
Monitoring logic that drives transitions is implemented in later phases.
"""

from __future__ import annotations

from app.domain.enums import SignalStatus

#: Allowed transitions of the signal state machine.  Any transition not listed
#: here is invalid and must be rejected (and logged) by callers.
ALLOWED_TRANSITIONS: dict[SignalStatus, frozenset[SignalStatus]] = {
    SignalStatus.CANDIDATE: frozenset({SignalStatus.REJECTED, SignalStatus.WATCHING}),
    SignalStatus.REJECTED: frozenset(),
    SignalStatus.WATCHING: frozenset(
        {
            SignalStatus.OPEN,
            SignalStatus.INVALIDATED,
            SignalStatus.EXPIRED,
            SignalStatus.PAUSED,
        }
    ),
    SignalStatus.OPEN: frozenset(
        {
            SignalStatus.PARTIAL,
            SignalStatus.BREAK_EVEN,
            SignalStatus.TRAILING,
            SignalStatus.CLOSED_TP,
            SignalStatus.CLOSED_STOP,
            SignalStatus.CLOSED_STRUCTURE_EXIT,
            SignalStatus.CLOSED_TIME_EXIT,
            SignalStatus.PAUSED,
        }
    ),
    SignalStatus.PARTIAL: frozenset(
        {
            SignalStatus.BREAK_EVEN,
            SignalStatus.TRAILING,
            SignalStatus.CLOSED_TP,
            SignalStatus.CLOSED_STOP,
            SignalStatus.CLOSED_STRUCTURE_EXIT,
            SignalStatus.CLOSED_TIME_EXIT,
            SignalStatus.PAUSED,
        }
    ),
    SignalStatus.BREAK_EVEN: frozenset(
        {
            SignalStatus.TRAILING,
            SignalStatus.CLOSED_TP,
            SignalStatus.CLOSED_STOP,
            SignalStatus.CLOSED_STRUCTURE_EXIT,
            SignalStatus.CLOSED_TIME_EXIT,
            SignalStatus.PAUSED,
        }
    ),
    SignalStatus.TRAILING: frozenset(
        {
            SignalStatus.CLOSED_TP,
            SignalStatus.CLOSED_STOP,
            SignalStatus.CLOSED_STRUCTURE_EXIT,
            SignalStatus.CLOSED_TIME_EXIT,
            SignalStatus.PAUSED,
        }
    ),
    SignalStatus.CLOSED_TP: frozenset(),
    SignalStatus.CLOSED_STOP: frozenset(),
    SignalStatus.CLOSED_STRUCTURE_EXIT: frozenset(),
    SignalStatus.CLOSED_TIME_EXIT: frozenset(),
    SignalStatus.INVALIDATED: frozenset(),
    SignalStatus.EXPIRED: frozenset(),
    # PAUSED preserves the previous live state; resuming is handled by the
    # monitoring layer which restores the stored pre-pause state.
    SignalStatus.PAUSED: frozenset(
        {
            SignalStatus.WATCHING,
            SignalStatus.OPEN,
            SignalStatus.PARTIAL,
            SignalStatus.BREAK_EVEN,
            SignalStatus.TRAILING,
            SignalStatus.EXPIRED,
            SignalStatus.INVALIDATED,
        }
    ),
}

TERMINAL_STATES: frozenset[SignalStatus] = frozenset(
    state for state, targets in ALLOWED_TRANSITIONS.items() if not targets
)

OPEN_STATES: frozenset[SignalStatus] = frozenset(
    {
        SignalStatus.WATCHING,
        SignalStatus.OPEN,
        SignalStatus.PARTIAL,
        SignalStatus.BREAK_EVEN,
        SignalStatus.TRAILING,
        SignalStatus.PAUSED,
    }
)


def is_transition_allowed(current: SignalStatus, new: SignalStatus) -> bool:
    return new in ALLOWED_TRANSITIONS.get(current, frozenset())
