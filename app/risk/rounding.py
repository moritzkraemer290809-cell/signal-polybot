"""Tick and quantity rounding helpers (deterministic, Decimal-based)."""

from __future__ import annotations

from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_EVEN, Decimal


def tick_size_for(price_decimals: int, tick_size: float | None) -> Decimal:
    if tick_size is not None and tick_size > 0:
        return Decimal(str(tick_size))
    return Decimal(1).scaleb(-max(0, price_decimals))


def round_price(
    value: float, *, price_decimals: int, tick_size: float | None, mode: str = "nearest"
) -> float:
    """Round a price to the instrument grid.  ``mode``: down|up|nearest."""
    tick = tick_size_for(price_decimals, tick_size)
    if tick <= 0:
        return value
    rounding = {"down": ROUND_FLOOR, "up": ROUND_CEILING}.get(mode, ROUND_HALF_EVEN)
    ticks = (Decimal(str(value)) / tick).quantize(Decimal(1), rounding=rounding)
    return float(ticks * tick)


def round_quantity_down(value: float, quantity_decimals: int) -> float:
    """Quantities are always rounded DOWN - never up into more risk."""
    quantum = Decimal(1).scaleb(-max(0, quantity_decimals))
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_FLOOR))
