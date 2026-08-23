"""Expiry monitoring (all timestamps UTC).

Enforces the signal's own expiry, the maximum entry wait and the maximum
post-entry research duration.  Pre-entry expiry always maps to EXPIRED;
post-entry follows the configured policy (default EXPIRE - a technical
exit is only claimed when the policy explicitly says so).  Terminal states
are never monitored.
"""

from __future__ import annotations

from datetime import timedelta

from app.config import SignalLifecycleSettings
from app.signals.enums import (
    PostEntryExpiryPolicy,
    SignalEventType,
    SignalState,
    SignalUpdateType,
)
from app.signals.models import MonitorContext, MonitorFinding
from app.signals.state_machine import is_post_entry

_EXPIRY_WARNING_SECONDS = 300.0


def _expire_finding(
    context: MonitorContext, post_entry: bool, cause: str, settings: SignalLifecycleSettings
) -> MonitorFinding:
    policy = PostEntryExpiryPolicy(settings.monitor_post_entry_expiry_policy)
    if post_entry and policy is PostEntryExpiryPolicy.TECHNICAL_EXIT:
        to_state, event, key = (
            SignalState.TECHNICAL_EXIT,
            SignalEventType.TECHNICAL_EXIT,
            "TECHNICAL_EXIT",
        )
        reason = (
            f"{cause} - post-entry expiry policy TECHNICAL_EXIT "
            "(research lifecycle, no actual position exit implied)"
        )
    else:
        to_state, event, key = SignalState.EXPIRED, SignalEventType.EXPIRED, "EXPIRED"
        reason = f"{cause} - research monitoring window ended"
    return MonitorFinding(
        monitor="expiry",
        priority_key=key,
        to_state=to_state,
        event_type=event,
        reason=reason,
        metadata={
            "cause": cause,
            "post_entry": post_entry,
            "expires_at": context.signal.expires_at.isoformat(),
            "observed_at": context.as_of.isoformat(),
        },
    )


def check_expiry(
    context: MonitorContext, settings: SignalLifecycleSettings
) -> MonitorFinding | None:
    signal = context.signal
    now = context.as_of
    post_entry = is_post_entry(signal.state)

    if now >= signal.expires_at:
        return _expire_finding(context, post_entry, "signal expiry reached", settings)

    if (
        signal.state is SignalState.WATCHING_ENTRY
        and signal.watching_entry_at is not None
        and now - signal.watching_entry_at
        > timedelta(seconds=settings.lifecycle_entry_max_wait_seconds)
    ):
        return _expire_finding(context, False, "maximum entry wait window exceeded", settings)

    if (
        post_entry
        and signal.entry_confirmed_at is not None
        and now - signal.entry_confirmed_at
        > timedelta(seconds=settings.lifecycle_active_max_duration_seconds)
    ):
        return _expire_finding(
            context, True, "maximum research monitoring duration exceeded", settings
        )

    remaining = (signal.expires_at - now).total_seconds()
    if 0 < remaining <= _EXPIRY_WARNING_SECONDS:
        return MonitorFinding(
            monitor="expiry",
            priority_key="INFO",
            to_state=None,
            event_type=None,
            reason=f"expiry in {remaining:.0f}s",
            updates=(
                (
                    SignalUpdateType.EXPIRY_WARNING,
                    f"research window ends in {remaining:.0f}s",
                ),
            ),
        )
    return None
