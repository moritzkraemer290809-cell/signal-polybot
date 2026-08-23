"""Conservative liquidation buffer check.

The technical invalidation must sit sufficiently BEFORE the hypothetical
liquidation threshold (mark-price-referenced), by configurable minimums in
bps and ATR multiples.  This is a conservative model check - it is never a
guarantee against liquidation and is documented as such everywhere.
"""

from __future__ import annotations

from app.config import RiskSettings
from app.risk.models import LiquidationBufferResult, MarginModelResult


def check_liquidation_buffer(
    *,
    invalidation_price: float,
    margin: MarginModelResult,
    atr: float | None,
    bullish: bool,
    settings: RiskSettings,
) -> LiquidationBufferResult:
    liquidation = margin.hypothetical_liquidation_price
    reference = margin.mark_price_reference
    min_bps = settings.min_liquidation_buffer_bps
    min_atr = settings.min_liquidation_buffer_atr_multiple
    if liquidation is None or reference is None or reference <= 0:
        return LiquidationBufferResult(
            sufficient=False,
            buffer_abs=None,
            buffer_bps=None,
            buffer_atr_multiple=None,
            min_required_bps=min_bps,
            min_required_atr_multiple=min_atr,
            detail="no liquidation threshold available - conservative fail",
        )
    # buffer: distance from invalidation to the liquidation threshold,
    # positive only when the invalidation triggers BEFORE liquidation
    buffer_abs = invalidation_price - liquidation if bullish else liquidation - invalidation_price
    buffer_bps = buffer_abs / reference * 10_000
    buffer_atr = buffer_abs / atr if atr is not None and atr > 0 else None
    sufficient = (
        buffer_abs > 0
        and buffer_bps >= min_bps
        and buffer_atr is not None
        and buffer_atr >= min_atr
    )
    return LiquidationBufferResult(
        sufficient=sufficient,
        buffer_abs=buffer_abs,
        buffer_bps=buffer_bps,
        buffer_atr_multiple=buffer_atr,
        min_required_bps=min_bps,
        min_required_atr_multiple=min_atr,
        detail=(
            f"invalidation {invalidation_price:.6g} vs hypothetical liquidation "
            f"{liquidation:.6g}: buffer {buffer_bps:.1f}bps"
            + (f" / {buffer_atr:.2f}xATR" if buffer_atr is not None else " / ATR n/a")
            + f" (min {min_bps:.0f}bps, {min_atr:g}xATR) - conservative model "
            "check, no liquidation guarantee"
        ),
    )
