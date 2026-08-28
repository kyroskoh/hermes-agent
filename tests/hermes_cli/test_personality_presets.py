"""Tests for the expanded personality preset library.

Covers the work-mode presets (ops-sre, incident-mode, code-reviewer,
research-mode, arcgis-analyst) and the novelty voices (gordon-ramsay,
retro-arcade, terminal-guru, cyberpunk-netrunner, noir-detective-2) added
to BUILTIN_PERSONALITIES in hermes_cli/personality.py.

Goals
-----
1. Every advertised preset name is registered.
2. Work-mode presets render as structured dicts (system_prompt + tone + style)
   and resolve_ephemeral_system_prompt returns a meaningful, non-empty prompt.
3. Novelty voices render as plain strings (parity with existing built-ins).
4. describe_personality surfaces the description for dict presets and the
   truncated body for string presets — used by the dashboard list view.
5. User-config overlay (agent.personalities.<name>) still wins over the
   built-in, even for the new presets.
"""

from __future__ import annotations

import pytest

from hermes_cli.personality import (
    BUILTIN_PERSONALITIES,
    available_personalities,
    describe_personality,
    render_personality_prompt,
    resolve_ephemeral_system_prompt,
    resolve_personality,
)


WORK_MODE_PRESETS = [
    "ops-sre",
    "incident-mode",
    "code-reviewer",
    "research-mode",
    "arcgis-analyst",
]

NOVELTY_PRESETS = [
    "gordon-ramsay",
    "retro-arcade",
    "terminal-guru",
    "cyberpunk-netrunner",
    "noir-detective-2",
]

# Singapore-local voices. Structured dicts (system_prompt + tone + style +
# description) so the dashboard list view shows the description and the
# rendered prompt includes Tone/Style lines. See personality.py for the full
# definitions.
SG_PRESETS = [
    "sg-auntie",
    "sg-kiasu",
    "sg-kiasi",
    "sg-kopitiam-uncle",
    "sg-heartland-student",
    "sg-property-agent",
    "sg-ns-commando",
    "sg-circuit-breaker-veteran",
    "sg-grandmother",
]

ALL_NEW_PRESETS = WORK_MODE_PRESETS + NOVELTY_PRESETS + SG_PRESETS


# ── registry completeness ───────────────────────────────────────────────────


@pytest.mark.parametrize("name", ALL_NEW_PRESETS)
def test_new_preset_is_registered(name: str):
    """Every advertised preset name is in BUILTIN_PERSONALITIES."""
    assert name in BUILTIN_PERSONALITIES, (
        f"preset {name!r} advertised but not registered"
    )


@pytest.mark.parametrize("name", WORK_MODE_PRESETS + SG_PRESETS)
def test_structured_preset_is_dict(name: str):
    """Work-mode AND Singapore-local presets are structured dicts."""
    defn = BUILTIN_PERSONALITIES[name]
    assert isinstance(defn, dict), (
        f"structured preset {name!r} should be a dict, got {type(defn).__name__}"
    )
    assert "system_prompt" in defn and defn["system_prompt"].strip(), (
        f"{name!r} missing non-empty system_prompt"
    )
    assert "description" in defn and defn["description"].strip(), (
        f"{name!r} missing description (used by dashboard list)"
    )
    assert "tone" in defn, f"{name!r} should declare tone"
    assert "style" in defn, f"{name!r} should declare style"


@pytest.mark.parametrize("name", SG_PRESETS)
def test_sg_preset_mentions_singapore_context(name: str):
    """Every SG preset should reference Singapore context in its system_prompt.
    Guard against future contributors accidentally retitling without updating
    the local-flavour marker."""
    defn = BUILTIN_PERSONALITIES[name]
    prompt = (defn.get("system_prompt", "") + " " + defn.get("description", "")).lower()
    # Accept any common SG marker
    sg_markers = [
        "singapore", "sg ", "kopitiam", "singlish", "hokkien",
        "teochew", "cantonese", "kiasu", "kiasi", "auntie",
        "uncle", "hawker", "mrt", "hdb", "to a payoh",
    ]
    assert any(marker in prompt for marker in sg_markers), (
        f"SG preset {name!r} has no Singapore-context marker in prompt or "
        f"description (none of {sg_markers} matched)"
    )


@pytest.mark.parametrize("name", NOVELTY_PRESETS)
def test_novelty_preset_is_string(name: str):
    """Novelty voices stay as plain strings for parity with existing built-ins."""
    defn = BUILTIN_PERSONALITIES[name]
    assert isinstance(defn, str), (
        f"novelty preset {name!r} should be a plain string, got {type(defn).__name__}"
    )
    # Not empty
    assert defn.strip(), f"novelty preset {name!r} body is empty"


