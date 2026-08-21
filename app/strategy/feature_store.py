"""Feature store adapter: persists research artefacts around the pure core.

Persistence failures degrade the store (flag + count) but never crash the
evaluation loop; unpersisted state is never reported as successful by the
candidate manager (see strategy job)."""

from __future__ import annotations

import uuid

from app.config import StrategySettings
from app.observability.logging import get_logger
from app.observability.metrics import metrics
from app.repositories.feature_repository import FeatureRepository
from app.repositories.regime_repository import RegimeRepository
from app.strategy.models import EvaluationOutcome


class FeatureStore:
    def __init__(
        self,
        features: FeatureRepository,
        regimes: RegimeRepository,
        settings: StrategySettings,
    ) -> None:
        self._features = features
        self._regimes = regimes
        self._settings = settings
        self._log = get_logger("feature_store")
        self.degraded = False

    async def persist(
        self,
        instrument_pk: int,
        symbol: str,
        outcome: EvaluationOutcome,
        snapshot_id: uuid.UUID,
        config_hash: str,
    ) -> None:
        try:
            if self._settings.persist_feature_snapshots:
                await self._features.add_snapshot(
                    snapshot_id,
                    instrument_pk,
                    symbol,
                    outcome,
                    strategy_name=self._settings.name,
                    strategy_version=self._settings.version,
                    feature_schema_version="fs-1",
                    config_hash=config_hash,
                )
                metrics.increment("strategy.feature_snapshots_created")
            for timeframe, analysis in outcome.structure_by_timeframe.items():
                swings = await self._features.add_swings(
                    instrument_pk, list(analysis.swings), self._settings.version
                )
                events = await self._features.add_structure_events(
                    instrument_pk, list(analysis.events), self._settings.version
                )
                if swings:
                    metrics.increment("strategy.swings_detected", float(swings))
                if events:
                    metrics.increment("strategy.structure_events_detected", float(events))
                del timeframe
            if outcome.regime is not None:
                await self._regimes.record_if_changed(
                    instrument_pk,
                    "1h",
                    outcome.regime,
                    outcome.as_of,
                    self._settings.version,
                )
            self.degraded = False
        except Exception as exc:
            self.degraded = True
            metrics.increment("strategy.persistence_failures")
            self._log.error("feature_persist_failed", error=type(exc).__name__)
