"""MonitorContext/AdmissionContext assembly from live adapters.

The only signals module that talks to repositories/services.  It converts
phase-9 plan rows, phase-8 candidate rows and phase-5 public market data
into the immutable snapshots the pure lifecycle core consumes - no REST
calls, no account data, no Telegram.  Missing or stale inputs produce
conservative snapshots (not fresh / not ok) instead of optimistic ones.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from app.config import SignalLifecycleSettings
from app.data.data_quality import DataQualityService
from app.data.market_data_service import MarketDataService
from app.data.orderbook_service import OrderbookManager
from app.domain.enums import Channel, FreshnessStatus, InstrumentQualityStatus, Timeframe
from app.repositories.candle_repository import CandleRepository
from app.repositories.feature_repository import FeatureRepository
from app.repositories.instrument_risk_repository import InstrumentRiskRepository
from app.repositories.orm import RiskPlanRecord, SignalLifecycleRecord
from app.repositories.risk_plan_repository import RiskPlanRepository
from app.repositories.setup_candidate_repository import SetupCandidateRepository
from app.signals.enums import EntryTriggerType, SignalState
from app.signals.models import (
    CandidateStateSnapshot,
    MarketStateSnapshot,
    MonitorContext,
    PlanSnapshot,
    SignalSnapshot,
)
from app.signals.version import lifecycle_configuration_hash

_FRESH = (FreshnessStatus.FRESH, FreshnessStatus.AGING)
_CANDLE_TIMEFRAME = Timeframe.M5
_CANDLE_WINDOW = timedelta(minutes=45)
_OPPOSING_EVENT_TYPE = "CHANGE_OF_CHARACTER"


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _floats(values: Any) -> tuple[float, ...]:
    out: list[float] = []
    for value in values or ():
        try:
            out.append(float(value))
        except (TypeError, ValueError):
            continue
    return tuple(out)


def plan_snapshot_from_record(row: RiskPlanRecord) -> PlanSnapshot | None:
    """Immutable plan view from the persisted phase-9 row.

    Returns None when the payload carries no complete model reference
    levels - such a plan can never be admitted (configuration invalid)."""
    payload = row.payload or {}
    levels = payload.get("model_reference_levels") or {}
    zone = levels.get("entry_zone") or []
    targets = _floats(target.get("price") for target in levels.get("reference_targets") or [])
    try:
        entry_low = float(zone[0])
        entry_high = float(zone[1])
        entry_reference = float(levels["entry_reference"])
        invalidation = float(levels["technical_invalidation"])
    except (IndexError, KeyError, TypeError, ValueError):
        return None
    as_of = _utc(row.as_of)
    expiry_at = _utc(row.expiry_at)
    assert as_of is not None and expiry_at is not None
    return PlanSnapshot(
        plan_id=row.id,
        candidate_id=row.candidate_id,
        status=row.status,
        instrument_pk=row.instrument_pk,
        instrument_id=int(row.instrument_id),
        symbol=row.symbol,
        asset_class=row.asset_class,
        candidate_type=row.candidate_type,
        direction=row.direction,
        entry_low=entry_low,
        entry_high=entry_high,
        entry_reference_price=entry_reference,
        entry_basis=str(levels.get("entry_basis") or "UNKNOWN"),
        invalidation_price=invalidation,
        invalidation_basis=str(levels.get("invalidation_basis") or "UNKNOWN"),
        target_prices=targets,
        as_of=as_of,
        expiry_at=expiry_at,
        dedupe_key=row.dedupe_key,
        strategy_version=row.strategy_version,
        risk_model_version=row.risk_model_version,
        cost_model_version=row.cost_model_version,
        fee_schedule_version=row.fee_schedule_version,
        risk_config_hash=row.risk_config_hash,
        cost_config_hash=row.cost_config_hash,
    )


def signal_snapshot_from_record(row: SignalLifecycleRecord) -> SignalSnapshot:
    targets = _floats((row.target_prices or {}).get("targets"))
    expires_at = _utc(row.expires_at)
    assert expires_at is not None
    return SignalSnapshot(
        signal_id=row.id,
        plan_id=row.plan_id,
        candidate_id=row.candidate_id,
        state=SignalState(row.state),
        state_version=int(row.state_version),
        direction=row.direction,
        candidate_type=row.candidate_type,
        instrument_pk=row.instrument_pk,
        instrument_id=int(row.instrument_id),
        symbol=row.symbol,
        asset_class=row.asset_class,
        entry_low=float(row.entry_low),
        entry_high=float(row.entry_high),
        entry_reference_price=float(row.entry_reference_price),
        invalidation_price=float(row.invalidation_price),
        target_prices=targets,
        entry_trigger=EntryTriggerType(row.entry_trigger),
        expires_at=expires_at,
        watching_entry_at=_utc(row.watching_entry_at),
        entry_confirmed_at=_utc(row.entry_confirmed_at),
        data_degraded_since=_utc(row.data_degraded_since),
        dedupe_key=row.dedupe_key,
        correlation_id=row.correlation_id,
    )


class SignalLifecycleContextBuilder:
    def __init__(
        self,
        settings: SignalLifecycleSettings,
        plan_repo: RiskPlanRepository,
        candidate_repo: SetupCandidateRepository,
        candle_repo: CandleRepository,
        feature_repo: FeatureRepository,
        risk_snapshot_repo: InstrumentRiskRepository,
        market_data: MarketDataService | None,
        data_quality: DataQualityService | None,
        books: OrderbookManager | None,
    ) -> None:
        self._settings = settings
        self._plans = plan_repo
        self._candidates = candidate_repo
        self._candles = candle_repo
        self._features = feature_repo
        self._risk_snapshots = risk_snapshot_repo
        self._market_data = market_data
        self._data_quality = data_quality
        self._books = books
        self.lifecycle_config_hash = lifecycle_configuration_hash(settings)

    # ------------------------------------------------------------- snapshots

    async def candidate_state(self, candidate_id: uuid.UUID) -> CandidateStateSnapshot | None:
        row = await self._candidates.get(candidate_id)
        if row is None:
            return None
        return CandidateStateSnapshot(
            candidate_id=row.id,
            state=row.state,
            expiry_at=_utc(row.expiry_at),
            as_of=_utc(row.as_of),
        )

    async def _last_closed_5m(
        self, instrument_pk: int, as_of: datetime
    ) -> tuple[float, float, float, datetime] | None:
        """(close, low, high, close_time) of the last CLOSED 5m candle."""
        duration = timedelta(milliseconds=_CANDLE_TIMEFRAME.milliseconds)
        rows = await self._candles.get_range(
            instrument_pk,
            _CANDLE_TIMEFRAME,
            as_of - _CANDLE_WINDOW,
            as_of + timedelta(seconds=1),
        )
        for row in reversed(rows):
            open_time = _utc(row.open_time)
            assert open_time is not None
            close_time = open_time + duration
            if close_time <= as_of:  # forming candles never confirm anything
                return float(row.close), float(row.low), float(row.high), close_time
        return None

    async def market_state(
        self, instrument_pk: int, instrument_id: int, as_of: datetime
    ) -> MarketStateSnapshot:
        tracker = (
            self._market_data.tracker_for(instrument_id) if self._market_data is not None else None
        )
        mark = (
            float(tracker.last_mark_price)
            if tracker is not None and tracker.last_mark_price is not None
            else None
        )
        bbo = tracker.last_bbo if tracker is not None else None
        best_bid = float(bbo.bid_price) if bbo is not None else None
        best_ask = float(bbo.ask_price) if bbo is not None else None
        bbo_at = datetime.fromtimestamp(bbo.ts_ms / 1000, tz=UTC) if bbo is not None else None
        bbo_fresh = (
            bbo is not None
            and self._data_quality is not None
            and self._data_quality.channel_freshness(instrument_id, Channel.BBO) in _FRESH
        )

        book_fresh = False
        book_resyncing = False
        if self._books is not None:
            book = self._books.book(instrument_id)
            book_resyncing = bool(book.needs_resync)
            book_fresh = (
                book.reliable
                and self._data_quality is not None
                and self._data_quality.channel_freshness(instrument_id, Channel.ORDERBOOK) in _FRESH
            )

        data_quality_status = "UNKNOWN"
        data_quality_ok = False
        if self._data_quality is not None:
            report = self._data_quality.evaluate(instrument_id)
            data_quality_status = report.status.value
            data_quality_ok = report.status is InstrumentQualityStatus.HEALTHY or (
                report.status is InstrumentQualityStatus.DEGRADED
                and self._settings.lifecycle_allow_degraded_data
            )

        candle = await self._last_closed_5m(instrument_pk, as_of)
        return MarketStateSnapshot(
            mark_price=mark,
            best_bid=best_bid,
            best_ask=best_ask,
            bbo_fresh=bbo_fresh,
            book_fresh=book_fresh,
            book_resyncing=book_resyncing,
            last_closed_5m_close=candle[0] if candle else None,
            last_closed_5m_low=candle[1] if candle else None,
            last_closed_5m_high=candle[2] if candle else None,
            last_closed_5m_close_time=candle[3] if candle else None,
            data_quality_status=data_quality_status,
            data_quality_ok=data_quality_ok,
            snapshot_at=bbo_at or (candle[3] if candle else None),
        )

    async def instrument_snapshot_age_seconds(
        self, instrument_pk: int, as_of: datetime
    ) -> float | None:
        row = await self._risk_snapshots.latest_for(instrument_pk)
        if row is None:
            return None
        snapshot_at = _utc(row.as_of)
        assert snapshot_at is not None
        return max(0.0, (as_of - snapshot_at).total_seconds())

    # -------------------------------------------------------- plan relations

    async def plan_relations(
        self, signal: SignalSnapshot
    ) -> tuple[PlanSnapshot | None, uuid.UUID | None]:
        """(own plan snapshot, superseding ELIGIBLE plan id) in one query."""
        rows = await self._plans.plans_for_candidate(signal.candidate_id)
        own_row = next((row for row in rows if row.id == signal.plan_id), None)
        own = plan_snapshot_from_record(own_row) if own_row is not None else None
        superseding: uuid.UUID | None = None
        own_created = _utc(own_row.created_at) if own_row is not None else None
        for row in rows:  # ascending created_at - the last match is the newest
            if row.id == signal.plan_id or row.status != "ELIGIBLE":
                continue
            created = _utc(row.created_at)
            if own_created is None or (created is not None and created >= own_created):
                superseding = row.id
        return own, superseding

    async def opposing_structure_events(self, signal: SignalSnapshot) -> tuple[str, ...]:
        """Confirmed opposing ChoCh signatures observed after entry."""
        anchor = signal.entry_confirmed_at
        if anchor is None:
            return ()
        marker = "bearish ChoCh" if signal.bullish else "bullish ChoCh"
        rows = await self._features.structure_events_since(signal.instrument_pk, anchor)
        events = []
        for row in rows:
            if row.event_type != _OPPOSING_EVENT_TYPE or not row.detail:
                continue
            if marker not in row.detail:
                continue
            confirmed = _utc(row.confirm_close_time)
            assert confirmed is not None
            events.append(
                f"{row.timeframe} {marker} @ {float(row.price):.6g} "
                f"(confirmed {confirmed.isoformat()})"
            )
        return tuple(events[:5])

    # ----------------------------------------------------------------- build

    async def monitor_context(
        self,
        row: SignalLifecycleRecord,
        *,
        session_state: str,
        session_allowed: bool,
        watchlist_active: bool,
        market_quality_score: int | None,
        bot_paused: bool,
        as_of: datetime,
    ) -> MonitorContext:
        signal = signal_snapshot_from_record(row)
        plan, superseding = await self.plan_relations(signal)
        candidate = await self.candidate_state(signal.candidate_id)
        market = await self.market_state(signal.instrument_pk, signal.instrument_id, as_of)
        opposing = await self.opposing_structure_events(signal)
        return MonitorContext(
            signal=signal,
            plan=plan,
            candidate=candidate,
            market=market,
            session_state=session_state,
            session_allowed=session_allowed,
            watchlist_active=watchlist_active,
            market_quality_score=market_quality_score,
            bot_paused=bot_paused,
            opposing_structure_events=opposing,
            superseding_plan_id=superseding,
            as_of=as_of,
            data_timestamps={
                "evaluated_at": as_of.isoformat(),
                "market_snapshot_at": (
                    market.snapshot_at.isoformat() if market.snapshot_at else "none"
                ),
                "last_closed_5m_close_time": (
                    market.last_closed_5m_close_time.isoformat()
                    if market.last_closed_5m_close_time
                    else "none"
                ),
            },
        )
