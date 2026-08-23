"""Conservative order book VWAP walking for a hypothetical quantity.

Long entry walks the ask side, long invalidation exit walks the bid side;
short entry walks the bid side, short invalidation exit walks the ask side.
There is never a perfect mid-price execution assumption.  Depth beyond the
configured level count or price-distance window does not count.
"""

from __future__ import annotations

from app.costs.models import DepthLevel, OrderbookDepthSnapshot, VwapResult


def _within_distance(price: float, reference: float, max_distance_bps: float) -> bool:
    if reference <= 0:
        return False
    return abs(price - reference) / reference * 10_000 <= max_distance_bps


def walk_book(
    book: OrderbookDepthSnapshot,
    side: str,
    quantity: float,
    *,
    max_levels: int,
    max_distance_bps: float,
) -> VwapResult:
    """Walk one side of a fresh book for ``quantity`` units.

    ``side`` is the side being CONSUMED: buying consumes "ask", selling
    consumes "bid".  Slippage is measured against the best price of that
    side (the top-of-book spread component is accounted for separately by
    the caller when comparing against mid/technical reference prices).
    """
    levels: tuple[DepthLevel, ...] = book.asks if side == "ask" else book.bids
    if quantity <= 0:
        return VwapResult(
            side=side,
            requested_quantity=quantity,
            filled_quantity=0.0,
            unfilled_quantity=max(0.0, quantity),
            vwap_price=None,
            reference_price=None,
            slippage_abs=None,
            slippage_bps=None,
            levels_consumed=0,
            detail="non-positive quantity",
        )
    if not book.fresh or not levels:
        return VwapResult(
            side=side,
            requested_quantity=quantity,
            filled_quantity=0.0,
            unfilled_quantity=quantity,
            vwap_price=None,
            reference_price=levels[0].price if levels else None,
            slippage_abs=None,
            slippage_bps=None,
            levels_consumed=0,
            detail="book not fresh or side empty",
        )

    reference = levels[0].price
    remaining = quantity
    notional = 0.0
    filled = 0.0
    consumed = 0
    for level in levels[:max_levels]:
        if not _within_distance(level.price, reference, max_distance_bps):
            break
        take = min(remaining, level.quantity)
        if take <= 0:
            continue
        notional += take * level.price
        filled += take
        remaining -= take
        consumed += 1
        if remaining <= 1e-12:
            remaining = 0.0
            break

    vwap = notional / filled if filled > 0 else None
    slippage_abs = None
    slippage_bps = None
    if vwap is not None and reference > 0:
        slippage_abs = abs(vwap - reference)
        slippage_bps = slippage_abs / reference * 10_000
    return VwapResult(
        side=side,
        requested_quantity=quantity,
        filled_quantity=filled,
        unfilled_quantity=remaining,
        vwap_price=vwap,
        reference_price=reference,
        slippage_abs=slippage_abs,
        slippage_bps=slippage_bps,
        levels_consumed=consumed,
        detail=(
            f"filled {filled:.8g}/{quantity:.8g} across {consumed} level(s)"
            if filled > 0
            else "no executable depth within window"
        ),
    )
