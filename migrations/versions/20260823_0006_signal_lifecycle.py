"""internal signal lifecycle: signals, events, updates, rejections

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-23
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

JSONB = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
NUMERIC = sa.Numeric(38, 18)
UTCNOW = sa.func.now()


def _pk_bigint() -> sa.types.TypeEngine:
    return sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def upgrade() -> None:
    op.create_table(
        "signal_lifecycles",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("plan_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column(
            "instrument_pk",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            sa.ForeignKey("instruments.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("instrument_id", sa.BigInteger(), nullable=False),
        sa.Column("symbol", sa.String(64), nullable=False),
        sa.Column("asset_class", sa.String(16), nullable=False),
        sa.Column("candidate_type", sa.String(40), nullable=False),
        sa.Column("direction", sa.String(8), nullable=False),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("state_version", sa.Integer(), nullable=False),
        sa.Column("entry_low", NUMERIC, nullable=False),
        sa.Column("entry_high", NUMERIC, nullable=False),
        sa.Column("entry_reference_price", NUMERIC, nullable=False),
        sa.Column("invalidation_price", NUMERIC, nullable=False),
        sa.Column("target_prices", JSONB, nullable=True),
        sa.Column("entry_trigger", sa.String(40), nullable=False),
        sa.Column("admission_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("watching_entry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("entry_confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("active_research_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("terminal_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_evaluated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_market_data_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_data_quality_status", sa.String(24), nullable=True),
        sa.Column("last_session_state", sa.String(32), nullable=True),
        sa.Column("last_event_type", sa.String(24), nullable=True),
        sa.Column("data_degraded_since", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reference_snapshot", JSONB, nullable=True),
        sa.Column("approximation_warnings", JSONB, nullable=True),
        sa.Column("lifecycle_model_name", sa.String(64), nullable=False),
        sa.Column("lifecycle_model_version", sa.String(32), nullable=False),
        sa.Column("lifecycle_config_hash", sa.String(32), nullable=False),
        sa.Column("strategy_version", sa.String(32), nullable=False),
        sa.Column("risk_model_version", sa.String(32), nullable=False),
        sa.Column("cost_model_version", sa.String(32), nullable=False),
        sa.Column("fee_schedule_version", sa.String(48), nullable=False),
        sa.Column("dedupe_key", sa.String(32), nullable=False),
        sa.Column("active_key", sa.String(32), nullable=True),
        sa.Column("lease_owner", sa.String(64), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("correlation_id", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=UTCNOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=UTCNOW, nullable=False),
        sa.UniqueConstraint("active_key", name="uq_signal_lifecycles_active_key"),
    )
    op.create_index("ix_signal_lifecycles_plan_id", "signal_lifecycles", ["plan_id"])
    op.create_index("ix_signal_lifecycles_candidate_id", "signal_lifecycles", ["candidate_id"])
    op.create_index("ix_signal_lifecycles_instrument_id", "signal_lifecycles", ["instrument_id"])
    op.create_index("ix_signal_lifecycles_state", "signal_lifecycles", ["state"])
    op.create_index(
        "ix_signal_lifecycles_instrument_state", "signal_lifecycles", ["instrument_pk", "state"]
    )
    op.create_index("ix_signal_lifecycles_expires_at", "signal_lifecycles", ["expires_at"])
    op.create_index(
        "ix_signal_lifecycles_last_evaluated_at", "signal_lifecycles", ["last_evaluated_at"]
    )
    op.create_index("ix_signal_lifecycles_dedupe", "signal_lifecycles", ["dedupe_key"])
    op.create_index(
        "ix_signal_lifecycles_lifecycle_model_version",
        "signal_lifecycles",
        ["lifecycle_model_version"],
    )

    op.create_table(
        "signal_lifecycle_events",
        sa.Column("id", _pk_bigint(), primary_key=True, autoincrement=True),
        sa.Column("signal_id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.String(24), nullable=False),
        sa.Column("from_state", sa.String(24), nullable=True),
        sa.Column("to_state", sa.String(24), nullable=True),
        sa.Column("state_version", sa.Integer(), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=True),
        sa.Column("idempotency_key", sa.String(64), nullable=False),
        sa.Column("detail", JSONB, nullable=True),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=UTCNOW, nullable=False),
        sa.UniqueConstraint("idempotency_key", name="uq_signal_lifecycle_events_idempotency_key"),
    )
    op.create_index(
        "ix_signal_lifecycle_events_signal_id", "signal_lifecycle_events", ["signal_id"]
    )

    op.create_table(
        "signal_updates",
        sa.Column("id", _pk_bigint(), primary_key=True, autoincrement=True),
        sa.Column("signal_id", sa.Uuid(), nullable=False),
        sa.Column("update_type", sa.String(32), nullable=False),
        sa.Column("idempotency_key", sa.String(64), nullable=False),
        sa.Column("detail", JSONB, nullable=True),
        sa.Column("count", sa.Integer(), nullable=False),
        sa.Column("first_as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=UTCNOW, nullable=False),
        sa.UniqueConstraint("idempotency_key", name="uq_signal_updates_idempotency_key"),
    )
    op.create_index("ix_signal_updates_signal_id", "signal_updates", ["signal_id"])
    op.create_index("ix_signal_updates_update_type", "signal_updates", ["update_type"])

    op.create_table(
        "signal_lifecycle_rejections",
        sa.Column("id", _pk_bigint(), primary_key=True, autoincrement=True),
        sa.Column("plan_id", sa.Uuid(), nullable=True),
        sa.Column("candidate_id", sa.Uuid(), nullable=True),
        sa.Column("signal_id", sa.Uuid(), nullable=True),
        sa.Column("context_key", sa.String(40), nullable=False),
        sa.Column(
            "instrument_pk",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            sa.ForeignKey("instruments.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("symbol", sa.String(64), nullable=False),
        sa.Column("primary_code", sa.String(48), nullable=False),
        sa.Column("codes", JSONB, nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("count", sa.Integer(), nullable=False),
        sa.Column("first_as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_bucket", sa.String(24), nullable=False),
        sa.Column("lifecycle_model_version", sa.String(32), nullable=False),
        sa.Column("lifecycle_config_hash", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=UTCNOW, nullable=False),
        sa.UniqueConstraint(
            "context_key",
            "primary_code",
            "window_bucket",
            "lifecycle_model_version",
            name="uq_signal_lifecycle_rejections_identity",
        ),
    )
    op.create_index(
        "ix_signal_lifecycle_rejections_plan_id", "signal_lifecycle_rejections", ["plan_id"]
    )
    op.create_index(
        "ix_signal_lifecycle_rejections_primary_code",
        "signal_lifecycle_rejections",
        ["primary_code"],
    )


def downgrade() -> None:
    op.drop_table("signal_lifecycle_rejections")
    op.drop_table("signal_updates")
    op.drop_table("signal_lifecycle_events")
    op.drop_table("signal_lifecycles")
