"""Immutable risk model versioning and deterministic configuration hashing.

Every plan, cost estimate and rejection references risk_model_name/version,
cost model versions, the fee schedule version, the execution assumption
version and the instrument snapshot version.  Configuration changes yield a
new hash and therefore new plans - existing rows are never rewritten.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from app.config import RiskSettings

#: version of the InstrumentRiskSnapshot structure itself
INSTRUMENT_RISK_SNAPSHOT_VERSION = "irs-1"


def _canonical(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def risk_configuration_hash(settings: RiskSettings) -> str:
    """Deterministic hash over the full risk configuration."""
    return hashlib.sha256(_canonical(settings.model_dump(mode="json")).encode()).hexdigest()[:16]


def plan_dedupe_key(
    *,
    candidate_id: str,
    risk_model_version: str,
    cost_model_version: str,
    risk_config_hash: str,
    cost_config_hash: str,
    fee_schedule_version: str,
    instrument_snapshot_version: str,
    invalidation_price: float,
    entry_basis: str,
    first_target_price: float,
) -> str:
    """Stable identity of one plan evaluation.

    Level-based components (tick-rounded invalidation/target) are used
    instead of BBO-driven prices so ordinary quote noise does not spawn new
    plans, while any material technical or configuration change does.
    """
    raw = "|".join(
        [
            candidate_id,
            risk_model_version,
            cost_model_version,
            risk_config_hash,
            cost_config_hash,
            fee_schedule_version,
            instrument_snapshot_version,
            f"{invalidation_price:.10g}",
            entry_basis,
            f"{first_target_price:.10g}",
        ]
    )
    return hashlib.sha256(raw.encode()).hexdigest()[:24]
