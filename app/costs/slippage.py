"""Leg-level conservative execution estimates (VWAP + stress buffer + fees).

Sides per leg (never mid-price):
- bullish entry -> consume ask;  bullish target/invalidation exit -> consume bid
- bearish entry -> consume bid;  bearish target/invalidation exit -> consume ask
Stop exits carry an additional stress buffer applied AGAINST the plan.
"""

from __future__ import annotations

from app.config import CostSettings
from app.costs.enums import ExecutionMode
from app.costs.execution_assumptions import stress_bps_for
from app.costs.fee_resolver import fee_cost, fee_kind_for
from app.costs.models import (
    CostModelError,
    FeeScheduleSnapshot,
    LegExecutionEstimate,
    OrderbookDepthSnapshot,
)
from app.costs.orderbook_vwap import walk_book


def _side_for(leg: str, bullish: bool) -> str:
    entering = leg == "entry"
    buying = entering if bullish else not entering
    return "ask" if buying else "bid"


def _adverse_direction(leg: str, bullish: bool) -> float:
    """Sign of an ADVERSE price move for this leg (+1 = higher is worse)."""
    buying = (leg == "entry") == bullish
    return 1.0 if buying else -1.0


def estimate_leg(
    *,
    leg: str,
    mode: ExecutionMode,
    bullish: bool,
    reference_price: float,
    quantity: float,
    book: OrderbookDepthSnapshot,
    schedule: FeeScheduleSnapshot,
    settings: CostSettings,
) -> LegExecutionEstimate:
    """Conservative execution estimate for one hypothetical leg.

    Raises :class:`CostModelError` when the book cannot carry the reference
    quantity inside the configured window - a plan is never built on
    partially-executable assumptions.
    """
    if not settings.slippage_enabled:
        raise CostModelError("SLIPPAGE_MODEL_UNAVAILABLE", "slippage model disabled")
    if reference_price <= 0 or quantity <= 0:
        raise CostModelError("SLIPPAGE_MODEL_UNAVAILABLE", f"invalid {leg} reference inputs")
    if settings.require_fresh_orderbook and not book.fresh:
        raise CostModelError("ORDERBOOK_NOT_FRESH", f"book not fresh for {leg} estimate")

    side = _side_for(leg, bullish)
    vwap = walk_book(
        book,
        side,
        quantity,
        max_levels=settings.orderbook_max_levels,
        max_distance_bps=settings.orderbook_max_distance_bps,
    )
    if not vwap.fully_filled or vwap.vwap_price is None:
        raise CostModelError(
            "ORDERBOOK_DEPTH_INSUFFICIENT",
            f"{leg}: only {vwap.filled_quantity:.8g} of {quantity:.8g} executable "
            f"within {settings.orderbook_max_levels} levels / "
            f"{settings.orderbook_max_distance_bps:.0f}bps ({vwap.detail})",
        )

    stress_bps = stress_bps_for(settings, leg, mode)
    adverse = _adverse_direction(leg, bullish)
    # The walk yields the RELATIVE depth penalty of executing the reference
    # quantity (vs the side's best price).  That penalty plus the stress
    # buffer is applied to the LEG'S reference price: exit legs execute when
    # price has reached their level, so the depth shape - not the book's
    # current absolute location - is what carries over.  Always adverse,
    # never credited as improvement.
    depth_bps = vwap.slippage_bps or 0.0
    execution_price = reference_price * (1.0 + adverse * (depth_bps + stress_bps) / 10_000)
    if execution_price <= 0:
        raise CostModelError("SLIPPAGE_MODEL_UNAVAILABLE", f"{leg}: degenerate execution price")
    slippage_cost = abs(execution_price - reference_price) * quantity
    notional = execution_price * quantity
    return LegExecutionEstimate(
        leg=leg,
        mode=mode,
        fee_kind=fee_kind_for(mode),
        reference_price=reference_price,
        execution_price=execution_price,
        vwap=vwap,
        stress_bps_applied=stress_bps,
        slippage_cost=slippage_cost,
        fee_cost=fee_cost(schedule, mode, execution_price, quantity),
        notional=notional,
    )
