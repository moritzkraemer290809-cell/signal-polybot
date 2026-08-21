"""Candle series utilities: gap detection for the data quality layer.

Pure functions - no I/O.  Automated REST gap backfill builds on this in a
later step (jobs/historical_backfill.py); phase 5 only detects and reports
gaps so the data quality layer can account for them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from itertools import pairwise

from app.domain.enums import Timeframe
from app.domain.models import CandleData


@dataclass(frozen=True)
class CandleGap:
    """A missing range of candle buckets (inclusive start, exclusive end)."""

    timeframe: Timeframe
    start: datetime
    end: datetime

    @property
    def missing_buckets(self) -> int:
        span_ms = (self.end - self.start).total_seconds() * 1000
        return int(span_ms // self.timeframe.milliseconds)


def find_gaps(
    candles: list[CandleData],
    timeframe: Timeframe,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
) -> list[CandleGap]:
    """Detect missing buckets in a candle series.

    ``start``/``end`` optionally extend the check beyond the observed range
    (e.g. expected session bounds).  Candles are deduplicated on open_time and
    sorted before evaluation.
    """
    interval_ms = timeframe.milliseconds
    unique = sorted({candle.open_time for candle in candles})
    if not unique:
        if start is not None and end is not None and end > start:
            return [CandleGap(timeframe=timeframe, start=start, end=end)]
        return []

    gaps: list[CandleGap] = []
    if start is not None and unique[0] > start:
        gaps.append(CandleGap(timeframe=timeframe, start=start, end=unique[0]))
    for previous, current in pairwise(unique):
        expected_next_ms = previous.timestamp() * 1000 + interval_ms
        if current.timestamp() * 1000 > expected_next_ms:
            gaps.append(
                CandleGap(
                    timeframe=timeframe,
                    start=previous.fromtimestamp(expected_next_ms / 1000, tz=previous.tzinfo),
                    end=current,
                )
            )
    if end is not None:
        last_end_ms = unique[-1].timestamp() * 1000 + interval_ms
        if end.timestamp() * 1000 > last_end_ms:
            gaps.append(
                CandleGap(
                    timeframe=timeframe,
                    start=unique[-1].fromtimestamp(last_end_ms / 1000, tz=unique[-1].tzinfo),
                    end=end,
                )
            )
    return gaps
