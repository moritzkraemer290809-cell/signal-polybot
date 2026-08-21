"""Health and status endpoints with a fully faked application context."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from fastapi.testclient import TestClient
from tests.conftest import make_settings

from app.main import create_app


class FakePingable:
    def __init__(self, ok: bool = True) -> None:
        self.ok = ok

    async def ping(self) -> bool:
        return self.ok


class FakeInstrumentRepo:
    def __init__(self, rows: list | None = None) -> None:
        self.rows = rows or []

    async def list_all(self) -> list:
        return self.rows

    async def list_enabled(self) -> list:
        return [row for row in self.rows if row.enabled]


class FakeSignalRepo:
    async def count_open(self) -> int:
        return 0


def make_ctx(*, db_ok: bool = True, redis_ok: bool = True, kill_switch: bool = False):
    settings = make_settings()
    settings.app.kill_switch = kill_switch
    instrument_row = SimpleNamespace(
        symbol="BTC-PERP",
        instrument_id=1,
        asset_class="CRYPTO",
        status="ACTIVE",
        enabled=True,
        max_leverage=10,
        last_seen_at=datetime(2026, 8, 21, 12, 0, tzinfo=UTC),
    )
    return SimpleNamespace(
        settings=settings,
        db=FakePingable(db_ok),
        cache=FakePingable(redis_ok),
        instrument_repo=FakeInstrumentRepo([instrument_row]),
        signal_repo=FakeSignalRepo(),
        instrument_service=SimpleNamespace(last_refresh_at=datetime(2026, 8, 21, tzinfo=UTC)),
        started_at=datetime(2026, 8, 21, tzinfo=UTC),
    )


def test_health_ok() -> None:
    app = create_app(context=make_ctx())
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["components"]["postgres"] == "ok"
    assert body["components"]["redis"] == "ok"
    assert body["components"]["telegram"] == "not_configured"


def test_health_degraded_when_database_down() -> None:
    app = create_app(context=make_ctx(db_ok=False))
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 503
    assert response.json()["components"]["postgres"] == "unavailable"


def test_status_reports_universe_and_bot_state() -> None:
    app = create_app(context=make_ctx())
    with TestClient(app) as client:
        response = client.get("/status")
    body = response.json()
    assert response.status_code == 200
    assert body["bot_state"] == "RUNNING"
    assert body["open_signals"] == 0
    assert body["universe"][0]["symbol"] == "BTC-PERP"
    assert body["universe"][0]["enabled"] is True


def test_status_kill_switch_pauses_bot() -> None:
    app = create_app(context=make_ctx(kill_switch=True))
    with TestClient(app) as client:
        response = client.get("/status")
    assert response.json()["bot_state"] == "PAUSED"


def test_dashboard_summary() -> None:
    app = create_app(context=make_ctx())
    with TestClient(app) as client:
        response = client.get("/dashboard")
    body = response.json()
    assert body["enabled_markets"] == ["BTC-PERP"]
    assert "metrics" in body
