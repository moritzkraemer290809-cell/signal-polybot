"""Technical invalidation monitoring.

Uses conservative mark-price/BBO/candle-close references per configured
policy - never last price alone, never stale data.  A confirmed
invalidation means the research thesis is technically invalid; it is NEVER
reported as a "stop filled".  After TARGET_2/TRAILING an adverse level
breach maps to TECHNICAL_EXIT (INVALIDATED is no longer a legal
transition there).
"""

from __future__ import annotations

from app.config import SignalLifecycleSettings
from app.signals.enums import (
    InvalidationTriggerPolicy,
    SignalEventType,
    SignalState,
    SignalUpdateType,
)
from app.signals.models import MonitorContext, MonitorFinding

_INVALIDATABLE = {
    SignalState.WATCHING_ENTRY,
    SignalState.ENTRY_CONFIRMED,
    SignalState.ACTIVE_RESEARCH,
    SignalState.TARGET_1_REACHED,
}
_EXIT_MAPPED = {SignalState.TARGET_2_REACHED, SignalState.TRAILING_RESEARCH}


def _sources_beyond_level(
    context: MonitorContext, settings: SignalLifecycleSettings
) -> list[tuple[str, float]]:
    """(source, price) pairs of FRESH references at/beyond the level."""
    signal = context.signal
    market = context.market
    level = signal.invalidation_price
    bullish = signal.bullish
    hits: list[tuple[str, float]] = []
    if settings.monitor_use_mark_price and market.mark_price is not None and market.data_quality_ok:
        beyond = market.mark_price <= level if bullish else market.mark_price >= level
        if beyond:
            hits.append(("MARK_PRICE", market.mark_price))
    if settings.monitor_use_bbo and market.bbo_fresh:
        reference = market.best_bid if bullish else market.best_ask
        if reference is not None:
            beyond = reference <= level if bullish else reference >= level
            if beyond:
                hits.append(("BBO", reference))
    if market.last_closed_5m_close is not None and market.last_closed_5m_close_time is not None:
        close = market.last_closed_5m_close
        beyond = close <= level if bullish else close >= level
        if beyond:
            hits.append(("5M_CLOSE", close))
    return hits


def check_invalidation(
    context: MonitorContext, settings: SignalLifecycleSettings
) -> MonitorFinding | None:
    signal = context.signal
    if signal.state not in _INVALIDATABLE and signal.state not in _EXIT_MAPPED:
        return None
    if not context.market.data_quality_ok and not context.market.bbo_fresh:
        return None  # stale data never confirms an invalidation
    hits = _sources_beyond_level(context, settings)
    if not hits:
        return None

    policy = InvalidationTriggerPolicy(settings.invalidation_trigger_default)
    sources = {source for source, _ in hits}
    if policy is InvalidationTriggerPolicy.MARK_PRICE_TOUCH and "MARK_PRICE" not in sources:
        return None
    if policy is InvalidationTriggerPolicy.BBO_TOUCH and "BBO" not in sources:
        return None
    if policy is InvalidationTriggerPolicy.FIVE_M_CLOSE_BEYOND_LEVEL and "5M_CLOSE" not in sources:
        return None
    if settings.invalidation_close_confirmation_required and "5M_CLOSE" not in sources:
        return None
    # CONSERVATIVE_COMBINED: any fresh source at/beyond the level triggers

    source, price = hits[0]
    if signal.state in _EXIT_MAPPED:
        to_state, event, key = (
            SignalState.TECHNICAL_EXIT,
            SignalEventType.TECHNICAL_EXIT,
            "TECHNICAL_EXIT",
        )
        reason = (
            f"invalidation level breached after targets ({source} {price:.6g} vs "
            f"{signal.invalidation_price:.6g}) - technical research exit, no "
            "actual position exit implied"
        )
    else:
        to_state, event, key = (
            SignalState.INVALIDATED,
            SignalEventType.INVALIDATED,
            "INVALIDATED",
        )
        reason = (
            f"technical invalidation: {source} {price:.6g} at/beyond level "
            f"{signal.invalidation_price:.6g} - research thesis invalid, "
            "never a stop fill"
        )
    return MonitorFinding(
        monitor="invalidation",
        priority_key=key,
        to_state=to_state,
        event_type=event,
        reason=reason,
        metadata={
            "policy": policy.value,
            "source": source,
            "price": price,
            "level": signal.invalidation_price,
            "all_sources": [{"source": s, "price": p} for s, p in hits],
            "data_fresh": context.market.data_quality_ok,
            "observed_at": context.as_of.isoformat(),
        },
        updates=(
            (
                SignalUpdateType.INVALIDATION_CONDITION,
                f"invalidation condition via {source} (research lifecycle)",
            ),
        ),
    )
