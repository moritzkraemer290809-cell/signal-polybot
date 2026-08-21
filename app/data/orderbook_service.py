"""In-memory order book model per instrument (snapshot + ordered deltas).

Pure data structure - no I/O, no strategy logic.  Later phases (slippage,
liquidity, strategy) consume this model read-only; they are NOT implemented
here.

Consistency rules:
- the book is only reliable after a snapshot has been applied
- deltas must arrive with monotonically increasing sequence numbers (when the
  feed provides them); a gap marks the book unreliable and requests a resync
- a crossed book (best ask < best bid) after any update marks the book
  unreliable and requests a resync
- while unreliable, the book never reports FRESH state
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from app.data.event_validation import BookMessage
from app.domain.models import BookLevel, ms_to_utc


@dataclass(frozen=True)
class BookTop:
    """Derived top-of-book metrics of a reliable order book."""

    best_bid: BookLevel | None
    best_ask: BookLevel | None
    mid_price: Decimal | None
    spread_abs: Decimal | None
    spread_bps: Decimal | None


@dataclass
class OrderbookState:
    """Mutable per-instrument order book."""

    instrument_id: int
    initialized: bool = False
    needs_resync: bool = False
    resync_count: int = 0
    sequence_gaps: int = 0
    last_sequence: int | None = None
    last_reliable_update: datetime | None = None
    last_snapshot_at: datetime | None = None
    _bids: dict[Decimal, Decimal] = field(default_factory=dict)
    _asks: dict[Decimal, Decimal] = field(default_factory=dict)

    # ------------------------------------------------------------ lifecycle

    @property
    def reliable(self) -> bool:
        return self.initialized and not self.needs_resync

    def mark_unreliable(self) -> None:
        if not self.needs_resync:
            self.needs_resync = True

    def reset_for_resync(self) -> None:
        """Forget all state; awaiting a fresh snapshot."""
        self._bids.clear()
        self._asks.clear()
        self.initialized = False
        self.last_sequence = None

    def apply_snapshot(self, message: BookMessage) -> bool:
        """Initialise (or re-initialise) the book from a full snapshot."""
        self._bids = {level.price: level.quantity for level in message.bids if level.quantity > 0}
        self._asks = {level.price: level.quantity for level in message.asks if level.quantity > 0}
        self.last_sequence = message.sequence
        self.initialized = True
        was_resyncing = self.needs_resync
        self.needs_resync = False
        ts = ms_to_utc(message.ts_ms)
        self.last_snapshot_at = ts
        if self._crossed():
            self.mark_unreliable()
            return False
        if was_resyncing:
            self.resync_count += 1
        self.last_reliable_update = ts
        return True

    def apply_delta(self, message: BookMessage) -> bool:
        """Apply an incremental update.  Returns True when the book stays reliable."""
        if not self.initialized:
            # delta before snapshot: cannot build a book from it
            self.mark_unreliable()
            return False
        if self.needs_resync:
            return False
        if message.sequence is not None and self.last_sequence is not None:
            if message.sequence <= self.last_sequence:
                # duplicate/out-of-date delta: ignore, book stays reliable
                return True
            if message.sequence != self.last_sequence + 1:
                self.sequence_gaps += 1
                self.mark_unreliable()
                return False
        if message.sequence is not None:
            self.last_sequence = message.sequence

        for level in message.bids:
            if level.quantity == 0:
                self._bids.pop(level.price, None)
            else:
                self._bids[level.price] = level.quantity
        for level in message.asks:
            if level.quantity == 0:
                self._asks.pop(level.price, None)
            else:
                self._asks[level.price] = level.quantity

        if self._crossed():
            self.mark_unreliable()
            return False
        self.last_reliable_update = ms_to_utc(message.ts_ms)
        return True

    # ------------------------------------------------------------- derived

    def _crossed(self) -> bool:
        if not self._bids or not self._asks:
            return False
        return min(self._asks) < max(self._bids)

    @property
    def best_bid(self) -> BookLevel | None:
        if not self._bids:
            return None
        price = max(self._bids)
        return BookLevel(price=price, quantity=self._bids[price])

    @property
    def best_ask(self) -> BookLevel | None:
        if not self._asks:
            return None
        price = min(self._asks)
        return BookLevel(price=price, quantity=self._asks[price])

    @property
    def mid_price(self) -> Decimal | None:
        bid, ask = self.best_bid, self.best_ask
        if bid is None or ask is None:
            return None
        return (bid.price + ask.price) / 2

    @property
    def spread_abs(self) -> Decimal | None:
        bid, ask = self.best_bid, self.best_ask
        if bid is None or ask is None:
            return None
        return ask.price - bid.price

    @property
    def spread_bps(self) -> Decimal | None:
        mid = self.mid_price
        spread = self.spread_abs
        if mid is None or spread is None or mid == 0:
            return None
        return spread / mid * Decimal(10_000)

    def top(self) -> BookTop:
        return BookTop(
            best_bid=self.best_bid,
            best_ask=self.best_ask,
            mid_price=self.mid_price,
            spread_abs=self.spread_abs,
            spread_bps=self.spread_bps,
        )

    def depth_within_bps(self, side: str, bps: Decimal) -> Decimal:
        """Cumulative quantity within ``bps`` of the mid price on one side."""
        mid = self.mid_price
        if mid is None or mid == 0:
            return Decimal(0)
        threshold = mid * bps / Decimal(10_000)
        if side == "bid":
            return sum(
                (qty for price, qty in self._bids.items() if mid - price <= threshold),
                Decimal(0),
            )
        if side == "ask":
            return sum(
                (qty for price, qty in self._asks.items() if price - mid <= threshold),
                Decimal(0),
            )
        raise ValueError(f"unknown side {side!r}")

    def depth_top_levels(self, side: str, levels: int) -> Decimal:
        """Cumulative quantity of the best ``levels`` price levels on one side."""
        if side == "bid":
            prices = sorted(self._bids, reverse=True)[:levels]
            return sum((self._bids[price] for price in prices), Decimal(0))
        if side == "ask":
            prices = sorted(self._asks)[:levels]
            return sum((self._asks[price] for price in prices), Decimal(0))
        raise ValueError(f"unknown side {side!r}")

    def levels(self, side: str, count: int) -> list[BookLevel]:
        if side == "bid":
            prices = sorted(self._bids, reverse=True)[:count]
            return [BookLevel(price=price, quantity=self._bids[price]) for price in prices]
        if side == "ask":
            prices = sorted(self._asks)[:count]
            return [BookLevel(price=price, quantity=self._asks[price]) for price in prices]
        raise ValueError(f"unknown side {side!r}")


class OrderbookManager:
    """Holds one :class:`OrderbookState` per instrument and routes messages."""

    def __init__(self) -> None:
        self._books: dict[int, OrderbookState] = {}

    def book(self, instrument_id: int) -> OrderbookState:
        if instrument_id not in self._books:
            self._books[instrument_id] = OrderbookState(instrument_id=instrument_id)
        return self._books[instrument_id]

    def apply(self, message: BookMessage) -> bool:
        """Route a validated book message.  Returns book reliability afterwards."""
        book = self.book(message.instrument_id)
        if message.is_snapshot:
            return book.apply_snapshot(message)
        return book.apply_delta(message)

    def books_needing_resync(self) -> list[int]:
        return [book.instrument_id for book in self._books.values() if book.needs_resync]

    def drop(self, instrument_id: int) -> None:
        self._books.pop(instrument_id, None)

    def all_books(self) -> list[OrderbookState]:
        return list(self._books.values())
