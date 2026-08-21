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


class FakeWsClient:
    def __init__(self) -> None:
        from app.domain.enums import WsConnectionState

        self.state = WsConnectionState.CONNECTED
        self.reconnect_count = 2
        self.messages_received = 1234
        self.dropped_frames = 0
        self.last_message_at = datetime(2026, 8, 21, 12, 0, 5, tzinfo=UTC)
        self.connected_since = datetime(2026, 8, 21, 11, 0, tzinfo=UTC)
        self.active_subscription_count = 12
        self.desired_subscription_count = 12

    @property
    def is_connected(self) -> bool:
        from app.domain.enums import WsConnectionState

        return self.state is WsConnectionState.CONNECTED


class FakeDataQuality:
    def summary(self):
        return {
            "instruments": {
                "BTC-PERP": {
                    "status": "HEALTHY",
                    "channels": {
                        "ticker": "FRESH",
                        "bbo": "FRESH",
                        "orderbook": "FRESH",
                        "trades": "AGING",
                        "klines": "FRESH",
                    },
                    "invalid_events_in_window": 0,
                    "resync_requests": 1,
                    "last_event_at": None,
                    "reasons": [],
                }
            },
            "status_counts": {"HEALTHY": 1},
            "stale_count": 0,
        }


class FakeMarketData:
    buffers = None

    def trackers(self):
        return [SimpleNamespace(symbol="BTC-PERP")]

    def total_invalid_events(self):
        return 3

    def total_resync_requests(self):
        return 1


def make_ws_ctx(**kwargs):
    ctx = make_ctx(**kwargs)
    ctx.ws_client = FakeWsClient()
    ctx.data_quality = FakeDataQuality()
    ctx.market_data = FakeMarketData()
    return ctx


def test_health_includes_websocket_and_freshness() -> None:
    app = create_app(context=make_ws_ctx())
    with TestClient(app) as client:
        body = client.get("/health").json()
    ws = body["components"]["websocket"]
    assert ws["state"] == "CONNECTED"
    assert ws["active_connections"] == 1
    assert ws["active_subscriptions"] == 12
    assert body["data_stale_assets"] == 0
    critical = body["critical_channel_freshness"]["BTC-PERP"]
    assert set(critical) == {"ticker", "bbo", "orderbook"}  # no non-critical leak


def test_health_reports_websocket_disabled_without_client() -> None:
    app = create_app(context=make_ctx())
    with TestClient(app) as client:
        body = client.get("/health").json()
    assert body["components"]["websocket"] == "disabled"


def test_status_includes_connection_and_quality() -> None:
    app = create_app(context=make_ws_ctx())
    with TestClient(app) as client:
        body = client.get("/status").json()
    assert body["websocket"]["reconnects"] == 2
    assert body["websocket"]["state"] == "CONNECTED"
    assert body["market_data"]["invalid_events_total"] == 3
    assert body["market_data"]["orderbook_resync_requests"] == 1
    assert body["data_quality"]["instruments"]["BTC-PERP"]["status"] == "HEALTHY"


def test_status_contains_no_raw_payloads_or_secrets() -> None:
    app = create_app(context=make_ws_ctx())
    with TestClient(app) as client:
        text = client.get("/status").text
    assert "bot_token" not in text
    assert "TELEGRAM" not in text


class FailingRepo:
    async def list_all(self):
        raise ConnectionError("db down")

    async def list_enabled(self):
        raise ConnectionError("db down")

    async def count_open(self):
        raise ConnectionError("db down")


def test_status_survives_database_outage() -> None:
    ctx = make_ctx()
    ctx.instrument_repo = FailingRepo()
    ctx.signal_repo = FailingRepo()
    app = create_app(context=ctx)
    with TestClient(app) as client:
        response = client.get("/status")
    assert response.status_code == 200
    body = response.json()
    assert body["database"] == "unavailable"
    assert body["universe"] is None
    assert body["open_signals"] is None


def test_dashboard_survives_database_outage() -> None:
    ctx = make_ctx()
    ctx.instrument_repo = FailingRepo()
    ctx.signal_repo = FailingRepo()
    app = create_app(context=ctx)
    with TestClient(app) as client:
        response = client.get("/dashboard")
    assert response.status_code == 200
    assert response.json()["enabled_markets"] is None
