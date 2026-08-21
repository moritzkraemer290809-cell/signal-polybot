"""Formatter: escaping, truncation, safe content, timezone rendering."""

from __future__ import annotations

from datetime import UTC, datetime

from app.domain.enums import TelegramDeliveryType
from app.telegram.formatter import MAX_MESSAGE_LENGTH, TelegramFormatter, escape, truncate
from app.telegram.models import (
    DailyStatusPayload,
    SignalPayload,
    SystemMessagePayload,
    WatchlistPayload,
)

formatter = TelegramFormatter("Europe/Berlin")


def test_escape_neutralises_html() -> None:
    assert escape("<b>x</b> & <script>") == "&lt;b&gt;x&lt;/b&gt; &amp; &lt;script&gt;"


def test_system_message_escapes_all_values() -> None:
    payload = SystemMessagePayload(
        title="<injected>", fields={"Feld <1>": "Wert & mehr"}, note="<i>note</i>"
    )
    text = formatter.render(TelegramDeliveryType.SYSTEM_WARNING, payload)
    assert "<injected>" not in text
    assert "&lt;injected&gt;" in text
    assert "Feld &lt;1&gt;: Wert &amp; mehr" in text
    assert "&lt;i&gt;note&lt;/i&gt;" in text
    assert text.startswith("🟠 <b>Systemwarnung</b>")


def test_truncation_respects_limit() -> None:
    long_note = "x" * 10_000
    payload = SystemMessagePayload(title="t", fields={}, note=long_note)
    text = formatter.render(TelegramDeliveryType.SYSTEM_ERROR, payload)
    assert len(text) <= MAX_MESSAGE_LENGTH
    assert text.endswith("(gekürzt)")
    assert truncate("short") == "short"


def test_startup_message_shape() -> None:
    payload = SystemMessagePayload(
        title="",
        fields={
            "Modus": "Read-only Market Data",
            "Trading": "deaktiviert",
            "Aktive Märkte": "2",
        },
    )
    text = formatter.render(TelegramDeliveryType.SYSTEM_STARTUP, payload)
    assert "PolySignal Intelligence gestartet" in text
    assert "Trading: deaktiviert" in text


def test_daily_status_contains_no_performance_claims() -> None:
    payload = DailyStatusPayload(
        uptime_seconds=7_260,
        ws_reconnects=3,
        invalid_events=7,
        quality_counts={"HEALTHY": 2},
        delivery_counts={"SENT": 5},
    )
    text = formatter.render(TelegramDeliveryType.DAILY_STATUS, payload)
    assert "Betriebszeit: 2h 01m" in text
    assert "keine Performance-Aussage" in text


def test_generic_signal_template_renders_provided_fields_only() -> None:
    # clearly-marked local TEST data; phase 6 never computes these values
    payload = SignalPayload(
        symbol="TEST-PERP",
        direction="LONG",
        signal_id="TEST-L-00000000-0000-00",
        status="WATCHING",
        entry="100.00-100.50 (Testdaten)",
        stop="99.00 (Testdaten)",
        reason="Nur Formatierungstest.",
    )
    text = formatter.render(TelegramDeliveryType.SIGNAL_OPEN, payload)
    assert "🟢 <b>TEST-PERP · LONG</b>" in text
    assert "Entry: 100.00-100.50 (Testdaten)" in text
    assert "TP1" not in text  # omitted fields do not render
    assert "Netto-CRV" not in text


def test_watchlist_render() -> None:
    text = formatter.render(
        TelegramDeliveryType.WATCHLIST,
        WatchlistPayload(symbol="TEST-PERP", note="Beobachtung (Testdaten)"),
    )
    assert "Watchlist" in text and "TEST-PERP" in text


def test_no_secret_material_in_any_template() -> None:
    for delivery_type in (
        TelegramDeliveryType.SYSTEM_STARTUP,
        TelegramDeliveryType.SYSTEM_ERROR,
        TelegramDeliveryType.DATA_STALE,
        TelegramDeliveryType.WEBSOCKET_DEGRADED,
        TelegramDeliveryType.BOT_PAUSED,
    ):
        text = formatter.render(
            delivery_type, SystemMessagePayload(title="Hinweis", fields={"Status": "ok"})
        )
        for needle in ("token", "postgres://", "redis://", "http", "polysignal:"):
            assert needle not in text.lower()


def test_time_formatting_uses_display_timezone() -> None:
    ts = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
    rendered = formatter.format_time(ts)
    assert "14:00" in rendered  # Europe/Berlin is UTC+2 in August
    assert TelegramFormatter("Invalid/Zone").format_time(ts)  # falls back to UTC
