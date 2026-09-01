"""Tests for multi-home-channel broadcast support.

Verifies that PlatformConfig + GatewayConfig can carry multiple home
channels per platform (back-compat: singular `home_channel` still works),
and that `get_home_channels()` returns the merged list in stable order.

Context: gateway lifecycle broadcasts (shutdown, restart, state.db
failure) must reach every WhatsApp LID Kyros is reachable on, not just
the singular `home_channel`. This test pins the loading / accessor
behavior so the broadcast loop in `gateway/run.py` can rely on it.
"""

from __future__ import annotations

import pytest

from gateway.config import (
    GatewayConfig,
    HomeChannel,
    Platform,
    PlatformConfig,
)


# ── PlatformConfig: round-trip ──────────────────────────────────────────────


def test_platform_config_singular_home_channel_only():
    """Legacy config with only `home_channel` loads and `home_channels`
    falls back to `[home_channel]`."""
    data = {
        "enabled": True,
        "home_channel": {
            "platform": "whatsapp",
            "chat_id": "5927843410163@lid",
            "name": "legacy",
            "user_id": "5927843410163@lid",
        },
    }
    cfg = PlatformConfig.from_dict(data)
    assert cfg.home_channel is not None
    assert cfg.home_channel.chat_id == "5927843410163@lid"
    # get_home_channels returns the singular wrapped in a list
    assert len(cfg.home_channels) == 1
    assert cfg.home_channels[0].chat_id == "5927843410163@lid"


def test_platform_config_plural_home_channels_only():
    """New-style config with `home_channels` list, no singular field."""
    data = {
        "enabled": True,
        "home_channels": [
            {
                "platform": "whatsapp",
                "chat_id": "5927843410163@lid",
                "name": "primary",
                "user_id": "5927843410163@lid",
            },
            {
                "platform": "whatsapp",
                "chat_id": "188661404582023@lid",
                "name": "secondary",
                "user_id": "188661404582023@lid",
            },
            {
                "platform": "whatsapp",
                "chat_id": "199999480688782@lid",
                "name": "Vivo",
                "user_id": "199999480688782@lid",
            },
        ],
    }
    cfg = PlatformConfig.from_dict(data)
    assert cfg.home_channel is None  # singular is unset
    assert len(cfg.home_channels) == 3
    chat_ids = [hc.chat_id for hc in cfg.home_channels]
    assert chat_ids == [
        "5927843410163@lid",
        "188661404582023@lid",
        "199999480688782@lid",
    ]


def test_platform_config_singular_and_plural_dedup_singular_first():
    """When both `home_channel` and `home_channels` are set, the singular
    appears FIRST in the list (back-compat ordering for callers that
    expect the primary home to lead) and duplicates are dropped."""
    data = {
        "enabled": True,
        "home_channel": {
            "platform": "whatsapp",
            "chat_id": "5927843410163@lid",
            "name": "primary",
            "user_id": "5927843410163@lid",
        },
        "home_channels": [
            {
                "platform": "whatsapp",
                "chat_id": "5927843410163@lid",  # dup of singular
                "name": "primary-dup",
                "user_id": "5927843410163@lid",
            },
            {
                "platform": "whatsapp",
                "chat_id": "188661404582023@lid",
                "name": "secondary",
                "user_id": "188661404582023@lid",
            },
        ],
    }
    cfg = PlatformConfig.from_dict(data)
    assert len(cfg.home_channels) == 2
    assert cfg.home_channels[0].chat_id == "5927843410163@lid"
    assert cfg.home_channels[0].name == "primary"  # singular wins, not the dup
    assert cfg.home_channels[1].chat_id == "188661404582023@lid"


def test_platform_config_skips_malformed_home_channel_entries():
    """One bad entry shouldn't invalidate the whole list."""
    data = {
        "enabled": True,
        "home_channels": [
            {"platform": "whatsapp", "chat_id": "A@lid", "name": "ok"},
            {"platform": "whatsapp", "chat_id": "B@lid"},  # missing 'name'
            {"this is": "not a home channel at all"},
            {"platform": "whatsapp", "chat_id": "C@lid", "name": "ok2"},
        ],
    }
    cfg = PlatformConfig.from_dict(data)
    chat_ids = [hc.chat_id for hc in cfg.home_channels]
    # `B@lid` has no name (HomeChannel.from_dict defaults name="Home")
    # so it WILL load — the test asserts the loader doesn't crash.
    assert "A@lid" in chat_ids
    assert "C@lid" in chat_ids


