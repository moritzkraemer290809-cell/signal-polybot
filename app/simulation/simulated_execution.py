"""Modelled leg execution via conservative order book walking.

Reuses the phase-9 cost engine (VWAP walk, fee resolution, stress
buffers) instead of duplicating it.  The result is a MODEL price: what a
conservative taker-side walk of the public book WOULD have implied at the
reference instant.  It is never a fill, never an execution and nobody
transacted at it.

Sides (adverse by construction):
- bullish entry consumes the ask, bullish exit consumes the bid
- bearish entry consumes the bid, bearish exit consumes the ask
"""

from __future__ import annotations

import uuid

from app.config import CostSettings, ShadowSimulationSettings
from app.costs.enums import ExecutionMode
from app.costs.execution_assumptions import stress_bps_for
from app.costs.fee_resolver import fee_cost
from app.costs.models import FeeScheduleSnapshot
from app.costs.orderbook_vwap import walk_book
from app.simulation.enums import SimulationLegKind, SimulationRejectionCode
from app.simulation.models import MarketReferenceSnapshot, ModelledExecution


class ExecutionModelError(Exception):
    """A modelled leg could not be derived from the public book."""

    def __init__(self, code: SimulationRejectionCode, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


def side_for(*, entering: bool, bullish: bool) -> str:
    """The book side a conservative model would consume."""
    buying = entering if bullish else not entering
    return "ask" if buying else "bid"


def _adverse_sign(*, entering: bool, bullish: bool) -> float:
    buying = entering == bullish
    return 1.0 if buying else -1.0


def model_execution(
    *,
    leg: SimulationLegKind,
    entering: bool,
    bullish: bool,
    quantity: float,
    market: MarketReferenceSnapshot,
    schedule: FeeScheduleSnapshot,
    mode: ExecutionMode,
    cost_settings: CostSettings,
    shadow_settings: ShadowSimulationSettings,
    max_staleness_seconds: float,
    stale_code: SimulationRejectionCode,
    depth_code: SimulationRejectionCode,
) -> ModelledExecution:
    """Model one leg from a public book snapshot.

    Raises :class:`ExecutionModelError` when the book is stale or too thin -
    a hypothetical result is never built on partially executable depth.
    """
    if quantity <= 0:
        raise ExecutionModelError(
            SimulationRejectionCode.CONFIGURATION_INVALID,
            f"{leg.value}: non-positive modelled quantity",
        )
    staleness = market.staleness_seconds(market.as_of)
    if not market.book.fresh or staleness is None or staleness > max_staleness_seconds:
        raise ExecutionModelError(
            stale_code,
            f"{leg.value}: public book not fresh enough at the reference instant "
            f"(staleness {staleness if staleness is not None else float('nan'):.1f}s > "
            f"{max_staleness_seconds:.1f}s)",
        )

    side = side_for(entering=entering, bullish=bullish)
    vwap = walk_book(
        market.book,
        side,
        quantity,
        max_levels=cost_settings.orderbook_max_levels,
        max_distance_bps=cost_settings.orderbook_max_distance_bps,
    )
    if not vwap.fully_filled or vwap.vwap_price is None or vwap.reference_price is None:
        raise ExecutionModelError(
            depth_code,
            f"{leg.value}: only {vwap.filled_quantity:.8g} of {quantity:.8g} units modellable "
            f"within {cost_settings.orderbook_max_levels} levels / "
            f"{cost_settings.orderbook_max_distance_bps:.0f}bps ({vwap.detail})",
        )

    cost_leg = "entry" if entering else "invalidation"
    stress_bps = stress_bps_for(cost_settings, cost_leg, mode)
    adverse = _adverse_sign(entering=entering, bullish=bullish)
    # the walk already carries the depth penalty; the stress buffer is added
    # on top, always against the modelled position, never as an improvement
    modelled_price = vwap.vwap_price * (1.0 + adverse * stress_bps / 10_000)
    if modelled_price <= 0:
        raise ExecutionModelError(
            SimulationRejectionCode.SIMULATION_ERROR,
            f"{leg.value}: degenerate modelled price",
        )
    top_of_book = vwap.reference_price
    slippage_abs = abs(modelled_price - top_of_book)
    slippage_bps = slippage_abs / top_of_book * 10_000 if top_of_book > 0 else 0.0
    return ModelledExecution(
        execution_id=uuid.uuid4(),
        leg=leg,
        side_consumed=side,
        modelled_price=modelled_price,
        quantity=quantity,
        notional=modelled_price * quantity,
        reference_price=top_of_book,
        slippage_cost=slippage_abs * quantity,
        slippage_bps=slippage_bps,
        fee_cost=fee_cost(schedule, mode, modelled_price, quantity),
        levels_consumed=vwap.levels_consumed,
        book_snapshot_id=market.snapshot_id,
        book_at=market.book_at,
        as_of=market.as_of,
        assumptions={
            "execution_mode": mode.value,
            "stress_bps": stress_bps,
            "depth_slippage_bps": vwap.slippage_bps,
            "fee_schedule_version": schedule.version,
            "book_staleness_seconds": round(staleness, 3),
            "note": (
                "modelled book walk on public data - hypothetical reference price, "
                "no execution and no position"
            ),
        },
    )
