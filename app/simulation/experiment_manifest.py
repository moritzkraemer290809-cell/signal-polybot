"""Immutable, versioned experiment manifests.

A manifest pins EVERYTHING a hypothetical run depended on: model and
config versions, hashes, data source metadata, ordering/clock versions,
window, universe, seeds and parameters.  Once a run started its manifest
is frozen - any change to strategy, risk, costs, fees, funding, slippage,
delay, filters, window, universe or configuration requires a NEW manifest
and therefore a NEW run.  Existing results are never rewritten.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.config import (
    BacktestSettings,
    CostSettings,
    RiskSettings,
    ShadowSimulationSettings,
    SignalLifecycleSettings,
    SimulationReportingSettings,
    StrategySettings,
)
from app.costs.version import cost_configuration_hash
from app.risk.version import risk_configuration_hash
from app.signals.version import lifecycle_configuration_hash
from app.simulation.enums import RunType
from app.simulation.event_ordering import EventOrderingPolicy
from app.simulation.version import (
    MANIFEST_SCHEMA_VERSION,
    METRIC_DEFINITION_VERSION,
    REPLAY_CLOCK_VERSION,
    backtest_configuration_hash,
    manifest_hash,
    shadow_configuration_hash,
)
from app.strategy.version import configuration_hash as strategy_configuration_hash


class ManifestError(Exception):
    """A manifest was incomplete or an immutable manifest was mutated."""


@dataclass(frozen=True)
class ExperimentManifest:
    """Frozen description of one hypothetical run."""

    manifest_id: uuid.UUID
    experiment_id: uuid.UUID
    run_type: RunType
    payload: dict[str, Any]
    content_hash: str
    created_at: datetime
    schema_version: str = MANIFEST_SCHEMA_VERSION

    def require_complete(self) -> None:
        """Every field a reproducible run needs must be present."""
        missing = [key for key in _REQUIRED_KEYS if key not in self.payload]
        if missing:
            raise ManifestError(f"manifest is incomplete - missing {', '.join(sorted(missing))}")
        if self.content_hash != manifest_hash(self.payload):
            raise ManifestError("manifest content hash does not match its payload")

    def with_field(self, key: str, value: Any) -> ExperimentManifest:
        """Manifests are immutable - deriving one yields a NEW manifest."""
        payload = {**self.payload, key: value}
        return ExperimentManifest(
            manifest_id=uuid.uuid4(),
            experiment_id=self.experiment_id,
            run_type=self.run_type,
            payload=payload,
            content_hash=manifest_hash(payload),
            created_at=self.created_at,
            schema_version=self.schema_version,
        )


_REQUIRED_KEYS = frozenset(
    {
        "run_type",
        "simulation_model_name",
        "simulation_model_version",
        "simulation_configuration_hash",
        "strategy_name",
        "strategy_version",
        "strategy_config_hash",
        "risk_model_name",
        "risk_model_version",
        "risk_config_hash",
        "cost_model_name",
        "cost_model_version",
        "cost_config_hash",
        "lifecycle_model_name",
        "lifecycle_model_version",
        "lifecycle_config_hash",
        "fee_schedule_version",
        "execution_assumption_version",
        "input_data",
        "universe",
        "asset_classes",
        "timeframes",
        "start_at",
        "end_at",
        "created_by",
        "replay_ordering",
        "replay_clock_version",
        "metrics_version",
        "disclaimer_version",
        "parameters",
    }
)


def build_manifest(
    *,
    experiment_id: uuid.UUID,
    run_type: RunType,
    shadow_settings: ShadowSimulationSettings,
    backtest_settings: BacktestSettings,
    strategy_settings: StrategySettings,
    risk_settings: RiskSettings,
    cost_settings: CostSettings,
    lifecycle_settings: SignalLifecycleSettings,
    reporting_settings: SimulationReportingSettings,
    fee_schedule_version: str,
    input_data: dict[str, Any],
    universe: tuple[str, ...],
    asset_classes: tuple[str, ...],
    timeframes: tuple[str, ...],
    start_at: datetime,
    end_at: datetime,
    created_at: datetime,
    created_by: str = "system",
    random_seed: int | None = None,
    parameters: dict[str, Any] | None = None,
    code_version: str | None = None,
) -> ExperimentManifest:
    """Assemble the immutable manifest of one hypothetical run."""
    ordering = EventOrderingPolicy(version=backtest_settings.replay_ordering_version)
    payload: dict[str, Any] = {
        "run_type": run_type.value,
        "simulation_model_name": shadow_settings.simulation_model_name,
        "simulation_model_version": shadow_settings.simulation_model_version,
        "simulation_configuration_hash": shadow_configuration_hash(shadow_settings),
        "backtest_model_name": backtest_settings.model_name,
        "backtest_model_version": backtest_settings.model_version,
        "backtest_configuration_hash": backtest_configuration_hash(backtest_settings),
        "strategy_name": strategy_settings.name,
        "strategy_version": strategy_settings.version,
        "strategy_config_hash": strategy_configuration_hash(strategy_settings),
        "risk_model_name": risk_settings.model_name,
        "risk_model_version": risk_settings.model_version,
        "risk_config_hash": risk_configuration_hash(risk_settings),
        "cost_model_name": cost_settings.model_name,
        "cost_model_version": cost_settings.model_version,
        "cost_config_hash": cost_configuration_hash(cost_settings),
        "lifecycle_model_name": lifecycle_settings.lifecycle_model_name,
        "lifecycle_model_version": lifecycle_settings.lifecycle_model_version,
        "lifecycle_config_hash": lifecycle_configuration_hash(lifecycle_settings),
        "fee_schedule_version": fee_schedule_version,
        "execution_assumption_version": cost_settings.execution_assumption_version,
        "input_data": dict(input_data),
        "universe": list(universe),
        "asset_classes": list(asset_classes),
        "timeframes": list(timeframes),
        "start_at": start_at.isoformat(),
        "end_at": end_at.isoformat(),
        "created_at": created_at.isoformat(),
        "created_by": created_by,
        "random_seed": random_seed,
        "replay_ordering": ordering.document(),
        "replay_clock_version": REPLAY_CLOCK_VERSION,
        "metrics_version": reporting_settings.metrics_version or METRIC_DEFINITION_VERSION,
        "disclaimer_version": reporting_settings.disclaimer_version,
        "delay_model": shadow_settings.delay_model,
        "parameters": dict(parameters or {}),
        "code_version": code_version,
        "note": (
            "hypothetical simulation manifest - modelled assumptions over public "
            "data, no execution and no statement about real outcomes"
        ),
    }
    manifest = ExperimentManifest(
        manifest_id=uuid.uuid4(),
        experiment_id=experiment_id,
        run_type=run_type,
        payload=payload,
        content_hash=manifest_hash(payload),
        created_at=created_at,
    )
    manifest.require_complete()
    return manifest


@dataclass(frozen=True)
class Experiment:
    """A named research question with one or more hypothetical runs."""

    experiment_id: uuid.UUID
    name: str
    description: str
    status: str
    created_at: datetime
    created_by: str = "system"
    tags: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)
