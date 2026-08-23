"""Conservative funding cost projection.

Funding is a cost/context model, never a trigger and never a profit source:
a favourable funding direction is floored at zero benefit.  Missing funding
data never silently means zero - the configured policy either blocks
(default) or applies a clearly labelled conservative buffer for short
intraday horizons.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta

from app.config import CostSettings
from app.costs.enums import FundingModelState
from app.costs.models import CostModelError, FundingProjection, FundingSnapshot

#: hold assumptions at or below this horizon may fall back to the buffered
#: model when funding data is missing but not strictly required
_INTRADAY_BUFFER_MAX_MINUTES = 24 * 60


def _percentile(values: list[float], percentile: float) -> float:
    """Deterministic nearest-rank percentile (values need not be sorted)."""
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, math.ceil(percentile / 100.0 * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


def _adverse(rate: float, bullish: bool) -> float:
    """Adverse funding per interval: longs pay positive rates, shorts pay
    negative rates (public Polymarket perps definition).  Favourable rates
    are floored at zero - funding income is never planned as profit."""
    return max(rate, 0.0) if bullish else max(-rate, 0.0)


def project_funding(
    funding: FundingSnapshot,
    *,
    bullish: bool,
    hold_minutes: int,
    notional: float,
    as_of: datetime,
    settings: CostSettings,
) -> FundingProjection:
    direction = "BULLISH" if bullish else "BEARISH"
    interval_hours = funding.interval_hours or settings.default_funding_interval_hours
    buffer_cost = notional * settings.funding_buffer_bps / 10_000

    if not settings.funding_enabled:
        return FundingProjection(
            state=FundingModelState.BUFFERED_ONLY,
            direction=direction,
            hold_minutes=hold_minutes,
            intervals_charged=0,
            assumed_rate_per_interval=0.0,
            expected_cost=buffer_cost,
            buffer_cost=buffer_cost,
            basis="funding model disabled - conservative buffer only",
            detail=f"buffer {settings.funding_buffer_bps:.1f}bps of notional",
        )

    lookback_cutoff = as_of - timedelta(hours=settings.funding_lookback_hours)
    history = [rate for ts, rate in funding.history if ts >= lookback_cutoff]
    has_data = funding.current_rate is not None or bool(history)

    if not has_data or not funding.data_fresh:
        if settings.require_funding_data or hold_minutes > _INTRADAY_BUFFER_MAX_MINUTES:
            raise CostModelError(
                "FUNDING_MODEL_UNAVAILABLE",
                "no fresh funding rate/history available and policy requires it",
            )
        return FundingProjection(
            state=FundingModelState.BUFFERED_ONLY,
            direction=direction,
            hold_minutes=hold_minutes,
            intervals_charged=0,
            assumed_rate_per_interval=0.0,
            expected_cost=buffer_cost,
            buffer_cost=buffer_cost,
            basis="funding data missing - intraday conservative buffer",
            detail=f"buffer {settings.funding_buffer_bps:.1f}bps of notional",
        )

    adverse_history = [_adverse(rate, bullish) for rate in history]
    adverse_current = _adverse(funding.current_rate or 0.0, bullish)
    percentile_rate = _percentile(adverse_history, settings.funding_conservative_percentile)
    assumed_rate = max(adverse_current, percentile_rate)

    # intervals charged over the technical hold window (round UP, min 1 when
    # a funding timestamp is known to fall inside the window)
    hold_hours = hold_minutes / 60.0
    intervals = math.ceil(hold_hours / interval_hours) if interval_hours > 0 else 0
    if funding.next_funding_at is not None:
        window_end = as_of + timedelta(minutes=hold_minutes)
        if funding.next_funding_at > window_end:
            intervals = 0  # no funding event inside the hold window
    thin_history = len(adverse_history) < 3
    expected = notional * assumed_rate * intervals
    applied_buffer = buffer_cost if thin_history else 0.0
    return FundingProjection(
        state=FundingModelState.MODELED,
        direction=direction,
        hold_minutes=hold_minutes,
        intervals_charged=intervals,
        assumed_rate_per_interval=assumed_rate,
        expected_cost=expected + applied_buffer,
        buffer_cost=applied_buffer,
        basis=(
            f"max(current adverse {adverse_current:.6g}, "
            f"p{settings.funding_conservative_percentile:.0f} of {len(adverse_history)} "
            f"samples {percentile_rate:.6g}) x {intervals} interval(s)"
        ),
        detail=(
            "conservative research hold assumption "
            f"{hold_minutes}min at {interval_hours:g}h funding interval"
        ),
    )
