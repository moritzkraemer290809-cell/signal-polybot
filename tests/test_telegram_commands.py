"""Admin commands: authorization, cooldowns, pause/resume, info commands."""

from __future__ import annotations

from typing import Any

from tests.conftest import FakeClock
from tests.fake_telegram import command_update
from tests.test_telegram_queue import GROUP, make_settings

from app.bot_state import BotStateService
from app.domain.enums import TelegramDeliveryStatus
from app.repositories.app_state_repository import AppStateRepository
from app.repositories.telegram_delivery_repository import TelegramDeliveryRepository
from app.telegram.authorization import TelegramAuthorizer
from app.telegram.command_router import CommandProviders, TelegramCommandRouter, parse_update
from app.telegram.delivery_service import TelegramDeliveryService
from app.telegram.models import DailyStatusPayload

ADMIN = 1
STRANGER = 999


class RouterHarness:
    def __init__(self, session_factory, *, kill_switch: bool = False) -> None:
        self.settings = make_settings(admin_user_ids=[ADMIN])
        self.session_factory = session_factory
        self.repo = TelegramDeliveryRepository(session_factory)
        self.bot_state = BotStateService(
            AppStateRepository(session_factory), kill_switch=kill_switch
        )
        self.delivery = TelegramDeliveryService(self.repo, self.settings)
        self.audits: list[tuple[str, dict[str, Any]]] = []
        self.clock = FakeClock()

        async def audit(event_type: str, message: str, context: dict[str, Any]) -> None:
            self.audits.append((event_type, context))

        async def status_snapshot() -> dict[str, Any]:
            return {"Bot": "ACTIVE", "Aktive Instrumente": 2}

        async def health_snapshot() -> dict[str, Any]:
            return {"overall": "HEALTHY", "Datenbank": "ok"}

        async def daily_snapshot() -> DailyStatusPayload:
            return DailyStatusPayload(
                uptime_seconds=3600,
                ws_reconnects=1,
                invalid_events=2,
                quality_counts={"HEALTHY": 2},
                delivery_counts={"SENT": 3},
            )

        self.router = TelegramCommandRouter(
            self.settings,
            TelegramAuthorizer(self.settings, audit=audit),
            self.bot_state,
            self.delivery,
            CommandProviders(status_snapshot, health_snapshot, daily_snapshot),
            bot_username="polysignal_test_bot",
            audit=audit,
            clock=self.clock,
        )

    async def send(self, text: str, user_id: int = ADMIN, **kwargs):
        kwargs.setdefault("chat_id", GROUP)
        return await self.router.handle_update(command_update(text, user_id=user_id, **kwargs))


async def test_status_command_replies(session_factory) -> None:
    harness = RouterHarness(session_factory)
    chat_id, reply = await harness.send("/status")
    assert chat_id == GROUP
    assert "<b>Status</b>" in reply and "Aktive Instrumente: 2" in reply


async def test_health_command_replies(session_factory) -> None:
    harness = RouterHarness(session_factory)
    _, reply = await harness.send("/health", chat_id=ADMIN, chat_type="private")
    assert "Health: HEALTHY" in reply


async def test_open_and_watchlist_are_explicit_placeholders(session_factory) -> None:
    harness = RouterHarness(session_factory)
    harness.clock.advance(100)
    _, open_reply = await harness.send("/open")
    assert open_reply == (
        "Keine offenen Signale. Signal-Lifecycle wird in einer späteren Phase aktiviert."
    )
    _, watchlist_reply = await harness.send("/watchlist")
    assert watchlist_reply == (
        "Keine Watchlist-Signale verfügbar. Marktselektion und Strategie folgen in späteren Phasen."
    )


async def test_daily_command_reports_technical_status(session_factory) -> None:
    harness = RouterHarness(session_factory)
    _, reply = await harness.send("/daily")
    assert "Betriebszeit: 1h 00m" in reply
    assert "keine Performance- oder Trading-Aussage" in reply


async def test_pause_resume_flow_with_confirmation_deliveries(session_factory) -> None:
    harness = RouterHarness(session_factory)
    _, reply = await harness.send("/pause")
    assert reply == "⏸ Bot pausiert."
    assert await harness.bot_state.is_paused() is True
    counts = await harness.repo.counts_by_status()
    assert counts.get(TelegramDeliveryStatus.PENDING.value) == 1

    # repeated pause is idempotent: no error, no second delivery
    _, reply = await harness.send("/pause")
    assert reply == "Bot ist bereits pausiert."
    counts = await harness.repo.counts_by_status()
    assert counts.get(TelegramDeliveryStatus.PENDING.value) == 1

    _, reply = await harness.send("/resume")
    assert reply == "▶️ Bot fortgesetzt."
    assert await harness.bot_state.is_paused() is False
    _, reply = await harness.send("/resume")
    assert reply == "Bot ist bereits aktiv."

    audit_types = [event_type for event_type, _ in harness.audits]
    assert audit_types.count("telegram_admin_pause") == 2
    assert audit_types.count("telegram_admin_resume") == 2


async def test_pause_state_survives_restart(session_factory) -> None:
    harness = RouterHarness(session_factory)
    await harness.send("/pause")
    # "restart": fresh BotStateService over the same database
    restarted = BotStateService(AppStateRepository(session_factory), kill_switch=False)
    await restarted.load()
    assert await restarted.is_paused() is True


async def test_unauthorized_user_gets_no_reply_and_changes_nothing(session_factory) -> None:
    harness = RouterHarness(session_factory)
    result = await harness.send("/pause", user_id=STRANGER)
    assert result is None
    assert await harness.bot_state.is_paused() is False
    assert (
        "telegram_unauthorized_command",
        {
            "user_id": STRANGER,
            "chat_type": "supergroup",
            "command": "pause",
        },
    ) in harness.audits
    assert await harness.repo.open_count() == 0


async def test_admin_in_foreign_group_is_rejected(session_factory) -> None:
    harness = RouterHarness(session_factory)
    result = await harness.send("/pause", chat_id=-42, chat_type="supergroup")
    assert result is None
    assert await harness.bot_state.is_paused() is False


async def test_command_with_bot_suffix(session_factory) -> None:
    harness = RouterHarness(session_factory)
    result = await harness.send("/status@polysignal_test_bot")
    assert result is not None
    # addressed to another bot: ignored
    assert await harness.send("/status@other_bot") is None


async def test_cooldown_applies_to_read_commands_only(session_factory) -> None:
    harness = RouterHarness(session_factory)
    assert await harness.send("/status") is not None
    assert await harness.send("/status") is None  # within cooldown
    harness.clock.advance(20)
    assert await harness.send("/status") is not None
    # control commands have no cooldown
    assert await harness.send("/pause") is not None
    assert await harness.send("/resume") is not None


async def test_unknown_command_is_ignored(session_factory) -> None:
    harness = RouterHarness(session_factory)
    assert await harness.send("/doesnotexist") is None


async def test_kill_switch_keeps_bot_paused(session_factory) -> None:
    harness = RouterHarness(session_factory, kill_switch=True)
    assert await harness.bot_state.is_paused() is True
    # resume clears only the runtime pause; the env kill switch still holds
    await harness.send("/resume")
    assert await harness.bot_state.is_paused() is True


def test_parse_update_edge_cases() -> None:
    assert parse_update({}, None) is None
    assert parse_update({"message": {"text": "hello"}}, None) is None
    parsed = parse_update(command_update("/Status@PolySignal_Test_Bot"), "polysignal_test_bot")
    assert parsed is not None and parsed.command == "status"
