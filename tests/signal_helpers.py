"""Deterministic factories for phase-10 signal lifecycle tests.

Default scenario: a bullish research signal with entry zone [100, 101],
technical invalidation 98, reference targets 104/107, watching entry.  The
default market state is neutral (no monitor fires): fresh healthy data
around 102.5, above the zone, below target 1, above the invalidation.
"""

from __future__ import annotations

import dataclasses
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from app.config import SignalLifecycleSettings
from app.signals.enums import EntryTriggerType, SignalState
from app.signals.models import (
    CandidateStateSnapshot,
    MarketStateSnapshot,
    MonitorContext,
    PlanSnapshot,
    SignalSnapshot,
)

NOW = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
SIGNAL_SETTINGS = SignalLifecycleSettings(_env_file=None)

PLAN_ID = uuid.UUID("00000000-0000-0000-0000-00000000aa01")
CANDIDATE_ID = uuid.UUID("00000000-0000-0000-0000-00000000bb01")
SIGNAL_ID = uuid.UUID("00000000-0000-0000-0000-00000000cc01")


def make_signal_settings(**overrides: Any) -> SignalLifecycleSettings:
    return SignalLifecycleSettings(_env_file=None, **overrides)


def make_plan(**overrides: Any) -> PlanSnapshot:
    defaults: dict[str, Any] = {
        "plan_id": PLAN_ID,
        "candidate_id": CANDIDATE_ID,
        "status": "ELIGIBLE",
        "instrument_pk": 1,
        "instrument_id": 1,
        "symbol": "BTC-PERP",
        "asset_class": "CRYPTO",
        "candidate_type": "BULLISH_SWEEP_REVERSAL",
        "direction": "BULLISH",
        "entry_low": 100.0,
        "entry_high": 101.0,
        "entry_reference_price": 100.5,
        "entry_basis": "RECLAIMED_LEVEL_RETEST",
        "invalidation_price": 98.0,
        "invalidation_basis": "SWEPT_SWING_LOW",
        "target_prices": (104.0, 107.0),
        "as_of": NOW - timedelta(minutes=2),
        "expiry_at": NOW + timedelta(minutes=30),
        "dedupe_key": "plandedupe0001",
        "strategy_version": "1.0.0",
        "risk_model_version": "1.0.0",
        "cost_model_version": "1.0.0",
        "fee_schedule_version": "polymarket-perps-assumed-1",
        "risk_config_hash": "riskcfg",
        "cost_config_hash": "costcfg",
    }
    defaults.update(overrides)
    return PlanSnapshot(**defaults)


def make_candidate_state(**overrides: Any) -> CandidateStateSnapshot:
    defaults: dict[str, Any] = {
        "candidate_id": CANDIDATE_ID,
        "state": "CONFIRMED",
        "expiry_at": NOW + timedelta(hours=1),
        "as_of": NOW - timedelta(minutes=5),
    }
    defaults.update(overrides)
    return CandidateStateSnapshot(**defaults)


def make_market(**overrides: Any) -> MarketStateSnapshot:
    """Neutral fresh market: above zone, below targets, above invalidation."""
    defaults: dict[str, Any] = {
        "mark_price": 102.5,
        "best_bid": 102.4,
        "best_ask": 102.6,
        "bbo_fresh": True,
        "book_fresh": True,
        "book_resyncing": False,
        "last_closed_5m_close": 102.5,
        "last_closed_5m_low": 102.2,
        "last_closed_5m_high": 102.8,
        "last_closed_5m_close_time": NOW - timedelta(minutes=1),
        "data_quality_status": "HEALTHY",
        "data_quality_ok": True,
        "snapshot_at": NOW - timedelta(seconds=5),
    }
    defaults.update(overrides)
    return MarketStateSnapshot(**defaults)


def make_signal(**overrides: Any) -> SignalSnapshot:
    defaults: dict[str, Any] = {
        "signal_id": SIGNAL_ID,
        "plan_id": PLAN_ID,
        "candidate_id": CANDIDATE_ID,
        "state": SignalState.WATCHING_ENTRY,
        "state_version": 2,
        "direction": "BULLISH",
        "candidate_type": "BULLISH_SWEEP_REVERSAL",
        "instrument_pk": 1,
        "instrument_id": 1,
        "symbol": "BTC-PERP",
        "asset_class": "CRYPTO",
        "entry_low": 100.0,
        "entry_high": 101.0,
        "entry_reference_price": 100.5,
        "invalidation_price": 98.0,
        "target_prices": (104.0, 107.0),
        "entry_trigger": EntryTriggerType.ZONE_TOUCH_AND_5M_CLOSE_CONFIRM,
        "expires_at": NOW + timedelta(minutes=30),
        "watching_entry_at": NOW - timedelta(minutes=5),
        "entry_confirmed_at": None,
        "data_degraded_since": None,
        "dedupe_key": "signaldedupe01",
        "correlation_id": "corr-test",
    }
    defaults.update(overrides)
    return SignalSnapshot(**defaults)


def bearish_signal(**overrides: Any) -> SignalSnapshot:
    """Mirror scenario: entry zone [104, 105], invalidation 107, targets down."""
    defaults: dict[str, Any] = {
        "direction": "BEARISH",
        "candidate_type": "BEARISH_SWEEP_REVERSAL",
        "entry_low": 104.0,
        "entry_high": 105.0,
        "entry_reference_price": 104.5,
        "invalidation_price": 107.0,
        "target_prices": (101.0, 98.0),
    }
    defaults.update(overrides)
    return make_signal(**defaults)


def make_context(**overrides: Any) -> MonitorContext:
    defaults: dict[str, Any] = {
        "signal": make_signal(),
        "plan": make_plan(),
        "candidate": make_candidate_state(),
        "market": make_market(),
        "session_state": "CRYPTO_24_7",
        "session_allowed": True,
        "watchlist_active": True,
        "market_quality_score": 85,
        "bot_paused": False,
        "opposing_structure_events": (),
        "superseding_plan_id": None,
        "as_of": NOW,
        "data_timestamps": {},
    }
    defaults.update(overrides)
    return MonitorContext(**defaults)


def post_entry_context(state: SignalState = SignalState.ACTIVE_RESEARCH, **overrides: Any):
    signal = overrides.pop("signal", None) or make_signal(
        state=state,
        state_version=4,
        entry_confirmed_at=NOW - timedelta(minutes=10),
        expires_at=NOW + timedelta(hours=2),
    )
    return make_context(signal=signal, **overrides)


def replace(obj: Any, **changes: Any) -> Any:
    return dataclasses.replace(obj, **changes)
