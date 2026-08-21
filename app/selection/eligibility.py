"""Hard eligibility gates.  A high quality score never overrides these.

Gates are evaluated in a fixed, documented priority order; all triggered
reasons are collected, the first blocking gate defines the eligibility
status.  No gate knows anything about trade direction.
"""

from __future__ import annotations

from app.domain.enums import AssetClass, InstrumentQualityStatus, InstrumentStatus
from app.selection.enums import (
    REASON_DATA_DEGRADED,
    REASON_ORDERBOOK_STALE,
    REASON_QUALITY_BELOW_MINIMUM,
    InstrumentEligibilityStatus,
)
from app.selection.models import QualityResult, Reason, SelectionInputs

Status = InstrumentEligibilityStatus


def evaluate_eligibility(
    inputs: SelectionInputs, quality: QualityResult
) -> tuple[Status, list[Reason]]:
    reasons: list[Reason] = []
    blocking: Status | None = None

    def block(status: Status, detail: str) -> None:
        nonlocal blocking
        reasons.append(Reason(status.value, detail))
        if blocking is None:
            blocking = status

    # 1. instrument identity / configuration
    if inputs.instrument_status == InstrumentStatus.DELISTED.value:
        block(Status.INSTRUMENT_DELISTED, "instrument delisted from discovery")
    elif not inputs.instrument_enabled:
        block(Status.INSTRUMENT_NOT_CONFIGURED, "not part of the configured universe")

    # 2. denylist always wins
    if inputs.denylisted:
        block(Status.DISABLED_BY_CONFIG, "symbol denylisted")

    # 3. asset class policy
    asset_class = inputs.classification.asset_class
    if asset_class is AssetClass.UNKNOWN:
        block(Status.ASSET_CLASS_UNSUPPORTED, "asset class UNKNOWN is never analysed")

    # 4. session / calendar gate (allowlist can never bypass this)
    if inputs.session.blocking_status is not None:
        block(inputs.session.blocking_status, inputs.session.detail or "session closed")

    # 5. market status
    if inputs.instrument_status not in (InstrumentStatus.ACTIVE.value,):
        if blocking is not Status.INSTRUMENT_DELISTED:
            block(Status.MARKET_INACTIVE, f"instrument status {inputs.instrument_status}")
    elif not inputs.market.instrument_active:
        block(Status.MARKET_INACTIVE, "market reported inactive")

    # 6. data quality (allowlist can never bypass this either)
    dq = inputs.market.data_quality_status
    if dq is None or dq is InstrumentQualityStatus.UNAVAILABLE:
        block(Status.DATA_UNAVAILABLE, "no live data available")
    elif dq is InstrumentQualityStatus.DATA_STALE:
        block(Status.DATA_STALE, "live data stale")
    elif dq is InstrumentQualityStatus.DATA_INVALID:
        block(Status.DATA_INVALID, "live data invalid")
    elif dq is InstrumentQualityStatus.ORDERBOOK_RESYNCING:
        block(Status.ORDERBOOK_RESYNCING, "order book resyncing")
    elif dq is InstrumentQualityStatus.DEGRADED and not inputs.thresholds.allow_degraded_data:
        reasons.append(Reason(REASON_DATA_DEGRADED, "degraded data not allowed by policy"))
        if blocking is None:
            blocking = Status.INELIGIBLE

    # 7. order book freshness
    if inputs.thresholds.require_fresh_orderbook and not (
        inputs.market.book_reliable and inputs.market.book_fresh
    ):
        if not inputs.market.book_reliable:
            block(Status.ORDERBOOK_RESYNCING, "order book unreliable/awaiting snapshot")
        else:
            reasons.append(Reason(REASON_ORDERBOOK_STALE, "order book not fresh"))
            if blocking is None:
                blocking = Status.DATA_STALE

    # 8. prices
    if not inputs.market.bbo_present:
        block(Status.PRICE_INVALID, "no current BBO")
    if inputs.market.mark_price is None or inputs.market.mark_price <= 0:
        block(Status.PRICE_INVALID, "mark price missing or invalid")

    # 9. volume
    if inputs.thresholds.require_volume:
        if inputs.market.volume_24h_pusd is None or not inputs.market.volume_fresh:
            block(Status.VOLUME_UNAVAILABLE, "24h volume unavailable")
        elif inputs.market.volume_24h_pusd < inputs.thresholds.min_volume_24h_pusd:
            block(
                Status.LOW_VOLUME,
                f"volume {inputs.market.volume_24h_pusd:.0f} < "
                f"{inputs.thresholds.min_volume_24h_pusd:.0f} pUSD",
            )

    # 10. spread (strictly below the limit; on-limit blocks)
    if inputs.market.spread_bps is not None and (
        inputs.market.spread_bps >= inputs.thresholds.max_spread_bps
    ):
        block(
            Status.SPREAD_TOO_WIDE,
            f"spread {inputs.market.spread_bps:.1f}bps >= "
            f"{inputs.thresholds.max_spread_bps:.1f}bps",
        )

    # 11. depth (weaker side must reach the minimum)
    if inputs.market.bid_depth_pusd is not None and inputs.market.ask_depth_pusd is not None:
        weak_side = min(inputs.market.bid_depth_pusd, inputs.market.ask_depth_pusd)
        if weak_side < inputs.thresholds.min_book_depth_pusd:
            block(
                Status.INSUFFICIENT_DEPTH,
                f"depth {weak_side:.0f} < {inputs.thresholds.min_book_depth_pusd:.0f} pUSD",
            )
    elif inputs.thresholds.require_fresh_orderbook:
        block(Status.INSUFFICIENT_DEPTH, "depth unavailable")

    # 12. price consistency beyond the hard limit (reason set by quality engine)
    for reason in quality.reasons:
        if reason.code == Status.PRICE_INVALID.value:
            block(Status.PRICE_INVALID, reason.detail)
            break

    # 13. minimum market quality score
    if quality.score < inputs.thresholds.min_quality_score:
        reasons.append(
            Reason(
                REASON_QUALITY_BELOW_MINIMUM,
                f"score {quality.score} < minimum {inputs.thresholds.min_quality_score}",
            )
        )
        if blocking is None:
            blocking = Status.INELIGIBLE

    if blocking is None:
        return Status.ELIGIBLE, reasons
    return blocking, reasons
