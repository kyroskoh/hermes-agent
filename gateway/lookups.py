"""Refresh cached runtime lookups without restarting the gateway.

Some Hermes subsystems cache module-level globals at import time —
``hermes_cli.commands.GATEWAY_KNOWN_COMMANDS`` is the canonical example.
After an operator adds an alias or installs a new plugin, those cached
references stay stale until the gateway process restarts. ``/reload``
calls ``rebuild_lookups()`` so the gateway can pick up changes live.

What ``rebuild_lookups()`` does:

1. ``importlib.reload(hermes_cli.commands)`` — re-runs the module-level
   code that builds ``COMMAND_REGISTRY``,
   ``GATEWAY_KNOWN_COMMANDS``, and ``quick_commands`` from the current
   source on disk. Because callers do
   ``from hermes_cli.commands import X`` inside their handler bodies,
   every subsequent invocation sees the refreshed module attribute.

2. Re-imports other cached lookups: skill commands, profile_routes,
   policy resolver inputs. Anything that stores a snapshot at
   startup is reloaded here.

3. Returns a human-readable summary of the operations performed so
   the ``/reload`` slash command can show it.

When NOT to call this:

- Mid-conversation agent rebuilds — those use the existing
  ``_agent_cache_signature`` invalidation; rebuild_lookups() doesn't
  touch that path.
- Profile routing table changes that need immediate effect — those
  are already picked up via the per-message ``_resolve_profile_home_for_source``
  lookup. Still safe to call; just unnecessary.
- Honcho identity changes — those read ``honcho.json`` on every
  message so they don't need this either.
"""

from __future__ import annotations

import importlib
import logging
from typing import List

logger = logging.getLogger(__name__)


def rebuild_lookups(*, verbose: bool = False) -> List[str]:
    """Reload the module-level caches that gateway handlers read at call time.

    Returns a list of human-readable operations performed. ``/reload``
    shows this back to the operator.

    The function is intentionally narrow: only modules whose top-level
    state has known startup-time staleness are re-imported. Adding more
    modules is to a safe but pointless when no caller reads them at
    handler-call time.
    """
    log: List[str] = []

    # 1. hernes_cli.commands — the main registry of slash commands,
    # including GATEWAY_KNOWN_COMMANDS (built once at import time from
    # COMMAND_REGISTRY). Reload picks up edits to /commands + /aliases.
    try:
        import hermes_cli.commands as hc

        before = sorted(getattr(hc, "GATEWAY_KNOWN_COMMANDS", frozenset()))
        importlib.reload(hc)
        after = sorted(getattr(hc, "GATEWAY_KNOWN_COMMANDS", frozenset()))
        added = [n for n in after if n not in set(before)]
        removed = [n for n in before if n not in set(after)]
        log.append(
            f"hermes_cli.commands: {len(after)} known "
            f"(+{len(added)}, -{len(removed)})"
        )
        if verbose:
            if added:
                log.append("  + " + ", ".join(sorted(added)))
            if removed:
                log.append("  - " + ", ".join(sorted(removed)))
    except Exception as exc:
        logger.warning("reload hermes_cli.commands failed: %s", exc)
        log.append(f"hermes_cli.commands: reload failed ({exc})")

    # 2. gateway.profile_routing — parsed routes snapshot lives on
    # the config dataclass; reload only matters if the parser module
    # itself was edited. Cheap to do.
    try:
        import gateway.profile_routing as pr

        importlib.reload(pr)
        log.append("gateway.profile_routing: reloaded")
    except Exception as exc:
        logger.warning("reload gateway.profile_routing failed: %s", exc)
        log.append(f"gateway.profile_routing: reload failed ({exc})")

    # 3. gateway.user_profile_policy — reads config.yaml + honcho.json
    # lazily on every call, so it doesn't strictly need a reload. Reload
    # anyway so any in-process caches get cleared.
    try:
        import gateway.user_profile_policy as upp

        importlib.reload(upp)
        log.append("gateway.user_profile_policy: reloaded")
    except Exception as exc:
        logger.warning("reload gateway.user_profile_policy failed: %s", exc)
        log.append(f"gateway.user_profile_policy: reload failed ({exc})")

    # 4. agent.skill_commands — skill registry is built once at import
    # time. Reload so newly installed skills are visible to /help and
    # /commands listings.
    try:
        import agent.skill_commands as sc

        importlib.reload(sc)
        log.append("agent.skill_commands: reloaded")
    except Exception as exc:
        logger.warning("reload agent.skill_commands failed: %s", exc)
        log.append(f"agent.skill_commands: reload failed ({exc})")

    return log


__all__ = ["rebuild_lookups"]