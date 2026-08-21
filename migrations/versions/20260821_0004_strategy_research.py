"""strategy research: features, swings, structure, liquidity, candidates

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-21
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

JSONB = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
NUMERIC = sa.Numeric(38, 18)
UTCNOW = sa.func.now()


def _pk_bigint() -> sa.types.TypeEngine:
    return sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def _instrument_fk() -> sa.Column:
    return sa.Column(
        "instrument_pk",
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        sa.ForeignKey("instruments.id", ondelete="CASCADE"),
        nullable=False,
    )


def upgrade() -> None:
    op.create_table(
        "feature_snapshots",
        sa.Column("id", sa.Uuid(), primary_key=True),
        _instrument_fk(),
        sa.Column("symbol", sa.String(64), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("candle_window_metadata", JSONB, nullable=True),
        sa.Column("feature_values", JSONB, nullable=True),
        sa.Column("feature_validity", JSONB, nullable=True),
        sa.Column("warnings", JSONB, nullable=True),
        sa.Column("strategy_name", sa.String(64), nullable=False),
        sa.Column("strategy_version", sa.String(32), nullable=False),
        sa.Column("feature_schema_version", sa.String(16), nullable=False),
        sa.Column("config_hash", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=UTCNOW, nullable=False),
    )
    op.create_index(
        "ix_feature_snapshots_instrument_as_of", "feature_snapshots", ["instrument_pk", "as_of"]
    )
    op.create_index(
        "ix_feature_snapshots_strategy_version", "feature_snapshots", ["strategy_version"]
    )

    op.create_table(
        "swing_points",
        sa.Column("id", _pk_bigint(), primary_key=True, autoincrement=True),
        _instrument_fk(),
        sa.Column("timeframe", sa.String(8), nullable=False),
        sa.Column("swing_type", sa.String(8), nullable=False),
        sa.Column("price", NUMERIC, nullable=False),
        sa.Column("candle_open_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("strength", NUMERIC, nullable=True),
        sa.Column("relevance", NUMERIC, nullable=True),
        sa.Column("params", JSONB, nullable=True),
        sa.Column("strategy_version", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=UTCNOW, nullable=False),
        sa.UniqueConstraint(
            "instrument_pk",
            "timeframe",
            "swing_type",
            "candle_open_time",
            "strategy_version",
            name="uq_swing_points_identity",
        ),
    )

    op.create_table(
        "structure_events",
        sa.Column("id", _pk_bigint(), primary_key=True, autoincrement=True),
        _instrument_fk(),
        sa.Column("timeframe", sa.String(8), nullable=False),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("price", NUMERIC, nullable=False),
        sa.Column("reference_swing", sa.String(96), nullable=True),
        sa.Column("confirm_close_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("strategy_version", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=UTCNOW, nullable=False),
        sa.UniqueConstraint(
            "instrument_pk",
            "timeframe",
            "event_type",
            "confirm_close_time",
            "strategy_version",
            name="uq_structure_events_identity",
        ),
    )
    op.create_index("ix_structure_events_event_type", "structure_events", ["event_type"])

    op.create_table(
        "liquidity_levels",
        sa.Column("id", _pk_bigint(), primary_key=True, autoincrement=True),
        _instrument_fk(),
        sa.Column("timeframe", sa.String(8), nullable=False),
        sa.Column("level_type", sa.String(24), nullable=False),
        sa.Column("price", NUMERIC, nullable=False),
        sa.Column("price_key", sa.String(32), nullable=False),
        sa.Column("relevance", NUMERIC, nullable=True),
        sa.Column("touches", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("is_major", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("strategy_version", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=UTCNOW, nullable=False),
        sa.UniqueConstraint(
            "instrument_pk",
            "timeframe",
            "level_type",
            "price_key",
            "strategy_version",
            name="uq_liquidity_levels_identity",
        ),
    )

    op.create_table(
        "liquidity_events",
        sa.Column("id", _pk_bigint(), primary_key=True, autoincrement=True),
        _instrument_fk(),
        sa.Column("timeframe", sa.String(8), nullable=False),
        sa.Column("event_type", sa.String(24), nullable=False),
        sa.Column("direction", sa.String(8), nullable=False),
        sa.Column("level_price", NUMERIC, nullable=False),
        sa.Column("level_price_key", sa.String(32), nullable=False),
        sa.Column("event_candle_open_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("overshoot_bps", NUMERIC, nullable=True),
        sa.Column("confirmed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("confidence", NUMERIC, nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("strategy_version", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=UTCNOW, nullable=False),
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

    op.create_table(
        "setup_candidates",
        sa.Column("id", sa.Uuid(), primary_key=True),
        _instrument_fk(),
        sa.Column("instrument_id", sa.BigInteger(), nullable=False),
        sa.Column("symbol", sa.String(64), nullable=False),
        sa.Column("asset_class", sa.String(16), nullable=False),
        sa.Column("candidate_type", sa.String(40), nullable=False),
        sa.Column("direction", sa.String(8), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expiry_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("setup_score", sa.Integer(), nullable=False),
        sa.Column("confidence", sa.Integer(), nullable=False),
        sa.Column("primary_regime", sa.String(24), nullable=False),
        sa.Column("regime_confidence", sa.Integer(), nullable=False),
        sa.Column("higher_timeframe_structure", sa.String(28), nullable=False),
        sa.Column("local_structure", sa.String(28), nullable=False),
        sa.Column("session_state", sa.String(32), nullable=False),
        sa.Column("market_quality_score", sa.Integer(), nullable=True),
        sa.Column("data_quality_status", sa.String(24), nullable=False),
        sa.Column("retest_status", sa.String(16), nullable=False),
        sa.Column("reason_summary", sa.Text(), nullable=False),
        sa.Column("details", JSONB, nullable=True),
        sa.Column("dedupe_key", sa.String(32), nullable=False),
        sa.Column("active_key", sa.String(32), nullable=True, unique=True),
        sa.Column("feature_snapshot_id", sa.Uuid(), nullable=True),
        sa.Column("strategy_name", sa.String(64), nullable=False),
        sa.Column("strategy_version", sa.String(32), nullable=False),
        sa.Column("feature_schema_version", sa.String(16), nullable=False),
        sa.Column("config_hash", sa.String(32), nullable=False),
        sa.Column("ruleset_hash", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=UTCNOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=UTCNOW, nullable=False),
    )
    op.create_index("ix_setup_candidates_candidate_type", "setup_candidates", ["candidate_type"])
    op.create_index("ix_setup_candidates_state", "setup_candidates", ["state"])
    op.create_index(
        "ix_setup_candidates_strategy_version", "setup_candidates", ["strategy_version"]
    )
    op.create_index(
        "ix_setup_candidates_instrument_state", "setup_candidates", ["instrument_pk", "state"]
    )
    op.create_index("ix_setup_candidates_dedupe", "setup_candidates", ["dedupe_key"])

    op.create_table(
        "setup_candidate_events",
        sa.Column("id", _pk_bigint(), primary_key=True, autoincrement=True),
        sa.Column(
            "candidate_id",
            sa.Uuid(),
            sa.ForeignKey("setup_candidates.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("event_type", sa.String(24), nullable=False),
        sa.Column("from_state", sa.String(16), nullable=True),
        sa.Column("to_state", sa.String(16), nullable=True),
        sa.Column("detail", JSONB, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=UTCNOW, nullable=False),
    )
    op.create_index(
        "ix_setup_candidate_events_candidate_id", "setup_candidate_events", ["candidate_id"]
    )

    op.create_table(
        "setup_rejections",
        sa.Column("id", _pk_bigint(), primary_key=True, autoincrement=True),
        _instrument_fk(),
        sa.Column("symbol", sa.String(64), nullable=False),
        sa.Column("primary_code", sa.String(40), nullable=False),
        sa.Column("candidate_type", sa.String(40), nullable=False, server_default=""),
        sa.Column("codes", JSONB, nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("first_as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_bucket", sa.String(24), nullable=False),
        sa.Column("strategy_version", sa.String(32), nullable=False),
        sa.Column("config_hash", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=UTCNOW, nullable=False),
        sa.UniqueConstraint(
            "instrument_pk",
            "primary_code",
            "candidate_type",
            "window_bucket",
            "strategy_version",
            name="uq_setup_rejections_bucket",
        ),
    )
    op.create_index("ix_setup_rejections_primary_code", "setup_rejections", ["primary_code"])
    op.create_index(
        "ix_setup_rejections_instrument_created",
        "setup_rejections",
        ["instrument_pk", "created_at"],
    )


def downgrade() -> None:
    for table in (
        "setup_rejections",
        "setup_candidate_events",
        "setup_candidates",
        "liquidity_events",
        "liquidity_levels",
        "structure_events",
        "swing_points",
        "feature_snapshots",
    ):
        op.drop_table(table)
