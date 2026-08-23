"""Entry monitoring for WATCHING_ENTRY signals.

Confirms only technical zone conditions from fresh public data - never a
fill, never a position.  No confirmation with stale data, in a blocked
session, past expiry, on the wrong side of the invalidation, or while the
bot is globally paused.
"""

from __future__ import annotations

from app.config import SignalLifecycleSettings
from app.signals.entry_trigger import evaluate_entry_trigger
from app.signals.enums import SignalEventType, SignalState, SignalUpdateType
from app.signals.models import MonitorContext, MonitorFinding


def _conservative_side_price(context: MonitorContext) -> float | None:
    """Adverse-side reference: the price a research exit would face."""
    market = context.market
    candidates = []
    if context.signal.bullish:
        if market.bbo_fresh and market.best_bid is not None:
            candidates.append(market.best_bid)
        if market.mark_price is not None:
            candidates.append(market.mark_price)
        return min(candidates) if candidates else None
    if market.bbo_fresh and market.best_ask is not None:
        candidates.append(market.best_ask)
    if market.mark_price is not None:
        candidates.append(market.mark_price)
    return max(candidates) if candidates else None


def check_entry(
    context: MonitorContext, settings: SignalLifecycleSettings
) -> MonitorFinding | None:
    signal = context.signal
    if signal.state is not SignalState.WATCHING_ENTRY:
        return None
    market = context.market
    if context.bot_paused:
        return None  # global paused mode never confirms entries
    if not context.session_allowed:
        return None  # session monitor decides; no confirmation here
    if not market.data_quality_ok or market.book_resyncing:
        return None  # stale/invalid data never confirms an entry
    if context.as_of >= signal.expires_at:
        return None  # expiry monitor decides
    side_price = _conservative_side_price(context)
    if side_price is None:
        return None
    valid_side = (
        side_price > signal.invalidation_price
        if signal.bullish
        else side_price < signal.invalidation_price
    )
    if not valid_side:
        return None  # invalidation monitor outranks entry anyway

    evaluation = evaluate_entry_trigger(signal, market, signal.entry_trigger, settings)
    if evaluation.triggered:
        return MonitorFinding(
            monitor="entry",
            priority_key="ENTRY_CONFIRMED",
            to_state=SignalState.ENTRY_CONFIRMED,
            event_type=SignalEventType.ENTRY_CONFIRMED,
            reason=(
                f"technical entry condition confirmed ({evaluation.detail}) - "
                "research observation, not a fill"
            ),
            metadata={
                "trigger": signal.entry_trigger.value,
                "reference_price": evaluation.reference_price,
                "source": evaluation.source,
                "confidence": evaluation.confidence,
                "approximation_flags": list(evaluation.approximation_flags),
                "observed_at": context.as_of.isoformat(),
            },
            updates=(
                (
                    SignalUpdateType.ENTRY_CONFIRMED,
                    f"entry condition confirmed via {evaluation.source} "
                    "(research lifecycle, no execution)",
                ),
            ),
            approximation_flags=evaluation.approximation_flags,
        )
    if evaluation.touched:
        return MonitorFinding(
            monitor="entry",
            priority_key="INFO",
            to_state=None,
            event_type=None,
            reason="entry zone touched without confirmation",
            updates=(
                (
                    SignalUpdateType.ENTRY_CONDITION_OBSERVED,
                    f"reference entry zone touched ({evaluation.source}), confirmation pending",
                ),
            ),
        )
    return None
