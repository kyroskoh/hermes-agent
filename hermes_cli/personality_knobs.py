"""Per-profile persona knob registry + config persistence.

FORK: kyroskoh/hermes-agent — operator-tunable persona percentages
that are surfaced in the dashboard's /personality page and persisted
under ``personality.knobs.<name>`` in the active profile's
``~/.hermes/profiles/<name>/config.yaml``.

Why a registry, not a free-form dict
-------------------------------------
Persona knobs are surfaced in the dashboard with a stable schema
(name, label, description, min/max/default). Free-form values would
let a typo or out-of-range integer slip into SOUL.md silently. The
:data:`REGISTRY` is the source of truth; :func:`resolve_knobs`
materialises the live effective value for every registered knob by
reading the per-profile config and overlaying any override.

Storage contract
----------------
A knob lives under ``personality.knobs.<name>`` and is an integer
between ``min`` and ``max`` (inclusive). Removing the key restores
the factory default — ``resolve_knobs`` reports ``is_default=True``
in that case so the dashboard can render a "default" badge.

This module is intentionally dependency-free at import time so
``hermes_cli.web_server`` can import it from a request handler
without dragging the full config stack in at module-load time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

__all__ = [
    "Knob",
    "REGISTRY",
    "get_knob",
    "resolve_knobs",
    "set_knob",
    "unset_knob",
    "list_knob_names",
]


@dataclass(frozen=True)
class Knob:
    """A registered persona knob.

    Attributes
    ----------
    name:
        Stable identifier — also the YAML key under ``personality.knobs.<name>``.
    label:
        Human-readable short label for the dashboard.
    description:
        Longer explanation shown in the UI tooltip / detail card.
    default:
        Factory default value. Returned when no override is set.
    min:
        Lower bound (inclusive). Values below are clamped at write time.
    max:
        Upper bound (inclusive). Values above are clamped at write time.
    """

    name: str
    label: str
    description: str
    default: int
    min: int = 0
    max: int = 100

    def coerce(self, value: Any) -> int:
        """Coerce + clamp an incoming value into a valid int.

        Raises ``ValueError`` if the value is not coercible to int.
        Floats are rounded to the nearest int; strings are parsed
        via ``int(...)`` after stripping whitespace.
        """
        if isinstance(value, bool):
            # bool is a subclass of int — reject explicitly so True/False
            # don't silently become 1/0 in the dashboard.
            raise ValueError(
                f"{self.name}: expected int, got bool"
            )
        try:
            coerced = int(round(float(value)))
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"{self.name}: cannot coerce {value!r} to int ({exc})"
            ) from exc
        if coerced < self.min:
            coerced = self.min
        elif coerced > self.max:
            coerced = self.max
        return coerced


# Single source of truth for every knob the dashboard may surface.
# To add a new knob: drop an entry here and the rest of the system
# (config save, knob resolver, dashboard render, persona SOUL footer)
# picks it up automatically. No new code paths.
REGISTRY: Dict[str, Knob] = {
    "memory_recall": Knob(
        name="memory_recall",
        label="Memory recall",
        description=(
            "How often the persona consults Honcho and its own SOUL.md "
            "before answering. 0 = pure persona, never references stored "
            "memory. 100 = always opens with recalled facts. Default "
            "balances voice with continuity."
        ),
        default=75,
    ),
}


def list_knob_names() -> List[str]:
    """Return the registered knob names in stable order."""
    return list(REGISTRY.keys())


def get_knob(name: str) -> Knob:
    """Return the knob registered under ``name``.

    Raises ``KeyError`` if ``name`` is not registered — callers
    should map that to a 404 at the HTTP boundary.
    """
    try:
        return REGISTRY[name]
    except KeyError as exc:
        raise KeyError(
            f"unknown personality knob: {name!r} "
            f"(registered: {sorted(REGISTRY)})"
        ) from exc


def _config_path(config: Dict[str, Any]) -> Dict[str, Any]:
    """Return the ``personality.knobs`` sub-dict, creating it lazily."""
    personality = config.setdefault("personality", {})
    if not isinstance(personality, dict):
        # Personality got clobbered with a non-dict (e.g. a string from
        # a malformed config save). Reset to a dict so we can keep going.
        config["personality"] = {}
        personality = config["personality"]
    knobs = personality.setdefault("knobs", {})
    if not isinstance(knobs, dict):
        personality["knobs"] = {}
        knobs = personality["knobs"]
    return knobs


def resolve_knobs(config: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Materialise every registered knob's effective state.

    Returns a dict keyed by knob name. Each value is the public
    ``PersonalityKnob`` shape the dashboard consumes::

        {
            "name":        "memory_recall",
            "label":       "Memory recall",
            "description": "...",
            "value":       75,        # effective (override or default)
            "default":     75,
            "min":         0,
            "max":         100,
            "is_default":  True,      # True ⇔ no override is set
        }

    Unknown keys in ``personality.knobs`` are silently ignored —
    they're either from a removed knob or hand-edited garbage and
    the dashboard shouldn't surface them.
    """
    knobs_section = (
        config.get("personality", {}).get("knobs", {})
        if isinstance(config.get("personality"), dict)
        else {}
    )
    if not isinstance(knobs_section, dict):
        knobs_section = {}

    out: Dict[str, Dict[str, Any]] = {}
    for name, knob in REGISTRY.items():
        raw = knobs_section.get(name)
        if raw is None:
            value = knob.default
            is_default = True
        else:
            try:
                value = knob.coerce(raw)
                is_default = value == knob.default
            except ValueError:
                # Garbage value in the config — fall back to default
                # but keep the row visible so the operator can reset it.
                value = knob.default
                is_default = True
        out[name] = {
            "name": knob.name,
            "label": knob.label,
            "description": knob.description,
            "value": value,
            "default": knob.default,
            "min": knob.min,
            "max": knob.max,
            "is_default": is_default,
        }
    return out


def set_knob(
    config: Dict[str, Any],
    name: str,
    value: int,
) -> int:
    """Persist an override for ``name`` and return the clamped value.

    Mutates ``config`` in place — the HTTP layer is responsible for
    saving it via the normal ``save_config`` path.

    Raises ``KeyError`` if the knob is not registered, ``ValueError``
    if the value is not coercible / out of range.
    """
    knob = get_knob(name)  # raises KeyError on unknown
    clamped = knob.coerce(value)
    knobs = _config_path(config)
    knobs[name] = clamped
    return clamped


def unset_knob(config: Dict[str, Any], name: str) -> bool:
    """Remove the override for ``name``. Returns ``True`` if a key was removed.

    No-op if the knob isn't registered or has no override — the dashboard
    treats both as a successful reset so it can re-fetch the row in
    its default state.
    """
    try:
        get_knob(name)
    except KeyError:
        return False
    knobs_section = (
        config.get("personality", {}).get("knobs", {})
        if isinstance(config.get("personality"), dict)
        else {}
    )
    if not isinstance(knobs_section, dict):
        return False
    if name in knobs_section:
        del knobs_section[name]
        return True
    return False