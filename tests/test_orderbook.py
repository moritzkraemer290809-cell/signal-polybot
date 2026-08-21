"""In-memory order book: snapshot, deltas, sequence gaps, resync, metrics."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.data.event_validation import validate_book_message
from app.data.orderbook_service import OrderbookManager, OrderbookState

TS = 1766120400000


def snapshot_message(sequence: int = 1000):
    return validate_book_message(
        {
            "instrument_id": 1,
            "type": "snapshot",
            "bids": [["100.00", "1.0"], ["99.50", "2.0"], ["99.00", "3.0"]],
            "asks": [["100.10", "1.5"], ["100.50", "2.5"], ["101.00", "4.0"]],
            "timestamp": TS,
            "sequence": sequence,
        }
    ).parsed


def delta_message(sequence: int, bids=None, asks=None, ts: int = TS + 1000):
    return validate_book_message(
        {
            "instrument_id": 1,
            "bids": bids or [],
            "asks": asks or [],
            "timestamp": ts,
            "sequence": sequence,
        }
    ).parsed


def test_snapshot_initialises_book() -> None:
    book = OrderbookState(instrument_id=1)
    assert not book.reliable
    assert book.apply_snapshot(snapshot_message())
    assert book.reliable
    assert book.best_bid.price == Decimal("100.00")
    assert book.best_ask.price == Decimal("100.10")
    assert book.mid_price == Decimal("100.05")
    assert book.spread_abs == Decimal("0.10")
    assert book.spread_bps == pytest.approx(Decimal("9.995"), rel=Decimal("0.001"))


def test_delta_updates_and_removes_levels() -> None:
    book = OrderbookState(instrument_id=1)
    book.apply_snapshot(snapshot_message())
    ok = book.apply_delta(
        delta_message(1001, bids=[["100.00", "0.5"], ["99.50", "0"]], asks=[["100.10", "2.0"]])
    )
    assert ok and book.reliable
    assert book.best_bid.quantity == Decimal("0.5")
    assert book.depth_top_levels("bid", 5) == Decimal("3.5")  # 0.5 + 3.0 (99.50 removed)
    assert book.best_ask.quantity == Decimal("2.0")


def test_delta_before_snapshot_marks_resync() -> None:
    book = OrderbookState(instrument_id=1)
    assert not book.apply_delta(delta_message(5, bids=[["100.00", "1"]]))
    assert book.needs_resync and not book.reliable


def test_sequence_gap_triggers_resync() -> None:
    book = OrderbookState(instrument_id=1)
    book.apply_snapshot(snapshot_message(sequence=1000))
    assert not book.apply_delta(delta_message(1005, bids=[["100.00", "1"]]))
    assert book.needs_resync
    assert book.sequence_gaps == 1
    # while resyncing, further deltas are ignored
    assert not book.apply_delta(delta_message(1006, bids=[["100.00", "1"]]))


def test_duplicate_sequence_is_ignored_but_reliable() -> None:
    book = OrderbookState(instrument_id=1)
    book.apply_snapshot(snapshot_message(sequence=1000))
    assert book.apply_delta(delta_message(1000, bids=[["1.00", "9"]]))  # stale: ignored
    assert book.best_bid.price == Decimal("100.00")
    assert book.reliable


def test_crossed_book_after_delta_triggers_resync() -> None:
    book = OrderbookState(instrument_id=1)
    book.apply_snapshot(snapshot_message(sequence=1000))
    assert not book.apply_delta(delta_message(1001, bids=[["100.50", "1"]]))
    assert book.needs_resync


def test_resync_flow_restores_reliability() -> None:
    book = OrderbookState(instrument_id=1)
    book.apply_snapshot(snapshot_message(sequence=1000))
    book.apply_delta(delta_message(1005))  # gap
    assert book.needs_resync
    book.reset_for_resync()
    assert not book.initialized
    assert book.apply_snapshot(snapshot_message(sequence=2000))
    assert book.reliable
    assert book.resync_count == 1
    assert book.last_snapshot_at is not None


def test_crossed_snapshot_is_rejected() -> None:
    book = OrderbookState(instrument_id=1)
    message = validate_book_message(
        {
            "instrument_id": 1,
            "type": "snapshot",
            # crossed inside the snapshot payload itself is caught by
            # validation; simulate a subtle case via direct construction:
            "bids": [["100.00", "1.0"]],
            "asks": [["100.10", "1.0"]],
            "timestamp": TS,
            "sequence": 1,
        }
    ).parsed
    # tamper: force crossed book by removing the ask and adding a lower one
    from app.domain.models import BookLevel

    crossed = message.__class__(
        instrument_id=1,
        is_snapshot=True,
        bids=(BookLevel(price=Decimal("100.20"), quantity=Decimal("1")),),
        asks=(BookLevel(price=Decimal("100.10"), quantity=Decimal("1")),),
        ts_ms=TS,
        sequence=1,
    )
    assert not book.apply_snapshot(crossed)
    assert book.needs_resync


def test_depth_within_bps() -> None:
    book = OrderbookState(instrument_id=1)
    book.apply_snapshot(snapshot_message())
    # mid = 100.05; 50bps threshold = ~0.50025
    # bids within: 100.00 (1.0)          -- 99.50 is 0.55 away, excluded
    # asks within: 100.10 (1.5) + 100.50 (2.5) -- 101.00 is 0.95 away, excluded
    assert book.depth_within_bps("bid", Decimal(50)) == Decimal("1.0")
    assert book.depth_within_bps("ask", Decimal(50)) == Decimal("4.0")
    # wide threshold includes everything
    assert book.depth_within_bps("bid", Decimal(1000)) == Decimal("6.0")
    assert book.depth_within_bps("ask", Decimal(1000)) == Decimal("8.0")


def test_manager_routes_and_tracks_resyncs() -> None:
    manager = OrderbookManager()
    assert manager.apply(snapshot_message())
    assert not manager.apply(delta_message(2000, bids=[["100.00", "1"]]))  # gap
    assert manager.books_needing_resync() == [1]
    manager.drop(1)
    assert manager.books_needing_resync() == []
