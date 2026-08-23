"""Order book VWAP walking and conservative leg execution estimates."""

from __future__ import annotations

import pytest
from tests.risk_helpers import COST_SETTINGS, make_book, make_fee_schedule

from app.config import CostSettings
from app.costs.enums import ExecutionMode
from app.costs.models import CostModelError
from app.costs.orderbook_vwap import walk_book
from app.costs.slippage import estimate_leg

SCHEDULE = make_fee_schedule()


def test_walk_ask_side_full_depth() -> None:
    book = make_book(asks=[(100.0, 10.0), (100.1, 10.0), (100.2, 10.0)])
    result = walk_book(book, "ask", 15.0, max_levels=25, max_distance_bps=50)
    assert result.fully_filled
    assert result.levels_consumed == 2
    assert result.vwap_price == pytest.approx((10 * 100.0 + 5 * 100.1) / 15)
    assert result.reference_price == 100.0
    assert result.slippage_bps is not None and result.slippage_bps > 0


def test_walk_bid_side_full_depth() -> None:
    book = make_book(bids=[(99.9, 10.0), (99.8, 10.0)])
    result = walk_book(book, "bid", 12.0, max_levels=25, max_distance_bps=50)
    assert result.fully_filled
    assert result.vwap_price == pytest.approx((10 * 99.9 + 2 * 99.8) / 12)


def test_insufficient_depth_reports_unfilled() -> None:
    book = make_book(asks=[(100.0, 5.0)])
    result = walk_book(book, "ask", 20.0, max_levels=25, max_distance_bps=50)
    assert not result.fully_filled
    assert result.unfilled_quantity == pytest.approx(15.0)


def test_max_levels_and_distance_bound_the_walk() -> None:
    asks = [(100.0 + i * 0.05, 1.0) for i in range(30)]
    book = make_book(asks=asks)
    limited = walk_book(book, "ask", 30.0, max_levels=5, max_distance_bps=1000)
    assert limited.filled_quantity == pytest.approx(5.0)
    # 0.25% distance window cuts off levels beyond ~100.25
    distance_limited = walk_book(book, "ask", 30.0, max_levels=30, max_distance_bps=25)
    assert distance_limited.filled_quantity <= 6.0


def test_stale_book_is_never_walked() -> None:
    book = make_book(fresh=False)
    result = walk_book(book, "ask", 1.0, max_levels=25, max_distance_bps=50)
    assert not result.fully_filled
    assert result.vwap_price is None


def test_entry_leg_sides_per_direction() -> None:
    book = make_book(bids=[(99.5, 100.0)], asks=[(100.5, 100.0)])
    long_entry = estimate_leg(
        leg="entry",
        mode=ExecutionMode.ENTRY_TAKER,
        bullish=True,
        reference_price=100.5,
        quantity=10.0,
        book=book,
        schedule=SCHEDULE,
        settings=COST_SETTINGS,
    )
    assert long_entry.vwap.side == "ask"
    short_entry = estimate_leg(
        leg="entry",
        mode=ExecutionMode.ENTRY_TAKER,
        bullish=False,
        reference_price=99.5,
        quantity=10.0,
        book=book,
        schedule=SCHEDULE,
        settings=COST_SETTINGS,
    )
    assert short_entry.vwap.side == "bid"
    long_exit = estimate_leg(
        leg="invalidation",
        mode=ExecutionMode.STOP_STRESS_TAKER,
        bullish=True,
        reference_price=98.0,
        quantity=10.0,
        book=book,
        schedule=SCHEDULE,
        settings=COST_SETTINGS,
    )
    assert long_exit.vwap.side == "bid"
    short_exit = estimate_leg(
        leg="invalidation",
        mode=ExecutionMode.STOP_STRESS_TAKER,
        bullish=False,
        reference_price=101.0,
        quantity=10.0,
        book=book,
        schedule=SCHEDULE,
        settings=COST_SETTINGS,
    )
    assert short_exit.vwap.side == "ask"


