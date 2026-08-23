"""Session monitoring: a blocked session ends or pauses monitoring per
explicit, documented policy - never silently.  Crypto stays a separate
24/7 session family; no new entry confirmations happen in a blocked
session (enforced by the entry monitor)."""

from __future__ import annotations

from app.config import SignalLifecycleSettings
from app.signals.enums import (
    SessionEndPolicy,
    SignalEventType,
    SignalState,
    SignalUpdateType,
)
from app.signals.models import MonitorContext, MonitorFinding
from app.signals.state_machine import is_post_entry

_STATE_FOR_POLICY = {
    SessionEndPolicy.EXPIRE: (SignalState.EXPIRED, SignalEventType.EXPIRED, "EXPIRED"),
    SessionEndPolicy.PAUSE: (SignalState.PAUSED, SignalEventType.PAUSED, "PAUSED"),
    SessionEndPolicy.TECHNICAL_EXIT: (
        SignalState.TECHNICAL_EXIT,
        SignalEventType.TECHNICAL_EXIT,
        "TECHNICAL_EXIT",
    ),
}


def check_session(
    context: MonitorContext, settings: SignalLifecycleSettings
) -> MonitorFinding | None:
    signal = context.signal
    if context.session_allowed:
        return None
    if signal.state is SignalState.PAUSED:
        return None  # already dormant
    policy = SessionEndPolicy(settings.monitor_session_end_policy)
    post_entry = is_post_entry(signal.state)
    if not post_entry and policy is SessionEndPolicy.TECHNICAL_EXIT:
        # a technical exit before any entry confirmation makes no sense -
        # pre-entry the policy conservatively degrades to EXPIRE
        policy = SessionEndPolicy.EXPIRE
    to_state, event, key = _STATE_FOR_POLICY[policy]
    return MonitorFinding(
        monitor="session",
        priority_key=key,
        to_state=to_state,
        event_type=event,
        reason=(
            f"session {context.session_state} no longer allowed for "
            f"{signal.asset_class} - policy {policy.value} "
            f"({'post' if post_entry else 'pre'}-entry, research lifecycle)"
        ),
        metadata={
            "session_state": context.session_state,
            "policy": policy.value,
            "post_entry": post_entry,
            "observed_at": context.as_of.isoformat(),
        },
        updates=(
            (
                SignalUpdateType.SESSION_TRANSITION,
                f"session became {context.session_state} (blocked)",
            ),
        ),
    )
