"""Market structure from confirmed swings: HH/HL/LH/LL, EQH/EQL, BOS, ChoCh.

Structure never breaks on a single unconfirmed wick: BOS/ChoCh require the
configured number of confirming candle CLOSES beyond the reference swing.
"""

from __future__ import annotations

import itertools

from app.strategy.enums import StructureEventType, StructureState, SwingType
from app.strategy.models import CandleSeries, StructureAnalysis, StructureEvent, Swing


def _relation_events(swings: list[Swing], tolerance_bps: float) -> list[StructureEvent]:
    events: list[StructureEvent] = []
    highs = [swing for swing in swings if swing.swing_type is SwingType.HIGH]
    lows = [swing for swing in swings if swing.swing_type is SwingType.LOW]

    def relate(
        sequence: list[Swing],
        up: StructureEventType,
        down: StructureEventType,
        equal: StructureEventType,
    ) -> None:
        for previous, current in itertools.pairwise(sequence):
            if previous.price <= 0:
                continue
            delta_bps = (current.price - previous.price) / previous.price * 10_000
            if abs(delta_bps) <= tolerance_bps:
                event_type = equal
                detail = f"equal level within {tolerance_bps}bps"
            elif delta_bps > 0:
                event_type = up
                detail = f"+{delta_bps:.1f}bps vs previous swing"
            else:
                event_type = down
                detail = f"{delta_bps:.1f}bps vs previous swing"
            events.append(
                StructureEvent(
                    event_type=event_type,
                    timeframe=current.timeframe,
                    price=current.price,
                    reference_swing_id=previous.swing_id,
                    confirm_close_time=current.confirmed_at,
                    detail=detail,
                )
            )

    relate(
        highs,
        StructureEventType.HIGHER_HIGH,
        StructureEventType.LOWER_HIGH,
        StructureEventType.EQUAL_HIGH,
    )
    relate(
        lows,
        StructureEventType.HIGHER_LOW,
        StructureEventType.LOWER_LOW,
        StructureEventType.EQUAL_LOW,
    )
    events.sort(key=lambda event: event.confirm_close_time)
    return events


def _break_events(
    series: CandleSeries, swings: list[Swing], confirmation_closes: int, state: StructureState
) -> list[StructureEvent]:
    """BOS: close(s) beyond the latest confirmed swing in trend direction.
    ChoCh: close(s) beyond the latest confirmed counter-swing."""
    events: list[StructureEvent] = []
    highs = [swing for swing in swings if swing.swing_type is SwingType.HIGH]
    lows = [swing for swing in swings if swing.swing_type is SwingType.LOW]
    candles = series.candles

    def closes_beyond(price: float, above: bool, since_index: int) -> StructureEvent | None:
        count = 0
        for candle in candles[since_index:]:
            beyond = candle.close > price if above else candle.close < price
            if beyond:
                count += 1
                if count >= confirmation_closes:
                    return StructureEvent(
                        event_type=StructureEventType.BREAK_OF_STRUCTURE,
                        timeframe=series.timeframe,
                        price=price,
                        reference_swing_id=None,
                        confirm_close_time=candle.close_time,
                        detail=(f"{count} close(s) {'above' if above else 'below'} {price:.6g}"),
                    )
            else:
                count = 0
        return None

    def index_after(swing: Swing) -> int:
        for index, candle in enumerate(candles):
            if candle.close_time > swing.confirmed_at:
                return index
        return len(candles)

    if highs:
        last_high = highs[-1]
        event = closes_beyond(last_high.price, above=True, since_index=index_after(last_high))
        if event is not None:
            events.append(
                event
                if state is not StructureState.BEARISH
                else StructureEvent(
                    event_type=StructureEventType.CHANGE_OF_CHARACTER,
                    timeframe=event.timeframe,
                    price=event.price,
                    reference_swing_id=last_high.swing_id,
                    confirm_close_time=event.confirm_close_time,
                    detail=f"bullish ChoCh: {event.detail}",
                )
            )
    if lows:
        last_low = lows[-1]
        event = closes_beyond(last_low.price, above=False, since_index=index_after(last_low))
        if event is not None:
            events.append(
                event
                if state is not StructureState.BULLISH
                else StructureEvent(
                    event_type=StructureEventType.CHANGE_OF_CHARACTER,
                    timeframe=event.timeframe,
                    price=event.price,
                    reference_swing_id=last_low.swing_id,
                    confirm_close_time=event.confirm_close_time,
                    detail=f"bearish ChoCh: {event.detail}",
                )
            )
    return events


def _classify_state(events: list[StructureEvent]) -> tuple[StructureState, str]:
    recent = [
        event
        for event in events
        if event.event_type
        in (
            StructureEventType.HIGHER_HIGH,
            StructureEventType.HIGHER_LOW,
            StructureEventType.LOWER_HIGH,
            StructureEventType.LOWER_LOW,
        )
    ][-4:]
    if len(recent) < 2:
        return StructureState.INSUFFICIENT_STRUCTURE, "fewer than two swing relations"
    bullish = sum(
        1
        for event in recent
        if event.event_type in (StructureEventType.HIGHER_HIGH, StructureEventType.HIGHER_LOW)
    )
    bearish = sum(
        1
        for event in recent
        if event.event_type in (StructureEventType.LOWER_HIGH, StructureEventType.LOWER_LOW)
    )
    if bullish and not bearish:
        return StructureState.BULLISH, f"{bullish} bullish relations, no bearish"
    if bearish and not bullish:
        return StructureState.BEARISH, f"{bearish} bearish relations, no bullish"
    if bullish and bearish:
        return StructureState.TRANSITIONAL, f"mixed relations ({bullish} bull / {bearish} bear)"
    return StructureState.NEUTRAL, "only equal levels in recent structure"


def analyze_structure(
    series: CandleSeries,
    swings: list[Swing],
    *,
    equal_tolerance_bps: float,
    confirmation_closes: int,
) -> StructureAnalysis:
    relation_events = _relation_events(swings, equal_tolerance_bps)
    state, detail = _classify_state(relation_events)
    break_events = _break_events(series, swings, confirmation_closes, state)
    all_events = sorted(
        [*relation_events, *break_events], key=lambda event: event.confirm_close_time
    )
    # a confirmed ChoCh flips the state to TRANSITIONAL
    if any(event.event_type is StructureEventType.CHANGE_OF_CHARACTER for event in break_events):
        state, detail = StructureState.TRANSITIONAL, "change of character confirmed"
    return StructureAnalysis(
        state=state, events=tuple(all_events), swings=tuple(swings), detail=detail
    )
