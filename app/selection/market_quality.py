"""Market quality score (0-100): purely a data / tradability / liquidity
score.  It is NOT a setup or trading-signal score and uses no technical
indicator, no trend, no direction, no funding and no historical performance.

Default components (configured weights documented here):
- data freshness/quality      30
- spread quality              20
- order book depth            20
- 24h volume                  15
- market/instrument status    10
- mark/index/mid consistency   5

Missing values are always conservative: an unknown value never scores.
"""

from __future__ import annotations

from app.domain.enums import InstrumentQualityStatus
from app.selection.enums import (
    REASON_DATA_DEGRADED,
    REASON_PRICE_CONSISTENCY_UNAVAILABLE,
    InstrumentEligibilityStatus,
)
from app.selection.liquidity_filters import evaluate_depth, evaluate_spread, evaluate_volume
from app.selection.models import MarketSnapshot, QualityResult, Reason, ResolvedThresholds

POINTS_DATA = 30.0
POINTS_SPREAD = 20.0
POINTS_DEPTH = 20.0
POINTS_VOLUME = 15.0
POINTS_STATUS = 10.0
POINTS_CONSISTENCY = 5.0


def _evaluate_data_quality(market: MarketSnapshot) -> tuple[float, list[Reason]]:
    status = market.data_quality_status
    if status is InstrumentQualityStatus.HEALTHY:
        return POINTS_DATA, []
    if status is InstrumentQualityStatus.DEGRADED:
        return POINTS_DATA / 2, [Reason(REASON_DATA_DEGRADED, "data quality DEGRADED")]
    code = {
        InstrumentQualityStatus.DATA_STALE: InstrumentEligibilityStatus.DATA_STALE,
        InstrumentQualityStatus.DATA_INVALID: InstrumentEligibilityStatus.DATA_INVALID,
        InstrumentQualityStatus.ORDERBOOK_RESYNCING: (
            InstrumentEligibilityStatus.ORDERBOOK_RESYNCING
        ),
    }.get(
        status,  # type: ignore[arg-type]
        InstrumentEligibilityStatus.DATA_UNAVAILABLE,
    )
    return 0.0, [Reason(code.value, f"data quality {status.value if status else 'unknown'}")]


def _pairwise_deviation_bps(market: MarketSnapshot) -> float | None:
    """Max pairwise deviation between mark, index and mid prices in bps."""
    prices = [
        price
        for price in (market.mark_price, market.index_price, market.mid_price)
        if price is not None and price > 0
    ]
    if len(prices) < 2:
        return None
    lowest, highest = min(prices), max(prices)
    anchor = (lowest + highest) / 2
    return (highest - lowest) / anchor * 10_000


def _evaluate_consistency(
    market: MarketSnapshot, thresholds: ResolvedThresholds
) -> tuple[float, list[Reason]]:
    deviation = _pairwise_deviation_bps(market)
    if deviation is None:
        return 0.0, [
            Reason(REASON_PRICE_CONSISTENCY_UNAVAILABLE, "fewer than two reference prices")
        ]
    limit = thresholds.max_mark_index_mid_deviation_bps
    if deviation > limit:
        return 0.0, [
            Reason(
                InstrumentEligibilityStatus.PRICE_INVALID.value,
                f"mark/index/mid deviation {deviation:.1f}bps > limit {limit:.1f}bps",
            )
        ]
    if deviation <= limit / 2:
        return POINTS_CONSISTENCY, []
    return POINTS_CONSISTENCY * 0.4, []


def evaluate_market_quality(
    market: MarketSnapshot, thresholds: ResolvedThresholds
) -> QualityResult:
    components: dict[str, float] = {}
    reasons: list[Reason] = []

    for name, (points, component_reasons) in {
        "data_quality": _evaluate_data_quality(market),
        "spread": evaluate_spread(market, thresholds, POINTS_SPREAD),
        "depth": evaluate_depth(market, thresholds, POINTS_DEPTH),
        "volume": evaluate_volume(market, thresholds, POINTS_VOLUME),
        "market_status": (
            (POINTS_STATUS, [])
            if market.instrument_active
            else (
                0.0,
                [Reason(InstrumentEligibilityStatus.MARKET_INACTIVE.value, "not active")],
            )
        ),
        "price_consistency": _evaluate_consistency(market, thresholds),
    }.items():
        components[name] = round(points, 2)
        reasons.extend(component_reasons)

    score = round(sum(components.values()))
    return QualityResult(score=max(0, min(100, score)), components=components, reasons=reasons)
