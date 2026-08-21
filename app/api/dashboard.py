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
    return {
        "enabled_markets": enabled_markets,
        "open_signals": open_signals,
        "data_quality": data_quality.summary() if data_quality is not None else None,
        "metrics": metrics.snapshot(),
    }