def test_no_perfect_mid_price_execution() -> None:
    """A bullish entry executes at or above the reference, never at mid."""
    book = make_book(bids=[(99.0, 100.0)], asks=[(101.0, 100.0)])
    leg = estimate_leg(
        leg="entry",
        mode=ExecutionMode.ENTRY_TAKER,
        bullish=True,
        reference_price=101.0,
        quantity=1.0,
        book=book,
        schedule=SCHEDULE,
        settings=COST_SETTINGS,
    )
    assert leg.execution_price > 100.0  # above mid
    assert leg.execution_price >= 101.0  # at/above the ask reference
    assert leg.slippage_cost >= 0


def test_stop_stress_more_conservative_than_target() -> None:
    book = make_book(bids=[(99.0, 100.0)], asks=[(101.0, 100.0)])
    stop = estimate_leg(
        leg="invalidation",
        mode=ExecutionMode.STOP_STRESS_TAKER,
        bullish=True,
        reference_price=98.0,
        quantity=1.0,
        book=book,
        schedule=SCHEDULE,
        settings=COST_SETTINGS,
    )
    target = estimate_leg(
        leg="target",
        mode=ExecutionMode.TARGET_TAKER,
        bullish=True,
        reference_price=105.0,
        quantity=1.0,
        book=book,
        schedule=SCHEDULE,
        settings=COST_SETTINGS,
    )
    assert stop.stress_bps_applied > target.stress_bps_applied
    # stress is applied against the plan: stop exit below its reference
    assert stop.execution_price < 98.0
    assert target.execution_price < 105.0  # exit sells below the target level


def test_depth_insufficiency_rejects_the_leg() -> None:
    book = make_book(asks=[(100.0, 0.5)])
    with pytest.raises(CostModelError) as excinfo:
        estimate_leg(
            leg="entry",
            mode=ExecutionMode.ENTRY_TAKER,
            bullish=True,
            reference_price=100.0,
            quantity=10.0,
            book=book,
            schedule=SCHEDULE,
            settings=COST_SETTINGS,
        )
    assert excinfo.value.code == "ORDERBOOK_DEPTH_INSUFFICIENT"


def test_stale_book_rejects_the_leg() -> None:
    with pytest.raises(CostModelError) as excinfo:
        estimate_leg(
            leg="entry",
            mode=ExecutionMode.ENTRY_TAKER,
            bullish=True,
            reference_price=100.0,
            quantity=1.0,
            book=make_book(fresh=False),
            schedule=SCHEDULE,
            settings=COST_SETTINGS,
        )
    assert excinfo.value.code == "ORDERBOOK_NOT_FRESH"


def test_slippage_costs_reported_in_abs_and_bps() -> None:
    book = make_book(asks=[(100.0, 5.0), (100.2, 10.0)])
    leg = estimate_leg(
        leg="entry",
        mode=ExecutionMode.ENTRY_TAKER,
        bullish=True,
        reference_price=100.0,
        quantity=10.0,
        book=book,
        schedule=SCHEDULE,
        settings=COST_SETTINGS,
    )
    assert leg.vwap.slippage_bps == pytest.approx(10.0, rel=0.05)  # 0.1 avg on 100
    expected_exec = 100.0 * (1 + (leg.vwap.slippage_bps + 2.0) / 10_000)
    assert leg.execution_price == pytest.approx(expected_exec)
    assert leg.slippage_cost == pytest.approx((expected_exec - 100.0) * 10.0)


def test_slippage_disabled_rejects() -> None:
    disabled = CostSettings(_env_file=None, slippage_enabled=False)
    with pytest.raises(CostModelError) as excinfo:
        estimate_leg(
            leg="entry",
            mode=ExecutionMode.ENTRY_TAKER,
            bullish=True,
            reference_price=100.0,
            quantity=1.0,
            book=make_book(),
            schedule=SCHEDULE,
            settings=disabled,
        )
    assert excinfo.value.code == "SLIPPAGE_MODEL_UNAVAILABLE"
