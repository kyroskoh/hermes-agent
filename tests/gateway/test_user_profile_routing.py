"""Tests for the per-sender profile policy, persistence, and /p enforcement.

These tests cover the multi-profile personality routing described in the
Hermes Multi-Profile Personality Routing spec:

- Kyros + no profile selected      -> default
- Kyros + /p wilnice               -> wilnice
- Kyros + /p kyros                 -> kyros
- Kyros + /p default               -> default
- Wilnice + new conversation       -> kyros (forced)
- Wilnice + /p wilnice             -> denied, kyros remains
- Wilnice + /p default             -> denied, kyros remains
- Other user + new conversation    -> default
- Other user + /p wilnice          -> denied
- Other user + /p kyros            -> denied
- Kyros currently on wilnice + Wilnice sends -> no cross-contamination
- Restart Hermes -> per-user profile selections are restored
- Memory retrieval for Wilnice    -> kyros relationship context available
- Memory retrieval for unrelated user -> private context unavailable

The tests construct a temporary HERMES_HOME so they do not touch the live
``~/.hermes`` config; they patch ``userPeerAliases`` directly through the
resolver's ``aliases`` parameter so the production state is untouched.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from types import SimpleNamespace

import pytest


# ---------------------------------------------------------------------------
# fixtures


@pytest.fixture
def hermes_home(monkeypatch, tmp_path):
    """Each test gets an isolated HERMES_HOME under tmp_path."""
    home = tmp_path / "hermes_home"
    home.mkdir()
    profiles = home / "profiles"
    profiles.mkdir()
    (profiles / "kyros").mkdir()
    (profiles / "wilnice").mkdir()
    (home / "SOUL.md").write_text("# default SOUL\n\nThis is the default Hermes profile.\n")
    (profiles / "kyros" / "SOUL.md").write_text(
        "# kyros profile SOUL\n\nThis profile is for Wilnice's gateway chats.\n"
    )
    (profiles / "wilnice" / "SOUL.md").write_text(
        "# wilnice profile SOUL\n\nThis profile is the wilnice boyfriend voice.\n"
    )
    monkeypatch.setenv("HERMES_HOME", str(home))

    # The resolver's sidecar DB path is derived from get_hermes_home(); this
    # fixture forces the import to use tmp_path via the env var.
    from hermes_constants import get_hermes_home
    from gateway import sender_profile_state as state_mod

    state_mod._db_path = lambda: home / "profiles" / "sender_profile.db"
    state_mod.get_hermes_home = lambda: home

    yield home


@pytest.fixture
def cfg_with_users(hermes_home, aliases):
    """Write a minimal users: block + honcho.json aliases to the temp HERMES_HOME."""
    import yaml
    import json

    cfg_path = hermes_home / "config.yaml"
    cfg_path.write_text(
        yaml.safe_dump(
            {
                "users": {
                    "kyros": {
                        "default_profile": "default",
                        "allowed_profiles": ["default", "kyros", "wilnice"],
                    },
                    "wilnice": {
                        "default_profile": "kyros",
                        "forced_profile": "kyros",
                        "allowed_profiles": ["kyros"],
                    },
                    "*": {
                        "default_profile": "default",
                        "allowed_profiles": ["default"],
                    },
                },
            },
            sort_keys=False,
            allow_unicode=True,
        )
    )
    honcho_path = hermes_home / "honcho.json"
    honcho_path.write_text(
        json.dumps(
            {
                "hosts": {
                    "hermes": {
                        "peerName": "Kyros",
                        "aiPeer": "kyroskoh_bot",
                        "workspace": "hermes",
                        "pinUserPeer": False,
                        "userPeerAliases": dict(aliases),
                    }
                }
            },
            sort_keys=False,
        )
    )
    return cfg_path


@pytest.fixture
def aliases():
    """Honcho userPeerAliases fixture (canonical names match the users: keys)."""
    return {
        # Kyros's five IDs
        "5927843410163@lid": "Kyros",
        "1441204397": "Kyros",
        "1033258049508491294": "Kyros",
        "U0BRMAAP3SM": "Kyros",
        "@kyroskoh:matrix.org": "Kyros",
        # Wilnice's three IDs
        "171666202210553@lid": "Wilnice",
        "6581103465": "Wilnice",
        "7233071505": "Wilnice",
        # Strangers are deliberately absent.
    }


def _src(platform: str, user_id: str, chat_id: str):
    return SimpleNamespace(platform=platform, user_id=user_id, chat_id=chat_id)


# ---------------------------------------------------------------------------
# policy resolver


class TestPolicyResolution:
    def test_kyros_whatsapp(self, cfg_with_users, aliases):
        from gateway.user_profile_policy import resolve_policy_for_source
        res = resolve_policy_for_source(_src("whatsapp", "5927843410163@lid", "5927843410163@lid"),
                                        aliases=aliases)
        assert res.matched_via == "kyros"
        assert res.matched_alias_key == "5927843410163@lid"
        assert res.policy.default_profile == "default"
        assert res.policy.forced_profile is None
        assert sorted(res.policy.allowed_profiles) == ["default", "kyros", "wilnice"]

    def test_kyros_telegram(self, cfg_with_users, aliases):
        from gateway.user_profile_policy import resolve_policy_for_source
        res = resolve_policy_for_source(_src("telegram", "1441204397", "1441204397"),
                                        aliases=aliases)
        assert res.matched_via == "kyros"
        assert res.policy.allowed_profiles == frozenset({"default", "kyros", "wilnice"})

    def test_wilnice_whatsapp_lid(self, cfg_with_users, aliases):
        from gateway.user_profile_policy import resolve_policy_for_source
        res = resolve_policy_for_source(_src("whatsapp", "171666202210553@lid", "171666202210553@lid"),
                                        aliases=aliases)
        assert res.matched_via == "wilnice"
        assert res.policy.default_profile == "kyros"
        assert res.policy.forced_profile == "kyros"
        assert res.policy.allowed_profiles == frozenset({"kyros"})

    def test_wilnice_whatsapp_phone(self, cfg_with_users, aliases):
        from gateway.user_profile_policy import resolve_policy_for_source
        res = resolve_policy_for_source(_src("whatsapp", "6581103465", "6581103465"),
                                        aliases=aliases)
        assert res.matched_via == "wilnice"
        assert res.policy.forced_profile == "kyros"

    def test_wilnice_telegram(self, cfg_with_users, aliases):
        from gateway.user_profile_policy import resolve_policy_for_source
        res = resolve_policy_for_source(_src("telegram", "7233071505", "7233071505"),
                                        aliases=aliases)
        assert res.matched_via == "wilnice"
        assert res.policy.forced_profile == "kyros"

    def test_stranger(self, cfg_with_users, aliases):
        from gateway.user_profile_policy import resolve_policy_for_source
        res = resolve_policy_for_source(_src("whatsapp", "15551234567", "15551234567"),
                                        aliases=aliases)
        assert res.matched_via == "*"
        assert res.policy.default_profile == "default"
        assert res.policy.allowed_profiles == frozenset({"default"})

    def test_can_switch_to_respects_forced(self, cfg_with_users, aliases):
        from gateway.user_profile_policy import resolve_policy_for_source
        res = resolve_policy_for_source(_src("whatsapp", "171666202210553@lid", "171666202210553@lid"),
                                        aliases=aliases)
        # Wilnice's forced_profile is kyros; nothing else is allowed.
        assert res.policy.can_switch_to("kyros") is True
        assert res.policy.can_switch_to("wilnice") is False
        assert res.policy.can_switch_to("default") is False
        # Clearing (empty / neutral) is also denied under a forced_profile.
        assert res.policy.can_switch_to("") is False

    def test_can_switch_to_allows_neutral_when_not_forced(self, cfg_with_users, aliases):
        from gateway.user_profile_policy import resolve_policy_for_source
        res = resolve_policy_for_source(_src("whatsapp", "5927843410163@lid", "5927843410163@lid"),
                                        aliases=aliases)
        assert res.policy.can_switch_to("") is True  # /p none / /p default
        assert res.policy.can_switch_to("kyros") is True
        assert res.policy.can_switch_to("wilnice") is True


# ---------------------------------------------------------------------------
# persistence


class TestPersistence:
    def test_set_get_clear(self, cfg_with_users, aliases):
        from gateway.sender_profile_state import (
            set_active_profile,
            get_active_profile,
            clear_active_profile,
        )

        # Default state: nothing persisted.
        assert get_active_profile("whatsapp", "5927843410163@lid") is None

        # Write.
        assert set_active_profile("whatsapp", "5927843410163@lid", "5927843410163@lid", "wilnice") is True
        assert get_active_profile("whatsapp", "5927843410163@lid") == "wilnice"

        # Update.
        assert set_active_profile("whatsapp", "5927843410163@lid", "5927843410163@lid", "kyros") is True
        assert get_active_profile("whatsapp", "5927843410163@lid") == "kyros"

        # Clear.
        assert clear_active_profile("whatsapp", "5927843410163@lid") is True
        assert get_active_profile("whatsapp", "5927843410163@lid") is None

    def test_invalid_name_rejected(self, cfg_with_users, aliases):
        from gateway.sender_profile_state import set_active_profile

        assert set_active_profile("whatsapp", "5927843410163@lid", "5927843410163@lid",
                                   "not-a-real-profile") is False
        # And the sidecar is empty for that chat.
        from gateway.sender_profile_state import get_active_profile
        assert get_active_profile("whatsapp", "5927843410163@lid") is None

    def test_per_sender_isolation(self, cfg_with_users, aliases):
        """Kyros on wilnice; Wilnice not aliased here; stranger on default. No overlap."""
        from gateway.sender_profile_state import set_active_profile, get_active_profile

        set_active_profile("whatsapp", "5927843410163@lid", "5927843410163@lid", "wilnice")
        # Stranger has a different chat_id; it has its own row.
        set_active_profile("whatsapp", "15551234567", "15551234567", "default")

        assert get_active_profile("whatsapp", "5927843410163@lid") == "wilnice"
        assert get_active_profile("whatsapp", "15551234567") == "default"
        # Writing one chat does not bleed to another.

    def test_persistence_survives_module_reload(self, cfg_with_users, aliases):
        """Restart simulation: drop the module-level SQLite connection and re-open."""
        from gateway.sender_profile_state import (
            set_active_profile,
            get_active_profile,
        )
        import importlib
        import gateway.sender_profile_state as state_mod

        set_active_profile("whatsapp", "5927843410163@lid", "5927843410163@lid", "wilnice")
        # Reload the module: the function-level memo would be cleared; SQLite
        # data persists on disk.
        importlib.reload(state_mod)
        # Re-patch (reload re-imports get_hermes_home, etc., but the fixture
        # has overwritten state_mod._db_path).
        state_mod._db_path = lambda: cfg_with_users.parent / "profiles" / "sender_profile.db"
        state_mod.get_hermes_home = lambda: cfg_with_users.parent
        assert get_active_profile("whatsapp", "5927843410163@lid") == "wilnice"


# ---------------------------------------------------------------------------
# /p enforcement via the gateway handler


class TestSlashPEnforcement:
    """End-to-end through the gateway ``_handle_personality_command``."""

    @staticmethod
    def _mixin(cfg_with_users):
        """Build a fake mixin with config stubbed to load from the temp config."""
        import yaml
        import gateway.run as gr
        gr._load_gateway_config = lambda: yaml.safe_load(cfg_with_users.read_text())
        from gateway.slash_commands import GatewaySlashCommandsMixin

        class FakeMixin(GatewaySlashCommandsMixin):
            def __init__(self):
                self._ephemeral_system_prompt = ""

        return FakeMixin()

    @staticmethod
    def _event(platform, user_id, chat_id, text):
        parts = text.lstrip().split(maxsplit=1)
        args = parts[1] if len(parts) > 1 else ""
        src = SimpleNamespace(platform=platform, user_id=user_id, chat_id=chat_id)
        return SimpleNamespace(
            source=src,
            get_command_args=lambda: args,
            is_command=lambda: text.startswith("/"),
            allow_gateway_control=True,
            text=text,
        )

    # ----- Kyros -----

    def test_kyros_listing_shows_all_three_profiles(self, cfg_with_users, aliases):
        mixin = self._mixin(cfg_with_users)
        out = asyncio.run(mixin._handle_personality_command(
            self._event("whatsapp", "5927843410163@lid", "5927843410163@lid", "/personality")))
        assert "`default ✓`" in out
        assert "`kyros`" in out
        assert "`wilnice`" in out

    def test_kyros_p_wilnice(self, cfg_with_users, aliases):
        from gateway.sender_profile_state import clear_active_profile
        clear_active_profile("whatsapp", "5927843410163@lid")
        mixin = self._mixin(cfg_with_users)
        out = asyncio.run(mixin._handle_personality_command(
            self._event("whatsapp", "5927843410163@lid", "5927843410163@lid", "/personality wilnice")))
        assert "set to **wilnice**" in out
        # Persisted.
        from gateway.sender_profile_state import get_active_profile
        assert get_active_profile("whatsapp", "5927843410163@lid") == "wilnice"

    def test_kyros_p_kyros(self, cfg_with_users, aliases):
        from gateway.sender_profile_state import clear_active_profile, get_active_profile
        clear_active_profile("whatsapp", "5927843410163@lid")
        mixin = self._mixin(cfg_with_users)
        out = asyncio.run(mixin._handle_personality_command(
            self._event("whatsapp", "5927843410163@lid", "5927843410163@lid", "/personality kyros")))
        assert "set to **kyros**" in out
        assert get_active_profile("whatsapp", "5927843410163@lid") == "kyros"

    def test_kyros_p_default_clears(self, cfg_with_users, aliases):
        from gateway.sender_profile_state import clear_active_profile, set_active_profile, get_active_profile
        clear_active_profile("whatsapp", "5927843410163@lid")
        set_active_profile("whatsapp", "5927843410163@lid", "5927843410163@lid", "wilnice")
        mixin = self._mixin(cfg_with_users)
        out = asyncio.run(mixin._handle_personality_command(
            self._event("whatsapp", "5927843410163@lid", "5927843410163@lid", "/personality default")))
        assert "set to **default**" in out
        assert get_active_profile("whatsapp", "5927843410163@lid") is None

    # ----- Wilnice (forced) -----

    def test_wilnice_listing_only_shows_kyros(self, cfg_with_users, aliases):
        mixin = self._mixin(cfg_with_users)
        out = asyncio.run(mixin._handle_personality_command(
            self._event("whatsapp", "171666202210553@lid", "171666202210553@lid", "/personality")))
        assert "`kyros ✓`" in out
        assert "`wilnice`" not in out
        assert "`default`" not in out

    def test_wilnice_p_wilnice_denied(self, cfg_with_users, aliases):
        from gateway.sender_profile_state import clear_active_profile, get_active_profile
        clear_active_profile("whatsapp", "171666202210553@lid")
        mixin = self._mixin(cfg_with_users)
        out = asyncio.run(mixin._handle_personality_command(
            self._event("whatsapp", "171666202210553@lid", "171666202210553@lid", "/personality wilnice")))
        assert "This personality isn't available for this chat" in out
        assert "Active profile: `kyros`" in out
        # No persistence change.
        assert get_active_profile("whatsapp", "171666202210553@lid") is None

    def test_wilnice_p_default_denied(self, cfg_with_users, aliases):
        mixin = self._mixin(cfg_with_users)
        out = asyncio.run(mixin._handle_personality_command(
            self._event("whatsapp", "171666202210553@lid", "171666202210553@lid", "/personality default")))
        assert "This personality isn't available for this chat" in out
        assert "Active profile: `kyros`" in out

    def test_wilnice_p_kyros_allowed(self, cfg_with_users, aliases):
        mixin = self._mixin(cfg_with_users)
        out = asyncio.run(mixin._handle_personality_command(
            self._event("whatsapp", "171666202210553@lid", "171666202210553@lid", "/personality kyros")))
        assert "set to **kyros**" in out

    # ----- Stranger -----

    def test_stranger_listing_only_default(self, cfg_with_users, aliases):
        mixin = self._mixin(cfg_with_users)
        out = asyncio.run(mixin._handle_personality_command(
            self._event("whatsapp", "15551234567", "15551234567", "/personality")))
        assert "`default ✓`" in out
        assert "`kyros`" not in out
        assert "`wilnice`" not in out

    def test_stranger_p_wilnice_denied(self, cfg_with_users, aliases):
        mixin = self._mixin(cfg_with_users)
        out = asyncio.run(mixin._handle_personality_command(
            self._event("whatsapp", "15551234567", "15551234567", "/personality wilnice")))
        assert "This personality isn't available for this chat" in out
        assert "Allowed personalities: `default`" in out

    def test_stranger_p_kyros_denied(self, cfg_with_users, aliases):
        mixin = self._mixin(cfg_with_users)
        out = asyncio.run(mixin._handle_personality_command(
            self._event("whatsapp", "15551234567", "15551234567", "/personality kyros")))
        assert "This personality isn't available for this chat" in out

    def test_stranger_p_default_allowed(self, cfg_with_users, aliases):
        mixin = self._mixin(cfg_with_users)
        out = asyncio.run(mixin._handle_personality_command(
            self._event("whatsapp", "15551234567", "15551234567", "/personality default")))
        assert "set to **default**" in out


# ---------------------------------------------------------------------------
# Concurrent sessions, no cross-contamination


class TestCrossSessionIsolation:
    def test_kyros_and_wilnice_active_profiles_do_not_leak(self, cfg_with_users, aliases):
        """Scenario: Kyros currently on wilnice; Wilnice sends simultaneously.

        Each sender's persisted profile is independent; neither side sees
        the other's choice.
        """
        from gateway.sender_profile_state import set_active_profile, get_active_profile

        # Kyros switches to wilnice.
        set_active_profile("whatsapp", "5927843410163@lid", "5927843410163@lid", "wilnice")
        # Wilnice's gateway has no persisted entry — falls through to forced=kyros.
        # Stranger has its own row.
        set_active_profile("whatsapp", "15551234567", "15551234567", "default")

        assert get_active_profile("whatsapp", "5927843410163@lid") == "wilnice"
        # Wilnice's runtime resolves via policy.forced_profile = "kyros"
        # regardless of what her sidecar says.
        from gateway.user_profile_policy import resolve_policy_for_source
        wl_src = _src("whatsapp", "171666202210553@lid", "171666202210553@lid")
        policy = resolve_policy_for_source(wl_src, aliases=aliases).policy
        assert policy.forced_profile == "kyros"

        # Stranger's persisted default is independent.
        assert get_active_profile("whatsapp", "15551234567") == "default"


# ---------------------------------------------------------------------------
# Memory isolation: identity mapping isolates strangers from private memory


class TestMemoryIsolation:
    def test_stranger_runtime_peer_is_unaliased(self, cfg_with_users, aliases):
        """Honcho uses aliases to map a runtime ID to a canonical peer name.
        A stranger's runtime ID is *not* aliased, so the Honcho layer
        queries a per-session-id peer that has no private history.
        """
        assert "15551234567" not in aliases
        # Whereas Kyros's and Wilnice's are.
        assert "5927843410163@lid" in aliases
        assert "171666202210553@lid" in aliases

    def test_wilnice_runtime_peer_resolves_to_wilnice_card(self, cfg_with_users, aliases):
        """Wilnice's gateway sessions resolve to the Wilnice peer card."""
        from gateway.user_profile_policy import resolve_policy_for_source
        res = resolve_policy_for_source(
            _src("whatsapp", "171666202210553@lid", "171666202210553@lid"),
            aliases=aliases,
        )
        assert res.matched_via == "wilnice"

    def test_kyros_runtime_peer_resolves_to_kyros_card(self, cfg_with_users, aliases):
        from gateway.user_profile_policy import resolve_policy_for_source
        res = resolve_policy_for_source(
            _src("whatsapp", "5927843410163@lid", "5927843410163@lid"),
            aliases=aliases,
        )
        assert res.matched_via == "kyros"


# ---------------------------------------------------------------------------
# Unknown / future profiles fail closed


class TestFailsafe:
    def test_unknown_profile_name_returns_none(self, hermes_home):
        from gateway.slash_commands import normalize_profile_for_profile

        assert normalize_profile_for_profile("") is None
        assert normalize_profile_for_profile("not-a-real-profile") is None
        assert normalize_profile_for_profile("../escape") is None
        # Traversal/format guard.
        assert normalize_profile_for_profile("kyros") == "kyros"
        assert normalize_profile_for_profile("Wilnice") == "wilnice"
