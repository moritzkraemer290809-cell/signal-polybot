"""Settings: defaults, env var names, missing secrets, list parsing."""

from __future__ import annotations

from tests.conftest import make_settings

from app.config import (
    PolymarketSettings,
    RedisSettings,
    TelegramSettings,
    UniverseSettings,
)


def test_defaults_are_safe() -> None:
    settings = make_settings()
    assert settings.app.kill_switch is False
    assert settings.app.environment == "local"
    assert settings.redis.key_prefix == "polysignal:"
    assert settings.universe.equity_symbols == ["AAPL-PERP"]
    assert settings.universe.crypto_symbols == ["BTC-PERP"]
    assert settings.polymarket.rate_limit_budget_per_minute <= 1000


def test_telegram_env_variable_names(monkeypatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("TELEGRAM_GROUP_ID", "-1000123")
    monkeypatch.setenv("TELEGRAM_ADMIN_USER_IDS", "11, 22")
    tg = TelegramSettings(_env_file=None)
    assert tg.configured is True
    assert tg.group_id == -1000123
    assert tg.admin_user_ids == [11, 22]
    assert tg.is_admin(11) and tg.is_admin(22)
    assert not tg.is_admin(33)


def test_telegram_missing_secrets_is_not_configured(monkeypatch) -> None:
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_GROUP_ID", raising=False)
    monkeypatch.setenv("TELEGRAM_ADMIN_USER_IDS", "")
    tg = TelegramSettings(_env_file=None)
    assert tg.configured is False
    assert tg.admin_user_ids == []


def test_telegram_token_not_leaked_in_repr(monkeypatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "super-secret-token")
    tg = TelegramSettings(_env_file=None)
    assert "super-secret-token" not in repr(tg)
    assert "super-secret-token" not in str(tg)


def test_universe_csv_and_json_parsing(monkeypatch) -> None:
    monkeypatch.setenv("UNIVERSE_EQUITY_SYMBOLS", "AAPL-PERP, MSFT-PERP")
    monkeypatch.setenv("UNIVERSE_CRYPTO_SYMBOLS", '["BTC-PERP","ETH-PERP"]')
    universe = UniverseSettings(_env_file=None)
    assert universe.equity_symbols == ["AAPL-PERP", "MSFT-PERP"]
    assert universe.crypto_symbols == ["BTC-PERP", "ETH-PERP"]
    assert universe.all_symbols == {"AAPL-PERP", "MSFT-PERP", "BTC-PERP", "ETH-PERP"}


def test_polymarket_book_weight_mapping() -> None:
    weights = PolymarketSettings(_env_file=None).endpoint_weights
    assert weights.for_book_depth(10) == 2
    assert weights.for_book_depth(100) == 5
    assert weights.for_book_depth(500) == 10
    assert weights.for_book_depth(1000) == 20


def test_redis_prefix_override(monkeypatch) -> None:
    monkeypatch.setenv("REDIS_KEY_PREFIX", "polysignal:")
    assert RedisSettings(_env_file=None).key_prefix == "polysignal:"


def test_ws_settings_env_variable_names(monkeypatch) -> None:
    from app.config import PolymarketWsSettings

    monkeypatch.setenv("POLYMARKET_PERPS_WS_URL", "wss://example.test/v1/ws")
    monkeypatch.setenv("POLYMARKET_WS_ENABLED", "false")
    monkeypatch.setenv("POLYMARKET_WS_RECONNECT_MAX_SECONDS", "45")
    monkeypatch.setenv("POLYMARKET_WS_MAX_SUBSCRIPTIONS", "50")
    monkeypatch.setenv("POLYMARKET_WS_EVENT_QUEUE_SIZE", "500")
    monkeypatch.setenv("POLYMARKET_WS_PERSIST_BATCH_SIZE", "99")
    monkeypatch.setenv("POLYMARKET_WS_KLINE_TIMEFRAMES", "1m,5m")
    ws = PolymarketWsSettings(_env_file=None)
    assert ws.url == "wss://example.test/v1/ws"
    assert ws.enabled is False
    assert ws.reconnect_max_seconds == 45
    assert ws.max_subscriptions == 50
    assert ws.event_queue_size == 500
    assert ws.persist_batch_size == 99
    assert ws.kline_timeframes == ["1m", "5m"]


def test_freshness_settings_env_variable_names(monkeypatch) -> None:
    from app.config import DataFreshnessSettings

    monkeypatch.setenv("DATA_FRESHNESS_TICKER_SECONDS", "7")
    monkeypatch.setenv("DATA_FRESHNESS_BBO_SECONDS", "5")
    monkeypatch.setenv("DATA_FRESHNESS_ORDERBOOK_SECONDS", "9")
    monkeypatch.setenv("DATA_FRESHNESS_TRADES_SECONDS", "60")
    monkeypatch.setenv("DATA_FRESHNESS_CANDLES_SECONDS", "120")
    freshness = DataFreshnessSettings(_env_file=None)
    assert freshness.ticker_seconds == 7
    assert freshness.bbo_seconds == 5
    assert freshness.orderbook_seconds == 9
    assert freshness.trades_seconds == 60
    assert freshness.candles_seconds == 120


def test_data_quality_outlier_env_alias(monkeypatch) -> None:
    from app.config import DataQualitySettings

    monkeypatch.setenv("DATA_OUTLIER_MAX_DEVIATION_BPS", "750")
    monkeypatch.setenv("DATA_QUALITY_INVALID_EVENT_THRESHOLD", "5")
    quality = DataQualitySettings(_env_file=None)
    assert quality.outlier_max_deviation_bps == 750
    assert quality.invalid_event_threshold == 5
