"""Gateway command help rendering tests."""

import pytest

from gateway.config import Platform
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource


def _make_event(text: str, platform: Platform) -> MessageEvent:
    return MessageEvent(
        text=text,
        source=SessionSource(
            platform=platform,
            chat_id="chat-1",
            user_id="user-1",
            user_name="tester",
            chat_type="dm",
        ),
    )


def _make_runner():
    from gateway.run import GatewayRunner

    return object.__new__(GatewayRunner)


@pytest.mark.asyncio
async def test_help_sanitizes_slash_command_mentions_for_telegram(monkeypatch):
    """Telegram help output must not expose invalid uppercase/hyphenated slashes."""
    monkeypatch.setattr(
        "agent.skill_commands.get_skill_commands",
        lambda: {
            "/Linear": {"description": "Open Linear"},
            "/Custom-Thing": {"description": "Run a custom thing"},
        },
    )

    result = await _make_runner()._handle_help_command(
        _make_event("/help", Platform.TELEGRAM)
    )

    assert "`/linear`" in result
    assert "`/custom_thing`" in result
    assert "`/Linear`" not in result
    assert "`/Custom-Thing`" not in result


@pytest.mark.asyncio
async def test_commands_sanitizes_slash_command_mentions_for_telegram(monkeypatch):
    """Paginated Telegram /commands output uses Telegram-valid slash mentions."""
    monkeypatch.setattr(
        "agent.skill_commands.get_skill_commands",
        lambda: {"/Linear": {"description": "Open Linear"}},
    )

    result = await _make_runner()._handle_commands_command(
        _make_event("/commands 999", Platform.TELEGRAM)
    )

    assert "`/linear`" in result
    assert "`/Linear`" not in result


@pytest.mark.asyncio
async def test_help_filters_for_gated_non_admin_user():
    """Non-admin user only sees allowed commands and always-allowed floor in /help."""
    from gateway.config import GatewayConfig, PlatformConfig

    runner = _make_runner()
    runner.config = GatewayConfig(
        platforms={
            Platform.WHATSAPP: PlatformConfig(
                enabled=True,
                extra={
                    "allow_admin_from": ["admin-1"],
                    "user_allowed_commands": ["status", "new", "reset"],
                },
            )
        }
    )

    event = MessageEvent(
        text="/help",
        source=SessionSource(
            platform=Platform.WHATSAPP,
            chat_id="chat-1",
            user_id="user-1",
            user_name="regular_user",
            chat_type="dm",
        ),
    )

    result = await runner._handle_help_command(event)
    assert "/status" in result
    assert "/new" in result
    assert "/help" in result
    assert "/whoami" in result
    # Admin-only / unlisted commands must NOT appear
    assert "/save" not in result
    assert "/rollback" not in result
    assert "/diff" not in result
    assert "/approvals" not in result


@pytest.mark.asyncio
async def test_help_shows_all_for_admin_user():
    """Admin user sees full command registry in /help."""
    from gateway.config import GatewayConfig, PlatformConfig

    runner = _make_runner()
    runner.config = GatewayConfig(
        platforms={
            Platform.WHATSAPP: PlatformConfig(
                enabled=True,
                extra={
                    "allow_admin_from": ["admin-1"],
                    "user_allowed_commands": ["status"],
                },
            )
        }
    )

    event = MessageEvent(
        text="/help",
        source=SessionSource(
            platform=Platform.WHATSAPP,
            chat_id="chat-1",
            user_id="admin-1",
            user_name="admin_user",
            chat_type="dm",
        ),
    )

    result = await runner._handle_help_command(event)
    assert "/status" in result
    assert "/save" in result
    assert "/rollback" in result
    assert "/approvals" in result


