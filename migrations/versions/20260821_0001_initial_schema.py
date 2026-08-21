"""initial schema: all core tables

Revision ID: 0001
Revises:
Create Date: 2026-08-21
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

JSONB = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
NUMERIC = sa.Numeric(38, 18)
UTCNOW = sa.func.now()


def _pk_bigint() -> sa.types.TypeEngine:
    return sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def upgrade() -> None:
    op.create_table(
        "instruments",
        sa.Column("id", _pk_bigint(), primary_key=True, autoincrement=True),
        sa.Column("instrument_id", sa.BigInteger(), nullable=False, unique=True),
        sa.Column("symbol", sa.String(64), nullable=False, unique=True),
        sa.Column("instrument_type", sa.String(32), nullable=False),
        sa.Column("category", sa.String(32), nullable=False),
        sa.Column("asset_class", sa.String(16), nullable=False),
        sa.Column("base_asset", sa.String(32), nullable=False),
        sa.Column("quote_asset", sa.String(32), nullable=False),
        sa.Column("price_decimals", sa.Integer(), nullable=False),
        sa.Column("quantity_decimals", sa.Integer(), nullable=False),
        sa.Column("min_notional", NUMERIC, nullable=False),
        sa.Column("max_leverage", sa.Integer(), nullable=False),
        sa.Column("funding_interval", sa.String(16), nullable=True),
        sa.Column("liquidation_fee", NUMERIC, nullable=True),
        sa.Column("isolated_only", sa.Boolean(), nullable=False),
        sa.Column("risk_tiers", JSONB, nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("metadata_raw", JSONB, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=UTCNOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=UTCNOW, nullable=False),
    )
    op.create_index("ix_instruments_status", "instruments", ["status"])
    op.create_index("ix_instruments_enabled", "instruments", ["enabled"])

    op.create_table(
        "market_ticks",
        sa.Column("id", _pk_bigint(), primary_key=True, autoincrement=True),
        sa.Column(
            "instrument_pk",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            sa.ForeignKey("instruments.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("mark_price", NUMERIC, nullable=True),
        sa.Column("index_price", NUMERIC, nullable=True),
        sa.Column("last_price", NUMERIC, nullable=True),
        sa.Column("mid_price", NUMERIC, nullable=True),
        sa.Column("open_interest", NUMERIC, nullable=True),
        sa.Column("funding_rate", NUMERIC, nullable=True),
        sa.Column("next_funding_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=UTCNOW, nullable=False),
    )
    op.create_index("ix_market_ticks_instrument_ts", "market_ticks", ["instrument_pk", "ts"])

    op.create_table(
        "candles",
        sa.Column("id", _pk_bigint(), primary_key=True, autoincrement=True),
        sa.Column(
            "instrument_pk",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            sa.ForeignKey("instruments.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("timeframe", sa.String(8), nullable=False),
        sa.Column("open_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("open", NUMERIC, nullable=False),
        sa.Column("high", NUMERIC, nullable=False),
        sa.Column("low", NUMERIC, nullable=False),
        sa.Column("close", NUMERIC, nullable=False),
        sa.Column("volume", NUMERIC, nullable=False),
        sa.Column("trade_count", sa.Integer(), nullable=False),
        sa.Column("complete", sa.Boolean(), nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=UTCNOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=UTCNOW, nullable=False),
        sa.UniqueConstraint(
            "instrument_pk", "timeframe", "open_time", name="uq_candles_instrument_tf_open"
        ),
    )

    op.create_table(
        "orderbook_snapshots",
        sa.Column("id", _pk_bigint(), primary_key=True, autoincrement=True),
        sa.Column(
            "instrument_pk",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            sa.ForeignKey("instruments.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=True),
        sa.Column("depth_level", sa.Integer(), nullable=False),
        sa.Column("bids", JSONB, nullable=True),
        sa.Column("asks", JSONB, nullable=True),
        sa.Column("best_bid", NUMERIC, nullable=True),
        sa.Column("best_ask", NUMERIC, nullable=True),
        sa.Column("spread_bps", NUMERIC, nullable=True),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=UTCNOW, nullable=False),
    )
    op.create_index(
        "ix_orderbook_snapshots_instrument_ts", "orderbook_snapshots", ["instrument_pk", "ts"]
    )

    op.create_table(
        "funding_rates",
        sa.Column("id", _pk_bigint(), primary_key=True, autoincrement=True),
        sa.Column(
            "instrument_pk",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            sa.ForeignKey("instruments.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("funding_rate", NUMERIC, nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=UTCNOW, nullable=False),
        sa.UniqueConstraint("instrument_pk", "ts", name="uq_funding_rates_instrument_ts"),
    )

    op.create_table(
        "market_regimes",
        sa.Column("id", _pk_bigint(), primary_key=True, autoincrement=True),
        sa.Column(
            "instrument_pk",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            sa.ForeignKey("instruments.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("timeframe", sa.String(8), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("regime", sa.String(32), nullable=False),
        sa.Column("confidence", NUMERIC, nullable=True),
        sa.Column("features", JSONB, nullable=True),
        sa.Column("strategy_version", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=UTCNOW, nullable=False),
    )
    op.create_index("ix_market_regimes_instrument_ts", "market_regimes", ["instrument_pk", "ts"])

    op.create_table(
        "signals",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("short_id", sa.String(64), nullable=False, unique=True),
        sa.Column(
            "instrument_pk",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            sa.ForeignKey("instruments.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("symbol", sa.String(64), nullable=False),
        sa.Column("direction", sa.String(8), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("score", NUMERIC, nullable=True),
        sa.Column("score_breakdown", JSONB, nullable=True),
        sa.Column("entry_low", NUMERIC, nullable=True),
        sa.Column("entry_high", NUMERIC, nullable=True),
        sa.Column("entry_trigger", sa.Text(), nullable=True),
        sa.Column("stop_price", NUMERIC, nullable=True),
        sa.Column("invalidation_reason", sa.Text(), nullable=True),
        sa.Column("tp1", NUMERIC, nullable=True),
        sa.Column("tp2", NUMERIC, nullable=True),
        sa.Column("trailing_rule", JSONB, nullable=True),
        sa.Column("gross_rr", NUMERIC, nullable=True),
        sa.Column("net_rr", NUMERIC, nullable=True),
        sa.Column("leverage_min", sa.Integer(), nullable=True),
        sa.Column("leverage_max", sa.Integer(), nullable=True),
        sa.Column("reference_risk_pct", NUMERIC, nullable=True),
        sa.Column("regime", sa.String(32), nullable=True),
        sa.Column("setup_reason", sa.Text(), nullable=True),
        sa.Column("cost_assumptions", JSONB, nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("estimated_hold_minutes", sa.Integer(), nullable=True),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("strategy_version", sa.String(32), nullable=False),
        sa.Column("config_version", sa.String(32), nullable=False),
        sa.Column("correlation_id", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=UTCNOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=UTCNOW, nullable=False),
    )
    op.create_index("ix_signals_status", "signals", ["status"])
    op.create_index("ix_signals_correlation_id", "signals", ["correlation_id"])
    op.create_index("ix_signals_instrument_status", "signals", ["instrument_pk", "status"])

    op.create_table(
        "signal_events",
        sa.Column("id", _pk_bigint(), primary_key=True, autoincrement=True),
        sa.Column(
            "signal_id", sa.Uuid(), sa.ForeignKey("signals.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("from_status", sa.String(32), nullable=True),
        sa.Column("to_status", sa.String(32), nullable=True),
        sa.Column("payload", JSONB, nullable=True),
        sa.Column("idempotency_key", sa.String(128), nullable=False, unique=True),
        sa.Column("correlation_id", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=UTCNOW, nullable=False),
    )
    op.create_index("ix_signal_events_signal_id", "signal_events", ["signal_id"])

    op.create_table(
        "strategy_decisions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "instrument_pk",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            sa.ForeignKey("instruments.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("symbol", sa.String(64), nullable=False),
        sa.Column("decision", sa.String(32), nullable=False),
        sa.Column("direction", sa.String(8), nullable=True),
        sa.Column("reasons", JSONB, nullable=True),
        sa.Column("score", NUMERIC, nullable=True),
        sa.Column("score_breakdown", JSONB, nullable=True),
        sa.Column("features", JSONB, nullable=True),
        sa.Column("regime", sa.String(32), nullable=True),
        sa.Column("trade_plan", JSONB, nullable=True),
        sa.Column("cost_assumptions", JSONB, nullable=True),
        sa.Column("data_quality_status", sa.String(16), nullable=False),
        sa.Column(
            "signal_id", sa.Uuid(), sa.ForeignKey("signals.id", ondelete="SET NULL"), nullable=True
        ),
        sa.Column("strategy_version", sa.String(32), nullable=False),
        sa.Column("config_version", sa.String(32), nullable=False),
        sa.Column("correlation_id", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=UTCNOW, nullable=False),
    )
    op.create_index("ix_strategy_decisions_decision", "strategy_decisions", ["decision"])
    op.create_index(
        "ix_strategy_decisions_correlation_id", "strategy_decisions", ["correlation_id"]
    )
    op.create_index(
        "ix_strategy_decisions_instrument_created",
        "strategy_decisions",
        ["instrument_pk", "created_at"],
    )

    op.create_table(
        "fee_schedule_versions",
        sa.Column("id", _pk_bigint(), primary_key=True, autoincrement=True),
        sa.Column("version", sa.String(32), nullable=False, unique=True),
        sa.Column("schedule", JSONB, nullable=False),
        sa.Column("source", sa.String(64), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("effective_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=UTCNOW, nullable=False),
    )
    op.create_index("ix_fee_schedule_versions_active", "fee_schedule_versions", ["active"])

    op.create_table(
        "system_events",
        sa.Column("id", _pk_bigint(), primary_key=True, autoincrement=True),
        sa.Column("level", sa.String(16), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("context", JSONB, nullable=True),
        sa.Column("correlation_id", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=UTCNOW, nullable=False),
    )
    op.create_index("ix_system_events_level", "system_events", ["level"])
    op.create_index("ix_system_events_event_type", "system_events", ["event_type"])

    op.create_table(
        "telegram_deliveries",
        sa.Column("id", _pk_bigint(), primary_key=True, autoincrement=True),
        sa.Column(
            "signal_id", sa.Uuid(), sa.ForeignKey("signals.id", ondelete="SET NULL"), nullable=True
        ),
        sa.Column("message_type", sa.String(64), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("message_id", sa.BigInteger(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False, unique=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=UTCNOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=UTCNOW, nullable=False),
    )
    op.create_index("ix_telegram_deliveries_signal_id", "telegram_deliveries", ["signal_id"])
    op.create_index("ix_telegram_deliveries_status", "telegram_deliveries", ["status"])


def downgrade() -> None:
    for table in (
        "telegram_deliveries",
        "system_events",
        "fee_schedule_versions",
        "strategy_decisions",
        "signal_events",
        "signals",
        "market_regimes",
        "funding_rates",
        "orderbook_snapshots",
        "candles",
        "market_ticks",
        "instruments",
    ):
        op.drop_table(table)
