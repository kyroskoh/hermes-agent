"""Sender-driven per-user profile policy.

Maps a ``SessionSource`` (platform + chat_id / user_id) to:

- ``default_profile``  — the profile that mounts when no override is set
- ``forced_profile``   — when set, the user cannot escape this profile
- ``allowed_profiles`` — the names ``/p`` accepts for this user

Configuration lives in ``config.yaml`` under a top-level ``users`` block,
alongside (and parallel to) the existing ``gateway.profile_routes``:

    users:
      kyros:
        default_profile: default
        allowed_profiles: [default, kyros, wilnice]
      wilnice:
        default_profile: kyros
        forced_profile: kyros
        allowed_profiles: [kyros]
      "*":
        default_profile: default
        allowed_profiles: [default]

Identity matching uses ``honcho.json`` ``hosts.<host>.userPeerAliases`` —
the same canonical alias table the Honcho memory provider uses to map a
runtime ID to a canonical peer name. This keeps a single source of truth
for "which real person is talking" rather than scattering it across two
config files.

The ``users.<canonical-name>`` keys are the canonical Honcho peer names.
A canonical peer can have many aliases (multiple gateway IDs across
WhatsApp / Telegram / Discord / etc.); all of them resolve to the same
policy entry.

For unknown senders, the wildcard ``"*"`` entry applies. The wildcard
ships with safe defaults (``default_profile: default``,
``allowed_profiles: [default]``) so an unconfigured platform cannot
accidentally leak private Kyros/Wilnice personas.

This module does NOT mutate any state. Per-sender active-profile
persistence is handled by ``gateway/sender_profile_state.py``.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, FrozenSet, Mapping, Optional

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)


_WILDCARD_USER = "*"


def _validate_profile_name(name: Any) -> Optional[str]:
    """Reuse hermes_cli.profiles' validators to keep profile names sane."""
    if not isinstance(name, str):
        return None
    n = name.strip()
    if not n:
        return None
    try:
        from hermes_cli.profiles import normalize_profile_name, validate_profile_name

        n = normalize_profile_name(n)
        validate_profile_name(n)
        return n
    except (ValueError, ImportError):
        return None


@dataclass(frozen=True)
class UserProfilePolicy:
    """Resolved profile-permission policy for one canonical user."""

    canonical_user: str
    default_profile: str
    allowed_profiles: FrozenSet[str]
    forced_profile: Optional[str] = None

    def can_switch_to(self, profile: str) -> bool:
        """True when this user can run ``/p <profile>`` (or the alias).

        An empty / neutral request (``/p none`` / ``/p default`` /
        ``/p kyros``) is always accepted when the user is not forced —
        clearing the overlay back to the user-owned manual prompt is
        the simplest legitimate use of the slash command and must not
        require the empty-string name to be in the allowlist.
        """
        if not profile:
            return self.forced_profile is None
        if self.forced_profile:
            return profile == self.forced_profile
        return profile in self.allowed_profiles


@dataclass(frozen=True)
class PolicyLookup:
    """Result of resolving a SessionSource to a profile policy."""

    policy: UserProfilePolicy
    matched_via: str  # "kyros" | "wilnice" | "wildcard" | "default"
    matched_alias_key: Optional[str] = None  # the gateway runtime ID that hit


class UserProfilePolicyError(ValueError):
    """Raised when the ``users:`` config block is malformed."""


def _read_yaml() -> Mapping[str, Any]:
    """Read the raw ``users:`` block from ``config.yaml``."""
    cfg_path = get_hermes_home() / "config.yaml"
    if not cfg_path.exists():
        return {}
    try:
        import yaml
    except ImportError:
        logger.warning("PyYAML unavailable; user_profile_policy will use defaults.")
        return {}
    try:
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    except Exception as exc:  # pragma: no cover - parse failure path
        logger.warning("config.yaml parse failed (%s); using defaults.", exc)
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def _read_aliases() -> Mapping[str, str]:
    """Read ``userPeerAliases`` from ``honcho.json``.

    Returns the empty mapping on missing file / malformed JSON — never
    raises. The aliases are a *runtime_id → canonical_peer_name* map.
    """
    honcho_path = get_hermes_home() / "honcho.json"
    if not honcho_path.exists():
        return {}
    try:
        import json
    except ImportError:  # pragma: no cover
        return {}
    try:
        data = json.loads(honcho_path.read_text(encoding="utf-8"))
    except Exception as exc:  # pragma: no cover
        logger.warning("honcho.json parse failed (%s); alias matching disabled.", exc)
        return {}
    hosts = data.get("hosts") if isinstance(data, dict) else None
    if not isinstance(hosts, dict):
        return {}
    hermes_host = hosts.get("hermes")
    if not isinstance(hermes_host, dict):
        return {}
    aliases = hermes_host.get("userPeerAliases")
    if not isinstance(aliases, dict):
        return {}
    out: dict[str, str] = {}
    for k, v in aliases.items():
        if not isinstance(k, str) or not isinstance(v, str):
            continue
        ks = k.strip()
        vs = v.strip()
        if ks and vs:
            out[ks] = vs
    return out


def _coerce_str_set(raw: Any) -> FrozenSet[str]:
    if raw is None:
        return frozenset()
    if isinstance(raw, str):
        items = [s.strip() for s in re.split(r"[,\s]+", raw) if s.strip()]
    elif isinstance(raw, (list, tuple, set, frozenset)):
        items = [str(x).strip() for x in raw]
    else:
        items = [str(raw).strip()]
    out: list[str] = []
    for it in items:
        v = _validate_profile_name(it)
        if v:
            out.append(v)
    return frozenset(out)