# ── rendering ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("name", ALL_NEW_PRESETS)
def test_new_preset_renders_to_non_empty_prompt(name: str):
    """render_personality_prompt returns a non-empty string for every preset."""
    rendered = render_personality_prompt(BUILTIN_PERSONALITIES[name])
    assert isinstance(rendered, str)
    assert rendered.strip(), f"preset {name!r} renders to empty prompt"


@pytest.mark.parametrize("name", WORK_MODE_PRESETS + SG_PRESETS)
def test_structured_render_contains_tone_and_style(name: str):
    """render_personality_prompt appends 'Tone:' and 'Style:' lines for dicts
    (work-mode AND Singapore-local presets)."""
    rendered = render_personality_prompt(BUILTIN_PERSONALITIES[name])
    assert "Tone:" in rendered, f"structured {name!r} render missing 'Tone:'"
    assert "Style:" in rendered, f"structured {name!r} render missing 'Style:'"


@pytest.mark.parametrize("name", WORK_MODE_PRESETS + SG_PRESETS)
def test_structured_render_contains_system_prompt(name: str):
    """render_personality_prompt keeps the system_prompt text in the output."""
    defn = BUILTIN_PERSONALITIES[name]
    rendered = render_personality_prompt(defn)
    # Use the first sentence to assert presence (avoids exact-string brittleness)
    first_sentence = defn["system_prompt"].split(".")[0]
    assert first_sentence in rendered, (
        f"structured {name!r} render dropped the first system_prompt sentence"
    )


# ── description (list-UI surface) ──────────────────────────────────────────


@pytest.mark.parametrize("name", WORK_MODE_PRESETS + SG_PRESETS)
def test_describe_uses_description_field(name: str):
    """describe_personality surfaces the description, not the prompt body."""
    defn = BUILTIN_PERSONALITIES[name]
    desc = describe_personality(defn, width=200)
    assert desc == defn["description"], (
        f"describe_personality({name!r}) should return description, got {desc!r}"
    )


def test_describe_truncates_long_descriptions():
    """Width-bounded description keeps the dashboard list compact."""
    defn = {
        "description": "x" * 80,
        "system_prompt": "tail",
    }
    desc = describe_personality(defn, width=50)
    assert desc == "x" * 50 + "..."
    assert "\n" not in desc


# ── resolution through available_personalities ──────────────────────────────


@pytest.mark.parametrize("name", ALL_NEW_PRESETS)
def test_new_preset_resolves_via_cfg(name: str):
    """resolve_personality(<new_name>, {}) returns the canonical name + prompt."""
    canon, prompt = resolve_personality(name, {})
    assert canon == name
    assert prompt.strip()


def test_available_personalities_includes_new_presets():
    """available_personalities() surfaces every preset without a user override."""
    merged = available_personalities({})
    for name in ALL_NEW_PRESETS:
        assert name in merged, f"available_personalities dropped preset {name!r}"


def test_user_override_wins_for_new_preset():
    """User agent.personalities.<name> overrides the built-in for new presets too."""
    cfg = {
        "agent": {
            "personalities": {
                "ops-sre": {
                    "description": "Kyros's customized SRE voice",
                    "system_prompt": "Custom SRE prompt",
                    "tone": "snarky",
                    "style": "snarky bullets",
                }
            }
        }
    }
    merged = available_personalities(cfg)
    assert merged["ops-sre"]["system_prompt"] == "Custom SRE prompt"
    assert merged["ops-sre"]["tone"] == "snarky"


# ── resolve_ephemeral_system_prompt works end-to-end ───────────────────────


@pytest.mark.parametrize("name", WORK_MODE_PRESETS + SG_PRESETS)
def test_structured_resolves_to_full_prompt_via_display_personality(name: str):
    """When display.personality = <structured preset>, the resolved prompt is
    non-empty and includes both system_prompt and tone/style rendering.
    Covers work-mode AND Singapore-local presets."""
    cfg = {"display": {"personality": name}}
    prompt = resolve_ephemeral_system_prompt(cfg)
    assert prompt.strip()
    assert "Tone:" in prompt
    assert "Style:" in prompt


# ── guard against accidental regression to non-list types ──────────────────


def test_builtin_personalities_dict_type_is_preserved():
    """BUILTIN_PERSONALITIES must remain a dict (not a TypedDict or similar)
    so available_personalities() can keep merging user overlays."""
    assert isinstance(BUILTIN_PERSONALITIES, dict)
