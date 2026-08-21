"""Validation and normalisation of incoming public WebSocket market events.

Every event is validated before any further processing.  Invalid events are
rejected with a structured reason; they never crash the process and never feed
market state.  Prices/quantities must be finite, positive decimals; BBO and
order book structures must not be crossed; candle timeframes must be known.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from app.domain.enums import Timeframe
from app.domain.models import BookLevel, CandleData, TickerData, TradeData, ms_to_utc

# structured rejection reasons (persisted in metrics / system events)
MALFORMED = "MALFORMED"
MISSING_FIELD = "MISSING_FIELD"
BAD_TIMESTAMP = "BAD_TIMESTAMP"
NON_FINITE_VALUE = "NON_FINITE_VALUE"
NON_POSITIVE_PRICE = "NON_POSITIVE_PRICE"
NEGATIVE_QUANTITY = "NEGATIVE_QUANTITY"
CROSSED_BBO = "CROSSED_BBO"
CROSSED_BOOK = "CROSSED_BOOK"
INVALID_TIMEFRAME = "INVALID_TIMEFRAME"
OUTLIER_PRICE = "OUTLIER_PRICE"
OUT_OF_ORDER = "OUT_OF_ORDER"
BAD_SEQUENCE = "BAD_SEQUENCE"


@dataclass(frozen=True)
class ValidationOutcome:
    ok: bool
    reason: str | None = None
    parsed: Any | None = None

    @classmethod
    def rejected(cls, reason: str) -> ValidationOutcome:
        return cls(ok=False, reason=reason)

    @classmethod
    def accepted(cls, parsed: Any) -> ValidationOutcome:
        return cls(ok=True, parsed=parsed)


@dataclass(frozen=True)
class BboUpdate:
    """Normalised best-bid/offer update."""

    instrument_id: int
    bid_price: Decimal
    bid_quantity: Decimal
    ask_price: Decimal
    ask_quantity: Decimal
    ts_ms: int

    @property
    def mid_price(self) -> Decimal:
        return (self.bid_price + self.ask_price) / 2

    @property
    def spread_bps(self) -> Decimal | None:
        mid = self.mid_price
        if mid == 0:
            return None
        return (self.ask_price - self.bid_price) / mid * Decimal(10_000)


@dataclass(frozen=True)
class BookMessage:
    """Normalised order book snapshot or delta."""

    instrument_id: int
    is_snapshot: bool
    bids: tuple[BookLevel, ...]
    asks: tuple[BookLevel, ...]
    ts_ms: int
    sequence: int | None


def _decimal(value: Any) -> Decimal | None:
    """Parse to a finite Decimal; None when missing/NaN/Infinity/garbage."""
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    if not result.is_finite():
        return None
    return result


def _timestamp_ms(value: Any) -> int | None:
    try:
        ts = int(value)
    except (TypeError, ValueError):
        return None
    # sanity bounds: 2001-09-09 .. 2286-11-20 in unix ms
    if ts < 1_000_000_000_000 or ts > 9_999_999_999_999:
        return None
    return ts


def _instrument_id(value: Any) -> int | None:
    try:
        instrument_id = int(value)
    except (TypeError, ValueError):
        return None
    return instrument_id if instrument_id > 0 else None


def deviation_bps(price: Decimal, reference: Decimal) -> Decimal:
    if reference == 0:
        return Decimal(0)
    return abs(price - reference) / reference * Decimal(10_000)


def _outlier(price: Decimal, reference: Decimal | None, max_deviation_bps: float) -> bool:
    if reference is None or reference <= 0:
        return False
    return deviation_bps(price, reference) > Decimal(str(max_deviation_bps))


def validate_ticker(
    payload: Any,
    *,
    reference_price: Decimal | None = None,
    max_deviation_bps: float = 500.0,
) -> ValidationOutcome:
    if not isinstance(payload, dict):
        return ValidationOutcome.rejected(MALFORMED)
    instrument_id = _instrument_id(payload.get("instrument_id"))
    if instrument_id is None:
        return ValidationOutcome.rejected(MISSING_FIELD)
    ts = _timestamp_ms(payload.get("timestamp"))
    if ts is None:
        return ValidationOutcome.rejected(BAD_TIMESTAMP)

    prices: dict[str, Decimal | None] = {}
    for key in ("mark_price", "index_price", "last_price", "mid_price"):
        raw = payload.get(key)
        if raw in (None, ""):
            prices[key] = None
            continue
        value = _decimal(raw)
        if value is None:
            return ValidationOutcome.rejected(NON_FINITE_VALUE)
        if value <= 0:
            return ValidationOutcome.rejected(NON_POSITIVE_PRICE)
        prices[key] = value

    mark = prices.get("mark_price")
    if mark is not None and _outlier(mark, reference_price, max_deviation_bps):
        return ValidationOutcome.rejected(OUTLIER_PRICE)

    for key in ("open_interest",):
        raw = payload.get(key)
        if raw not in (None, ""):
            value = _decimal(raw)
            if value is None:
                return ValidationOutcome.rejected(NON_FINITE_VALUE)
            if value < 0:
                return ValidationOutcome.rejected(NEGATIVE_QUANTITY)

    funding = payload.get("funding_rate")
    if funding not in (None, "") and _decimal(funding) is None:
        return ValidationOutcome.rejected(NON_FINITE_VALUE)

    try:
        ticker = TickerData.from_api(payload)
    except Exception:
        return ValidationOutcome.rejected(MALFORMED)
    return ValidationOutcome.accepted(ticker)


def validate_bbo(payload: Any) -> ValidationOutcome:
    if not isinstance(payload, dict):
        return ValidationOutcome.rejected(MALFORMED)
    instrument_id = _instrument_id(payload.get("instrument_id"))
    if instrument_id is None:
        return ValidationOutcome.rejected(MISSING_FIELD)
    ts = _timestamp_ms(payload.get("timestamp"))
    if ts is None:
        return ValidationOutcome.rejected(BAD_TIMESTAMP)

    # accept both {"bid": [p, q], "ask": [p, q]} and flat field variants
    if isinstance(payload.get("bid"), (list, tuple)) and isinstance(
        payload.get("ask"), (list, tuple)
    ):
        bid_raw, ask_raw = payload["bid"], payload["ask"]
        if len(bid_raw) < 2 or len(ask_raw) < 2:
            return ValidationOutcome.rejected(MISSING_FIELD)
        values = (bid_raw[0], bid_raw[1], ask_raw[0], ask_raw[1])
    else:
        values = (
            payload.get("bid_price"),
            payload.get("bid_quantity"),
            payload.get("ask_price"),
            payload.get("ask_quantity"),
        )
    parsed = [_decimal(value) for value in values]
    if any(value is None for value in parsed):
        return ValidationOutcome.rejected(NON_FINITE_VALUE)
    bid_price, bid_qty, ask_price, ask_qty = parsed
    assert (
        bid_price is not None
        and bid_qty is not None
        and ask_price is not None
        and ask_qty is not None
    )
    if bid_price <= 0 or ask_price <= 0:
        return ValidationOutcome.rejected(NON_POSITIVE_PRICE)
    if bid_qty < 0 or ask_qty < 0:
        return ValidationOutcome.rejected(NEGATIVE_QUANTITY)
    if ask_price < bid_price:
        return ValidationOutcome.rejected(CROSSED_BBO)
    return ValidationOutcome.accepted(
        BboUpdate(
            instrument_id=instrument_id,
            bid_price=bid_price,
            bid_quantity=bid_qty,
            ask_price=ask_price,
            ask_quantity=ask_qty,
            ts_ms=ts,
        )
    )


def validate_trade(
    payload: Any,
    *,
    reference_price: Decimal | None = None,
    max_deviation_bps: float = 500.0,
) -> ValidationOutcome:
    if not isinstance(payload, dict):
        return ValidationOutcome.rejected(MALFORMED)
    instrument_id = _instrument_id(payload.get("instrument_id"))
    if instrument_id is None:
        return ValidationOutcome.rejected(MISSING_FIELD)
    ts = _timestamp_ms(payload.get("timestamp"))
    if ts is None:
        return ValidationOutcome.rejected(BAD_TIMESTAMP)
    price = _decimal(payload.get("price"))
    quantity = _decimal(payload.get("quantity"))
    if price is None or quantity is None:
        return ValidationOutcome.rejected(NON_FINITE_VALUE)
    if price <= 0:
        return ValidationOutcome.rejected(NON_POSITIVE_PRICE)
    if quantity <= 0:
        return ValidationOutcome.rejected(NEGATIVE_QUANTITY)
    if _outlier(price, reference_price, max_deviation_bps):
        return ValidationOutcome.rejected(OUTLIER_PRICE)
    try:
        trade = TradeData.from_api(payload)
    except Exception:
        return ValidationOutcome.rejected(MALFORMED)
    return ValidationOutcome.accepted(trade)


def validate_kline(payload: Any, timeframe_value: str) -> ValidationOutcome:
    try:
        timeframe = Timeframe(timeframe_value)
    except ValueError:
        return ValidationOutcome.rejected(INVALID_TIMEFRAME)

    row: list[Any] | None = None
    if isinstance(payload, (list, tuple)) and len(payload) >= 6:
        row = list(payload)
    elif isinstance(payload, dict):
        if isinstance(payload.get("data"), (list, tuple)) and len(payload["data"]) >= 6:
            row = list(payload["data"])
        elif all(key in payload for key in ("timestamp", "open", "high", "low", "close")):
            row = [
                payload.get("timestamp"),
                payload.get("open"),
                payload.get("high"),
                payload.get("low"),
                payload.get("close"),
                payload.get("volume", "0"),
                payload.get("trades", 0),
            ]
    if row is None:
        return ValidationOutcome.rejected(MALFORMED)

    ts = _timestamp_ms(row[0])
    if ts is None:
        return ValidationOutcome.rejected(BAD_TIMESTAMP)
    ohlc = [_decimal(value) for value in row[1:5]]
    volume = _decimal(row[5])
    if any(value is None for value in ohlc) or volume is None:
        return ValidationOutcome.rejected(NON_FINITE_VALUE)
    if any(value <= 0 for value in ohlc if value is not None):
        return ValidationOutcome.rejected(NON_POSITIVE_PRICE)
    if volume < 0:
        return ValidationOutcome.rejected(NEGATIVE_QUANTITY)
    open_, high, low, close = ohlc
    assert high is not None and low is not None and open_ is not None and close is not None
    if high < low or not (low <= open_ <= high) or not (low <= close <= high):
        return ValidationOutcome.rejected(MALFORMED)
    try:
        candle = CandleData.from_api_row(row, timeframe)
    except Exception:
        return ValidationOutcome.rejected(MALFORMED)
    return ValidationOutcome.accepted(candle)


def _levels(raw: Any) -> tuple[BookLevel, ...] | None:
    if raw is None:
        return ()
    if not isinstance(raw, (list, tuple)):
        return None
    levels: list[BookLevel] = []
    for entry in raw:
        if not isinstance(entry, (list, tuple)) or len(entry) < 2:
            return None
        price = _decimal(entry[0])
        quantity = _decimal(entry[1])
        if price is None or quantity is None:
            return None
        if price <= 0 or quantity < 0:
            return None
        levels.append(BookLevel(price=price, quantity=quantity))
    return tuple(levels)


def validate_book_message(payload: Any, *, instrument_id: int | None = None) -> ValidationOutcome:
    """Validate an order book snapshot or delta message.

    Snapshot detection: ``type == "snapshot"`` or ``snapshot: true``.  A delta
    may contain zero quantities (level removal); a snapshot must not be
    crossed.  Cross checks after delta application happen in the book model.
    """
    if not isinstance(payload, dict):
        return ValidationOutcome.rejected(MALFORMED)
    resolved_id = _instrument_id(payload.get("instrument_id")) or instrument_id
    if resolved_id is None:
        return ValidationOutcome.rejected(MISSING_FIELD)
    ts = _timestamp_ms(payload.get("timestamp"))
    if ts is None:
        return ValidationOutcome.rejected(BAD_TIMESTAMP)
    sequence_raw = payload.get("sequence", payload.get("sq"))
    sequence: int | None = None
    if sequence_raw is not None:
        try:
            sequence = int(sequence_raw)
        except (TypeError, ValueError):
            return ValidationOutcome.rejected(BAD_SEQUENCE)

    bids = _levels(payload.get("bids"))
    asks = _levels(payload.get("asks"))
    if bids is None or asks is None:
        return ValidationOutcome.rejected(NEGATIVE_QUANTITY)

    is_snapshot = payload.get("type") == "snapshot" or payload.get("snapshot") is True
    if is_snapshot:
        best_bid = max((level.price for level in bids if level.quantity > 0), default=None)
        best_ask = min((level.price for level in asks if level.quantity > 0), default=None)
        if best_bid is not None and best_ask is not None and best_ask < best_bid:
            return ValidationOutcome.rejected(CROSSED_BOOK)

    return ValidationOutcome.accepted(
        BookMessage(
            instrument_id=resolved_id,
            is_snapshot=is_snapshot,
            bids=bids,
            asks=asks,
            ts_ms=ts,
            sequence=sequence,
        )
    )


__all__ = [
    "BAD_SEQUENCE",
    "BAD_TIMESTAMP",
    "CROSSED_BBO",
    "CROSSED_BOOK",
    "INVALID_TIMEFRAME",
    "MALFORMED",
    "MISSING_FIELD",
    "NEGATIVE_QUANTITY",
    "NON_FINITE_VALUE",
    "NON_POSITIVE_PRICE",
    "OUTLIER_PRICE",
    "OUT_OF_ORDER",
    "BboUpdate",
    "BookMessage",
    "ValidationOutcome",
    "deviation_bps",
    "ms_to_utc",
    "validate_bbo",
    "validate_book_message",
    "validate_kline",
    "validate_ticker",
    "validate_trade",
]
