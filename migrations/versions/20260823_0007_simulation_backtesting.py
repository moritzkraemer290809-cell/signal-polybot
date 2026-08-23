"""phase 11: experiments, manifests, simulation/backtest runs, metrics

Revision ID: 0007
Revises: 0006
Create Date: 2026-08-23
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

JSONB = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
NUMERIC = sa.Numeric(38, 18)
RATIO = sa.Numeric(18, 6)
UTCNOW = sa.func.now()


def _pk_bigint() -> sa.types.TypeEngine:
    return sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def upgrade() -> None:
    op.create_table(
        "experiments",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("created_by", sa.String(64), nullable=False),
        sa.Column("tags", JSONB, nullable=True),
        sa.Column("detail", JSONB, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=UTCNOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=UTCNOW, nullable=False),
    )
    op.create_index("ix_experiments_name", "experiments", ["name"])
    op.create_index("ix_experiments_status", "experiments", ["status"])

    op.create_table(
        "experiment_manifests",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "experiment_id",
            sa.Uuid(),
            sa.ForeignKey("experiments.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("run_type", sa.String(16), nullable=False),
        sa.Column("schema_version", sa.String(16), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("payload", JSONB, nullable=False),
        sa.Column("simulation_model_version", sa.String(32), nullable=False),
        sa.Column("simulation_configuration_hash", sa.String(32), nullable=False),
        sa.Column("strategy_version", sa.String(32), nullable=False),
        sa.Column("risk_model_version", sa.String(32), nullable=False),
        sa.Column("cost_model_version", sa.String(32), nullable=False),
        sa.Column("lifecycle_model_version", sa.String(32), nullable=False),
        sa.Column("fee_schedule_version", sa.String(48), nullable=False),
        sa.Column("execution_assumption_version", sa.String(48), nullable=False),
        sa.Column("replay_ordering_version", sa.String(16), nullable=False),
        sa.Column("replay_clock_version", sa.String(16), nullable=False),
        sa.Column("metrics_version", sa.String(16), nullable=False),
        sa.Column("disclaimer_version", sa.String(16), nullable=False),
        sa.Column("delay_model", sa.String(32), nullable=False),
        sa.Column("random_seed", sa.BigInteger(), nullable=True),
        sa.Column("start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=UTCNOW, nullable=False),
        sa.UniqueConstraint("content_hash", name="uq_experiment_manifests_content_hash"),
    )
    op.create_index(
        "ix_experiment_manifests_experiment_id", "experiment_manifests", ["experiment_id"]
    )
    op.create_index("ix_experiment_manifests_run_type", "experiment_manifests", ["run_type"])
    op.create_index(
        "ix_experiment_manifests_experiment", "experiment_manifests", ["experiment_id", "run_type"]
    )
    for column in (
        "strategy_version",
        "risk_model_version",
        "cost_model_version",
        "lifecycle_model_version",
        "delay_model",
    ):
        op.create_index(
            f"ix_experiment_manifests_{column}", "experiment_manifests", [column]
        )

    op.create_table(
        "simulation_runs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "experiment_id",
            sa.Uuid(),
            sa.ForeignKey("experiments.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "manifest_id",
            sa.Uuid(),
            sa.ForeignKey("experiment_manifests.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("run_type", sa.String(16), nullable=False),
        sa.Column("run_status", sa.String(24), nullable=False),
        sa.Column("dedupe_key", sa.String(32), nullable=False),
        sa.Column("start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("events_replayed", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("simulations_total", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("simulations_completed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("simulations_incomplete", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("simulations_rejected", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("checkpoint", JSONB, nullable=True),
        sa.Column("excluded_intervals", JSONB, nullable=True),
        sa.Column("data_quality_summary", JSONB, nullable=True),
        sa.Column("warnings", JSONB, nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=UTCNOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=UTCNOW, nullable=False),
        sa.UniqueConstraint("dedupe_key", name="uq_simulation_runs_dedupe_key"),
    )
    op.create_index("ix_simulation_runs_experiment_id", "simulation_runs", ["experiment_id"])
    op.create_index("ix_simulation_runs_manifest_id", "simulation_runs", ["manifest_id"])
    op.create_index("ix_simulation_runs_run_type", "simulation_runs", ["run_type"])
    op.create_index("ix_simulation_runs_run_status", "simulation_runs", ["run_status"])
    op.create_index(
        "ix_simulation_runs_type_status", "simulation_runs", ["run_type", "run_status"]
    )
    op.create_index("ix_simulation_runs_window", "simulation_runs", ["start_at", "end_at"])

    op.create_table(
        "backtest_runs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "run_id",
            sa.Uuid(),
            sa.ForeignKey("simulation_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("run_status", sa.String(24), nullable=False),
        sa.Column("replay_ordering_version", sa.String(16), nullable=False),
        sa.Column("replay_clock_version", sa.String(16), nullable=False),
        sa.Column("data_complete", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("missing_channels", JSONB, nullable=True),
        sa.Column("gap_intervals", JSONB, nullable=True),
        sa.Column("events_replayed", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("last_event_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("checkpoint", JSONB, nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=UTCNOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=UTCNOW, nullable=False),
        sa.UniqueConstraint("run_id", name="uq_backtest_runs_run_id"),
    )
    op.create_index("ix_backtest_runs_run_id", "backtest_runs", ["run_id"])
    op.create_index("ix_backtest_runs_status", "backtest_runs", ["run_status"])

    op.create_table(
        "simulated_positions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "run_id",
            sa.Uuid(),
            sa.ForeignKey("simulation_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("lifecycle_signal_id", sa.Uuid(), nullable=False),
        sa.Column("plan_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("symbol", sa.String(64), nullable=False),
        sa.Column("asset_class", sa.String(16), nullable=False),
        sa.Column("candidate_type", sa.String(40), nullable=False),
        sa.Column("direction", sa.String(8), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("exit_reason", sa.String(32), nullable=True),
        sa.Column("delay_model", sa.String(32), nullable=False),
        sa.Column("delay_seconds", RATIO, nullable=True),
        sa.Column("event_reference_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("scheduled_entry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("modelled_entry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("modelled_exit_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("modelled_entry_price", NUMERIC, nullable=True),
        sa.Column("modelled_exit_price", NUMERIC, nullable=True),
        sa.Column("modelled_quantity", NUMERIC, nullable=True),
        sa.Column("gross_result", NUMERIC, nullable=True),
        sa.Column("net_result", NUMERIC, nullable=True),
        sa.Column("gross_r", RATIO, nullable=True),
        sa.Column("net_r", RATIO, nullable=True),
        sa.Column("fees_total", NUMERIC, nullable=True),
        sa.Column("slippage_total", NUMERIC, nullable=True),
        sa.Column("funding_total", NUMERIC, nullable=True),
        sa.Column("duration_seconds", sa.Numeric(18, 3), nullable=True),
        sa.Column("data_completeness", sa.String(16), nullable=False),
        sa.Column("warnings", JSONB, nullable=True),
        sa.Column("payload", JSONB, nullable=True),
        sa.Column("dedupe_key", sa.String(32), nullable=False),
        sa.Column("disclaimer_version", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=UTCNOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=UTCNOW, nullable=False),
        sa.UniqueConstraint("dedupe_key", name="uq_simulated_positions_dedupe_key"),
    )
    op.create_index("ix_simulated_positions_run_id", "simulated_positions", ["run_id"])
    op.create_index(
        "ix_simulated_positions_lifecycle_signal_id", "simulated_positions", ["lifecycle_signal_id"]
    )
    op.create_index("ix_simulated_positions_plan_id", "simulated_positions", ["plan_id"])
    op.create_index("ix_simulated_positions_candidate_id", "simulated_positions", ["candidate_id"])
    op.create_index("ix_simulated_positions_state", "simulated_positions", ["state"])
    op.create_index("ix_simulated_positions_exit_reason", "simulated_positions", ["exit_reason"])
    op.create_index("ix_simulated_positions_delay_model", "simulated_positions", ["delay_model"])
    op.create_index(
        "ix_simulated_positions_data_completeness", "simulated_positions", ["data_completeness"]
    )
    op.create_index(
        "ix_simulated_positions_run_state", "simulated_positions", ["run_id", "state"]
    )
    op.create_index("ix_simulated_positions_symbol", "simulated_positions", ["symbol"])

    op.create_table(
        "simulated_executions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "simulated_position_id",
            sa.Uuid(),
            sa.ForeignKey("simulated_positions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("leg", sa.String(32), nullable=False),
        sa.Column("side_consumed", sa.String(8), nullable=False),
        sa.Column("modelled_price", NUMERIC, nullable=False),
        sa.Column("reference_price", NUMERIC, nullable=False),
        sa.Column("quantity", NUMERIC, nullable=False),
        sa.Column("notional", NUMERIC, nullable=False),
        sa.Column("slippage_cost", NUMERIC, nullable=False),
        sa.Column("slippage_bps", RATIO, nullable=False),
        sa.Column("fee_cost", NUMERIC, nullable=False),
        sa.Column("levels_consumed", sa.Integer(), nullable=False),
        sa.Column("book_snapshot_id", sa.Uuid(), nullable=True),
        sa.Column("book_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("assumptions", JSONB, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=UTCNOW, nullable=False),
    )
    op.create_index(
        "ix_simulated_executions_simulated_position_id",
        "simulated_executions",
        ["simulated_position_id"],
    )
    op.create_index("ix_simulated_executions_run_id", "simulated_executions", ["run_id"])
    op.create_index(
        "ix_simulated_executions_position_leg",
        "simulated_executions",
        ["simulated_position_id", "leg"],
    )

    op.create_table(
        "simulation_events",
        sa.Column("id", _pk_bigint(), primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("simulated_position_id", sa.Uuid(), nullable=True),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("idempotency_key", sa.String(64), nullable=False),
        sa.Column("detail", JSONB, nullable=True),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=UTCNOW, nullable=False),
        sa.UniqueConstraint("idempotency_key", name="uq_simulation_events_idempotency_key"),
    )
    op.create_index("ix_simulation_events_run_id", "simulation_events", ["run_id"])
    op.create_index(
        "ix_simulation_events_simulated_position_id",
        "simulation_events",
        ["simulated_position_id"],
    )
    op.create_index(
        "ix_simulation_events_run_type", "simulation_events", ["run_id", "event_type"]
    )

    op.create_table(
        "simulation_rejections",
        sa.Column("id", _pk_bigint(), primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.Column("lifecycle_signal_id", sa.Uuid(), nullable=True),
        sa.Column("plan_id", sa.Uuid(), nullable=True),
        sa.Column("candidate_id", sa.Uuid(), nullable=True),
        sa.Column("context_key", sa.String(48), nullable=False),
        sa.Column("symbol", sa.String(64), nullable=False),
        sa.Column("primary_code", sa.String(48), nullable=False),
        sa.Column("codes", JSONB, nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("first_as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_bucket", sa.String(24), nullable=False),
        sa.Column("simulation_model_version", sa.String(32), nullable=False),
        sa.Column("simulation_config_hash", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=UTCNOW, nullable=False),
        sa.UniqueConstraint(
            "context_key",
            "primary_code",
            "window_bucket",
            "simulation_model_version",
            name="uq_simulation_rejections_identity",
        ),
    )
    op.create_index("ix_simulation_rejections_run_id", "simulation_rejections", ["run_id"])
    op.create_index(
        "ix_simulation_rejections_lifecycle_signal_id",
        "simulation_rejections",
        ["lifecycle_signal_id"],
    )
    op.create_index("ix_simulation_rejections_code", "simulation_rejections", ["primary_code"])

    op.create_table(
        "walk_forward_splits",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "run_id",
            sa.Uuid(),
            sa.ForeignKey("simulation_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("split_index", sa.Integer(), nullable=False),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("train_start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("train_end_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("validation_start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("validation_end_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("test_start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("test_end_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("selected_configuration", sa.String(64), nullable=True),
        sa.Column("sample_status", sa.String(24), nullable=False),
        sa.Column("selection_rationale", sa.Text(), nullable=True),
        sa.Column("candidates", JSONB, nullable=True),
        sa.Column("scores", JSONB, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=UTCNOW, nullable=False),
        sa.UniqueConstraint("run_id", "split_index", name="uq_walk_forward_splits_identity"),
    )
    op.create_index("ix_walk_forward_splits_run_id", "walk_forward_splits", ["run_id"])

    op.create_table(
        "performance_metric_sets",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "run_id",
            sa.Uuid(),
            sa.ForeignKey("simulation_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("set_key", sa.String(32), nullable=False),
        sa.Column("segment_kind", sa.String(32), nullable=False),
        sa.Column("segment_key", sa.String(64), nullable=False),
        sa.Column("sample_status", sa.String(24), nullable=False),
        sa.Column("complete_simulations", sa.Integer(), nullable=False),
        sa.Column("metrics_version", sa.String(16), nullable=False),
        sa.Column("disclaimer_version", sa.String(16), nullable=False),
        sa.Column("warnings", JSONB, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=UTCNOW, nullable=False),
        sa.UniqueConstraint("set_key", name="uq_performance_metric_sets_key"),
    )
    op.create_index("ix_performance_metric_sets_run_id", "performance_metric_sets", ["run_id"])
    op.create_index(
        "ix_performance_metric_sets_sample_status", "performance_metric_sets", ["sample_status"]
    )
    op.create_index(
        "ix_performance_metric_sets_run_segment",
        "performance_metric_sets",
        ["run_id", "segment_kind"],
    )

    op.create_table(
        "performance_metric_values",
        sa.Column("id", _pk_bigint(), primary_key=True, autoincrement=True),
        sa.Column(
            "metric_set_id",
            sa.Uuid(),
            sa.ForeignKey("performance_metric_sets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("value", sa.Numeric(38, 12), nullable=True),
        sa.Column("unit", sa.String(24), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.UniqueConstraint("metric_set_id", "name", name="uq_performance_metric_values_name"),
    )
    op.create_index(
        "ix_performance_metric_values_metric_set_id",
        "performance_metric_values",
        ["metric_set_id"],
    )

    op.create_table(
        "simulation_data_quality_summaries",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "run_id",
            sa.Uuid(),
            sa.ForeignKey("simulation_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("symbol", sa.String(64), nullable=False),
        sa.Column("checked_channels", JSONB, nullable=True),
        sa.Column("missing_channels", JSONB, nullable=True),
        sa.Column("gap_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("excluded_seconds", sa.Numeric(18, 3), nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=UTCNOW, nullable=False),
        sa.UniqueConstraint("run_id", "symbol", name="uq_simulation_dq_summary_identity"),
    )
    op.create_index(
        "ix_simulation_data_quality_summaries_run_id",
        "simulation_data_quality_summaries",
        ["run_id"],
    )


def downgrade() -> None:
    op.drop_table("simulation_data_quality_summaries")
    op.drop_table("performance_metric_values")
    op.drop_table("performance_metric_sets")
    op.drop_table("walk_forward_splits")
    op.drop_table("simulation_rejections")
    op.drop_table("simulation_events")
    op.drop_table("simulated_executions")
    op.drop_table("simulated_positions")
    op.drop_table("backtest_runs")
    op.drop_table("simulation_runs")
    op.drop_table("experiment_manifests")
    op.drop_table("experiments")
