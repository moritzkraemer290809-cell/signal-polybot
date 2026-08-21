"""telegram delivery queue fields and app_state table

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-21
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

JSONB = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    with op.batch_alter_table("telegram_deliveries") as batch:
        batch.alter_column("status", type_=sa.String(32), existing_type=sa.String(16))
        batch.add_column(sa.Column("delivery_id", sa.Uuid(), nullable=True))
        batch.add_column(sa.Column("system_event_id", sa.BigInteger(), nullable=True))
        batch.add_column(
            sa.Column("operation", sa.String(16), nullable=False, server_default="SEND")
        )
        batch.add_column(sa.Column("priority", sa.Integer(), nullable=False, server_default="3"))
        batch.add_column(sa.Column("payload", JSONB, nullable=True))
        batch.add_column(sa.Column("content_hash", sa.String(64), nullable=True))
        batch.add_column(
            sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0")
        )
        batch.add_column(sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("last_error_class", sa.String(64), nullable=True))
        batch.add_column(sa.Column("last_error_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("correlation_id", sa.String(64), nullable=True))
        batch.create_unique_constraint("uq_telegram_deliveries_delivery_id", ["delivery_id"])
    op.create_index(
        "ix_telegram_deliveries_claim",
        "telegram_deliveries",
        ["status", "priority", "scheduled_at"],
    )
    op.create_index("ix_telegram_deliveries_content_hash", "telegram_deliveries", ["content_hash"])

    op.create_table(
        "app_state",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("value", JSONB, nullable=True),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )


def downgrade() -> None:
    op.drop_table("app_state")
    op.drop_index("ix_telegram_deliveries_content_hash", table_name="telegram_deliveries")
    op.drop_index("ix_telegram_deliveries_claim", table_name="telegram_deliveries")
    with op.batch_alter_table("telegram_deliveries") as batch:
        batch.drop_constraint("uq_telegram_deliveries_delivery_id", type_="unique")
        for column in (
            "correlation_id",
            "last_error_at",
            "last_error_class",
            "lease_expires_at",
            "scheduled_at",
            "attempt_count",
            "content_hash",
            "payload",
            "priority",
            "operation",
            "system_event_id",
            "delivery_id",
        ):
            batch.drop_column(column)
        batch.alter_column("status", type_=sa.String(16), existing_type=sa.String(32))