def test_platform_config_round_trip_to_dict_includes_home_channels():
    """to_dict should round-trip both singular and plural."""
    cfg = PlatformConfig(
        enabled=True,
        home_channel=HomeChannel(platform=Platform.WHATSAPP, chat_id="A@lid", name="A"),
        home_channels=[
            HomeChannel(platform=Platform.WHATSAPP, chat_id="A@lid", name="A"),
            HomeChannel(platform=Platform.WHATSAPP, chat_id="B@lid", name="B"),
        ],
    )
    out = cfg.to_dict()
    assert out["home_channel"]["chat_id"] == "A@lid"
    # home_channels is dedup'd to_dict side, so we should see A and B
    ids = {h["chat_id"] for h in out["home_channels"]}
    assert ids == {"A@lid", "B@lid"}


# ── GatewayConfig.get_home_channels ─────────────────────────────────────────


def _gw(platform: Platform, *home_channels: HomeChannel) -> GatewayConfig:
    """Tiny helper to build a GatewayConfig with one platform."""
    pcfg = PlatformConfig(enabled=True, home_channels=list(home_channels))
    gw = GatewayConfig.__new__(GatewayConfig)
    gw.platforms = {platform: pcfg}
    return gw


def test_get_home_channel_returns_first_when_set():
    gw = _gw(
        Platform.WHATSAPP,
        HomeChannel(platform=Platform.WHATSAPP, chat_id="A@lid", name="A"),
        HomeChannel(platform=Platform.WHATSAPP, chat_id="B@lid", name="B"),
    )
    assert gw.get_home_channel(Platform.WHATSAPP).chat_id == "A@lid"


def test_get_home_channels_returns_full_list():
    gw = _gw(
        Platform.WHATSAPP,
        HomeChannel(platform=Platform.WHATSAPP, chat_id="A@lid", name="A"),
        HomeChannel(platform=Platform.WHATSAPP, chat_id="B@lid", name="B"),
        HomeChannel(platform=Platform.WHATSAPP, chat_id="C@lid", name="C"),
    )
    result = gw.get_home_channels(Platform.WHATSAPP)
    assert [h.chat_id for h in result] == ["A@lid", "B@lid", "C@lid"]
    # Returned list must be a copy so callers can mutate without affecting
    # the underlying PlatformConfig.home_channels. Replace with a sentinel
    # and verify the underlying list is untouched.
    original_len = len(result)
    result.clear()
    assert len(gw.get_home_channels(Platform.WHATSAPP)) == original_len


def test_get_home_channels_unknown_platform_returns_empty():
    gw = _gw(
        Platform.WHATSAPP,
        HomeChannel(platform=Platform.WHATSAPP, chat_id="A@lid", name="A"),
    )
    assert gw.get_home_channels(Platform.DISCORD) == []


def test_get_home_channel_unknown_platform_returns_none():
    gw = _gw(
        Platform.WHATSAPP,
        HomeChannel(platform=Platform.WHATSAPP, chat_id="A@lid", name="A"),
    )
    assert gw.get_home_channel(Platform.DISCORD) is None


def test_thread_id_is_part_of_dedup_key():
    """Two home channels with the same chat_id but different thread_ids
    are distinct destinations (forum topics). The config loader keeps both."""
    data = {
        "enabled": True,
        "home_channels": [
            {"platform": "discord", "chat_id": "DID", "thread_id": "T1", "name": "topic-1"},
            {"platform": "discord", "chat_id": "DID", "thread_id": "T2", "name": "topic-2"},
        ],
    }
    cfg = PlatformConfig.from_dict(data)
    keys = [(h.chat_id, h.thread_id) for h in cfg.home_channels]
    assert keys == [("DID", "T1"), ("DID", "T2")]
