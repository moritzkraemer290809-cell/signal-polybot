"""Mark/index/mid context features (data-plausibility context, no direction)."""

from __future__ import annotations

from dataclasses import dataclass

from app.strategy.candle_features import distance_bps
from app.strategy.models import MarketContextSnapshot


@dataclass(frozen=True)
class MarketContextFeatures:
    mark_to_index_bps: float | None
    mark_to_mid_bps: float | None
    index_to_mid_bps: float | None
    funding_rate: float | None  # neutral context value, never a setup trigger
    valid: bool
    detail: str


def compute_market_context(market: MarketContextSnapshot) -> MarketContextFeatures:
    mark, index, mid = market.mark_price, market.index_price, market.mid_price
    mark_to_index = distance_bps(mark, index) if mark is not None and index else None
    mark_to_mid = distance_bps(mark, mid) if mark is not None and mid else None
    index_to_mid = distance_bps(index, mid) if index is not None and mid else None
    valid = mark is not None and (index is not None or mid is not None)
    return MarketContextFeatures(
        mark_to_index_bps=mark_to_index,
        mark_to_mid_bps=mark_to_mid,
        index_to_mid_bps=index_to_mid,
        funding_rate=market.funding_rate,
        valid=valid,
        detail="ok" if valid else "insufficient reference prices",
    )
