"""Pre-run validation of historical inputs.

A backtest may only start when the configured channels actually cover the
requested window.  Missing data is never replaced by an idealised mid
price: depending on policy the run is rejected outright or the affected
intervals are excluded and reported in the manifest.  A run with excluded
intervals never claims to be a complete backtest.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta

from app.config import BacktestSettings
from app.simulation.enums import ReplayEventCategory
from app.simulation.models import DataCoverageReport, DataGapInterval, ReplayEvent

#: replay categories that carry actual market inputs (not derived stages)
_INPUT_CATEGORIES = {
    ReplayEventCategory.TICKER_BBO_TRADES.value: "ticker_bbo",
    ReplayEventCategory.CLOSED_CANDLE.value: "closed_candles",
    ReplayEventCategory.DATA_QUALITY_BOOK.value: "orderbook",
}


#: funding is published on a multi-hour cadence, so the tick-oriented gap
#: limit would reject every realistic dataset; its limit scales with the
#: configured funding interval instead
_CHANNEL_GAP_MULTIPLIER = {
    "closed_candles": 1.0,
    "ticker_bbo": 1.0,
    "orderbook": 1.0,
}
_FUNDING_GAP_SECONDS = 2 * 8 * 3600.0


def _gap_limit(channel: str, max_gap_seconds: float) -> float:
    if channel == "funding":
        return max(max_gap_seconds, _FUNDING_GAP_SECONDS)
    return max_gap_seconds * _CHANNEL_GAP_MULTIPLIER.get(channel, 1.0)


def _required_channels(settings: BacktestSettings) -> tuple[str, ...]:
    channels = ["closed_candles"]
    if settings.require_bbo:
        channels.append("ticker_bbo")
    if settings.require_orderbook:
        channels.append("orderbook")
    if settings.require_funding:
        channels.append("funding")
    return tuple(channels)


def _channel_of(event: ReplayEvent) -> str | None:
    channel = _INPUT_CATEGORIES.get(event.category)
    if channel == "ticker_bbo" and str(event.payload.get("kind", "")).lower() == "funding":
        return "funding"
    return channel


def detect_gaps(
    events: Iterable[ReplayEvent],
    *,
    start_at: datetime,
    end_at: datetime,
    max_gap_seconds: float,
    channels: tuple[str, ...],
) -> tuple[DataGapInterval, ...]:
    """Find windows in which a required channel produced nothing."""
    per_channel: dict[tuple[str, str], list[datetime]] = {}
    symbols: set[str] = set()
    for event in events:
        channel = _channel_of(event)
        if channel is None or channel not in channels:
            continue
        symbols.add(event.symbol)
        per_channel.setdefault((event.symbol, channel), []).append(event.as_of)

    gaps: list[DataGapInterval] = []
    for symbol in sorted(symbols):
        for channel in channels:
            limit = timedelta(seconds=_gap_limit(channel, max_gap_seconds))
            stamps = sorted(per_channel.get((symbol, channel), []))
            if not stamps:
                gaps.append(
                    DataGapInterval(
                        start_at=start_at,
                        end_at=end_at,
                        channel=channel,
                        symbol=symbol,
                        detail=f"no {channel} data in the requested window",
                    )
                )
                continue
            previous = start_at
            for stamp in stamps:
                if stamp - previous > limit:
                    gaps.append(
                        DataGapInterval(
                            start_at=previous,
                            end_at=stamp,
                            channel=channel,
                            symbol=symbol,
                            detail=(
                                f"{channel} gap of {(stamp - previous).total_seconds():.0f}s "
                                f"exceeds the {limit.total_seconds():.0f}s limit for this "
                                "channel"
                            ),
                        )
                    )
                previous = stamp
            if end_at - previous > limit:
                gaps.append(
                    DataGapInterval(
                        start_at=previous,
                        end_at=end_at,
                        channel=channel,
                        symbol=symbol,
                        detail=(
                            f"{channel} gap of {(end_at - previous).total_seconds():.0f}s "
                            "until the end of the window"
                        ),
                    )
                )
    return tuple(gaps)


def validate_coverage(
    events: Iterable[ReplayEvent],
    settings: BacktestSettings,
    *,
    start_at: datetime,
    end_at: datetime,
) -> DataCoverageReport:
    """Validate historical coverage before a run may start."""
    materialised = list(events)
    channels = _required_channels(settings)
    present = {
        channel for channel in (_channel_of(event) for event in materialised) if channel is not None
    }
    missing = tuple(channel for channel in channels if channel not in present)
    gaps = detect_gaps(
        materialised,
        start_at=start_at,
        end_at=end_at,
        max_gap_seconds=settings.max_data_gap_seconds,
        channels=channels,
    )

    if missing:
        return DataCoverageReport(
            complete=False,
            gaps=gaps,
            excluded_intervals=(),
            missing_channels=missing,
            checked_channels=channels,
            detail=(
                "required historical channel(s) absent: "
                f"{', '.join(missing)} - a complete backtest is not possible"
            ),
            warnings=("BACKTEST_DATA_INCOMPLETE",),
        )
    if not gaps:
        return DataCoverageReport(
            complete=True,
            gaps=(),
            excluded_intervals=(),
            missing_channels=(),
            checked_channels=channels,
            detail="all required channels cover the requested window",
        )
    if not settings.allow_segmented_data:
        return DataCoverageReport(
            complete=False,
            gaps=gaps,
            excluded_intervals=(),
            missing_channels=(),
            checked_channels=channels,
            detail=(
                f"{len(gaps)} data gap(s) exceed the configured limit and segmented "
                "runs are disabled - run rejected instead of interpolating"
            ),
            warnings=("DATA_GAP",),
        )
    return DataCoverageReport(
        complete=False,
        gaps=gaps,
        excluded_intervals=gaps,
        missing_channels=(),
        checked_channels=channels,
        detail=(
            f"{len(gaps)} interval(s) excluded from the run - results cover only the "
            "remaining segments and are not a complete backtest"
        ),
        warnings=("COMPLETED_WITH_GAPS",),
    )


def is_excluded(moment: datetime, excluded: tuple[DataGapInterval, ...], symbol: str) -> bool:
    """True when an instant falls inside an excluded interval."""
    for interval in excluded:
        if interval.symbol == symbol and interval.start_at <= moment <= interval.end_at:
            return True
    return False
