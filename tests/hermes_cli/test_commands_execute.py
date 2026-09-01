"""Invariant tests for registry-owned slash execution (CommandDef.execute).

Every ``CommandDef`` with ``execute`` set must:
  * name a key that exists in :data:`hermes_cli.slash_exec.EXECUTORS`, and
  * produce IDENTICAL core text across surfaces for a fixed context — the
    executor may only vary on ``args``/``options``, never on ``surface``.
"""

import pytest

from hermes_cli.commands import COMMAND_REGISTRY, resolve_command
from hermes_cli.personality import (
    normalize_personality_name,
    resolve_personality,
)
from hermes_cli.slash_exec import (
    EXECUTORS,
    CommandContext,
    CommandReply,
    execute_command,
    resolve_executor,
    run_execute,
)

MIGRATED = [cmd for cmd in COMMAND_REGISTRY if cmd.execute]

SURFACES = ("cli", "gateway", "tui")


def test_some_commands_are_migrated():
    names = {cmd.name for cmd in MIGRATED}
    # The thin-slice set — extend as more commands migrate.
    assert {"version", "egress", "profile", "bundles", "help", "commands"} <= names




def test_unknown_kyros_resolves_as_neutral():
    """``/personality kyros`` should clear the overlay and not raise.

    Operators use ``kyros`` as a symbolic alias for "no overlay / default
    profile". The resolver must return (``""``, ``""``) so the handler hits
    the cleared branch instead of erroring with Unknown personality.
    """
    # Both neutral forms pre-existing and the new ``"kyros"`` alias must
    # resolve identically. ``available_personalities`` should also hide the
    # ``kyros`` user entry from listings because it is a neutral alias.
    cfg = {"agent": {"personalities": {"kyros": "...", "wilnice": "..."}}}
    from hermes_cli.personality import available_personalities

    assert "kyros" not in available_personalities(cfg)
    name, prompt = resolve_personality("kyros", cfg)
    assert name == "" and prompt == ""
    # And the other neutral spellings keep working.
    for neutral in ("none", "default", "neutral", ""):
        n, p = resolve_personality(neutral, cfg)
        assert n == "" and p == "", neutral


def test_unknown_personality_raises_with_alias_aware_listing():
    """Unknown names must still fail loudly with a usable listing.

    Even when ``kyros`` is a registered user personality, it should not
    appear in the error listing because it is a neutral alias.
    """
    cfg = {"agent": {"personalities": {"kyros": "...", "wilnice": "..."}}}
    with pytest.raises(ValueError) as exc:
        resolve_personality("not-a-persona", cfg)
    msg = str(exc.value)
    assert "kyros" not in msg
    assert "wilnice" in msg


def test_normalize_kyros_to_empty():
    """``kyros`` is a symbolic alias for "no overlay" regardless of case.

    Operators may type ``/p Kyros``, ``/p KYROS``, ``/p kyros``, or pad
    with whitespace; they all collapse to the same canonical ``""`` so the
    handler's "cleared" branch fires. Non-ASCII folding also has to stay
    safe for future translated names, so we use ``casefold`` semantics
    (a German ß → ss style collapse).
    """
    for variant in ("kyros", "Kyros", "KYROS", "kYRoS", "  kyros  ", "\tkyros\n"):
        assert normalize_personality_name(variant) == "", variant
    # Other neutral spellings still work.
    for variant in ("none", "None", "NONE", "default", "Default", "neutral"):
        assert normalize_personality_name(variant) == "", variant
    # And a real, non-neutral user personality still resolves case-insensitively
    # to its lowercase canonical form.
    for variant in ("Wilnice", "WILNICE", "wilnice", "  Wilnice "):
        assert normalize_personality_name(variant) == "wilnice", variant


def test_resolve_existing_personalities_still_works():
    """Regression: ``wilnice`` must still resolve to its user overlay text."""
    cfg = {
        "agent": {
            "personalities": {
                "kyros": "(operator text)",
                "wilnice": "Use a playful boyfriend tone.",
            }
        }
    }
    name, prompt = resolve_personality("wilnice", cfg)
    assert name == "wilnice"
    assert prompt == "Use a playful boyfriend tone."


def test_unmigrated_commands_have_no_executor():
    for cmd in COMMAND_REGISTRY:
        if not cmd.execute:
            assert resolve_executor(cmd) is None
            assert run_execute(cmd, CommandContext()) is None










