"""Supersede monitoring (STRICT policy).

A newer confirmed plan replacing this signal's plan context supersedes the
signal deterministically.  There is never an automatic switch to an
opposite-direction lifecycle - the new candidate/plan must pass admission
separately and receives its own signal ID.
"""

from __future__ import annotations

from app.config import SignalLifecycleSettings
from app.signals.enums import SignalEventType, SignalState, SignalUpdateType
from app.signals.models import MonitorContext, MonitorFinding


def check_supersede(
    context: MonitorContext, settings: SignalLifecycleSettings
) -> MonitorFinding | None:
    if context.superseding_plan_id is None:
        return None
    if context.superseding_plan_id == context.signal.plan_id:
        return None
    return MonitorFinding(
        monitor="supersede",
        priority_key="SUPERSEDED",
        to_state=SignalState.SUPERSEDED,
        event_type=SignalEventType.SUPERSEDED,
        reason=(
            "a newer eligible plan replaced this signal's research context - "
            "the replacement requires its own admission and a new signal ID"
        ),
        metadata={
            "superseding_plan_id": str(context.superseding_plan_id),
            "policy": settings.monitor_supersede_policy,
            "observed_at": context.as_of.isoformat(),
        },
        updates=(
            (
                SignalUpdateType.SUPERSEDE_WARNING,
                "research context superseded by a newer plan",
            ),
        ),
    )
