"""market selection: classifications, decisions, watchlist, session events

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-21
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003"
down_revision = "0002"
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
        "asset_classifications",
        sa.Column("id", _pk_bigint(), primary_key=True, autoincrement=True),
        _instrument_fk(),
        sa.Column("symbol", sa.String(64), nullable=False),
        sa.Column("asset_class", sa.String(16), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("rule", sa.String(128), nullable=False),
        sa.Column("confidence", NUMERIC, nullable=False),
        sa.Column("classified_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=UTCNOW, nullable=False),
    )
    op.create_index(
        "ix_asset_classifications_instrument_created",
        "asset_classifications",
        ["instrument_pk", "created_at"],
    )

    op.create_table(
        "market_selection_decisions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        _instrument_fk(),
        sa.Column("symbol", sa.String(64), nullable=False),
        sa.Column("asset_class", sa.String(16), nullable=False),
        sa.Column("classification_source", sa.String(32), nullable=False),
        sa.Column("session_state", sa.String(32), nullable=False),
        sa.Column("calendar_version", sa.String(32), nullable=True),
        sa.Column("market_status", sa.String(16), nullable=False),
        sa.Column("data_quality_status", sa.String(24), nullable=False),
        sa.Column("selection_state", sa.String(32), nullable=False),
        sa.Column("eligibility_status", sa.String(32), nullable=False),
        sa.Column("market_quality_score", sa.Integer(), nullable=True),
        sa.Column("quality_components", JSONB, nullable=True),
        sa.Column("reasons", JSONB, nullable=True),
        sa.Column("configuration_version", sa.String(32), nullable=False),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("previous_selection_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=UTCNOW, nullable=False),
    )
    op.create_index(
        "ix_market_selection_decisions_selection_state",
        "market_selection_decisions",
        ["selection_state"],
    )
    op.create_index(
        "ix_market_selection_decisions_eligibility_status",
        "market_selection_decisions",
        ["eligibility_status"],
    )
    op.create_index(
        "ix_market_selection_decisions_instrument_created",
        "market_selection_decisions",
        ["instrument_pk", "created_at"],
    )

    op.create_table(
        "active_watchlist",
        sa.Column("id", _pk_bigint(), primary_key=True, autoincrement=True),
        sa.Column(
            "instrument_pk",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            sa.ForeignKey("instruments.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("symbol", sa.String(64), nullable=False, unique=True),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("asset_class", sa.String(16), nullable=False),
        sa.Column("quality_score", sa.Integer(), nullable=True),
        sa.Column("eligibility_status", sa.String(32), nullable=False),
        sa.Column("reasons", JSONB, nullable=True),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("paused_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_decision_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=UTCNOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=UTCNOW, nullable=False),
    )
    op.create_index("ix_active_watchlist_state", "active_watchlist", ["state"])

    op.create_table(
        "watchlist_events",
        sa.Column("id", _pk_bigint(), primary_key=True, autoincrement=True),
        _instrument_fk(),
        sa.Column("symbol", sa.String(64), nullable=False),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("from_state", sa.String(32), nullable=True),
        sa.Column("to_state", sa.String(32), nullable=True),
        sa.Column("reasons", JSONB, nullable=True),
        sa.Column("decision_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=UTCNOW, nullable=False),
    )
    op.create_index("ix_watchlist_events_event_type", "watchlist_events", ["event_type"])
    op.create_index(
        "ix_watchlist_events_instrument_created",
        "watchlist_events",
        ["instrument_pk", "created_at"],
    )

    op.create_table(
        "session_events",
        sa.Column("id", _pk_bigint(), primary_key=True, autoincrement=True),
        sa.Column("scope", sa.String(16), nullable=False),
        sa.Column("from_state", sa.String(32), nullable=True),
        sa.Column("to_state", sa.String(32), nullable=False),
        sa.Column("calendar_version", sa.String(32), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("details", JSONB, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=UTCNOW, nullable=False),
    )
    op.create_index("ix_session_events_scope_created", "session_events", ["scope", "created_at"])


def downgrade() -> None:
    for table in (
        "session_events",
        "watchlist_events",
        "active_watchlist",
        "market_selection_decisions",
        "asset_classifications",
    ):
        op.drop_table(table)
