"""Synthetic builders for phase-9 risk/cost tests.

All data is clearly artificial local fixture data - no market recordings,
no account data.  ``eligible_context()`` produces a scenario that passes
every conservative gate; individual tests perturb exactly one aspect.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from app.config import CostSettings, RiskSettings
from app.costs.execution_assumptions import build_execution_assumptions
from app.costs.models import (
    DepthLevel,
    FeeScheduleSnapshot,
    FundingSnapshot,
    OrderbookDepthSnapshot,
)
from app.costs.version import cost_configuration_hash
from app.risk.models import (
    CandidateSnapshot,
    InstrumentRiskSnapshot,
    RiskEvaluationContext,
    TechnicalLevel,
)
from app.risk.version import INSTRUMENT_RISK_SNAPSHOT_VERSION, risk_configuration_hash

AS_OF = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)

#: base-scenario settings: a wider ATR-driven invalidation buffer so the
#: technical risk distance comfortably dominates fees/slippage
RISK_SETTINGS = RiskSettings(_env_file=None, invalidation_buffer_atr_multiple=1.5)
COST_SETTINGS = CostSettings(_env_file=None)


def make_candidate(**overrides: Any) -> CandidateSnapshot:
    defaults: dict[str, Any] = dict(
        candidate_id=uuid.uuid4(),
        candidate_type="BULLISH_SWEEP_REVERSAL",
        direction="BULLISH",
        state="CONFIRMED",
        instrument_pk=10,
        instrument_id=1,
        symbol="BTC-PERP",
        asset_class="CRYPTO",
        as_of=AS_OF,
        expiry_at=AS_OF + timedelta(minutes=30),
        setup_score=85,
        primary_regime="TREND_UP",
        referenced_levels=({"type": "SWING_LOW", "price": 99.5, "timeframe": "15m"},),
        invalidation_conditions=("5m close below swept level",),
        strategy_name="market_structure_v1",
        strategy_version="1.0.0",
        strategy_config_hash="stratcfg",
        features={"atr_percentile_1h": 50.0},
    )
    defaults.update(overrides)
    return CandidateSnapshot(**defaults)


def make_instrument(**overrides: Any) -> InstrumentRiskSnapshot:
    defaults: dict[str, Any] = dict(
        instrument_pk=10,
        instrument_id=1,
        symbol="BTC-PERP",
        status="ACTIVE",
        asset_class="CRYPTO",
        mark_price=100.0,
        index_price=100.02,
        mid_price=99.575,
        last_price=None,
        max_leverage=10,
        min_notional=10.0,
        tick_size=None,
        price_decimals=2,
        quantity_decimals=3,
        risk_tiers=({"lower_bound": 0, "max_leverage": 10},),
        maintenance_margin_rate=0.05,
        initial_margin_rate=0.2,
        liquidation_fee_rate=0.01,
        isolated_only=True,
        funding_interval_hours=1.0,
        instrument_updated_at=AS_OF - timedelta(minutes=5),
        data_quality_status="HEALTHY",
        data_quality_ok=True,
        orderbook_fresh=True,
        source="test_fixture",
        snapshot_version=INSTRUMENT_RISK_SNAPSHOT_VERSION,
        as_of=AS_OF,
    )
    defaults.update(overrides)
    return InstrumentRiskSnapshot(**defaults)


def make_book(
    *,
    bids: list[tuple[float, float]] | None = None,
    asks: list[tuple[float, float]] | None = None,
    fresh: bool = True,
) -> OrderbookDepthSnapshot:
    bids = bids if bids is not None else [(99.55, 50.0), (99.45, 50.0), (99.35, 50.0)]
    asks = asks if asks is not None else [(99.6, 50.0), (99.7, 50.0), (99.8, 50.0)]
    return OrderbookDepthSnapshot(
        instrument_id=1,
        bids=tuple(DepthLevel(price=p, quantity=q) for p, q in bids),
        asks=tuple(DepthLevel(price=p, quantity=q) for p, q in asks),
        fresh=fresh,
        snapshot_at=AS_OF,
    )


def make_funding(**overrides: Any) -> FundingSnapshot:
    defaults: dict[str, Any] = dict(
        current_rate=0.00001,
        history=tuple((AS_OF - timedelta(hours=hour), 0.00001) for hour in range(6, 0, -1)),
        next_funding_at=AS_OF + timedelta(minutes=30),
        interval_hours=1.0,
        data_fresh=True,
    )
    defaults.update(overrides)
    return FundingSnapshot(**defaults)


def make_fee_schedule(
    settings: CostSettings | None = None, **overrides: Any
) -> FeeScheduleSnapshot:
    settings = settings or COST_SETTINGS
    defaults: dict[str, Any] = dict(
        version=settings.fee_schedule_version,
        source=settings.fee_schedule_source,
        tier=settings.assumed_fee_tier,
        maker_fee_rate=settings.default_maker_fee_rate,
        taker_fee_rate=settings.default_taker_fee_rate,
        config_hash=cost_configuration_hash(settings),
        active=True,
        effective_at=None,
    )
    defaults.update(overrides)
    return FeeScheduleSnapshot(**defaults)


def default_levels(bullish: bool = True) -> tuple[TechnicalLevel, ...]:
    if bullish:
        return (
            TechnicalLevel(
                price=107.0, kind="SWING_HIGH", timeframe="15m", relevance=0.8, source_id="swing:1"
            ),
            TechnicalLevel(
                price=109.0,
                kind="RANGE_HIGH",
                timeframe="15m",
                relevance=0.8,
                source_id="liquidity:2",
            ),
            TechnicalLevel(
                price=99.5, kind="SWING_LOW", timeframe="15m", relevance=0.7, source_id="swing:3"
            ),
        )
    return (
        TechnicalLevel(
            price=92.0, kind="SWING_LOW", timeframe="15m", relevance=0.8, source_id="swing:1"
        ),
        TechnicalLevel(
            price=90.0, kind="RANGE_LOW", timeframe="15m", relevance=0.8, source_id="liquidity:2"
        ),
        TechnicalLevel(
            price=99.5, kind="SWING_HIGH", timeframe="15m", relevance=0.7, source_id="swing:3"
        ),
    )


def make_context(
    risk_settings: RiskSettings | None = None,
    cost_settings: CostSettings | None = None,
    **overrides: Any,
) -> RiskEvaluationContext:
    risk_settings = risk_settings or RISK_SETTINGS
    cost_settings = cost_settings or COST_SETTINGS
    defaults: dict[str, Any] = dict(
        candidate=make_candidate(),
        instrument=make_instrument(),
        book=make_book(),
        best_bid=99.55,
        best_ask=99.6,
        bbo_at=AS_OF - timedelta(seconds=2),
        bbo_fresh=True,
        funding=make_funding(),
        fee_schedule=make_fee_schedule(cost_settings),
        execution_assumptions=build_execution_assumptions(cost_settings),
        levels=default_levels(),
        atr_5m=2.0,
        atr_15m=3.0,
        session_state="CRYPTO_24_7",
        session_allowed=True,
        equity_overnight_risk=False,
        watchlist_active=True,
        bot_paused=False,
        as_of=AS_OF,
        risk_model_name=risk_settings.model_name,
        risk_model_version=risk_settings.model_version,
        risk_config_hash=risk_configuration_hash(risk_settings),
        cost_model_name=cost_settings.model_name,
        cost_model_version=cost_settings.model_version,
        cost_config_hash=cost_configuration_hash(cost_settings),
        instrument_snapshot_version=INSTRUMENT_RISK_SNAPSHOT_VERSION,
    )
    defaults.update(overrides)
    return RiskEvaluationContext(**defaults)


def bearish_context(**overrides: Any) -> RiskEvaluationContext:
    """Mirrored bearish base scenario (rejection at a 15m swing high)."""
    candidate = make_candidate(
        candidate_type="BEARISH_SWEEP_REVERSAL",
        direction="BEARISH",
        referenced_levels=({"type": "SWING_HIGH", "price": 99.7, "timeframe": "15m"},),
    )
    return make_context(
        candidate=candidate,
        levels=default_levels(bullish=False),
        **overrides,
    )
