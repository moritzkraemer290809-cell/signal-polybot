"""Entry trigger evaluation - technical zone conditions, never fills.

"Entry trigger" means exclusively: market conditions touched/confirmed the
plan's technical reference entry zone.  It never means a user was executed
or a position exists.  Candle-based confirmations carry an explicit
approximation flag because intrabar order is not observable from candles.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.config import SignalLifecycleSettings
from app.signals.enums import EntryTriggerType
from app.signals.models import MarketStateSnapshot, SignalSnapshot

CANDLE_APPROXIMATION_FLAG = "CANDLE_APPROXIMATED_INTRABAR_ORDER"

#: candidate-type specific triggers (derived from the phase-8/9 context);
#: anything unknown falls back to the configured conservative default
_TRIGGER_BY_CANDIDATE_TYPE = {
    "BULLISH_SWEEP_REVERSAL": EntryTriggerType.RECLAIM_LEVEL_CLOSE_CONFIRM,
    "BEARISH_SWEEP_REVERSAL": EntryTriggerType.RECLAIM_LEVEL_CLOSE_CONFIRM,
    "BULLISH_RECLAIM_CONTINUATION": EntryTriggerType.RETEST_CONFIRM,
    "BEARISH_REJECTION_CONTINUATION": EntryTriggerType.RETEST_CONFIRM,
    "RANGE_BREAKOUT_UP": EntryTriggerType.BREAKOUT_CLOSE_CONFIRM,
    "RANGE_BREAKOUT_DOWN": EntryTriggerType.BREAKOUT_CLOSE_CONFIRM,
}


def default_trigger_for(candidate_type: str, settings: SignalLifecycleSettings) -> EntryTriggerType:
    if not settings.entry_close_confirmation_required:
        return EntryTriggerType.ZONE_TOUCH
    return _TRIGGER_BY_CANDIDATE_TYPE.get(
        candidate_type, EntryTriggerType(settings.entry_trigger_default)
    )


@dataclass(frozen=True)
class TriggerEvaluation:
    triggered: bool
    touched: bool
    reference_price: float | None
    source: str
    confidence: float
    detail: str
    approximation_flags: tuple[str, ...] = field(default=())


def _zone_bounds(signal: SignalSnapshot, tolerance_bps: float) -> tuple[float, float]:
    tolerance = signal.entry_reference_price * tolerance_bps / 10_000
    return signal.entry_low - tolerance, signal.entry_high + tolerance


def _zone_touch(
    signal: SignalSnapshot, market: MarketStateSnapshot, low: float, high: float
) -> tuple[bool, float | None, str]:
    """Conservative executable price inside the zone (never last price)."""
    executable = market.best_ask if signal.bullish else market.best_bid
    if market.bbo_fresh and executable is not None and low <= executable <= high:
        return True, executable, "BBO"
    if market.mark_price is not None and low <= market.mark_price <= high:
        return True, executable if executable is not None else market.mark_price, "MARK_PRICE"
    return False, None, "NONE"


def _closed_candle_available(market: MarketStateSnapshot) -> bool:
    return (
        market.last_closed_5m_close is not None
        and market.last_closed_5m_low is not None
        and market.last_closed_5m_high is not None
        and market.last_closed_5m_close_time is not None
    )


def evaluate_entry_trigger(
    signal: SignalSnapshot,
    market: MarketStateSnapshot,
    trigger: EntryTriggerType,
    settings: SignalLifecycleSettings,
) -> TriggerEvaluation:
    low, high = _zone_bounds(signal, settings.entry_zone_tolerance_bps)
    touched, executable, source = _zone_touch(signal, market, low, high)

    if trigger is EntryTriggerType.ZONE_TOUCH:
        if touched and source == "BBO":
            return TriggerEvaluation(
                triggered=True,
                touched=True,
                reference_price=executable,
                source=source,
                confidence=0.7,
                detail="fresh BBO inside the technical reference entry zone",
            )
        return TriggerEvaluation(
            triggered=False,
            touched=touched,
            reference_price=executable,
            source=source,
            confidence=0.0,
            detail="zone not touched by fresh BBO",
        )

    # all remaining triggers require a CLOSED 5m candle - open candles and
    # unprovable intrabar sequences never confirm anything
    if not _closed_candle_available(market):
        return TriggerEvaluation(
            triggered=False,
            touched=touched,
            reference_price=executable,
            source=source,
            confidence=0.0,
            detail="no fresh closed 5m candle for close confirmation",
        )
    close = float(market.last_closed_5m_close)  # type: ignore[arg-type]
    candle_low = float(market.last_closed_5m_low)  # type: ignore[arg-type]
    candle_high = float(market.last_closed_5m_high)  # type: ignore[arg-type]
    bullish = signal.bullish
    anchor = signal.entry_low if bullish else signal.entry_high

    if trigger is EntryTriggerType.ZONE_TOUCH_AND_5M_CLOSE_CONFIRM:
        candle_touched = candle_low <= high and candle_high >= low
        confirmed = candle_touched and low <= close <= high
        detail = (
            "closed 5m candle touched the zone and closed inside it"
            if confirmed
            else "no closed 5m candle confirming the zone"
        )
    elif trigger is EntryTriggerType.RECLAIM_LEVEL_CLOSE_CONFIRM:
        confirmed = (
            close >= anchor and close <= high if bullish else close <= anchor and close >= low
        )
        detail = (
            "closed 5m candle reclaimed the technical anchor level"
            if confirmed
            else "no close-confirmed reclaim of the anchor level"
        )
    elif trigger is EntryTriggerType.RETEST_CONFIRM:
        if bullish:
            confirmed = candle_low <= signal.entry_high and anchor <= close <= high
        else:
            confirmed = candle_high >= signal.entry_low and low <= close <= anchor
        detail = (
            "closed 5m candle retested the zone and closed back on the valid side"
            if confirmed
            else "no close-confirmed retest of the zone"
        )
    elif trigger is EntryTriggerType.BREAKOUT_CLOSE_CONFIRM:
        confirmed = close >= anchor if bullish else close <= anchor
        confirmed = confirmed and (low <= close <= high)
        detail = (
            "closed 5m candle confirmed the breakout level"
            if confirmed
            else "no close-confirmed breakout of the level"
        )
    else:  # pragma: no cover - enum is closed
        confirmed, detail = False, f"unsupported trigger {trigger}"

    return TriggerEvaluation(
        triggered=confirmed,
        touched=touched or confirmed,
        reference_price=executable if executable is not None else close,
        source="5M_CLOSE" if confirmed else source,
        confidence=0.8 if confirmed else 0.0,
        detail=detail,
        approximation_flags=(CANDLE_APPROXIMATION_FLAG,) if confirmed else (),
    )
