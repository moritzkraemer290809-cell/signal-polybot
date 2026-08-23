"""Deterministic lifecycle evaluation - pure, no I/O.

Runs every monitor over one immutable context, picks AT MOST ONE final
state transition per cycle via the configured strictly-distinct priority
map, validates it against the state machine, and returns every
non-conflicting observation as updates/metadata.  A target can never win
over a simultaneously confirmed invalidation, and stale data never
produces confirmations (each monitor enforces its own freshness).

The only documented transition chain is the entry chain: a confirmed entry
moves WATCHING_ENTRY -> ENTRY_CONFIRMED -> ACTIVE_RESEARCH inside one
cycle (two events, one final state), unless a higher-priority finding won
the cycle.
"""

from __future__ import annotations

from app.config import SignalLifecycleSettings
from app.signals.data_quality_monitor import check_data_quality
from app.signals.entry_monitor import check_entry
from app.signals.enums import SignalEventType, SignalState, SignalUpdateType
from app.signals.expiry_monitor import check_expiry
from app.signals.invalidation_monitor import check_invalidation
from app.signals.models import (
    EngineResult,
    MonitorContext,
    MonitorFinding,
    TransitionStep,
)
from app.signals.plan_monitor import check_plan
from app.signals.session_monitor import check_session
from app.signals.state_machine import can_transition, is_terminal
from app.signals.structure_exit_monitor import check_structure_exit
from app.signals.supersede_monitor import check_supersede
from app.signals.target_monitor import check_targets

#: monitors allowed to act on a PAUSED (dormant) signal
_PAUSED_MONITORS = ("data_quality", "plan", "supersede", "expiry")

_MONITORS = (
    check_data_quality,
    check_plan,
    check_supersede,
    check_expiry,
    check_session,
    check_invalidation,
    check_structure_exit,
    check_targets,
    check_entry,
)


def evaluate_signal(context: MonitorContext, settings: SignalLifecycleSettings) -> EngineResult:
    signal = context.signal
    empty = EngineResult(
        signal_id=signal.signal_id,
        transitions=(),
        updates=(),
        observed=(),
        suppressed_findings=(),
    )
    if is_terminal(signal.state):
        return empty  # terminal states are never monitored or reactivated

    priorities = settings.monitor_event_priority_json
    findings: list[MonitorFinding] = []
    for monitor in _MONITORS:
        finding = monitor(context, settings)
        if finding is None:
            continue
        if signal.state is SignalState.PAUSED and finding.monitor not in _PAUSED_MONITORS:
            continue
        findings.append(finding)

    # deterministic auto-progressions with the lowest transition priority
    if signal.state is SignalState.ENTRY_CONFIRMED:
        findings.append(
            MonitorFinding(
                monitor="engine",
                priority_key="INFO",
                to_state=SignalState.ACTIVE_RESEARCH,
                event_type=SignalEventType.ACTIVE_RESEARCH,
                reason="entry confirmed - active research monitoring begins",
            )
        )
    elif signal.state is SignalState.TARGET_2_REACHED:
        findings.append(
            MonitorFinding(
                monitor="engine",
                priority_key="INFO",
                to_state=SignalState.TRAILING_RESEARCH,
                event_type=SignalEventType.TRAILING_RESEARCH,
                reason="both reference targets reached - trailing research observation",
            )
        )

    updates: list[tuple[SignalUpdateType, str]] = []
    observed: list[str] = []
    suppressed: list[str] = []
    warnings: list[str] = []
    transition_findings: list[MonitorFinding] = []
    for finding in findings:
        updates.extend(finding.updates)
        if finding.to_state is None or finding.event_type is None:
            observed.append(f"{finding.monitor}: {finding.reason}")
            continue
        if finding.to_state is signal.state:
            continue  # idempotent: already reflected
        if not can_transition(signal.state, finding.to_state):
            suppressed.append(
                f"{finding.monitor}: invalid transition "
                f"{signal.state.value} -> {finding.to_state.value} ({finding.reason})"
            )
            continue
        if finding.priority_key not in priorities:
            warnings.append(f"unknown priority key {finding.priority_key!r} - skipped")
            continue
        transition_findings.append(finding)

    if not transition_findings:
        return EngineResult(
            signal_id=signal.signal_id,
            transitions=(),
            updates=tuple(updates),
            observed=tuple(observed),
            suppressed_findings=tuple(suppressed),
            warnings=tuple(warnings),
        )

    # strictly distinct priorities => a deterministic single winner
    winner = max(transition_findings, key=lambda item: priorities[item.priority_key])
    for finding in transition_findings:
        if finding is not winner:
            observed.append(
                f"{finding.monitor}: {finding.reason} (outranked by {winner.priority_key})"
            )

    assert winner.to_state is not None and winner.event_type is not None
    steps = [
        TransitionStep(
            from_state=signal.state,
            to_state=winner.to_state,
            event_type=winner.event_type,
            reason=winner.reason,
            priority=priorities[winner.priority_key],
            metadata=dict(winner.metadata),
        )
    ]
    # documented entry chain: confirmation and research activation happen in
    # the same deterministic cycle when nothing higher-priority intervened
    if winner.to_state is SignalState.ENTRY_CONFIRMED:
        steps.append(
            TransitionStep(
                from_state=SignalState.ENTRY_CONFIRMED,
                to_state=SignalState.ACTIVE_RESEARCH,
                event_type=SignalEventType.ACTIVE_RESEARCH,
                reason="entry confirmed - active research monitoring begins",
                priority=priorities["INFO"],
            )
        )
    return EngineResult(
        signal_id=signal.signal_id,
        transitions=tuple(steps),
        updates=tuple(updates),
        observed=tuple(observed),
        suppressed_findings=tuple(suppressed),
        warnings=tuple(warnings),
    )
