"""Funding projection: direction signs, conservatism, missing-data policy."""

from __future__ import annotations

from datetime import timedelta

import pytest
from tests.risk_helpers import AS_OF, COST_SETTINGS, make_funding

from app.config import CostSettings
from app.costs.enums import FundingModelState
from app.costs.funding_projection import project_funding
from app.costs.models import CostModelError


def _project(funding, *, bullish=True, hold=240, notional=10_000.0, settings=COST_SETTINGS):
    return project_funding(
        funding,
        bullish=bullish,
        hold_minutes=hold,
        notional=notional,
        as_of=AS_OF,
        settings=settings,
    )


def test_positive_funding_costs_longs_not_shorts() -> None:
    funding = make_funding(current_rate=0.0002)
    long_side = _project(funding, bullish=True)
    short_side = _project(funding, bullish=False)
    assert long_side.expected_cost > 0
    assert long_side.assumed_rate_per_interval == pytest.approx(0.0002)
    # shorts would RECEIVE positive funding - never credited as profit
    assert short_side.assumed_rate_per_interval == 0.0
    assert short_side.expected_cost >= 0


def test_negative_funding_costs_shorts_not_longs() -> None:
    funding = make_funding(
        current_rate=-0.0003,
        history=tuple((AS_OF - timedelta(hours=h), -0.0003) for h in range(6, 0, -1)),
    )
    short_side = _project(funding, bullish=False)
    long_side = _project(funding, bullish=True)
    assert short_side.assumed_rate_per_interval == pytest.approx(0.0003)
    assert short_side.expected_cost > 0
    assert long_side.assumed_rate_per_interval == 0.0
    assert long_side.expected_cost >= 0  # favourable side floored at zero benefit


def test_hold_duration_scales_intervals() -> None:
    funding = make_funding(current_rate=0.0001, next_funding_at=AS_OF + timedelta(minutes=5))
    short_hold = _project(funding, hold=60)
    long_hold = _project(funding, hold=240)
    assert short_hold.intervals_charged == 1
    assert long_hold.intervals_charged == 4
    assert long_hold.expected_cost == pytest.approx(4 * short_hold.expected_cost)


def test_next_funding_beyond_hold_window_charges_nothing() -> None:
    funding = make_funding(current_rate=0.0005, next_funding_at=AS_OF + timedelta(hours=10))
    projection = _project(funding, hold=60)
    assert projection.intervals_charged == 0
    assert projection.expected_cost == 0.0


def test_conservative_percentile_of_adverse_history() -> None:
    rates = [0.0001, 0.0002, 0.0003, 0.0004, 0.0005, 0.0006, 0.0007, 0.0008]
    funding = make_funding(
        current_rate=0.0001,
        history=tuple(
            (AS_OF - timedelta(hours=len(rates) - i), rate) for i, rate in enumerate(rates)
        ),
    )
    projection = _project(funding)
    # p75 nearest-rank of 8 samples -> 6th value = 0.0006 > current
    assert projection.assumed_rate_per_interval == pytest.approx(0.0006)
    assert "p75" in projection.basis


def test_missing_data_blocks_by_default() -> None:
    funding = make_funding(current_rate=None, history=(), data_fresh=False)
    with pytest.raises(CostModelError) as excinfo:
        _project(funding)
    assert excinfo.value.code == "FUNDING_MODEL_UNAVAILABLE"


def test_stale_data_blocks_even_when_history_exists() -> None:
    """data_fresh=False (stale rate/history) is a block, not silently zero."""
    funding = make_funding(data_fresh=False)  # history + current present but stale
    with pytest.raises(CostModelError) as excinfo:
        _project(funding)
    assert excinfo.value.code == "FUNDING_MODEL_UNAVAILABLE"


def test_missing_data_intraday_buffer_when_policy_allows() -> None:
    relaxed = CostSettings(_env_file=None, require_funding_data=False)
    funding = make_funding(current_rate=None, history=(), data_fresh=False)
    projection = _project(funding, hold=120, settings=relaxed)
    assert projection.state is FundingModelState.BUFFERED_ONLY
    assert projection.expected_cost == pytest.approx(10_000 * 1.0 / 10_000)  # 1bps buffer
    # beyond the intraday horizon even the relaxed policy blocks
    with pytest.raises(CostModelError):
        _project(funding, hold=60 * 48, settings=relaxed)


def test_thin_history_adds_buffer() -> None:
    funding = make_funding(
        current_rate=0.0001,
        history=((AS_OF - timedelta(hours=1), 0.0001),),
        next_funding_at=AS_OF + timedelta(minutes=5),
    )
    projection = _project(funding)
    assert projection.buffer_cost > 0
    assert projection.expected_cost > 10_000 * 0.0001 * 4 - 1e-9


def test_funding_never_required_as_profit_source() -> None:
    """Even a strongly favourable funding rate never reduces cost below 0."""
    funding = make_funding(
        current_rate=-0.01,
        history=tuple((AS_OF - timedelta(hours=h), -0.01) for h in range(6, 0, -1)),
    )
    projection = _project(funding, bullish=True)  # longs would be paid
    assert projection.expected_cost >= 0.0
    assert projection.assumed_rate_per_interval == 0.0


def test_funding_disabled_uses_buffer_only() -> None:
    disabled = CostSettings(_env_file=None, funding_enabled=False)
    projection = _project(make_funding(), settings=disabled)
    assert projection.state is FundingModelState.BUFFERED_ONLY
    assert projection.expected_cost > 0
