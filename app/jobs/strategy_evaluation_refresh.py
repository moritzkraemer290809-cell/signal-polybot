"""Strategy evaluation refresh job (phase 8).

Evaluates only ACTIVE watchlist instruments on an interval, with an
additional discipline: a full evaluation for an instrument only runs when a
NEW closed 5m candle exists since the last evaluation (never on open candles
alone).  Overlap lock, bounded concurrency, no REST calls - only DB/cache
snapshots.  In global PAUSED mode no new candidates are created and actives
whose data validity ends are expired.  DB outages degrade the subsystem;
unpersisted candidate state changes are never reported as successful.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from app.bot_state import BotStateService
from app.config import StrategySettings
from app.domain.enums import AssetClass, Timeframe
from app.observability.logging import get_logger
from app.observability.metrics import metrics
from app.repositories.candle_repository import CandleRepository
from app.repositories.instrument_repository import InstrumentRepository
from app.repositories.setup_candidate_repository import SetupCandidateRepository
from app.repositories.strategy_decision_repository import StrategyDecisionRepository
from app.selection.market_selection_engine import session_verdict_for_asset_class
from app.selection.selection_scheduler import MarketSelectionCoordinator
from app.strategy.enums import CandidateState, RejectionCode, StrategySubsystemState
from app.strategy.evaluation_context import ContextBuildError, EvaluationContextBuilder
from app.strategy.feature_store import FeatureStore
from app.strategy.models import EvaluationOutcome, SetupCandidate
from app.strategy.strategy_engine import evaluate_context

_DATA_REJECTIONS = {
    RejectionCode.DATA_QUALITY_NOT_SUFFICIENT,
    RejectionCode.ORDERBOOK_NOT_FRESH,
    RejectionCode.CANDLE_GAP,
    RejectionCode.INSUFFICIENT_CANDLE_HISTORY,
    RejectionCode.OPEN_CANDLE_ONLY,
}


class StrategyEvaluationJob:
    def __init__(
        self,
        settings: StrategySettings,
        context_builder: EvaluationContextBuilder,
        feature_store: FeatureStore,
        candidate_repo: SetupCandidateRepository,
        decision_repo: StrategyDecisionRepository,
        candle_repo: CandleRepository,
        instrument_repo: InstrumentRepository,
        selection: MarketSelectionCoordinator | None,
        bot_state: BotStateService | None,
        *,
        now_fn: Callable[[], datetime] = lambda: datetime.now(tz=UTC),
    ) -> None:
        self._settings = settings
        self._builder = context_builder
        self._store = feature_store
        self._candidates = candidate_repo
        self._decisions = decision_repo
        self._candles = candle_repo
        self._instruments = instrument_repo
        self._selection = selection
        self._bot_state = bot_state
        self._now = now_fn
        self._log = get_logger("strategy_evaluation")

        self._task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._wakeup = asyncio.Event()
        self._force_all = False
        self._last_evaluated_5m: dict[int, datetime] = {}
        self._recent_terminal_dedupe: dict[str, datetime] = {}

        self.degraded = False
        self.last_run_at: datetime | None = None
        self.last_success_at: datetime | None = None
        self.last_run_summary: dict[str, Any] = {}
        self.per_instrument: dict[str, dict[str, Any]] = {}
        self.evaluations_total = 0
        self.evaluations_skipped = 0
        self.evaluations_failed = 0

    # ------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="strategy_evaluation")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    @property
    def alive(self) -> bool:
        return self._task is not None and not self._task.done()

    def trigger(self, *, force: bool = True) -> None:
        """Immediate re-evaluation (watchlist/universe change)."""
        self._force_all = self._force_all or force
        self._wakeup.set()

    @property
    def state(self) -> StrategySubsystemState:
        if not self._settings.enabled:
            return StrategySubsystemState.DISABLED
        if self._task is None or self._task.done():
            return StrategySubsystemState.UNAVAILABLE
        if self.degraded or self._store.degraded:
            return StrategySubsystemState.DEGRADED
        return StrategySubsystemState.HEALTHY

    async def _run(self) -> None:
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.degraded = True
                self.evaluations_failed += 1
                metrics.increment("strategy.evaluations_failed")
                self._log.error("strategy_cycle_failed", error=type(exc).__name__)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    self._wakeup.wait(), timeout=self._settings.evaluation_refresh_seconds
                )
            self._wakeup.clear()

    # ----------------------------------------------------------------- cycle

    async def run_once(self) -> dict[str, Any]:
        if self._lock.locked():
            metrics.increment("strategy.overlap_skipped")
            return self.last_run_summary
        async with self._lock:
            return await self._cycle()

    async def _cycle(self) -> dict[str, Any]:
        started = time.monotonic()
        now = self._now()
        # degradation reflects THIS cycle: any persistence failure below sets
        # it again; a fully clean cycle recovers the subsystem.
        self.degraded = False
        force = self._force_all
        self._force_all = False
        paused = False
        if self._bot_state is not None:
            with contextlib.suppress(Exception):
                paused = await self._bot_state.is_paused()

        rows = await self._active_watchlist()
        evaluated = skipped = candidates_found = 0
        semaphore = asyncio.Semaphore(max(1, self._settings.max_concurrent_evaluations))

        async def evaluate_one(entry: dict[str, Any]) -> None:
            nonlocal evaluated, skipped, candidates_found
            async with semaphore:
                if not force and not await self._new_5m_close(entry["instrument_pk"], now):
                    skipped += 1
                    self.evaluations_skipped += 1
                    metrics.increment("strategy.evaluations_skipped")
                    return
                outcome = await self._evaluate_instrument(entry, now, paused)
                evaluated += 1
                self.evaluations_total += 1
                metrics.increment("strategy.evaluations_total")
                if outcome is not None:
                    candidates_found += len(outcome.candidates)

        await asyncio.gather(*(evaluate_one(entry) for entry in rows))
        await self._expire_candidates(now)
        self._prune_dedupe_memory(now)

        self.last_run_at = now
        duration = time.monotonic() - started
        metrics.increment("strategy.evaluation_duration_seconds", duration)
        self.last_run_summary = {
            "run_at": now.isoformat(),
            "watchlist_instruments": len(rows),
            "evaluated": evaluated,
            "skipped_no_new_candle": skipped,
            "candidates_found": candidates_found,
            "paused": paused,
            "duration_seconds": round(duration, 3),
        }
        if not self.degraded and not self._store.degraded:
            self.last_success_at = now
        return self.last_run_summary

    # ------------------------------------------------------------- helpers

    async def _active_watchlist(self) -> list[dict[str, Any]]:
        if self._selection is None:
            return []
        rows = await self._selection.active_watchlist_rows()
        if not rows:
            return []
        try:
            instruments = {row.id: row for row in await self._instruments.list_all()}
        except Exception:
            self.degraded = True
            return []
        equity = self._selection.last_equity_snapshot
        crypto = self._selection.last_crypto_snapshot
        entries: list[dict[str, Any]] = []
        for row in rows:
            instrument = instruments.get(row.instrument_pk)
            if instrument is None:
                continue
            session_state, session_allowed, limited = "UNKNOWN", False, False
            if equity is not None and crypto is not None:
                try:
                    verdict = session_verdict_for_asset_class(
                        AssetClass(row.asset_class), equity, crypto
                    )
                    session_state = verdict.session_state
                    session_allowed = verdict.blocking_status is None
                    limited = verdict.limited_liquidity
                except ValueError:
                    pass
            quality = row.quality_score
            low_liquidity = limited or (
                quality is not None
                and quality
                < float(self._settings.regime_rules_json.get("low_liquidity_quality_below", 60))
            )
            entries.append(
                {
                    "instrument_pk": row.instrument_pk,
                    "instrument_id": instrument.instrument_id,
                    "symbol": row.symbol,
                    "asset_class": row.asset_class,
                    "quality_score": quality,
                    "session_state": session_state,
                    "session_allowed": session_allowed,
                    "low_liquidity": low_liquidity,
                }
            )
        return entries

    async def _new_5m_close(self, instrument_pk: int, now: datetime) -> bool:
        """True when a new CLOSED 5m candle exists since the last evaluation."""
        try:
            latest_open = await self._candles.latest_open_time(instrument_pk, Timeframe.M5)
        except Exception:
            self.degraded = True
            return False
        if latest_open is None:
            return False
        if latest_open.tzinfo is None:
            latest_open = latest_open.replace(tzinfo=UTC)
        five = timedelta(minutes=5)
        latest_closed = latest_open if latest_open + five <= now else latest_open - five
        previous = self._last_evaluated_5m.get(instrument_pk)
        if previous is not None and latest_closed <= previous:
            return False
        self._last_evaluated_5m[instrument_pk] = latest_closed
        return True

    async def _evaluate_instrument(
        self, entry: dict[str, Any], now: datetime, paused: bool
    ) -> EvaluationOutcome | None:
        symbol = entry["symbol"]
        try:
            context = await self._builder.build(
                instrument_pk=entry["instrument_pk"],
                instrument_id=entry["instrument_id"],
                symbol=symbol,
                asset_class=entry["asset_class"],
                watchlist_active=True,
                bot_paused=paused,
                session_state=entry["session_state"],
                session_allowed=entry["session_allowed"],
                market_quality_score=entry["quality_score"],
                low_liquidity=entry["low_liquidity"],
                as_of=now,
            )
        except ContextBuildError as error:
            metrics.increment("strategy.feature_data_gap_count")
            await self._record_rejections(
                entry,
                [
                    build_rejection_from_gap(
                        entry, now, error, self._settings, self._builder.config_hash
                    )
                ],
            )
            await self._expire_for_data_loss(entry["instrument_pk"], error.detail)
            self._note_instrument(entry, None, [error.code.value])
            return None
        except Exception as exc:
            self.evaluations_failed += 1
            metrics.increment("strategy.evaluations_failed")
            self._log.error("context_build_failed", symbol=symbol, error=type(exc).__name__)
            return None

        snapshot_id = uuid.uuid4()
        outcome = evaluate_context(context, self._settings, feature_snapshot_id=snapshot_id)
        if outcome.regime is not None:
            metrics.increment(f"strategy.regime.{outcome.regime.regime.value.lower()}")
            await self._store.persist(
                entry["instrument_pk"], symbol, outcome, snapshot_id, context.config_hash
            )
        await self._record_rejections(entry, list(outcome.rejections))
        if any(r.primary_code in _DATA_REJECTIONS for r in outcome.rejections):
            await self._expire_for_data_loss(entry["instrument_pk"], "data validity ended")
        if not paused:
            for candidate in outcome.candidates:
                await self._admit_candidate(candidate, now)
        self._note_instrument(entry, outcome, [r.primary_code.value for r in outcome.rejections])
        return outcome

    async def _admit_candidate(self, candidate: SetupCandidate, now: datetime) -> None:
        try:
            existing = await self._candidates.active_by_dedupe_key(candidate.dedupe_key)
            if existing is not None:
                if (
                    existing.state == CandidateState.DETECTED.value
                    and candidate.state is CandidateState.CONFIRMED
                ):
                    if await self._candidates.transition(
                        existing.id,
                        CandidateState.CONFIRMED,
                        {"reason": "additional confirmation"},
                        score=candidate.setup_score,
                    ):
                        metrics.increment("strategy.setup_candidates_confirmed")
                else:
                    metrics.increment("strategy.candidate_dedupe_suppressed")
                return
            terminal_at = self._recent_terminal_dedupe.get(candidate.dedupe_key)
            if (
                terminal_at is not None
                and (now - terminal_at).total_seconds() < self._settings.candidate_dedupe_seconds
            ):
                metrics.increment("strategy.candidate_dedupe_suppressed")
                return
            actives = await self._candidates.active_candidates(candidate.instrument_pk)
            opposite = [row for row in actives if row.direction != candidate.direction.value]
            if candidate.state is CandidateState.CONFIRMED:
                for row in opposite:
                    if (
                        candidate.setup_score > row.setup_score
                        and await self._candidates.transition(
                            row.id,
                            CandidateState.SUPERSEDED,
                            {"superseded_by": str(candidate.candidate_id)},
                        )
                    ):
                        metrics.increment("strategy.setup_candidates_superseded")
                        self._recent_terminal_dedupe[row.dedupe_key] = now
                actives = await self._candidates.active_candidates(candidate.instrument_pk)
            if len(actives) >= self._settings.max_active_candidates_per_instrument:
                metrics.increment("strategy.candidate_dedupe_suppressed")
                return
            if await self._candidates.insert_new(candidate):
                metrics.increment("strategy.setup_candidates_detected")
                if candidate.state is CandidateState.CONFIRMED:
                    metrics.increment("strategy.setup_candidates_confirmed")
                self._log.info(
                    "setup_candidate_recorded",
                    symbol=candidate.symbol,
                    candidate_type=candidate.candidate_type.value,
                    state=candidate.state.value,
                    score=candidate.setup_score,
                    note="research candidate - not a trade signal",
                )
        except Exception as exc:
            # DB down: never report an unpersisted candidate as successful
            self.degraded = True
            self._log.error("candidate_persist_failed", error=type(exc).__name__)

    async def _expire_candidates(self, now: datetime) -> None:
        try:
            for row in await self._candidates.active_candidates():
                expiry = (
                    row.expiry_at if row.expiry_at.tzinfo else row.expiry_at.replace(tzinfo=UTC)
                )
                if expiry <= now and await self._candidates.transition(
                    row.id, CandidateState.EXPIRED, {"reason": "evaluation expiry"}
                ):
                    metrics.increment("strategy.setup_candidates_expired")
                    self._recent_terminal_dedupe[row.dedupe_key] = now
        except Exception as exc:
            self.degraded = True
            self._log.error("candidate_expiry_failed", error=type(exc).__name__)

    async def _expire_for_data_loss(self, instrument_pk: int, reason: str) -> None:
        with contextlib.suppress(Exception):
            for row in await self._candidates.active_candidates(instrument_pk):
                if await self._candidates.transition(
                    row.id, CandidateState.EXPIRED, {"reason": f"data validity ended: {reason}"}
                ):
                    metrics.increment("strategy.setup_candidates_expired")
                    self._recent_terminal_dedupe[row.dedupe_key] = self._now()

    async def _record_rejections(self, entry: dict[str, Any], rejections: list[Any]) -> None:
        for rejection in rejections:
            metrics.increment(f"strategy.rejections.{rejection.primary_code.value.lower()}")
            try:
                await self._decisions.record_rejection(
                    rejection, self._settings.rejection_persist_seconds
                )
            except Exception as exc:
                self.degraded = True
                self._log.error("rejection_persist_failed", error=type(exc).__name__)

    def _note_instrument(
        self, entry: dict[str, Any], outcome: EvaluationOutcome | None, rejection_codes: list[str]
    ) -> None:
        self.per_instrument[entry["symbol"]] = {
            "regime": outcome.regime.regime.value if outcome and outcome.regime else None,
            "regime_confidence": (
                outcome.regime.confidence if outcome and outcome.regime else None
            ),
            "structure_1h": (
                outcome.structure_by_timeframe["1h"].state.value
                if outcome and "1h" in outcome.structure_by_timeframe
                else None
            ),
            "candidates": len(outcome.candidates) if outcome else 0,
            "last_rejections": rejection_codes[:5],
            "data_fresh": outcome is not None,
            "as_of": (outcome.as_of.isoformat() if outcome else self._now().isoformat()),
        }

    def _prune_dedupe_memory(self, now: datetime) -> None:
        horizon = self._settings.candidate_dedupe_seconds * 2
        self._recent_terminal_dedupe = {
            key: value
            for key, value in self._recent_terminal_dedupe.items()
            if (now - value).total_seconds() < horizon
        }

    # -------------------------------------------------------------- reports

    async def health_stats(self) -> dict[str, Any]:
        active = 0
        with contextlib.suppress(Exception):
            active = len(await self._candidates.active_candidates())
        return {
            "state": self.state.value,
            "job_alive": self.alive,
            "watchlist_instruments": self.last_run_summary.get("watchlist_instruments", 0),
            "active_candidates": active,
            "evaluations_total": self.evaluations_total,
            "evaluations_failed": self.evaluations_failed,
            "last_success_at": (self.last_success_at.isoformat() if self.last_success_at else None),
        }

    async def status_stats(self) -> dict[str, Any]:
        stats = await self.health_stats()
        stats.update(
            {
                "strategy_name": self._settings.name,
                "strategy_version": self._settings.version,
                "config_hash": self._builder.config_hash,
                "last_run": self.last_run_summary,
                "instruments": self.per_instrument,
                "note": "Research candidates only - not trade signals.",
            }
        )
        return stats

    async def dashboard_details(self) -> dict[str, Any] | None:
        try:
            candidates = await self._candidates.recent(limit=20)
            rejections = await self._decisions.recent(limit=20)
            counts = await self._candidates.counts_by_state()
        except Exception:
            return None
        return {
            "disclaimer": "Research-Ausgabe. Kein Trade-Signal. Keine Renditeprognose.",
            "candidate_counts_by_state": counts,
            "recent_candidates": [
                {
                    "candidate_id": str(row.id),
                    "symbol": row.symbol,
                    "type": row.candidate_type,
                    "direction": f"{row.direction} (research classification)",
                    "state": row.state,
                    "score": row.setup_score,
                    "confidence": row.confidence,
                    "regime": row.primary_regime,
                    "retest": row.retest_status,
                    "as_of": row.as_of.isoformat(),
                    "expiry_at": row.expiry_at.isoformat(),
                    "reason_summary": row.reason_summary,
                }
                for row in candidates
            ],
            "recent_rejections": [
                {
                    "symbol": row.symbol,
                    "code": row.primary_code,
                    "candidate_type": row.candidate_type or None,
                    "count": row.count,
                    "detail": row.detail,
                    "last_as_of": row.last_as_of.isoformat(),
                }
                for row in rejections
            ],
        }


def build_rejection_from_gap(
    entry: dict[str, Any],
    now: datetime,
    error: ContextBuildError,
    settings: StrategySettings,
    config_hash: str,
) -> Any:
    from app.strategy.models import SetupRejection

    return SetupRejection(
        rejection_id=uuid.uuid4(),
        instrument_pk=entry["instrument_pk"],
        instrument_id=entry["instrument_id"],
        symbol=entry["symbol"],
        as_of=now,
        primary_code=error.code,
        codes=(error.code,),
        detail=error.detail,
        strategy_name=settings.name,
        strategy_version=settings.version,
        config_hash=config_hash,
    )
