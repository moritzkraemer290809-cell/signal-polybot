"""Deterministic versioning for hypothetical simulations.

Every run records the hashes below in its immutable manifest: a changed
configuration always produces a different hash and therefore a NEW run,
never a silent rewrite of an existing result.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from app.config import BacktestSettings, ShadowSimulationSettings, SimulationReportingSettings

#: bump when the metric catalogue or a metric definition changes
METRIC_DEFINITION_VERSION = "smv-1"
#: bump when the replay ordering policy changes
REPLAY_ORDERING_VERSION = "rov-1"
#: bump when the replay clock semantics change
REPLAY_CLOCK_VERSION = "rcv-1"
#: bump when the manifest schema changes
MANIFEST_SCHEMA_VERSION = "man-1"


def _canonical(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def _hash(payload: Any, length: int = 16) -> str:
    return hashlib.sha256(_canonical(payload).encode()).hexdigest()[:length]


def shadow_configuration_hash(settings: ShadowSimulationSettings) -> str:
    """Deterministic hash over the full shadow simulation configuration."""
    return _hash(settings.model_dump(mode="json"))


def backtest_configuration_hash(settings: BacktestSettings) -> str:
    """Deterministic hash over the full backtest configuration."""
    return _hash(settings.model_dump(mode="json"))


def reporting_configuration_hash(settings: SimulationReportingSettings) -> str:
    return _hash(settings.model_dump(mode="json"))


def manifest_hash(manifest: dict[str, Any]) -> str:
    """Content hash of an immutable manifest (32 chars for run identity)."""
    return _hash(manifest, length=32)


def simulation_dedupe_key(
    *,
    lifecycle_signal_id: str,
    run_id: str,
    simulation_model_version: str,
    simulation_config_hash: str,
) -> str:
    """Identity of one hypothetical simulation inside its run context."""
    raw = "|".join([lifecycle_signal_id, run_id, simulation_model_version, simulation_config_hash])
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def run_dedupe_key(
    *,
    experiment_id: str,
    run_type: str,
    manifest_hash_value: str,
) -> str:
    """Identity of one run context - blocks parallel duplicate runs."""
    raw = "|".join([experiment_id, run_type, manifest_hash_value])
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def metric_set_key(
    *,
    run_id: str,
    segment_kind: str,
    segment_key: str,
    metrics_version: str,
) -> str:
    raw = "|".join([run_id, segment_kind, segment_key, metrics_version])
    return hashlib.sha256(raw.encode()).hexdigest()[:24]
