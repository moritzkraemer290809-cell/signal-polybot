"""Minimal local dashboard API (JSON summary; extended in later phases)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from app.observability.metrics import metrics

router = APIRouter()


@router.get("/dashboard")
async def dashboard(request: Request) -> dict[str, Any]:
    ctx = request.app.state.ctx
    data_quality = getattr(ctx, "data_quality", None)
    enabled_markets: list[str] | None
    open_signals: int | None
    try:
        enabled_markets = [row.symbol for row in await ctx.instrument_repo.list_enabled()]
        open_signals = await ctx.signal_repo.count_open()
    except Exception:
        # database outage degrades the dashboard, never crashes it
        enabled_markets = None
        open_signals = None
    telegram_subsystem = getattr(ctx, "telegram", None)
    deliveries: list[dict[str, Any]] | None = None
    if telegram_subsystem is not None and telegram_subsystem.repository is not None:
        try:
            rows = await telegram_subsystem.repository.recent(limit=50)
            # deliberately no chat_id, no payload, no user ids
            deliveries = [
                {
                    "delivery_id": str(row.delivery_id),
                    "type": row.message_type,
                    "operation": row.operation,
                    "status": row.status,
                    "priority": row.priority,
                    "attempt_count": row.attempt_count,
                    "created_at": row.created_at.isoformat() if row.created_at else None,
                    "scheduled_at": row.scheduled_at.isoformat() if row.scheduled_at else None,
                    "sent_at": row.sent_at.isoformat() if row.sent_at else None,
                    "error_class": row.last_error_class,
                }
                for row in rows
            ]
        except Exception:
            deliveries = None
    selection = getattr(ctx, "selection", None)
    selection_dashboard: dict[str, Any] | None = None
    if selection is not None:
        selection_dashboard = await selection.dashboard_details()
    strategy = getattr(ctx, "strategy", None)
    strategy_dashboard: dict[str, Any] | None = None
    if strategy is not None:
        strategy_dashboard = await strategy.dashboard_details()
    return {
        "enabled_markets": enabled_markets,
        "open_signals": open_signals,
        "data_quality": data_quality.summary() if data_quality is not None else None,
        "telegram_deliveries": deliveries,
        "market_selection": selection_dashboard,
        "strategy": strategy_dashboard,
        "metrics": metrics.snapshot(),
    }
