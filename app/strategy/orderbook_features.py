"""Order book features from the fresh, validated market context snapshot.

A stale or unreliable book yields invalid (None) features - never estimates,
and no book feature alone may ever set a candidate direction (enforced by
the setup rules, which only use these as confirmation context).
"""

from __future__ import annotations

from dataclasses import dataclass

from app.strategy.models import MarketContextSnapshot


@dataclass(frozen=True)
class OrderbookFeatures:
    spread_bps: float | None
    depth_imbalance: float | None  # (bid-ask)/(bid+ask), -1..+1
    top_of_book_imbalance: float | None
    valid: bool
    detail: str


def compute_orderbook_features(market: MarketContextSnapshot) -> OrderbookFeatures:
    if not market.book_fresh or not market.bbo_fresh:
        return OrderbookFeatures(None, None, None, valid=False, detail="book/bbo not fresh")
    spread = market.spread_bps
    imbalance = None
    if (
        market.bid_depth_pusd is not None
        and market.ask_depth_pusd is not None
        and (market.bid_depth_pusd + market.ask_depth_pusd) > 0
    ):
        imbalance = (market.bid_depth_pusd - market.ask_depth_pusd) / (
            market.bid_depth_pusd + market.ask_depth_pusd
        )
    top_imbalance = None
    if market.best_bid is not None and market.best_ask is not None and market.mid_price:
        # positional proxy: where mid sits between best bid/ask is ~0 by
        # construction; use bid/ask distance asymmetry only when depths absent
        top_imbalance = imbalance
    valid = spread is not None
    return OrderbookFeatures(
        spread_bps=spread,
        depth_imbalance=imbalance,
        top_of_book_imbalance=top_imbalance,
        valid=valid,
        detail="ok" if valid else "no spread",
    )
