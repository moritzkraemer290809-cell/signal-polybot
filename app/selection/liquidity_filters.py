"""Threshold resolution and liquidity component checks.

Pure functions: spread, depth and volume evaluation for the market quality
engine.  Missing values are always treated conservatively - an unknown value
never scores positively.
"""

from __future__ import annotations

from app.config import MarketSelectionSettings, SelectionThresholdSettings
from app.domain.enums import AssetClass
from app.selection.enums import (
    REASON_DEPTH_ASYMMETRY,
    REASON_SPREAD_UNAVAILABLE,
    InstrumentEligibilityStatus,
)
from app.selection.models import MarketSnapshot, Reason, ResolvedThresholds

#: LIMITED_SESSION tightening factors for crypto thin-liquidity windows
_THIN_SPREAD_FACTOR = 0.7
_THIN_DEPTH_FACTOR = 1.5
_THIN_VOLUME_FACTOR = 1.5


def resolve_thresholds(
    asset_class: AssetClass,
    symbol: str,
    selection: MarketSelectionSettings,
    thresholds: SelectionThresholdSettings,
    *,
    thin_liquidity: bool = False,
) -> ResolvedThresholds:
    """Asset-class defaults -> symbol overrides -> thin-liquidity tightening.

    Non-EQUITY classes fall back to the (stricter) crypto thresholds.
    """
    if asset_class is AssetClass.EQUITY:
        base = {
            "max_spread_bps": thresholds.equity_max_spread_bps,
            "min_book_depth_pusd": thresholds.equity_min_book_depth_pusd,
            "min_volume_24h_pusd": thresholds.equity_min_volume_24h_pusd,
            "max_mark_index_mid_deviation_bps": (
                thresholds.equity_max_mark_index_mid_deviation_bps
            ),
        }
    else:
        base = {
            "max_spread_bps": thresholds.crypto_max_spread_bps,
            "min_book_depth_pusd": thresholds.crypto_min_book_depth_pusd,
            "min_volume_24h_pusd": thresholds.crypto_min_volume_24h_pusd,
            "max_mark_index_mid_deviation_bps": (
                thresholds.crypto_max_mark_index_mid_deviation_bps
            ),
        }
    flags = {
        "require_volume": selection.require_volume,
        "require_fresh_orderbook": selection.require_fresh_orderbook,
        "allow_degraded_data": selection.allow_degraded_data,
        "min_quality_score": selection.min_quality_score,
    }
    overrides = selection.symbol_overrides_json.get(symbol, {})
    for key, value in overrides.items():
        if key in base:
            base[key] = float(value)
        elif key in flags:
            flags[key] = type(flags[key])(value)

    if thin_liquidity:
        base["max_spread_bps"] *= _THIN_SPREAD_FACTOR
        base["min_book_depth_pusd"] *= _THIN_DEPTH_FACTOR
        base["min_volume_24h_pusd"] *= _THIN_VOLUME_FACTOR

    return ResolvedThresholds(
        max_spread_bps=base["max_spread_bps"],
        min_book_depth_pusd=base["min_book_depth_pusd"],
        min_volume_24h_pusd=base["min_volume_24h_pusd"],
        max_mark_index_mid_deviation_bps=base["max_mark_index_mid_deviation_bps"],
        depth_window_bps=selection.depth_window_bps,
        require_volume=bool(flags["require_volume"]),
        require_fresh_orderbook=bool(flags["require_fresh_orderbook"]),
        allow_degraded_data=bool(flags["allow_degraded_data"]),
        min_quality_score=int(flags["min_quality_score"]),
        tightened_for_thin_liquidity=thin_liquidity,
    )


def evaluate_spread(
    market: MarketSnapshot, thresholds: ResolvedThresholds, max_points: float = 20.0
) -> tuple[float, list[Reason]]:
    """Full points at <= half the limit, linearly down to 0 at the limit.

    Eligibility rule (checked in eligibility.py): spread must be strictly
    BELOW the limit; exactly on the limit blocks.
    """
    if not market.bbo_present or market.spread_bps is None:
        return 0.0, [Reason(REASON_SPREAD_UNAVAILABLE, "no current BBO")]
    spread = market.spread_bps
    limit = thresholds.max_spread_bps
    if spread >= limit:
        return 0.0, [
            Reason(
                InstrumentEligibilityStatus.SPREAD_TOO_WIDE.value,
                f"spread {spread:.1f}bps >= limit {limit:.1f}bps",
            )
        ]
    half = limit / 2
    if spread <= half:
        return max_points, []
    return max_points * (limit - spread) / (limit - half), []


def evaluate_depth(
    market: MarketSnapshot, thresholds: ResolvedThresholds, max_points: float = 20.0
) -> tuple[float, list[Reason]]:
    """Both sides are evaluated separately; the weaker side dominates.

    Depth only counts from a reliable, fresh order book.
    """
    if not (market.book_reliable and market.book_fresh):
        return 0.0, [Reason(REASON_SPREAD_UNAVAILABLE, "order book not fresh/reliable")]
    if market.bid_depth_pusd is None or market.ask_depth_pusd is None:
        return 0.0, [
            Reason(
                InstrumentEligibilityStatus.INSUFFICIENT_DEPTH.value,
                "depth unavailable",
            )
        ]
    minimum = thresholds.min_book_depth_pusd
    weak_side = min(market.bid_depth_pusd, market.ask_depth_pusd)
    strong_side = max(market.bid_depth_pusd, market.ask_depth_pusd)
    reasons: list[Reason] = []
    if weak_side > 0 and strong_side / weak_side > 3:
        reasons.append(
            Reason(
                REASON_DEPTH_ASYMMETRY,
                f"bid/ask depth ratio {strong_side / weak_side:.1f}",
            )
        )
    if weak_side < minimum:
        reasons.append(
            Reason(
                InstrumentEligibilityStatus.INSUFFICIENT_DEPTH.value,
                f"weaker side {weak_side:.0f} pUSD < min {minimum:.0f} pUSD "
                f"(window {thresholds.depth_window_bps:.0f}bps)",
            )
        )
        return 0.0, reasons
    if weak_side >= minimum * 1.5:
        return max_points, reasons
    return max_points / 2, reasons


def evaluate_volume(
    market: MarketSnapshot, thresholds: ResolvedThresholds, max_points: float = 15.0
) -> tuple[float, list[Reason]]:
    """>= 2x minimum: full points; >= minimum: half; below/missing: zero."""
    if market.volume_24h_pusd is None or not market.volume_fresh:
        return 0.0, [
            Reason(
                InstrumentEligibilityStatus.VOLUME_UNAVAILABLE.value,
                "no current 24h volume",
            )
        ]
    volume = market.volume_24h_pusd
    minimum = thresholds.min_volume_24h_pusd
    if volume < minimum:
        return 0.0, [
            Reason(
                InstrumentEligibilityStatus.LOW_VOLUME.value,
                f"24h volume {volume:.0f} pUSD < min {minimum:.0f} pUSD",
            )
        ]
    if volume >= minimum * 2:
        return max_points, []
    return max_points / 2 + max_points / 2 * (volume - minimum) / minimum, []
