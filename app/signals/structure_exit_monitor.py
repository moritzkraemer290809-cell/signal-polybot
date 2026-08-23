"""Structure-based technical exit monitoring.

Consumes ONLY existing phase-8 structure signatures (supplied as opposing
event strings by the context builder) and the plan's own anchor level - no
new strategy and no new structure engine.  A technical exit never claims an
actual position exit.
"""

from __future__ import annotations

from app.config import SignalLifecycleSettings
from app.signals.enums import SignalEventType, SignalState, SignalUpdateType
from app.signals.models import MonitorContext, MonitorFinding

_POST_ENTRY = {
    SignalState.ACTIVE_RESEARCH,
    SignalState.TARGET_1_REACHED,
    SignalState.TARGET_2_REACHED,
    SignalState.TRAILING_RESEARCH,
}
_BREAKOUT_TYPES = {"RANGE_BREAKOUT_UP", "RANGE_BREAKOUT_DOWN"}


def check_structure_exit(
    context: MonitorContext, settings: SignalLifecycleSettings
) -> MonitorFinding | None:
    signal = context.signal
    if not settings.monitor_structure_exit_enabled or signal.state not in _POST_ENTRY:
        return None
    market = context.market

    if context.opposing_structure_events:
        return MonitorFinding(
            monitor="structure_exit",
            priority_key="TECHNICAL_EXIT",
            to_state=SignalState.TECHNICAL_EXIT,
            event_type=SignalEventType.TECHNICAL_EXIT,
            reason=(
                "confirmed opposing structure change from phase-8 events - "
                "technical research exit, no actual position exit implied"
            ),
            metadata={
                "basis": "OPPOSING_STRUCTURE_EVENT",
                "events": list(context.opposing_structure_events)[:5],
                "observed_at": context.as_of.isoformat(),
            },
            updates=(
                (
                    SignalUpdateType.TECHNICAL_EXIT_CONDITION,
                    "opposing structure change confirmed (research lifecycle)",
                ),
            ),
        )

    # breakout candidates: a confirmed 5m close back beyond the anchor
    # (inside the prior range) is the candidate's own invalidation condition
    if (
        signal.candidate_type in _BREAKOUT_TYPES
        and market.last_closed_5m_close is not None
        and market.last_closed_5m_close_time is not None
        and market.data_quality_ok
    ):
        close = market.last_closed_5m_close
        anchor = signal.entry_low if signal.bullish else signal.entry_high
        back_inside = close < anchor if signal.bullish else close > anchor
        still_above_invalidation = (
            close > signal.invalidation_price
            if signal.bullish
            else close < signal.invalidation_price
        )
        if back_inside and still_above_invalidation:
            return MonitorFinding(
                monitor="structure_exit",
                priority_key="TECHNICAL_EXIT",
                to_state=SignalState.TECHNICAL_EXIT,
                event_type=SignalEventType.TECHNICAL_EXIT,
                reason=(
                    f"confirmed 5m close {close:.6g} back beyond the breakout anchor "
                    f"{anchor:.6g} (inside prior range) - technical research exit, "
                    "no actual position exit implied"
                ),
                metadata={
                    "basis": "CLOSE_BACK_INSIDE_RANGE",
                    "close": close,
                    "anchor": anchor,
                    "observed_at": context.as_of.isoformat(),
                },
                updates=(
                    (
                        SignalUpdateType.TECHNICAL_EXIT_CONDITION,
                        "close back inside prior range (research lifecycle)",
                    ),
                ),
            )
    return None
