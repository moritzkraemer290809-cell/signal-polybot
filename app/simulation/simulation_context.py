"""Shadow-mode input assembly from persisted public data.

The only simulation module that reads repositories/services.  It turns a
persisted phase-10 lifecycle plus phase-5 public market data into the
immutable snapshot the pure simulation core consumes.  No REST calls, no
WebSocket, no account data, no Telegram.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from app.config import CostSettings, ShadowSimulationSettings
from app.costs.models import (
    DepthLevel,
    FeeScheduleSnapshot,
    FundingSnapshot,
    OrderbookDepthSnapshot,
)
from app.data.data_quality import DataQualityService
from app.data.market_data_service import MarketDataService
from app.data.orderbook_service import OrderbookManager
from app.domain.enums import Channel, FreshnessStatus, InstrumentQualityStatus
from app.repositories.funding_repository import FundingRateRepository
from app.repositories.orderbook_repository import OrderbookSnapshotRepository
from app.repositories.orm import RiskPlanRecord, SignalLifecycleRecord
from app.repositories.risk_plan_repository import RiskPlanRepository
from app.repositories.setup_candidate_repository import SetupCandidateRepository
from app.repositories.signal_event_repository import SignalEventRepository
from app.simulation.enums import SimulationExitReason
from app.simulation.models import (
    LifecycleObservation,
    MarketReferenceSnapshot,
    SimulationInputs,
    SimulationPlanReference,
)
from app.simulation.simulated_lifecycle import (
    exit_reason_for_event,
    exit_reason_for_state,
    is_terminal_state,
)

_FRESH = (FreshnessStatus.FRESH, FreshnessStatus.AGING)


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def plan_reference_from_records(
    plan_row: RiskPlanRecord, signal_row: SignalLifecycleRecord
) -> SimulationPlanReference | None:
    """Audited reference values of the underlying phase-9 plan."""
    payload = plan_row.payload or {}
    levels = payload.get("model_reference_levels") or {}
    position = payload.get("reference_position") or {}
    targets = tuple(
        float(target["price"])
        for target in levels.get("reference_targets") or []
        if target.get("price") is not None
    )
    quantity = position.get("reference_quantity")
    notional = position.get("reference_notional")
    if quantity is None or notional is None or not targets:
        return None
    entry_reference = levels.get("entry_reference")
    invalidation = levels.get("technical_invalidation")
    if entry_reference is None or invalidation is None:
        return None
    risk_per_unit = abs(float(entry_reference) - float(invalidation))
    if risk_per_unit <= 0:
        return None
    return SimulationPlanReference(
        plan_id=plan_row.id,
        candidate_id=plan_row.candidate_id,
        instrument_pk=plan_row.instrument_pk,
        instrument_id=int(plan_row.instrument_id),
        symbol=plan_row.symbol,
        asset_class=plan_row.asset_class,
        candidate_type=plan_row.candidate_type,
        direction=plan_row.direction,
        entry_reference_price=float(entry_reference),
        invalidation_price=float(invalidation),
        target_prices=targets,
        reference_quantity=float(quantity),
        reference_notional=float(notional),
        risk_per_unit=risk_per_unit,
        virtual_account_pusd=float(position.get("virtual_account_pusd", 0.0) or 0.0),
        strategy_version=plan_row.strategy_version,
        risk_model_version=plan_row.risk_model_version,
        cost_model_version=plan_row.cost_model_version,
        fee_schedule_version=plan_row.fee_schedule_version,
        execution_assumption_version=plan_row.execution_assumption_version,
    )


class ShadowSimulationContextBuilder:
    """Builds :class:`SimulationInputs` from persisted phase 5-10 state."""

    def __init__(
        self,
        settings: ShadowSimulationSettings,
        cost_settings: CostSettings,
        plan_repo: RiskPlanRepository,
        candidate_repo: SetupCandidateRepository,
        signal_event_repo: SignalEventRepository,
        funding_repo: FundingRateRepository,
        orderbook_repo: OrderbookSnapshotRepository,
        market_data: MarketDataService | None,
        data_quality: DataQualityService | None,
        books: OrderbookManager | None,
    ) -> None:
        self._settings = settings
        self._costs = cost_settings
        self._plans = plan_repo
        self._candidates = candidate_repo
        self._signal_events = signal_event_repo
        self._funding = funding_repo
        self._orderbooks = orderbook_repo
        self._market_data = market_data
        self._data_quality = data_quality
        self._books = books

    # ------------------------------------------------------------- snapshots

    def market_reference(self, instrument_id: int, as_of: datetime) -> MarketReferenceSnapshot:
        """Public market state for a modelled leg (live in-memory state)."""
        tracker = (
            self._market_data.tracker_for(instrument_id) if self._market_data is not None else None
        )
        bbo = tracker.last_bbo if tracker is not None else None
        best_bid = float(bbo.bid_price) if bbo is not None else None
        best_ask = float(bbo.ask_price) if bbo is not None else None
        bbo_at = datetime.fromtimestamp(bbo.ts_ms / 1000, tz=UTC) if bbo is not None else None
        bids: tuple[DepthLevel, ...] = ()
        asks: tuple[DepthLevel, ...] = ()
        book_at: datetime | None = None
        fresh = False
        if self._books is not None:
            book = self._books.book(instrument_id)
            max_levels = self._costs.orderbook_max_levels
            bids = tuple(
                DepthLevel(price=float(level.price), quantity=float(level.quantity))
                for level in book.levels("bid", max_levels)
            )
            asks = tuple(
                DepthLevel(price=float(level.price), quantity=float(level.quantity))
                for level in book.levels("ask", max_levels)
            )
            book_at = _utc(book.last_reliable_update)
            fresh = (
                book.reliable
                and self._data_quality is not None
                and self._data_quality.channel_freshness(instrument_id, Channel.ORDERBOOK) in _FRESH
            )
        status = "UNKNOWN"
        ok = False
        if self._data_quality is not None:
            report = self._data_quality.evaluate(instrument_id)
            status = report.status.value
            ok = report.status is InstrumentQualityStatus.HEALTHY
        return MarketReferenceSnapshot(
            as_of=as_of,
            book=OrderbookDepthSnapshot(
                instrument_id=instrument_id,
                bids=bids,
                asks=asks,
                fresh=fresh,
                snapshot_at=book_at,
            ),
            best_bid=best_bid,
            best_ask=best_ask,
            mark_price=(
                float(tracker.last_mark_price)
                if tracker is not None and tracker.last_mark_price is not None
                else None
            ),
            bbo_at=bbo_at,
            book_at=book_at,
            data_quality_status=status,
            data_quality_ok=ok,
            snapshot_id=uuid.uuid4(),
        )

    async def funding_snapshot(
        self, instrument_pk: int, instrument_id: int, as_of: datetime
    ) -> FundingSnapshot | None:
        tracker = (
            self._market_data.tracker_for(instrument_id) if self._market_data is not None else None
        )
        since = as_of - timedelta(hours=self._costs.funding_lookback_hours)
        try:
            history = await self._funding.recent_rates(instrument_pk, since)
        except Exception:
            return None
        normalized = tuple(
            (stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC), rate) for stamp, rate in history
        )
        current = (
            float(tracker.last_funding_rate)
            if tracker is not None and tracker.last_funding_rate is not None
            else None
        )
        if current is None and not normalized:
            return None
        return FundingSnapshot(
            current_rate=current,
            history=normalized,
            next_funding_at=tracker.last_next_funding_at if tracker is not None else None,
            interval_hours=self._costs.default_funding_interval_hours,
            data_fresh=bool(normalized) or current is not None,
        )

    # ----------------------------------------------------------------- build

    async def lifecycle_observations(
        self, signal_row: SignalLifecycleRecord
    ) -> tuple[
        LifecycleObservation | None, LifecycleObservation | None, SimulationExitReason | None
    ]:
        """Entry and (optional) exit observations from the audit trail."""
        try:
            events = await self._signal_events.events_for(signal_row.id)
        except Exception:
            return None, None, None
        entry: LifecycleObservation | None = None
        exit_observation: LifecycleObservation | None = None
        reason: SimulationExitReason | None = None
        for event in events:
            as_of = _utc(event.as_of)
            assert as_of is not None
            observation = LifecycleObservation(
                signal_id=signal_row.id,
                plan_id=signal_row.plan_id,
                candidate_id=signal_row.candidate_id,
                state=str(event.to_state or signal_row.state),
                event_type=str(event.event_type),
                state_version=int(event.state_version),
                as_of=as_of,
                detail=dict(event.detail or {}),
            )
            if event.event_type == "ENTRY_CONFIRMED" and entry is None:
                entry = observation
                continue
            mapped = exit_reason_for_event(str(event.event_type))
            if mapped is not None and entry is not None:
                exit_observation = observation
                reason = mapped
        if exit_observation is None and is_terminal_state(signal_row.state):
            reason = exit_reason_for_state(signal_row.state)
            terminal_at = _utc(signal_row.terminal_at)
            if reason is not None and terminal_at is not None:
                exit_observation = LifecycleObservation(
                    signal_id=signal_row.id,
                    plan_id=signal_row.plan_id,
                    candidate_id=signal_row.candidate_id,
                    state=signal_row.state,
                    event_type=signal_row.state,
                    state_version=int(signal_row.state_version),
                    as_of=terminal_at,
                )
        return entry, exit_observation, reason

    async def build_inputs(
        self,
        signal_row: SignalLifecycleRecord,
        *,
        run_id: uuid.UUID,
        fee_schedule: FeeScheduleSnapshot | None,
        bot_paused: bool,
        duplicate_exists: bool,
        session_allowed: bool,
        session_state: str,
        as_of: datetime,
        entry_market: MarketReferenceSnapshot | None = None,
    ) -> SimulationInputs | None:
        """Assemble one shadow simulation input snapshot."""
        plan_rows = await self._plans.plans_for_candidate(signal_row.candidate_id)
        plan_row = next((row for row in plan_rows if row.id == signal_row.plan_id), None)
        plan = plan_reference_from_records(plan_row, signal_row) if plan_row is not None else None
        if plan is None:
            return None
        candidate_row = await self._candidates.get(signal_row.candidate_id)
        entry, exit_observation, reason = await self.lifecycle_observations(signal_row)
        if entry is None:
            return None
        market_now = self.market_reference(plan.instrument_id, as_of)
        funding = await self.funding_snapshot(plan.instrument_pk, plan.instrument_id, as_of)
        return SimulationInputs(
            lifecycle_signal_id=signal_row.id,
            plan=plan,
            entry_observation=entry,
            exit_observation=exit_observation,
            entry_market=entry_market or market_now,
            exit_market=market_now,
            fee_schedule=fee_schedule,
            funding=funding,
            funding_interval_hours=self._costs.default_funding_interval_hours,
            session_allowed=session_allowed,
            session_state=session_state,
            data_quality_ok=market_now.data_quality_ok,
            bot_paused=bot_paused,
            duplicate_exists=duplicate_exists,
            plan_audit_available=plan_row is not None,
            candidate_audit_available=candidate_row is not None,
            run_id=run_id,
            as_of=as_of,
            exit_reason=reason,
            metadata={
                "mode": "SHADOW",
                "session_state": session_state,
                "strategy_version": plan.strategy_version,
                "risk_model_version": plan.risk_model_version,
                "cost_model_version": plan.cost_model_version,
                "fee_schedule_version": plan.fee_schedule_version,
                "risk_per_unit": plan.risk_per_unit,
            },
        )


def market_metadata(inputs: SimulationInputs) -> dict[str, Any]:
    """Serializable provenance of one shadow simulation input snapshot."""
    return {
        "entry_market_at": (inputs.entry_market.as_of.isoformat() if inputs.entry_market else None),
        "exit_market_at": (inputs.exit_market.as_of.isoformat() if inputs.exit_market else None),
        "fee_schedule_version": (inputs.fee_schedule.version if inputs.fee_schedule else None),
        "funding_available": inputs.funding is not None,
    }
