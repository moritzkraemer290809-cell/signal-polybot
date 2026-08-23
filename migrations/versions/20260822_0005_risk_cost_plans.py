"""risk & cost research: instrument snapshots, cost estimates, eligibility plans

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-22
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0005"
down_revision = "0004"
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
        "instrument_risk_snapshots",
        sa.Column("id", sa.Uuid(), primary_key=True),
        _instrument_fk(),
        sa.Column("instrument_id", sa.BigInteger(), nullable=False),
        sa.Column("symbol", sa.String(64), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("snapshot_version", sa.String(16), nullable=False),
        sa.Column("mark_price", NUMERIC, nullable=True),
        sa.Column("max_leverage", sa.Integer(), nullable=True),
        sa.Column("min_notional", NUMERIC, nullable=True),
        sa.Column("data_quality_status", sa.String(24), nullable=False),
        sa.Column("orderbook_fresh", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("payload", JSONB, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=UTCNOW, nullable=False),
    )
    op.create_index(
        "ix_instrument_risk_snapshots_instrument_as_of",
        "instrument_risk_snapshots",
        ["instrument_pk", "as_of"],
    )
    op.create_index(
        "ix_instrument_risk_snapshots_instrument_id",
        "instrument_risk_snapshots",
        ["instrument_id"],
    )

    op.create_table(
        "execution_assumption_versions",
        sa.Column("id", _pk_bigint(), primary_key=True, autoincrement=True),
        sa.Column("version", sa.String(48), nullable=False),
        sa.Column("assumptions", JSONB, nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=UTCNOW, nullable=False),
        sa.UniqueConstraint("version", name="uq_execution_assumption_versions_version"),
    )
    op.create_index(
        "ix_execution_assumption_versions_active", "execution_assumption_versions", ["active"]
    )

    op.create_table(
        "cost_estimates",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("plan_id", sa.Uuid(), nullable=True),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        _instrument_fk(),
        sa.Column("symbol", sa.String(64), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cost_model_name", sa.String(64), nullable=False),
        sa.Column("cost_model_version", sa.String(32), nullable=False),
        sa.Column("cost_config_hash", sa.String(32), nullable=False),
        sa.Column("fee_schedule_version", sa.String(48), nullable=False),
        sa.Column("execution_assumption_version", sa.String(48), nullable=False),
        sa.Column("total_cost", NUMERIC, nullable=True),
        sa.Column("cost_to_risk_pct", NUMERIC, nullable=True),
        sa.Column("net_rr_primary", NUMERIC, nullable=True),
        sa.Column("payload", JSONB, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=UTCNOW, nullable=False),
    )
    op.create_index("ix_cost_estimates_plan_id", "cost_estimates", ["plan_id"])
    op.create_index("ix_cost_estimates_candidate_id", "cost_estimates", ["candidate_id"])
    op.create_index(
        "ix_cost_estimates_instrument_as_of", "cost_estimates", ["instrument_pk", "as_of"]
    )
    op.create_index(
        "ix_cost_estimates_cost_model_version", "cost_estimates", ["cost_model_version"]
    )
    op.create_index(
        "ix_cost_estimates_fee_schedule_version", "cost_estimates", ["fee_schedule_version"]
    )

    op.create_table(
        "risk_plans",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        _instrument_fk(),
        sa.Column("instrument_id", sa.BigInteger(), nullable=False),
        sa.Column("symbol", sa.String(64), nullable=False),
        sa.Column("asset_class", sa.String(16), nullable=False),
        sa.Column("candidate_type", sa.String(40), nullable=False),
        sa.Column("direction", sa.String(8), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("eligibility_score", sa.Integer(), nullable=False),
        sa.Column("net_rr_primary", NUMERIC, nullable=True),
        sa.Column("cost_to_risk_pct", NUMERIC, nullable=True),
        sa.Column("risk_model_name", sa.String(64), nullable=False),
        sa.Column("risk_model_version", sa.String(32), nullable=False),
        sa.Column("risk_config_hash", sa.String(32), nullable=False),
        sa.Column("cost_model_version", sa.String(32), nullable=False),
        sa.Column("cost_config_hash", sa.String(32), nullable=False),
        sa.Column("fee_schedule_version", sa.String(48), nullable=False),
        sa.Column("execution_assumption_version", sa.String(48), nullable=False),
        sa.Column("instrument_snapshot_id", sa.Uuid(), nullable=True),
        sa.Column("instrument_snapshot_version", sa.String(16), nullable=False),
        sa.Column("strategy_version", sa.String(32), nullable=False),
        sa.Column("strategy_config_hash", sa.String(32), nullable=False),
        sa.Column("payload", JSONB, nullable=True),
        sa.Column("dedupe_key", sa.String(32), nullable=False),
        sa.Column("active_key", sa.String(32), nullable=True),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expiry_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=UTCNOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=UTCNOW, nullable=False),
        sa.UniqueConstraint("active_key", name="uq_risk_plans_active_key"),
    )
    op.create_index("ix_risk_plans_candidate_id", "risk_plans", ["candidate_id"])
    op.create_index("ix_risk_plans_instrument_id", "risk_plans", ["instrument_id"])
    op.create_index("ix_risk_plans_status", "risk_plans", ["status"])
    op.create_index("ix_risk_plans_instrument_status", "risk_plans", ["instrument_pk", "status"])
    op.create_index("ix_risk_plans_as_of", "risk_plans", ["as_of"])
    op.create_index("ix_risk_plans_dedupe", "risk_plans", ["dedupe_key"])
    op.create_index("ix_risk_plans_risk_model_version", "risk_plans", ["risk_model_version"])
    op.create_index("ix_risk_plans_cost_model_version", "risk_plans", ["cost_model_version"])
    op.create_index("ix_risk_plans_fee_schedule_version", "risk_plans", ["fee_schedule_version"])

    op.create_table(
        "risk_plan_events",
        sa.Column("id", _pk_bigint(), primary_key=True, autoincrement=True),
        sa.Column("plan_id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.String(24), nullable=False),
        sa.Column("from_status", sa.String(24), nullable=True),
        sa.Column("to_status", sa.String(24), nullable=True),
        sa.Column("detail", JSONB, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=UTCNOW, nullable=False),
    )
    op.create_index("ix_risk_plan_events_plan_id", "risk_plan_events", ["plan_id"])

    op.create_table(
        "risk_plan_rejections",
        sa.Column("id", _pk_bigint(), primary_key=True, autoincrement=True),
        sa.Column("candidate_id", sa.Uuid(), nullable=True),
        sa.Column("candidate_key", sa.String(40), nullable=False),
        _instrument_fk(),
        sa.Column("symbol", sa.String(64), nullable=False),
        sa.Column("primary_code", sa.String(48), nullable=False),
        sa.Column("codes", JSONB, nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("count", sa.Integer(), nullable=False),
        sa.Column("first_as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_bucket", sa.String(24), nullable=False),
        sa.Column("risk_model_version", sa.String(32), nullable=False),
        sa.Column("risk_config_hash", sa.String(32), nullable=False),
        sa.Column("cost_model_version", sa.String(32), nullable=False),
        sa.Column("fee_schedule_version", sa.String(48), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=UTCNOW, nullable=False),
        sa.UniqueConstraint(
            "candidate_key",
            "primary_code",
            "window_bucket",
            "risk_model_version",
            name="uq_risk_plan_rejections_identity",
        ),
    )
    op.create_index(
        "ix_risk_plan_rejections_candidate_id", "risk_plan_rejections", ["candidate_id"]
    )
    op.create_index(
        "ix_risk_plan_rejections_primary_code", "risk_plan_rejections", ["primary_code"]
    )


def downgrade() -> None:
    op.drop_table("risk_plan_rejections")
    op.drop_table("risk_plan_events")
    op.drop_table("risk_plans")
    op.drop_table("cost_estimates")
    op.drop_table("execution_assumption_versions")
    op.drop_table("instrument_risk_snapshots")
