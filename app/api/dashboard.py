"""Minimal local dashboard API (JSON summary; extended in later phases)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from app.observability.metrics import metrics
from app.simulation.explainability import BACKTEST_DISCLAIMER, SIMULATION_DISCLAIMER

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
    risk = getattr(ctx, "risk", None)
    risk_dashboard: dict[str, Any] | None = None
    if risk is not None:
        risk_dashboard = await risk.dashboard_details()
    signals = getattr(ctx, "signals", None)
    signals_dashboard: dict[str, Any] | None = None
    if signals is not None:
        signals_dashboard = await signals.dashboard_details()
    shadow = getattr(ctx, "shadow", None)
    shadow_dashboard: dict[str, Any] | None = None
    if shadow is not None:
        shadow_dashboard = await shadow.dashboard_details()
    backtest = getattr(ctx, "backtest", None)
    backtest_dashboard: dict[str, Any] | None = None
    if backtest is not None:
        backtest_dashboard = await backtest.dashboard_details()
    simulation_active = shadow_dashboard is not None or backtest_dashboard is not None
    return {
        # phase-11 disclaimers stay at the very top of the payload
        "simulation_disclaimers": (
            [SIMULATION_DISCLAIMER, BACKTEST_DISCLAIMER] if simulation_active else []
        ),
        "enabled_markets": enabled_markets,
        "open_signals": open_signals,
        "data_quality": data_quality.summary() if data_quality is not None else None,
        "telegram_deliveries": deliveries,
        "market_selection": selection_dashboard,
        "strategy": strategy_dashboard,
        "risk": risk_dashboard,
        "signals": signals_dashboard,
        "shadow_simulation": shadow_dashboard,
        "backtest": backtest_dashboard,
        "metrics": metrics.snapshot(),
    }