def _parse_user_entry(name: str, raw: Any) -> UserProfilePolicy:
    if not isinstance(raw, dict):
        raise UserProfilePolicyError(
            f"users.{name!r} must be a mapping; got {type(raw).__name__}."
        )
    default_profile = raw.get("default_profile", "default")
    default_profile = _validate_profile_name(default_profile)
    if not default_profile:
        raise UserProfilePolicyError(
            f"users.{name!r}.default_profile must be a valid profile name."
        )
    allowed = _coerce_str_set(raw.get("allowed_profiles"))
    if not allowed:
        # Derive a safe default from default_profile so an entry never
        # becomes a black hole. Operators can widen the allowlist later.
        allowed = frozenset({default_profile})
    # default_profile must be in allowed (or forced), else the user can't
    # reach their own default.
    if default_profile not in allowed and not raw.get("forced_profile"):
        allowed = allowed | {default_profile}
    forced_raw = raw.get("forced_profile")
    forced_profile: Optional[str] = None
    if isinstance(forced_raw, str) and forced_raw.strip():
        forced_profile = _validate_profile_name(forced_raw.strip())
        if not forced_profile:
            raise UserProfilePolicyError(
                f"users.{name!r}.forced_profile must be a valid profile name."
            )
        # forced_profile always wins; allowed_profiles must contain it so
        # /p listings stay self-consistent.
        if forced_profile not in allowed:
            allowed = allowed | {forced_profile}
    return UserProfilePolicy(
        canonical_user=name,
        default_profile=default_profile,
        allowed_profiles=allowed,
        forced_profile=forced_profile,
    )


def load_user_profile_policies(
    config: Optional[Mapping[str, Any]] = None,
    aliases: Optional[Mapping[str, str]] = None,
) -> dict[str, UserProfilePolicy]:
    """Parse the ``users:`` block into a name → policy mapping.

    Includes a safe ``"*"`` wildcard even if the operator omitted it.

    Canonical user names are case-folded internally (so operators can
    write ``users.kyros`` or ``users.Kyros`` and match the canonical
    Honcho peer name regardless of how it is cased in
    ``userPeerAliases``). The lookup key is always lowercase.
    """
    cfg = config if config is not None else _read_yaml()
    users_raw = cfg.get("users") if isinstance(cfg, dict) else None
    if not isinstance(users_raw, dict):
        users_raw = {}
    out: dict[str, UserProfilePolicy] = {}
    for name, raw in users_raw.items():
        if not isinstance(name, str):
            continue
        n = name.strip()
        if not n:
            continue
        canonical = n.casefold()
        try:
            policy = _parse_user_entry(canonical, raw)
        except UserProfilePolicyError as exc:
            logger.warning("Skipping invalid users.%s: %s", n, exc)
            continue
        out[canonical] = policy
    if _WILDCARD_USER not in out:
        out[_WILDCARD_USER] = UserProfilePolicy(
            canonical_user=_WILDCARD_USER,
            default_profile="default",
            allowed_profiles=frozenset({"default"}),
            forced_profile=None,
        )
    return out


def _runtime_id_candidates(platform: str, user_id: Optional[str], chat_id: Optional[str]) -> list[str]:
    """Build an ordered list of runtime ID candidates for alias lookup.

    Order matters: the first match wins, so we put the most specific
    keys (chat_id, user_id) before the broader form. Aliases are
    stable identifiers — they never change — so plain string equality
    is sufficient; no normalisation is applied.
    """
    candidates: list[str] = []
    for raw in (user_id, chat_id):
        if isinstance(raw, str) and raw.strip():
            candidates.append(raw.strip())
    return candidates


def resolve_policy_for_source(
    source: Any,
    policies: Optional[Mapping[str, UserProfilePolicy]] = None,
    aliases: Optional[Mapping[str, str]] = None,
) -> PolicyLookup:
    """Resolve a SessionSource to its profile policy.

    ``source`` is duck-typed: any object with ``platform``, ``user_id``,
    and ``chat_id`` attributes works. The Telegram/WhatsApp SessionSource
    classes from ``gateway.session`` already have this shape.
    """
    if policies is None:
        policies = load_user_profile_policies()
    if aliases is None:
        aliases = _read_aliases()

    platform = getattr(source, "platform", None) or ""
    user_id = getattr(source, "user_id", None)
    chat_id = getattr(source, "chat_id", None)

    # Walk every candidate runtime ID through the alias table first.
    # The alias table maps runtime_id → canonical_peer_name; we
    # case-fold the canonical name so ``users.kyros`` matches an alias
    # stored as ``Kyros``.
    for runtime_id in _runtime_id_candidates(platform, user_id, chat_id):
        canonical = aliases.get(runtime_id)
        if isinstance(canonical, str):
            key = canonical.strip().casefold()
            if key in policies:
                policy = policies[key]
                return PolicyLookup(
                    policy=policy,
                    matched_via=key,
                    matched_alias_key=runtime_id,
                )

    # Fall back to the explicit users.<canonical-name> match using the
    # source's already-resolved identity (if any). This keeps a single
    # source of truth while also tolerating gateways that resolve the
    # canonical peer name outside the alias table.
    canonical_user = getattr(source, "canonical_user_name", None)
    if isinstance(canonical_user, str):
        key = canonical_user.strip().casefold()
        if key in policies:
            policy = policies[key]
            return PolicyLookup(policy=policy, matched_via=key)

    # Last resort: the wildcard.
    policy = policies.get(_WILDCARD_USER) or UserProfilePolicy(
        canonical_user=_WILDCARD_USER,
        default_profile="default",
        allowed_profiles=frozenset({"default"}),
        forced_profile=None,
    )
    return PolicyLookup(policy=policy, matched_via=_WILDCARD_USER)
