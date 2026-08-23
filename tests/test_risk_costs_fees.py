"""Fee schedule and fee resolution: versioned assumptions, notional basis."""

from __future__ import annotations

import pytest
from tests.risk_helpers import COST_SETTINGS, make_fee_schedule

from app.config import CostSettings
from app.costs.enums import ExecutionMode, FeeKind
from app.costs.fee_resolver import fee_cost, fee_kind_for
from app.costs.fee_schedule import build_default_schedule, snapshot_from_schedule
from app.costs.models import CostModelError
from app.costs.version import cost_configuration_hash


def test_default_schedule_document_carries_assumption_note() -> None:
    document = build_default_schedule(COST_SETTINGS)
    assert document["version"] == COST_SETTINGS.fee_schedule_version
    assert document["source"] == "local_config_assumption"
    assert "validate against" in document["note"].lower()
    tier = document["tiers"][COST_SETTINGS.assumed_fee_tier]
    assert tier["maker_fee_rate"] == COST_SETTINGS.default_maker_fee_rate
    assert tier["taker_fee_rate"] == COST_SETTINGS.default_taker_fee_rate


def test_snapshot_from_schedule_validates_rates_and_tier() -> None:
    document = build_default_schedule(COST_SETTINGS)
    snapshot = snapshot_from_schedule(document, COST_SETTINGS, active=True)
    assert snapshot.taker_fee_rate == pytest.approx(0.0007)
    assert snapshot.maker_fee_rate == pytest.approx(0.0002)
    assert snapshot.rate_for(FeeKind.TAKER) > snapshot.rate_for(FeeKind.MAKER)

    # missing tier -> unavailable
    with pytest.raises(CostModelError) as excinfo:
        snapshot_from_schedule({"version": "x", "tiers": {}}, COST_SETTINGS, active=True)
    assert excinfo.value.code == "FEE_SCHEDULE_UNAVAILABLE"

    # implausible rate -> unavailable, never silently accepted
    bad = build_default_schedule(COST_SETTINGS)
    bad["tiers"]["default"]["taker_fee_rate"] = 0.5
    with pytest.raises(CostModelError):
        snapshot_from_schedule(bad, COST_SETTINGS, active=True)


def test_multiple_tiers_resolved_via_assumed_tier() -> None:
    document = build_default_schedule(COST_SETTINGS)
    document["tiers"]["vip"] = {"maker_fee_rate": 0.0, "taker_fee_rate": 0.0003}
    # assumed tier stays "default" - the conservative configured tier wins
    snapshot = snapshot_from_schedule(document, COST_SETTINGS, active=True)
    assert snapshot.tier == "default"
    assert snapshot.taker_fee_rate == pytest.approx(0.0007)
    document["assumed_tier"] = "vip"
    snapshot = snapshot_from_schedule(document, COST_SETTINGS, active=True)
    assert snapshot.tier == "vip"
    assert snapshot.taker_fee_rate == pytest.approx(0.0003)


def test_fee_kind_per_execution_mode() -> None:
    assert fee_kind_for(ExecutionMode.ENTRY_TAKER) is FeeKind.TAKER
    assert fee_kind_for(ExecutionMode.ENTRY_MAKER) is FeeKind.MAKER
    assert fee_kind_for(ExecutionMode.TARGET_MAKER) is FeeKind.MAKER
    assert fee_kind_for(ExecutionMode.STOP_TAKER) is FeeKind.TAKER
    assert fee_kind_for(ExecutionMode.STOP_STRESS_TAKER) is FeeKind.TAKER


def test_fee_cost_is_notional_based() -> None:
    schedule = make_fee_schedule()
    # fee = rate * price * quantity (notional), NOT margin-based
    cost = fee_cost(schedule, ExecutionMode.ENTRY_TAKER, 100.0, 5.0)
    assert cost == pytest.approx(0.0007 * 500.0)
    assert fee_cost(schedule, ExecutionMode.ENTRY_MAKER, 100.0, 5.0) == pytest.approx(
        0.0002 * 500.0
    )
    assert fee_cost(schedule, ExecutionMode.ENTRY_TAKER, 0.0, 5.0) == 0.0
    assert fee_cost(schedule, ExecutionMode.ENTRY_TAKER, 100.0, 0.0) == 0.0


def test_fee_rate_configuration_is_validated() -> None:
    with pytest.raises(ValueError):
        CostSettings(_env_file=None, default_taker_fee_rate=0.5)
    with pytest.raises(ValueError):
        CostSettings(_env_file=None, default_maker_fee_rate=-0.001)


def test_cost_configuration_hash_changes_with_fee_config() -> None:
    base = cost_configuration_hash(COST_SETTINGS)
    assert base == cost_configuration_hash(CostSettings(_env_file=None))
    changed = CostSettings(_env_file=None, default_taker_fee_rate=0.0009)
    assert cost_configuration_hash(changed) != base
