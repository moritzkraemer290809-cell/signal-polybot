"""Liquidity sweep detection (candle-approximated, confidence-aware).

A sweep = intrabar move beyond an identified liquidity level followed by a
defined reaction (close back on the original side).  At candle resolution
the intrabar sequence is approximated: confidence is reduced accordingly and
documented.  A sweep alone never creates a candidate.
"""

from __future__ import annotations

from app.strategy.enums import LiquidityLevelType, SweepDirection
from app.strategy.models import CandleSeries, LiquidityLevel, SweepEvent

_BELOW_TYPES = (
    LiquidityLevelType.SWING_LOW,
    LiquidityLevelType.EQUAL_LOWS,
    LiquidityLevelType.RANGE_LOW,
)
_LOOKBACK_CANDLES = 8


def detect_sweeps(
    series: CandleSeries,
    levels: list[LiquidityLevel],
    *,
    min_overshoot_bps: float,
) -> list[SweepEvent]:
    events: list[SweepEvent] = []
    candles = series.candles[-_LOOKBACK_CANDLES:]
    for level in levels:
        if level.price <= 0:
            continue
        below = level.level_type in _BELOW_TYPES
        for index, candle in enumerate(candles):
            if below:
                overshoot_bps = (level.price - candle.low) / level.price * 10_000
                pierced = candle.low < level.price and overshoot_bps >= min_overshoot_bps
                closed_back = candle.close > level.price
            else:
                overshoot_bps = (candle.high - level.price) / level.price * 10_000
                pierced = candle.high > level.price and overshoot_bps >= min_overshoot_bps
                closed_back = candle.close < level.price
            if not pierced:
                continue
            # reaction: same candle closes back, or a following closed candle does
            reacted = closed_back
            confirm_offset = 0
            if not reacted:
                for offset, later in enumerate(candles[index + 1 :], start=1):
                    later_back = later.close > level.price if below else later.close < level.price
                    if later_back:
                        reacted = True
                        confirm_offset = offset
                        break
            confidence = 0.9 if reacted and confirm_offset == 0 else (0.7 if reacted else 0.3)
            # candle-approximated intrabar order -> cap confidence
            confidence = min(confidence, 0.9)
            events.append(
                SweepEvent(
                    level=level,
                    direction=SweepDirection.BELOW if below else SweepDirection.ABOVE,
                    sweep_candle_open_time=candle.open_time,
                    overshoot_bps=overshoot_bps,
                    reacted=reacted,
                    confirmed=reacted,
                    confidence=confidence,
                    detail=(
                        f"sweep of {level.level_type.value}@{level.price:.6g} "
                        f"overshoot={overshoot_bps:.1f}bps "
                        f"(candle-approximated intrabar order)"
                    ),
                )
            )
            break  # first sweep per level in the window
    events.sort(key=lambda event: event.sweep_candle_open_time)
    return events
