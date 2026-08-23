"""Reference target monitoring.

Watches ONLY the plan's persisted reference targets - never invents a
level.  A reached target is a technical observation ("target level
touched/closed beyond"), never a partial profit, fill, realised gain or
real position statement.  Idempotent: an already-reflected target never
re-fires.
"""

from __future__ import annotations

from app.config import SignalLifecycleSettings
from app.signals.enums import (
    SignalEventType,
    SignalState,
    SignalUpdateType,
    TargetTriggerPolicy,
)
from app.signals.models import MonitorContext, MonitorFinding

_TARGETABLE = {SignalState.ACTIVE_RESEARCH, SignalState.TARGET_1_REACHED}


def _target_reached(
    context: MonitorContext, target: float, settings: SignalLifecycleSettings
) -> tuple[bool, str, float | None]:
    market = context.market
    bullish = context.signal.bullish
    policy = TargetTriggerPolicy(settings.target_trigger_default)
    if policy is TargetTriggerPolicy.MARK_PRICE_TOUCH:
        if market.mark_price is None or not market.data_quality_ok:
            return False, "MARK_PRICE", None
        hit = market.mark_price >= target if bullish else market.mark_price <= target
        return hit, "MARK_PRICE", market.mark_price
    if policy is TargetTriggerPolicy.BBO_TOUCH:
        # conservative achievable side: bid for bullish exits, ask for bearish
        reference = market.best_bid if bullish else market.best_ask
        if reference is None or not market.bbo_fresh:
            return False, "BBO", None
        hit = reference >= target if bullish else reference <= target
        return hit, "BBO", reference
    close = market.last_closed_5m_close
    if close is None or market.last_closed_5m_close_time is None:
        return False, "5M_CLOSE", None
    hit = close >= target if bullish else close <= target
    return hit, "5M_CLOSE", close


def check_targets(
    context: MonitorContext, settings: SignalLifecycleSettings
) -> MonitorFinding | None:
    signal = context.signal
    if not settings.monitor_targets_enabled or signal.state not in _TARGETABLE:
        return None
    if not signal.target_prices:
        return None

    target_1 = signal.target_prices[0]
    target_2 = signal.target_prices[1] if len(signal.target_prices) > 1 else None

    hit_2 = source_2 = price_2 = None
    if target_2 is not None:
        reached, source_2, price_2 = _target_reached(context, target_2, settings)
        hit_2 = reached
    if hit_2 and source_2 is not None:
        return MonitorFinding(
            monitor="target",
            priority_key="TARGET_2_REACHED",
            to_state=SignalState.TARGET_2_REACHED,
            event_type=SignalEventType.TARGET_2_REACHED,
            reason=(
                f"reference target 2 ({target_2:.6g}) reached via {source_2} - "
                "technical observation, no fill or realised gain implied"
            ),
            metadata={
                "target_index": 2,
                "target_price": target_2,
                "source": source_2,
                "price": price_2,
                "observed_at": context.as_of.isoformat(),
            },
            updates=(
                (
                    SignalUpdateType.TARGET_2_OBSERVED,
                    f"reference target 2 observed via {source_2} (research lifecycle)",
                ),
            ),
        )

    if signal.state is SignalState.TARGET_1_REACHED:
        return None  # target 1 already reflected - idempotent
    reached, source_1, price_1 = _target_reached(context, target_1, settings)
    if not reached:
        return None
    return MonitorFinding(
        monitor="target",
        priority_key="TARGET_1_REACHED",
        to_state=SignalState.TARGET_1_REACHED,
        event_type=SignalEventType.TARGET_1_REACHED,
        reason=(
            f"reference target 1 ({target_1:.6g}) reached via {source_1} - "
            "technical observation, no fill or realised gain implied"
        ),
        metadata={
            "target_index": 1,
            "target_price": target_1,
            "source": source_1,
            "price": price_1,
            "observed_at": context.as_of.isoformat(),
        },
        updates=(
            (
                SignalUpdateType.TARGET_1_OBSERVED,
                f"reference target 1 observed via {source_1} (research lifecycle)",
            ),
        ),
    )
