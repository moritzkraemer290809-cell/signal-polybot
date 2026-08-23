"""Health endpoint: process, PostgreSQL, Redis, Telegram config, WebSocket,
subscriptions and data freshness. No secrets, no raw payloads."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Request, Response

router = APIRouter()


@router.get("/health")
async def health(request: Request, response: Response) -> dict[str, Any]:
    ctx = request.app.state.ctx
    db_ok = await ctx.db.ping()
    redis_ok = await ctx.cache.ping()

    telegram_subsystem = getattr(ctx, "telegram", None)
    if telegram_subsystem is not None:
        telegram_status: dict[str, Any] | str = await telegram_subsystem.health_stats()
    elif ctx.settings.telegram.enabled:
        telegram_status = "enabled_not_initialized"
    else:
        telegram_status = "disabled"
    last_refresh = ctx.instrument_service.last_refresh_at

    ws_client = getattr(ctx, "ws_client", None)
    data_quality = getattr(ctx, "data_quality", None)

    if ws_client is None:
        websocket: dict[str, Any] | str = "disabled"
        stale_assets = None
        critical_freshness: dict[str, Any] | None = None
    else:
        websocket = {
            "state": ws_client.state.value,
            "active_connections": 1 if ws_client.is_connected else 0,
            "active_subscriptions": ws_client.active_subscription_count,
            "desired_subscriptions": ws_client.desired_subscription_count,
            "last_message_at": (
                ws_client.last_message_at.isoformat() if ws_client.last_message_at else None
            ),
        }
        stale_assets = None
        critical_freshness = None
        if data_quality is not None:
            summary = data_quality.summary()
            stale_assets = summary["stale_count"]
            critical_freshness = {
                symbol: {
                    channel: value
                    for channel, value in info["channels"].items()
                    if channel in ("ticker", "bbo", "orderbook")
                }
                for symbol, info in summary["instruments"].items()
            }

    selection = getattr(ctx, "selection", None)
    if selection is not None:
        selection_status: dict[str, Any] | str = await selection.health_stats()
    elif ctx.settings.selection.enabled:
        selection_status = "enabled_not_initialized"
    else:
        selection_status = "disabled"

    strategy = getattr(ctx, "strategy", None)
    if strategy is not None:
        strategy_status: dict[str, Any] | str = await strategy.health_stats()
    elif ctx.settings.strategy.enabled:
        strategy_status = "enabled_not_initialized"
    else:
        strategy_status = "disabled"

    risk = getattr(ctx, "risk", None)
    if risk is not None:
        risk_status: dict[str, Any] | str = await risk.health_stats()
    elif ctx.settings.risk.engine_enabled:
        risk_status = "enabled_not_initialized"
    else:
        risk_status = "disabled"

    signals = getattr(ctx, "signals", None)
    if signals is not None:
        signals_status: dict[str, Any] | str = await signals.health_stats()
    elif ctx.settings.signals.lifecycle_enabled:
        signals_status = "enabled_not_initialized"
    else:
        signals_status = "disabled"

    components = {
        "process": "ok",
        "postgres": "ok" if db_ok else "unavailable",
        "redis": "ok" if redis_ok else "unavailable",
        "telegram": telegram_status,
        "websocket": websocket,
        "instrument_discovery": "ok" if last_refresh is not None else "pending",
        "market_selection": selection_status,
        "strategy": strategy_status,
        "risk": risk_status,
        "signals": signals_status,
    }
    healthy = db_ok and redis_ok
    if not healthy:
        response.status_code = 503
    return {
        "status": "ok" if healthy else "degraded",
        "timestamp": datetime.now(tz=UTC).isoformat(),
        "components": components,
        "data_stale_assets": stale_assets,
        "critical_channel_freshness": critical_freshness,
        "last_universe_refresh": last_refresh.isoformat() if last_refresh else None,
    }
