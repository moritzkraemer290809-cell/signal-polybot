"""Plan/candidate context monitoring.

When the underlying phase-9 plan or phase-8 candidate turns invalid,
expired or superseded, the signal follows a clearly configured mapping -
it never claims that a real position was closed.  Old plans stay
historical; no old plan context is reused for new entry confirmations
(a new plan requires a new signal via admission).
"""

from __future__ import annotations

from app.config import SignalLifecycleSettings
from app.signals.enums import SignalEventType, SignalState, SignalUpdateType
from app.signals.models import MonitorContext, MonitorFinding
from app.signals.state_machine import is_post_entry


def _finding(
    monitor: str,
    key: str,
    to_state: SignalState,
    event: SignalEventType,
    reason: str,
    update: tuple[SignalUpdateType, str],
    context: MonitorContext,
    **metadata: object,
) -> MonitorFinding:
    return MonitorFinding(
        monitor=monitor,
        priority_key=key,
        to_state=to_state,
        event_type=event,
        reason=reason,
        metadata={"observed_at": context.as_of.isoformat(), **metadata},
        updates=(update,),
    )


def check_plan(context: MonitorContext, settings: SignalLifecycleSettings) -> MonitorFinding | None:
    signal = context.signal
    post_entry = is_post_entry(signal.state)

    if context.plan is None:
        return _finding(
            "plan",
            "DATA_INVALID",
            SignalState.DATA_INVALID,
            SignalEventType.DATA_INVALID,
            "underlying eligibility plan no longer loadable - context lost",
            (SignalUpdateType.PLAN_CHANGED, "plan context lost"),
            context,
        )
    status = context.plan.status
    if status == "SUPERSEDED":
        return _finding(
            "plan",
            "SUPERSEDED",
            SignalState.SUPERSEDED,
            SignalEventType.SUPERSEDED,
            "underlying plan superseded - research context replaced",
            (SignalUpdateType.PLAN_CHANGED, "plan superseded"),
            context,
            plan_status=status,
        )
    if status == "EXPIRED":
        return _finding(
            "plan",
            "EXPIRED",
            SignalState.EXPIRED,
            SignalEventType.EXPIRED,
            "underlying plan expired",
            (SignalUpdateType.PLAN_CHANGED, "plan expired"),
            context,
            plan_status=status,
        )
    if status == "DATA_INVALID":
        return _finding(
            "plan",
            "DATA_INVALID",
            SignalState.DATA_INVALID,
            SignalEventType.DATA_INVALID,
            "underlying plan marked data-invalid",
            (SignalUpdateType.PLAN_CHANGED, "plan data-invalid"),
            context,
            plan_status=status,
        )
    if status != "ELIGIBLE":
        if post_entry:
            # documented default: after entry the signal ends as DATA_INVALID -
            # never a claim that any real position was closed
            return _finding(
                "plan",
                "DATA_INVALID",
                SignalState.DATA_INVALID,
                SignalEventType.DATA_INVALID,
                f"underlying plan no longer eligible ({status}) after entry",
                (SignalUpdateType.PLAN_CHANGED, f"plan {status}"),
                context,
                plan_status=status,
            )
        return _finding(
            "plan",
            "EXPIRED",  # priority slot; state REJECTED is legal pre-entry
            SignalState.REJECTED,
            SignalEventType.REJECTED,
            f"underlying plan no longer eligible ({status}) before entry",
            (SignalUpdateType.PLAN_CHANGED, f"plan {status}"),
            context,
            plan_status=status,
        )

    candidate = context.candidate
    if candidate is None:
        return _finding(
            "plan",
            "DATA_INVALID",
            SignalState.DATA_INVALID,
            SignalEventType.DATA_INVALID,
            "underlying candidate no longer loadable - context lost",
            (SignalUpdateType.CANDIDATE_CHANGED, "candidate context lost"),
            context,
        )
    if candidate.state == "SUPERSEDED":
        return _finding(
            "plan",
            "SUPERSEDED",
            SignalState.SUPERSEDED,
            SignalEventType.SUPERSEDED,
            "underlying candidate superseded",
            (SignalUpdateType.CANDIDATE_CHANGED, "candidate superseded"),
            context,
            candidate_state=candidate.state,
        )
    if candidate.state in ("EXPIRED", "REJECTED") and not post_entry:
        return _finding(
            "plan",
            "EXPIRED",
            SignalState.EXPIRED,
            SignalEventType.EXPIRED,
            f"underlying candidate {candidate.state} before entry",
            (SignalUpdateType.CANDIDATE_CHANGED, f"candidate {candidate.state}"),
            context,
            candidate_state=candidate.state,
        )
    # post-entry candidate expiry: the plan/signal expiry window governs -
    # documented, no action here
    return None
