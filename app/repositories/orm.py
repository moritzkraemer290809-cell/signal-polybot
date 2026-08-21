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
