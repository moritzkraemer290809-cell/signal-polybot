"""SQLAlchemy 2.x ORM models for all persistent tables.

Cross-dialect types are used (JSON with a JSONB variant on PostgreSQL, generic
Uuid) so the same metadata works against PostgreSQL in production and SQLite
in unit tests.  All timestamps are timezone-aware UTC.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

JSONVariant = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
PriceNumeric = sa.Numeric(38, 18)

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = sa.MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map = {  # noqa: RUF012 - SQLAlchemy declarative API
        dict[str, Any]: JSONVariant,
        Decimal: PriceNumeric,
    }


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        server_default=sa.func.now(),
        onupdate=sa.func.now(),
        nullable=False,
    )


class Instrument(Base, TimestampMixin):
    __tablename__ = "instruments"

    id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"), primary_key=True, autoincrement=True
    )
    instrument_id: Mapped[int] = mapped_column(sa.BigInteger, unique=True, nullable=False)
    symbol: Mapped[str] = mapped_column(sa.String(64), unique=True, nullable=False)
    instrument_type: Mapped[str] = mapped_column(sa.String(32), default="perpetual")
    category: Mapped[str] = mapped_column(sa.String(32), default="")
    asset_class: Mapped[str] = mapped_column(sa.String(16), default="UNKNOWN")
    base_asset: Mapped[str] = mapped_column(sa.String(32), default="")
    quote_asset: Mapped[str] = mapped_column(sa.String(32), default="")
    price_decimals: Mapped[int] = mapped_column(sa.Integer, default=2)
    quantity_decimals: Mapped[int] = mapped_column(sa.Integer, default=4)
    min_notional: Mapped[Decimal] = mapped_column(PriceNumeric, default=Decimal("0"))
    max_leverage: Mapped[int] = mapped_column(sa.Integer, default=1)
    funding_interval: Mapped[str | None] = mapped_column(sa.String(16))
    liquidation_fee: Mapped[Decimal | None] = mapped_column(PriceNumeric)
    isolated_only: Mapped[bool] = mapped_column(sa.Boolean, default=False)
    risk_tiers: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)
    status: Mapped[str] = mapped_column(sa.String(16), default="UNKNOWN", index=True)
    enabled: Mapped[bool] = mapped_column(sa.Boolean, default=False, index=True)
    first_seen_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    last_seen_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    metadata_raw: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)


class MarketTick(Base):
    __tablename__ = "market_ticks"
    __table_args__ = (sa.Index("ix_market_ticks_instrument_ts", "instrument_pk", "ts"),)

    id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"), primary_key=True, autoincrement=True
    )
    instrument_pk: Mapped[int] = mapped_column(
        sa.ForeignKey("instruments.id", ondelete="CASCADE"), nullable=False
    )
    ts: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    mark_price: Mapped[Decimal | None] = mapped_column(PriceNumeric)
    index_price: Mapped[Decimal | None] = mapped_column(PriceNumeric)
    last_price: Mapped[Decimal | None] = mapped_column(PriceNumeric)
    mid_price: Mapped[Decimal | None] = mapped_column(PriceNumeric)
    open_interest: Mapped[Decimal | None] = mapped_column(PriceNumeric)
    funding_rate: Mapped[Decimal | None] = mapped_column(PriceNumeric)
    next_funding_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    source: Mapped[str] = mapped_column(sa.String(16), default="REST")
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


class Candle(Base, TimestampMixin):
    __tablename__ = "candles"
    __table_args__ = (
        sa.UniqueConstraint(
            "instrument_pk", "timeframe", "open_time", name="uq_candles_instrument_tf_open"
        ),
    )

    id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"), primary_key=True, autoincrement=True
    )
    instrument_pk: Mapped[int] = mapped_column(
        sa.ForeignKey("instruments.id", ondelete="CASCADE"), nullable=False
    )
    timeframe: Mapped[str] = mapped_column(sa.String(8), nullable=False)
    open_time: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    open: Mapped[Decimal] = mapped_column(PriceNumeric, nullable=False)
    high: Mapped[Decimal] = mapped_column(PriceNumeric, nullable=False)
    low: Mapped[Decimal] = mapped_column(PriceNumeric, nullable=False)
    close: Mapped[Decimal] = mapped_column(PriceNumeric, nullable=False)
    volume: Mapped[Decimal] = mapped_column(PriceNumeric, nullable=False, default=Decimal("0"))
    trade_count: Mapped[int] = mapped_column(sa.Integer, default=0)
    complete: Mapped[bool] = mapped_column(sa.Boolean, default=True)
    source: Mapped[str] = mapped_column(sa.String(16), default="REST")


class OrderbookSnapshot(Base):
    __tablename__ = "orderbook_snapshots"
    __table_args__ = (sa.Index("ix_orderbook_snapshots_instrument_ts", "instrument_pk", "ts"),)

    id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"), primary_key=True, autoincrement=True
    )
    instrument_pk: Mapped[int] = mapped_column(
        sa.ForeignKey("instruments.id", ondelete="CASCADE"), nullable=False
    )
    ts: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    sequence: Mapped[int | None] = mapped_column(sa.BigInteger)
    depth_level: Mapped[int] = mapped_column(sa.Integer, default=100)
    bids: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)
    asks: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)
    best_bid: Mapped[Decimal | None] = mapped_column(PriceNumeric)
    best_ask: Mapped[Decimal | None] = mapped_column(PriceNumeric)
    spread_bps: Mapped[Decimal | None] = mapped_column(PriceNumeric)
    source: Mapped[str] = mapped_column(sa.String(16), default="REST")
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


class FundingRate(Base):
    __tablename__ = "funding_rates"
    __table_args__ = (
        sa.UniqueConstraint("instrument_pk", "ts", name="uq_funding_rates_instrument_ts"),
    )

    id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"), primary_key=True, autoincrement=True
    )
    instrument_pk: Mapped[int] = mapped_column(
        sa.ForeignKey("instruments.id", ondelete="CASCADE"), nullable=False
    )
    ts: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    funding_rate: Mapped[Decimal] = mapped_column(PriceNumeric, nullable=False)
    source: Mapped[str] = mapped_column(sa.String(16), default="REST")
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


class MarketRegimeRecord(Base):
    __tablename__ = "market_regimes"
    __table_args__ = (sa.Index("ix_market_regimes_instrument_ts", "instrument_pk", "ts"),)

    id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"), primary_key=True, autoincrement=True
    )
    instrument_pk: Mapped[int] = mapped_column(
        sa.ForeignKey("instruments.id", ondelete="CASCADE"), nullable=False
    )
    timeframe: Mapped[str] = mapped_column(sa.String(8), nullable=False)
    ts: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    regime: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    confidence: Mapped[Decimal | None] = mapped_column(PriceNumeric)
    features: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)
    strategy_version: Mapped[str] = mapped_column(sa.String(32), default="v1")
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


class Signal(Base, TimestampMixin):
    __tablename__ = "signals"
    __table_args__ = (sa.Index("ix_signals_instrument_status", "instrument_pk", "status"),)

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=uuid.uuid4)
    short_id: Mapped[str] = mapped_column(sa.String(64), unique=True, nullable=False)
    instrument_pk: Mapped[int] = mapped_column(
        sa.ForeignKey("instruments.id", ondelete="RESTRICT"), nullable=False
    )
    symbol: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    direction: Mapped[str] = mapped_column(sa.String(8), nullable=False)
    status: Mapped[str] = mapped_column(sa.String(32), nullable=False, index=True)
    score: Mapped[Decimal | None] = mapped_column(PriceNumeric)
    score_breakdown: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)
    entry_low: Mapped[Decimal | None] = mapped_column(PriceNumeric)
    entry_high: Mapped[Decimal | None] = mapped_column(PriceNumeric)
    entry_trigger: Mapped[str | None] = mapped_column(sa.Text)
    stop_price: Mapped[Decimal | None] = mapped_column(PriceNumeric)
    invalidation_reason: Mapped[str | None] = mapped_column(sa.Text)
    tp1: Mapped[Decimal | None] = mapped_column(PriceNumeric)
    tp2: Mapped[Decimal | None] = mapped_column(PriceNumeric)
    trailing_rule: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)
    gross_rr: Mapped[Decimal | None] = mapped_column(PriceNumeric)
    net_rr: Mapped[Decimal | None] = mapped_column(PriceNumeric)
    leverage_min: Mapped[int | None] = mapped_column(sa.Integer)
    leverage_max: Mapped[int | None] = mapped_column(sa.Integer)
    reference_risk_pct: Mapped[Decimal | None] = mapped_column(PriceNumeric)
    regime: Mapped[str | None] = mapped_column(sa.String(32))
    setup_reason: Mapped[str | None] = mapped_column(sa.Text)
    cost_assumptions: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)
    expires_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    estimated_hold_minutes: Mapped[int | None] = mapped_column(sa.Integer)
    opened_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    strategy_version: Mapped[str] = mapped_column(sa.String(32), default="v1")
    config_version: Mapped[str] = mapped_column(sa.String(32), default="v1")
    correlation_id: Mapped[str | None] = mapped_column(sa.String(64), index=True)


class SignalEvent(Base):
    __tablename__ = "signal_events"

    id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"), primary_key=True, autoincrement=True
    )
    signal_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("signals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    event_type: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    from_status: Mapped[str | None] = mapped_column(sa.String(32))
    to_status: Mapped[str | None] = mapped_column(sa.String(32))
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)
    idempotency_key: Mapped[str] = mapped_column(sa.String(128), unique=True, nullable=False)
    correlation_id: Mapped[str | None] = mapped_column(sa.String(64))
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


class StrategyDecision(Base):
    __tablename__ = "strategy_decisions"
    __table_args__ = (
        sa.Index("ix_strategy_decisions_instrument_created", "instrument_pk", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=uuid.uuid4)
    instrument_pk: Mapped[int] = mapped_column(
        sa.ForeignKey("instruments.id", ondelete="CASCADE"), nullable=False
    )
    symbol: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    decision: Mapped[str] = mapped_column(sa.String(32), nullable=False, index=True)
    direction: Mapped[str | None] = mapped_column(sa.String(8))
    reasons: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)
    score: Mapped[Decimal | None] = mapped_column(PriceNumeric)
    score_breakdown: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)
    features: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)
    regime: Mapped[str | None] = mapped_column(sa.String(32))
    trade_plan: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)
    cost_assumptions: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)
    data_quality_status: Mapped[str] = mapped_column(sa.String(16), default="UNKNOWN")
    signal_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("signals.id", ondelete="SET NULL")
    )
    strategy_version: Mapped[str] = mapped_column(sa.String(32), default="v1")
    config_version: Mapped[str] = mapped_column(sa.String(32), default="v1")
    correlation_id: Mapped[str | None] = mapped_column(sa.String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


class FeeScheduleVersion(Base):
    __tablename__ = "fee_schedule_versions"

    id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"), primary_key=True, autoincrement=True
    )
    version: Mapped[str] = mapped_column(sa.String(32), unique=True, nullable=False)
    schedule: Mapped[dict[str, Any]] = mapped_column(JSONVariant, nullable=False)
    source: Mapped[str] = mapped_column(sa.String(64), default="config")
    active: Mapped[bool] = mapped_column(sa.Boolean, default=False, index=True)
    effective_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


class SystemEvent(Base):
    __tablename__ = "system_events"

    id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"), primary_key=True, autoincrement=True
    )
    level: Mapped[str] = mapped_column(sa.String(16), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(sa.String(64), nullable=False, index=True)
    message: Mapped[str] = mapped_column(sa.Text, nullable=False)
    context: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)
    correlation_id: Mapped[str | None] = mapped_column(sa.String(64))
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


class TelegramDelivery(Base, TimestampMixin):
    __tablename__ = "telegram_deliveries"
    __table_args__ = (
        sa.Index("ix_telegram_deliveries_claim", "status", "priority", "scheduled_at"),
    )

    id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"), primary_key=True, autoincrement=True
    )
    delivery_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, unique=True, default=uuid.uuid4)
    signal_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("signals.id", ondelete="SET NULL"), index=True
    )
    system_event_id: Mapped[int | None] = mapped_column(sa.BigInteger)
    message_type: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    operation: Mapped[str] = mapped_column(sa.String(16), default="SEND", nullable=False)
    chat_id: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    message_id: Mapped[int | None] = mapped_column(sa.BigInteger)
    status: Mapped[str] = mapped_column(sa.String(32), default="PENDING", index=True)
    priority: Mapped[int] = mapped_column(sa.Integer, default=3, nullable=False)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)
    content_hash: Mapped[str | None] = mapped_column(sa.String(64), index=True)
    idempotency_key: Mapped[str] = mapped_column(sa.String(128), unique=True, nullable=False)
    attempt_count: Mapped[int] = mapped_column(sa.Integer, default=0, nullable=False)
    scheduled_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    lease_expires_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    last_error_class: Mapped[str | None] = mapped_column(sa.String(64))
    last_error_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(sa.Text)
    correlation_id: Mapped[str | None] = mapped_column(sa.String(64))


class AppState(Base):
    """Small persistent key-value store for runtime state (e.g. bot pause)."""

    __tablename__ = "app_state"

    key: Mapped[str] = mapped_column(sa.String(64), primary_key=True)
    value: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        server_default=sa.func.now(),
        onupdate=sa.func.now(),
        nullable=False,
    )


class AssetClassification(Base):
    """Immutable classification history per instrument."""

    __tablename__ = "asset_classifications"
    __table_args__ = (
        sa.Index("ix_asset_classifications_instrument_created", "instrument_pk", "created_at"),
    )

    id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"), primary_key=True, autoincrement=True
    )
    instrument_pk: Mapped[int] = mapped_column(
        sa.ForeignKey("instruments.id", ondelete="CASCADE"), nullable=False
    )
    symbol: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    asset_class: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    source: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    rule: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    confidence: Mapped[Decimal] = mapped_column(PriceNumeric, nullable=False)
    classified_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


class MarketSelectionDecision(Base):
    """Immutable market selection decision history."""

    __tablename__ = "market_selection_decisions"
    __table_args__ = (
        sa.Index("ix_market_selection_decisions_instrument_created", "instrument_pk", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True)
    instrument_pk: Mapped[int] = mapped_column(
        sa.ForeignKey("instruments.id", ondelete="CASCADE"), nullable=False
    )
    symbol: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    asset_class: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    classification_source: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    session_state: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    calendar_version: Mapped[str | None] = mapped_column(sa.String(32))
    market_status: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    data_quality_status: Mapped[str] = mapped_column(sa.String(24), nullable=False)
    selection_state: Mapped[str] = mapped_column(sa.String(32), nullable=False, index=True)
    eligibility_status: Mapped[str] = mapped_column(sa.String(32), nullable=False, index=True)
    market_quality_score: Mapped[int | None] = mapped_column(sa.Integer)
    quality_components: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)
    reasons: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)
    configuration_version: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    evaluated_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    previous_selection_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


class ActiveWatchlistEntry(Base, TimestampMixin):
    """Current watchlist snapshot (one row per instrument on the list)."""

    __tablename__ = "active_watchlist"

    id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"), primary_key=True, autoincrement=True
    )
    instrument_pk: Mapped[int] = mapped_column(
        sa.ForeignKey("instruments.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    symbol: Mapped[str] = mapped_column(sa.String(64), unique=True, nullable=False)
    state: Mapped[str] = mapped_column(sa.String(32), nullable=False, index=True)
    asset_class: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    quality_score: Mapped[int | None] = mapped_column(sa.Integer)
    eligibility_status: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    reasons: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)
    activated_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    paused_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    last_decision_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid)


class WatchlistEvent(Base):
    """Watchlist state-change history."""

    __tablename__ = "watchlist_events"
    __table_args__ = (
        sa.Index("ix_watchlist_events_instrument_created", "instrument_pk", "created_at"),
    )

    id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"), primary_key=True, autoincrement=True
    )
    instrument_pk: Mapped[int] = mapped_column(
        sa.ForeignKey("instruments.id", ondelete="CASCADE"), nullable=False
    )
    symbol: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    event_type: Mapped[str] = mapped_column(sa.String(32), nullable=False, index=True)
    from_state: Mapped[str | None] = mapped_column(sa.String(32))
    to_state: Mapped[str | None] = mapped_column(sa.String(32))
    reasons: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)
    decision_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


class SessionEvent(Base):
    """Session state transitions (equity/crypto)."""

    __tablename__ = "session_events"
    __table_args__ = (sa.Index("ix_session_events_scope_created", "scope", "created_at"),)

    id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"), primary_key=True, autoincrement=True
    )
    scope: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    from_state: Mapped[str | None] = mapped_column(sa.String(32))
    to_state: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    calendar_version: Mapped[str | None] = mapped_column(sa.String(32))
    occurred_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    details: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


class FeatureSnapshot(Base):
    """Immutable feature snapshot per evaluation (research reproducibility)."""

    __tablename__ = "feature_snapshots"
    __table_args__ = (sa.Index("ix_feature_snapshots_instrument_as_of", "instrument_pk", "as_of"),)

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True)
    instrument_pk: Mapped[int] = mapped_column(
        sa.ForeignKey("instruments.id", ondelete="CASCADE"), nullable=False
    )
    symbol: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    as_of: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    candle_window_metadata: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)
    feature_values: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)
    feature_validity: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)
    warnings: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)
    strategy_name: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    strategy_version: Mapped[str] = mapped_column(sa.String(32), nullable=False, index=True)
    feature_schema_version: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    config_hash: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


class SwingPoint(Base):
    __tablename__ = "swing_points"
    __table_args__ = (
        sa.UniqueConstraint(
            "instrument_pk",
            "timeframe",
            "swing_type",
            "candle_open_time",
            "strategy_version",
            name="uq_swing_points_identity",
        ),
    )

    id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"), primary_key=True, autoincrement=True
    )
    instrument_pk: Mapped[int] = mapped_column(
        sa.ForeignKey("instruments.id", ondelete="CASCADE"), nullable=False
    )
    timeframe: Mapped[str] = mapped_column(sa.String(8), nullable=False)
    swing_type: Mapped[str] = mapped_column(sa.String(8), nullable=False)
    price: Mapped[Decimal] = mapped_column(PriceNumeric, nullable=False)
    candle_open_time: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    confirmed_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    strength: Mapped[Decimal | None] = mapped_column(PriceNumeric)
    relevance: Mapped[Decimal | None] = mapped_column(PriceNumeric)
    params: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)
    strategy_version: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


class StructureEventRecord(Base):
    __tablename__ = "structure_events"
    __table_args__ = (
        sa.UniqueConstraint(
            "instrument_pk",
            "timeframe",
            "event_type",
            "confirm_close_time",
            "strategy_version",
            name="uq_structure_events_identity",
        ),
    )

    id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"), primary_key=True, autoincrement=True
    )
    instrument_pk: Mapped[int] = mapped_column(
        sa.ForeignKey("instruments.id", ondelete="CASCADE"), nullable=False
    )
    timeframe: Mapped[str] = mapped_column(sa.String(8), nullable=False)
    event_type: Mapped[str] = mapped_column(sa.String(32), nullable=False, index=True)
    price: Mapped[Decimal] = mapped_column(PriceNumeric, nullable=False)
    reference_swing: Mapped[str | None] = mapped_column(sa.String(96))
    confirm_close_time: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    detail: Mapped[str | None] = mapped_column(sa.Text)
    strategy_version: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


class LiquidityLevelRecord(Base):
    __tablename__ = "liquidity_levels"
    __table_args__ = (
        sa.UniqueConstraint(
            "instrument_pk",
            "timeframe",
            "level_type",
            "price_key",
            "strategy_version",
            name="uq_liquidity_levels_identity",
        ),
    )

    id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"), primary_key=True, autoincrement=True
    )
    instrument_pk: Mapped[int] = mapped_column(
        sa.ForeignKey("instruments.id", ondelete="CASCADE"), nullable=False
    )
    timeframe: Mapped[str] = mapped_column(sa.String(8), nullable=False)
    level_type: Mapped[str] = mapped_column(sa.String(24), nullable=False)
    price: Mapped[Decimal] = mapped_column(PriceNumeric, nullable=False)
    price_key: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    relevance: Mapped[Decimal | None] = mapped_column(PriceNumeric)
    touches: Mapped[int] = mapped_column(sa.Integer, default=1)
    is_major: Mapped[bool] = mapped_column(sa.Boolean, default=False)
    detail: Mapped[str | None] = mapped_column(sa.Text)
    strategy_version: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


class LiquidityEventRecord(Base):
    __tablename__ = "liquidity_events"
    __table_args__ = (
        sa.UniqueConstraint(
            "instrument_pk",
            "timeframe",
            "event_type",
            "level_price_key",
            "event_candle_open_time",
            "strategy_version",
            name="uq_liquidity_events_identity",
        ),
    )

    id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"), primary_key=True, autoincrement=True
    )
    instrument_pk: Mapped[int] = mapped_column(
        sa.ForeignKey("instruments.id", ondelete="CASCADE"), nullable=False
    )
    timeframe: Mapped[str] = mapped_column(sa.String(8), nullable=False)
    event_type: Mapped[str] = mapped_column(sa.String(24), nullable=False)
    direction: Mapped[str] = mapped_column(sa.String(8), nullable=False)
    level_price: Mapped[Decimal] = mapped_column(PriceNumeric, nullable=False)
    level_price_key: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    event_candle_open_time: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False
    )
    overshoot_bps: Mapped[Decimal | None] = mapped_column(PriceNumeric)
    confirmed: Mapped[bool] = mapped_column(sa.Boolean, default=False)
    confidence: Mapped[Decimal | None] = mapped_column(PriceNumeric)
    detail: Mapped[str | None] = mapped_column(sa.Text)
    strategy_version: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


class SetupCandidateRecord(Base):
    """Current state of a research setup candidate (history in events)."""

    __tablename__ = "setup_candidates"
    __table_args__ = (
        sa.Index("ix_setup_candidates_instrument_state", "instrument_pk", "state"),
        sa.Index("ix_setup_candidates_dedupe", "dedupe_key"),
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True)
    instrument_pk: Mapped[int] = mapped_column(
        sa.ForeignKey("instruments.id", ondelete="CASCADE"), nullable=False
    )
    instrument_id: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    symbol: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    asset_class: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    candidate_type: Mapped[str] = mapped_column(sa.String(40), nullable=False, index=True)
    direction: Mapped[str] = mapped_column(sa.String(8), nullable=False)
    state: Mapped[str] = mapped_column(sa.String(16), nullable=False, index=True)
    as_of: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    expiry_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    setup_score: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    confidence: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    primary_regime: Mapped[str] = mapped_column(sa.String(24), nullable=False)
    regime_confidence: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    higher_timeframe_structure: Mapped[str] = mapped_column(sa.String(28), nullable=False)
    local_structure: Mapped[str] = mapped_column(sa.String(28), nullable=False)
    session_state: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    market_quality_score: Mapped[int | None] = mapped_column(sa.Integer)
    data_quality_status: Mapped[str] = mapped_column(sa.String(24), nullable=False)
    retest_status: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    reason_summary: Mapped[str] = mapped_column(sa.Text, nullable=False)
    details: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)
    dedupe_key: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    #: equals dedupe_key while the candidate is active (DETECTED/CONFIRMED),
    #: NULL when terminal - unique constraint prevents duplicate actives.
    active_key: Mapped[str | None] = mapped_column(sa.String(32), unique=True)
    feature_snapshot_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid)
    strategy_name: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    strategy_version: Mapped[str] = mapped_column(sa.String(32), nullable=False, index=True)
    feature_schema_version: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    config_hash: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    ruleset_hash: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


class SetupCandidateEvent(Base):
    """Immutable candidate lifecycle history."""

    __tablename__ = "setup_candidate_events"

    id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"), primary_key=True, autoincrement=True
    )
    candidate_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("setup_candidates.id", ondelete="CASCADE"), nullable=False, index=True
    )
    event_type: Mapped[str] = mapped_column(sa.String(24), nullable=False)
    from_state: Mapped[str | None] = mapped_column(sa.String(16))
    to_state: Mapped[str | None] = mapped_column(sa.String(16))
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


class SetupRejectionRecord(Base):
    """Aggregated setup rejections (sampled/deduplicated per window)."""

    __tablename__ = "setup_rejections"
    __table_args__ = (
        sa.UniqueConstraint(
            "instrument_pk",
            "primary_code",
            "candidate_type",
            "window_bucket",
            "strategy_version",
            name="uq_setup_rejections_bucket",
        ),
        sa.Index("ix_setup_rejections_instrument_created", "instrument_pk", "created_at"),
    )

    id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"), primary_key=True, autoincrement=True
    )
    instrument_pk: Mapped[int] = mapped_column(
        sa.ForeignKey("instruments.id", ondelete="CASCADE"), nullable=False
    )
    symbol: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    primary_code: Mapped[str] = mapped_column(sa.String(40), nullable=False, index=True)
    candidate_type: Mapped[str] = mapped_column(sa.String(40), nullable=False, default="")
    codes: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)
    detail: Mapped[str | None] = mapped_column(sa.Text)
    count: Mapped[int] = mapped_column(sa.Integer, default=1, nullable=False)
    first_as_of: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    last_as_of: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    window_bucket: Mapped[str] = mapped_column(sa.String(24), nullable=False)
    strategy_version: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    config_hash: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


class InstrumentRiskSnapshotRecord(Base):
    """Public instrument + market state snapshot used by one risk evaluation."""

    __tablename__ = "instrument_risk_snapshots"
    __table_args__ = (
        sa.Index("ix_instrument_risk_snapshots_instrument_as_of", "instrument_pk", "as_of"),
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True)
    instrument_pk: Mapped[int] = mapped_column(
        sa.ForeignKey("instruments.id", ondelete="CASCADE"), nullable=False
    )
    instrument_id: Mapped[int] = mapped_column(sa.BigInteger, nullable=False, index=True)
    symbol: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    as_of: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    snapshot_version: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    mark_price: Mapped[Decimal | None] = mapped_column(PriceNumeric)
    max_leverage: Mapped[int | None] = mapped_column(sa.Integer)
    min_notional: Mapped[Decimal | None] = mapped_column(PriceNumeric)
    data_quality_status: Mapped[str] = mapped_column(sa.String(24), nullable=False)
    orderbook_fresh: Mapped[bool] = mapped_column(sa.Boolean, default=False)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


class ExecutionAssumptionVersion(Base):
    """Versioned execution assumptions (administered, immutable per version)."""

    __tablename__ = "execution_assumption_versions"

    id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"), primary_key=True, autoincrement=True
    )
    version: Mapped[str] = mapped_column(sa.String(48), unique=True, nullable=False)
    assumptions: Mapped[dict[str, Any]] = mapped_column(JSONVariant, nullable=False)
    active: Mapped[bool] = mapped_column(sa.Boolean, default=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


class CostEstimateRecord(Base):
    """Persisted conservative cost estimate for one plan evaluation."""

    __tablename__ = "cost_estimates"
    __table_args__ = (sa.Index("ix_cost_estimates_instrument_as_of", "instrument_pk", "as_of"),)

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True)
    plan_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid, index=True)
    candidate_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, nullable=False, index=True)
    instrument_pk: Mapped[int] = mapped_column(
        sa.ForeignKey("instruments.id", ondelete="CASCADE"), nullable=False
    )
    symbol: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    as_of: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    cost_model_name: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    cost_model_version: Mapped[str] = mapped_column(sa.String(32), nullable=False, index=True)
    cost_config_hash: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    fee_schedule_version: Mapped[str] = mapped_column(sa.String(48), nullable=False, index=True)
    execution_assumption_version: Mapped[str] = mapped_column(sa.String(48), nullable=False)
    total_cost: Mapped[Decimal | None] = mapped_column(PriceNumeric)
    cost_to_risk_pct: Mapped[Decimal | None] = mapped_column(PriceNumeric)
    net_rr_primary: Mapped[Decimal | None] = mapped_column(PriceNumeric)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


class RiskPlanRecord(Base):
    """Current state of a signal eligibility plan (history in events)."""

    __tablename__ = "risk_plans"
    __table_args__ = (
        sa.Index("ix_risk_plans_instrument_status", "instrument_pk", "status"),
        sa.Index("ix_risk_plans_dedupe", "dedupe_key"),
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True)
    candidate_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, nullable=False, index=True)
    instrument_pk: Mapped[int] = mapped_column(
        sa.ForeignKey("instruments.id", ondelete="CASCADE"), nullable=False
    )
    instrument_id: Mapped[int] = mapped_column(sa.BigInteger, nullable=False, index=True)
    symbol: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    asset_class: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    candidate_type: Mapped[str] = mapped_column(sa.String(40), nullable=False)
    direction: Mapped[str] = mapped_column(sa.String(8), nullable=False)
    status: Mapped[str] = mapped_column(sa.String(24), nullable=False, index=True)
    eligibility_score: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    net_rr_primary: Mapped[Decimal | None] = mapped_column(PriceNumeric)
    cost_to_risk_pct: Mapped[Decimal | None] = mapped_column(PriceNumeric)
    risk_model_name: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    risk_model_version: Mapped[str] = mapped_column(sa.String(32), nullable=False, index=True)
    risk_config_hash: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    cost_model_version: Mapped[str] = mapped_column(sa.String(32), nullable=False, index=True)
    cost_config_hash: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    fee_schedule_version: Mapped[str] = mapped_column(sa.String(48), nullable=False, index=True)
    execution_assumption_version: Mapped[str] = mapped_column(sa.String(48), nullable=False)
    instrument_snapshot_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid)
    instrument_snapshot_version: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    strategy_version: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    strategy_config_hash: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)
    dedupe_key: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    #: equals dedupe_key while the plan is active (ELIGIBLE), NULL when
    #: terminal - the unique constraint prevents duplicate active plans.
    active_key: Mapped[str | None] = mapped_column(sa.String(32), unique=True)
    as_of: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False, index=True)
    expiry_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


class RiskPlanEvent(Base):
    """Immutable plan lifecycle history."""

    __tablename__ = "risk_plan_events"

    id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"), primary_key=True, autoincrement=True
    )
    plan_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(sa.String(24), nullable=False)
    from_status: Mapped[str | None] = mapped_column(sa.String(24))
    to_status: Mapped[str | None] = mapped_column(sa.String(24))
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


class RiskPlanRejectionRecord(Base):
    """Aggregated risk-stage rejections (per candidate/code/time bucket)."""

    __tablename__ = "risk_plan_rejections"
    __table_args__ = (
        sa.UniqueConstraint(
            "candidate_key",
            "primary_code",
            "window_bucket",
            "risk_model_version",
            name="uq_risk_plan_rejections_identity",
        ),
    )

    id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"), primary_key=True, autoincrement=True
    )
    candidate_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid, index=True)
    #: string form of candidate_id (or "-") for the unique identity above
    candidate_key: Mapped[str] = mapped_column(sa.String(40), nullable=False)
    instrument_pk: Mapped[int] = mapped_column(
        sa.ForeignKey("instruments.id", ondelete="CASCADE"), nullable=False
    )
    symbol: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    primary_code: Mapped[str] = mapped_column(sa.String(48), nullable=False, index=True)
    codes: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)
    detail: Mapped[str | None] = mapped_column(sa.Text)
    count: Mapped[int] = mapped_column(sa.Integer, default=1, nullable=False)
    first_as_of: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    last_as_of: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    window_bucket: Mapped[str] = mapped_column(sa.String(24), nullable=False)
    risk_model_version: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    risk_config_hash: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    cost_model_version: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    fee_schedule_version: Mapped[str | None] = mapped_column(sa.String(48))
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


class SignalLifecycleRecord(Base):
    """Current state of an internal research lifecycle signal.

    History lives in signal_lifecycle_events; technical reference prices are
    immutable snapshot fields for local monitoring/dashboard only - never
    Telegram output in phase 10."""

    __tablename__ = "signal_lifecycles"
    __table_args__ = (
        sa.Index("ix_signal_lifecycles_instrument_state", "instrument_pk", "state"),
        sa.Index("ix_signal_lifecycles_dedupe", "dedupe_key"),
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True)
    plan_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, nullable=False, index=True)
    candidate_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, nullable=False, index=True)
    instrument_pk: Mapped[int] = mapped_column(
        sa.ForeignKey("instruments.id", ondelete="CASCADE"), nullable=False
    )
    instrument_id: Mapped[int] = mapped_column(sa.BigInteger, nullable=False, index=True)
    symbol: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    asset_class: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    candidate_type: Mapped[str] = mapped_column(sa.String(40), nullable=False)
    direction: Mapped[str] = mapped_column(sa.String(8), nullable=False)
    state: Mapped[str] = mapped_column(sa.String(24), nullable=False, index=True)
    state_version: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    entry_low: Mapped[Decimal] = mapped_column(PriceNumeric, nullable=False)
    entry_high: Mapped[Decimal] = mapped_column(PriceNumeric, nullable=False)
    entry_reference_price: Mapped[Decimal] = mapped_column(PriceNumeric, nullable=False)
    invalidation_price: Mapped[Decimal] = mapped_column(PriceNumeric, nullable=False)
    target_prices: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)
    entry_trigger: Mapped[str] = mapped_column(sa.String(40), nullable=False)
    admission_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    watching_entry_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    entry_confirmed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    active_research_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    terminal_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, index=True
    )
    last_evaluated_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), index=True
    )
    last_market_data_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    last_data_quality_status: Mapped[str] = mapped_column(sa.String(24), default="UNKNOWN")
    last_session_state: Mapped[str] = mapped_column(sa.String(32), default="UNKNOWN")
    last_event_type: Mapped[str | None] = mapped_column(sa.String(24))
    data_degraded_since: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    reference_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)
    approximation_warnings: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)
    lifecycle_model_name: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    lifecycle_model_version: Mapped[str] = mapped_column(sa.String(32), nullable=False, index=True)
    lifecycle_config_hash: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    strategy_version: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    risk_model_version: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    cost_model_version: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    fee_schedule_version: Mapped[str] = mapped_column(sa.String(48), nullable=False)
    dedupe_key: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    #: equals dedupe_key while non-terminal, NULL when terminal - the unique
    #: constraint prevents duplicate active lifecycle instances under races.
    active_key: Mapped[str | None] = mapped_column(sa.String(32), unique=True)
    lease_owner: Mapped[str | None] = mapped_column(sa.String(64))
    lease_expires_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    correlation_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


class SignalLifecycleEvent(Base):
    """Immutable, idempotent lifecycle transition history."""

    __tablename__ = "signal_lifecycle_events"

    id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"), primary_key=True, autoincrement=True
    )
    signal_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(sa.String(24), nullable=False)
    from_state: Mapped[str | None] = mapped_column(sa.String(24))
    to_state: Mapped[str | None] = mapped_column(sa.String(24))
    state_version: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    priority: Mapped[int | None] = mapped_column(sa.Integer)
    idempotency_key: Mapped[str] = mapped_column(sa.String(64), unique=True, nullable=False)
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)
    as_of: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


class SignalUpdateRecord(Base):
    """Aggregated observation stream, separate from state transitions."""

    __tablename__ = "signal_updates"

    id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"), primary_key=True, autoincrement=True
    )
    signal_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, nullable=False, index=True)
    update_type: Mapped[str] = mapped_column(sa.String(32), nullable=False, index=True)
    idempotency_key: Mapped[str] = mapped_column(sa.String(64), unique=True, nullable=False)
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)
    count: Mapped[int] = mapped_column(sa.Integer, default=1, nullable=False)
    first_as_of: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    last_as_of: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


class SignalLifecycleRejectionRecord(Base):
    """Aggregated admission/lifecycle rejections."""

    __tablename__ = "signal_lifecycle_rejections"
    __table_args__ = (
        sa.UniqueConstraint(
            "context_key",
            "primary_code",
            "window_bucket",
            "lifecycle_model_version",
            name="uq_signal_lifecycle_rejections_identity",
        ),
    )

    id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"), primary_key=True, autoincrement=True
    )
    plan_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid, index=True)
    candidate_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid)
    signal_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid)
    #: string form of the rejected context (plan or signal id) for the
    #: unique identity above
    context_key: Mapped[str] = mapped_column(sa.String(40), nullable=False)
    instrument_pk: Mapped[int] = mapped_column(
        sa.ForeignKey("instruments.id", ondelete="CASCADE"), nullable=False
    )
    symbol: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    primary_code: Mapped[str] = mapped_column(sa.String(48), nullable=False, index=True)
    codes: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)
    detail: Mapped[str | None] = mapped_column(sa.Text)
    count: Mapped[int] = mapped_column(sa.Integer, default=1, nullable=False)
    first_as_of: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    last_as_of: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    window_bucket: Mapped[str] = mapped_column(sa.String(24), nullable=False)
    lifecycle_model_version: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    lifecycle_config_hash: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


# --------------------------------------------------------------------------
# Phase 11 - hypothetical simulation, backtesting and analytics
#
# Every row here describes MODELLED, hypothetical output over public data:
# no order, no execution, no position, no account and no real performance.
# --------------------------------------------------------------------------


class ExperimentRecord(Base, TimestampMixin):
    """A named research question grouping hypothetical runs."""

    __tablename__ = "experiments"

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True)
    name: Mapped[str] = mapped_column(sa.String(120), nullable=False, index=True)
    description: Mapped[str | None] = mapped_column(sa.Text)
    status: Mapped[str] = mapped_column(sa.String(24), nullable=False, index=True)
    created_by: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    tags: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)


class ExperimentManifestRecord(Base):
    """Immutable manifest of ONE run - never updated in place."""

    __tablename__ = "experiment_manifests"
    __table_args__ = (
        sa.UniqueConstraint("content_hash", name="uq_experiment_manifests_content_hash"),
        sa.Index("ix_experiment_manifests_experiment", "experiment_id", "run_type"),
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True)
    experiment_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("experiments.id", ondelete="CASCADE"), nullable=False, index=True
    )
    run_type: Mapped[str] = mapped_column(sa.String(16), nullable=False, index=True)
    schema_version: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    content_hash: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONVariant, nullable=False)
    simulation_model_version: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    simulation_configuration_hash: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    strategy_version: Mapped[str] = mapped_column(sa.String(32), nullable=False, index=True)
    risk_model_version: Mapped[str] = mapped_column(sa.String(32), nullable=False, index=True)
    cost_model_version: Mapped[str] = mapped_column(sa.String(32), nullable=False, index=True)
    lifecycle_model_version: Mapped[str] = mapped_column(sa.String(32), nullable=False, index=True)
    fee_schedule_version: Mapped[str] = mapped_column(sa.String(48), nullable=False)
    execution_assumption_version: Mapped[str] = mapped_column(sa.String(48), nullable=False)
    replay_ordering_version: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    replay_clock_version: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    metrics_version: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    disclaimer_version: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    delay_model: Mapped[str] = mapped_column(sa.String(32), nullable=False, index=True)
    random_seed: Mapped[int | None] = mapped_column(sa.BigInteger)
    start_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    end_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


class SimulationRunRecord(Base, TimestampMixin):
    """One shadow or backtest run (state; history in simulation_events)."""

    __tablename__ = "simulation_runs"
    __table_args__ = (
        sa.UniqueConstraint("dedupe_key", name="uq_simulation_runs_dedupe_key"),
        sa.Index("ix_simulation_runs_type_status", "run_type", "run_status"),
        sa.Index("ix_simulation_runs_window", "start_at", "end_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True)
    experiment_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("experiments.id", ondelete="CASCADE"), nullable=False, index=True
    )
    manifest_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("experiment_manifests.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    run_type: Mapped[str] = mapped_column(sa.String(16), nullable=False, index=True)
    run_status: Mapped[str] = mapped_column(sa.String(24), nullable=False, index=True)
    dedupe_key: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    start_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    end_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    events_replayed: Mapped[int] = mapped_column(sa.BigInteger, default=0, nullable=False)
    simulations_total: Mapped[int] = mapped_column(sa.Integer, default=0, nullable=False)
    simulations_completed: Mapped[int] = mapped_column(sa.Integer, default=0, nullable=False)
    simulations_incomplete: Mapped[int] = mapped_column(sa.Integer, default=0, nullable=False)
    simulations_rejected: Mapped[int] = mapped_column(sa.Integer, default=0, nullable=False)
    checkpoint: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)
    excluded_intervals: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)
    data_quality_summary: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)
    warnings: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)
    detail: Mapped[str | None] = mapped_column(sa.Text)
    created_by: Mapped[str] = mapped_column(sa.String(64), nullable=False)


class BacktestRunRecord(Base, TimestampMixin):
    """Backtest-specific run state (validation, progress, checkpoints)."""

    __tablename__ = "backtest_runs"
    __table_args__ = (
        sa.UniqueConstraint("run_id", name="uq_backtest_runs_run_id"),
        sa.Index("ix_backtest_runs_status", "run_status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True)
    run_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("simulation_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    run_status: Mapped[str] = mapped_column(sa.String(24), nullable=False)
    replay_ordering_version: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    replay_clock_version: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    data_complete: Mapped[bool] = mapped_column(sa.Boolean, default=False, nullable=False)
    missing_channels: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)
    gap_intervals: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)
    events_replayed: Mapped[int] = mapped_column(sa.BigInteger, default=0, nullable=False)
    last_event_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    checkpoint: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)
    detail: Mapped[str | None] = mapped_column(sa.Text)


class SimulatedPositionRecord(Base, TimestampMixin):
    """ONE hypothetical simulated position - never a real position."""

    __tablename__ = "simulated_positions"
    __table_args__ = (
        sa.UniqueConstraint("dedupe_key", name="uq_simulated_positions_dedupe_key"),
        sa.Index("ix_simulated_positions_run_state", "run_id", "state"),
        sa.Index("ix_simulated_positions_symbol", "symbol"),
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True)
    run_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("simulation_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    lifecycle_signal_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, nullable=False, index=True)
    plan_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, nullable=False, index=True)
    candidate_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, nullable=False, index=True)
    symbol: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    asset_class: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    candidate_type: Mapped[str] = mapped_column(sa.String(40), nullable=False)
    direction: Mapped[str] = mapped_column(sa.String(8), nullable=False)
    state: Mapped[str] = mapped_column(sa.String(32), nullable=False, index=True)
    exit_reason: Mapped[str | None] = mapped_column(sa.String(32), index=True)
    delay_model: Mapped[str] = mapped_column(sa.String(32), nullable=False, index=True)
    delay_seconds: Mapped[Decimal | None] = mapped_column(sa.Numeric(18, 6))
    event_reference_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    scheduled_entry_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    modelled_entry_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    modelled_exit_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    modelled_entry_price: Mapped[Decimal | None] = mapped_column(PriceNumeric)
    modelled_exit_price: Mapped[Decimal | None] = mapped_column(PriceNumeric)
    modelled_quantity: Mapped[Decimal | None] = mapped_column(PriceNumeric)
    gross_result: Mapped[Decimal | None] = mapped_column(PriceNumeric)
    net_result: Mapped[Decimal | None] = mapped_column(PriceNumeric)
    gross_r: Mapped[Decimal | None] = mapped_column(sa.Numeric(18, 6))
    net_r: Mapped[Decimal | None] = mapped_column(sa.Numeric(18, 6))
    fees_total: Mapped[Decimal | None] = mapped_column(PriceNumeric)
    slippage_total: Mapped[Decimal | None] = mapped_column(PriceNumeric)
    funding_total: Mapped[Decimal | None] = mapped_column(PriceNumeric)
    duration_seconds: Mapped[Decimal | None] = mapped_column(sa.Numeric(18, 3))
    data_completeness: Mapped[str] = mapped_column(sa.String(16), nullable=False, index=True)
    warnings: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)
    dedupe_key: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    disclaimer_version: Mapped[str] = mapped_column(sa.String(16), nullable=False)


class SimulatedExecutionRecord(Base):
    """One modelled leg of a hypothetical simulation (never a fill)."""

    __tablename__ = "simulated_executions"
    __table_args__ = (
        sa.Index("ix_simulated_executions_position_leg", "simulated_position_id", "leg"),
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True)
    simulated_position_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("simulated_positions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    run_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, nullable=False, index=True)
    leg: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    side_consumed: Mapped[str] = mapped_column(sa.String(8), nullable=False)
    modelled_price: Mapped[Decimal] = mapped_column(PriceNumeric, nullable=False)
    reference_price: Mapped[Decimal] = mapped_column(PriceNumeric, nullable=False)
    quantity: Mapped[Decimal] = mapped_column(PriceNumeric, nullable=False)
    notional: Mapped[Decimal] = mapped_column(PriceNumeric, nullable=False)
    slippage_cost: Mapped[Decimal] = mapped_column(PriceNumeric, nullable=False)
    slippage_bps: Mapped[Decimal] = mapped_column(sa.Numeric(18, 6), nullable=False)
    fee_cost: Mapped[Decimal] = mapped_column(PriceNumeric, nullable=False)
    levels_consumed: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    book_snapshot_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid)
    book_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    as_of: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    assumptions: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


class SimulationEventRecord(Base):
    """Immutable audit trail of a run / simulation."""

    __tablename__ = "simulation_events"
    __table_args__ = (
        sa.UniqueConstraint("idempotency_key", name="uq_simulation_events_idempotency_key"),
        sa.Index("ix_simulation_events_run_type", "run_id", "event_type"),
    )

    id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"), primary_key=True, autoincrement=True
    )
    run_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, nullable=False, index=True)
    simulated_position_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid, index=True)
    event_type: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)
    as_of: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


class SimulationRejectionRecord(Base):
    """Aggregated structured reasons a simulation was not modelled."""

    __tablename__ = "simulation_rejections"
    __table_args__ = (
        sa.UniqueConstraint(
            "context_key",
            "primary_code",
            "window_bucket",
            "simulation_model_version",
            name="uq_simulation_rejections_identity",
        ),
        sa.Index("ix_simulation_rejections_code", "primary_code"),
    )

    id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"), primary_key=True, autoincrement=True
    )
    run_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid, index=True)
    lifecycle_signal_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid, index=True)
    plan_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid)
    candidate_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid)
    context_key: Mapped[str] = mapped_column(sa.String(48), nullable=False)
    symbol: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    primary_code: Mapped[str] = mapped_column(sa.String(48), nullable=False)
    codes: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)
    detail: Mapped[str | None] = mapped_column(sa.Text)
    count: Mapped[int] = mapped_column(sa.Integer, default=1, nullable=False)
    first_as_of: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    last_as_of: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    window_bucket: Mapped[str] = mapped_column(sa.String(24), nullable=False)
    simulation_model_version: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    simulation_config_hash: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


class WalkForwardSplitRecord(Base):
    """One immutable train/validation/test split of a walk-forward run."""

    __tablename__ = "walk_forward_splits"
    __table_args__ = (
        sa.UniqueConstraint("run_id", "split_index", name="uq_walk_forward_splits_identity"),
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True)
    run_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("simulation_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    split_index: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    mode: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    train_start_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    train_end_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    validation_start_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False
    )
    validation_end_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    test_start_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    test_end_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    selected_configuration: Mapped[str | None] = mapped_column(sa.String(64))
    sample_status: Mapped[str] = mapped_column(sa.String(24), nullable=False)
    selection_rationale: Mapped[str | None] = mapped_column(sa.Text)
    candidates: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)
    scores: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


class PerformanceMetricSetRecord(Base):
    """One segment's hypothetical metric set (versioned definitions)."""

    __tablename__ = "performance_metric_sets"
    __table_args__ = (
        sa.UniqueConstraint("set_key", name="uq_performance_metric_sets_key"),
        sa.Index("ix_performance_metric_sets_run_segment", "run_id", "segment_kind"),
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True)
    run_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("simulation_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    set_key: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    segment_kind: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    segment_key: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    sample_status: Mapped[str] = mapped_column(sa.String(24), nullable=False, index=True)
    complete_simulations: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    metrics_version: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    disclaimer_version: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    warnings: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


class PerformanceMetricValueRecord(Base):
    """One named hypothetical metric value of a metric set."""

    __tablename__ = "performance_metric_values"
    __table_args__ = (
        sa.UniqueConstraint("metric_set_id", "name", name="uq_performance_metric_values_name"),
    )

    id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"), primary_key=True, autoincrement=True
    )
    metric_set_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("performance_metric_sets.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    value: Mapped[Decimal | None] = mapped_column(sa.Numeric(38, 12))
    unit: Mapped[str] = mapped_column(sa.String(24), nullable=False)
    detail: Mapped[str | None] = mapped_column(sa.Text)


class SimulationDataQualitySummaryRecord(Base):
    """Optional per-run data quality/coverage summary."""

    __tablename__ = "simulation_data_quality_summaries"
    __table_args__ = (
        sa.UniqueConstraint("run_id", "symbol", name="uq_simulation_dq_summary_identity"),
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True)
    run_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("simulation_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    symbol: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    checked_channels: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)
    missing_channels: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)
    gap_count: Mapped[int] = mapped_column(sa.Integer, default=0, nullable=False)
    excluded_seconds: Mapped[Decimal | None] = mapped_column(sa.Numeric(18, 3))
    detail: Mapped[str | None] = mapped_column(sa.Text)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )
