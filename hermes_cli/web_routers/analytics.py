"""Raw-YAML config and token/cost analytics dashboard routes.

Extracted from ``hermes_cli.web_server``; app state and helpers are late-bound through
:mod:`hermes_cli.web_deps` (cycle-safe, monkeypatch-friendly).
"""

import asyncio
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import yaml
from fastapi import APIRouter, HTTPException, Query

from hermes_cli.config import get_config_path, read_raw_config
from hermes_cli.web_deps import late
from hermes_cli.web_routers._common import corrupt_store_as_status
from hermes_cli.web_server_profiles import (
    _approval_mode_of, _aux_task_summary, _aux_usage_rows, _broadcast_gateway_session_info, _is_other_profile, _merge_aux_into_by_model,
)
from hermes_cli.web_models import RawConfigUpdate

router = APIRouter()
_log = logging.getLogger("hermes_cli.web_server")

# Late-bound so a test's monkeypatch on the owning module wins at call time.
_open_session_db_for_profile = late("_open_session_db_for_profile", "hermes_cli.web_server_sessions")
_open_session_db_at_path = late("_open_session_db_at_path", "hermes_cli.web_server_sessions")
_session_db_path_for_profile = late("_session_db_path_for_profile", "hermes_cli.web_server_sessions")
_profile_scope = late("_profile_scope", "hermes_cli.web_server_profiles")
save_config = late("save_config", "hermes_cli.config")

# ── Raw YAML config ──────────────────────────────────────────────────────────


@router.get("/api/config/raw")
async def get_config_raw(profile: Optional[str] = None):
    """Raw config.yaml text plus its resolved path.

    ``path`` is resolved inside ``_profile_scope`` so the Config page header
    shows the file the switched profile actually reads/writes — /api/status's
    ``config_path`` is machine-global and always reports the dashboard
    process's own profile, which is wrong under the global profile switcher.
    """
    def _run():
        with _profile_scope(profile):
            path = get_config_path()
        if not path.exists():
            return {"yaml": "", "path": str(path)}
        return {"yaml": path.read_text(encoding="utf-8"), "path": str(path)}

    return await asyncio.to_thread(_run)


@router.put("/api/config/raw")
async def update_config_raw(body: RawConfigUpdate, profile: Optional[str] = None):
    def _run():
        parsed = yaml.safe_load(body.yaml_text)
        if not isinstance(parsed, dict):
            raise HTTPException(status_code=400, detail="YAML must be a mapping")
        with _profile_scope(body.profile or profile):
            # Full-document replacement: the editor owns the whole file; never
            # merge omitted sections back from disk.
            # See #62723.
            approvals_mode_changed = _approval_mode_of(parsed) != _approval_mode_of(read_raw_config())
            save_config(parsed, merge_existing=False)
        # Same indicator refresh as the schema-driven save.
        if approvals_mode_changed and not _is_other_profile(body.profile or profile):
            _broadcast_gateway_session_info()
        return {"ok": True}

    try:
        return await asyncio.to_thread(_run)
    except yaml.YAMLError as e:
        raise HTTPException(status_code=400, detail=f"Invalid YAML: {e}")


def _rows(db, sql: str, cutoff: float) -> List[Dict[str, Any]]:
    return [dict(r) for r in db._conn.execute(sql, (cutoff,)).fetchall()]


def _enumerate_profile_state_db_paths(profile: Optional[str] = None) -> List[Path]:
    """Return the state.db paths analytics endpoints should read from.

    The multiplexed gateway writes each profile's sessions to that profile's
    own ``state.db`` (e.g. ``profiles/kyros/state.db``,
    ``profiles/wilnice/state.db``), not the root ``state.db``. The cron
    scheduler and pre-multiplex legacy rows still land in the root DB.

    When the caller passes ``profile``, only that profile's DB is returned.
    When the caller passes ``None``, **all** discoverable profile DBs plus
    the root DB are returned so the analytics rollup reflects every chat
    that hit the gateway (Kyros's WhatsApp + Telegram + Discord + Wilnice's
    inbound + cron + CLI).

    Profiles are read from ``profiles/*/config.yaml`` in
    ``$HERMES_HOME``. Missing or unreadable profile dirs are skipped
    silently — analytics should degrade, not error.
    """
    from hermes_state import _default_db_path

    paths: List[Path] = []
    seen: Set[str] = set()

    root_db = Path(_default_db_path())
    if root_db.exists():
        paths.append(root_db)
        seen.add(str(root_db.resolve()))

    if profile:
        # Explicit profile: only that DB (root_db is already the default
        # when profile is empty/None, so when caller wants a profile we
        # ignore the root unless it IS the profile's home).
        try:
            from hermes_cli.web_server_cron import _cron_profile_home

            _name, prof_home = _cron_profile_home(profile)
            prof_db = Path(prof_home) / "state.db"
            if prof_db.exists() and str(prof_db.resolve()) not in seen:
                paths.append(prof_db)
                seen.add(str(prof_db.resolve()))
        except Exception:
            pass
        return paths

    # No profile filter: enumerate every profiles/<name>/state.db found on
    # disk so a session multiplexed for any profile is visible.
    try:
        profiles_dir = root_db.parent / "profiles"
        if profiles_dir.is_dir():
            for prof_dir in sorted(profiles_dir.iterdir()):
                if not prof_dir.is_dir():
                    continue
                # Skip hidden / system profiles
                if prof_dir.name.startswith("."):
                    continue
                prof_db = prof_dir / "state.db"
                if not prof_db.exists():
                    continue
                resolved = str(prof_db.resolve())
                if resolved in seen:
                    continue
                paths.append(prof_db)
                seen.add(resolved)
    except Exception:
        # Directory enumeration failure — analytics falls back to root only.
        pass
    return paths


def _open_session_db_read_only(path: Path, *, required: bool = False):
    """Open a SessionDB at ``path`` in read-only mode, swallowing stale-schema.

    Reuses ``_open_session_db_at_path`` but returns None on failure so the
    analytics merger can drop a corrupt DB instead of erroring the whole
    endpoint. ``required=True`` (the profile the caller actually asked to view,
    per ``_session_db_path_for_profile``) re-raises instead, so the endpoint's
    ``corrupt_store_as_status`` wrapper can turn a corrupt primary store into a
    503 rather than a silently empty result (#96591) — only the OTHER,
    incidentally-merged-in profile DBs get to degrade quietly.
    """
    try:
        return _open_session_db_at_path(path, read_only=True)
    except Exception as exc:
        if required:
            raise
        _log.warning(
            "analytics: failed to open %s in read-only mode (%s); "
            "skipping that profile's rows", path, exc,
        )
        return None


def _get_usage_analytics(days: int = 30, profile: Optional[str] = None):
    """Aggregate usage analytics across root + per-profile state DBs.

    Historically this read from a single ``state.db``. After the multiplexed
    gateway (#88532), each profile's sessions land in ``profiles/<name>/state.db``
    instead — cron sessions still land in the root DB. A single-DB query
    therefore misses every WhatsApp/Telegram/Discord chat that ran under a
    profile scope (Kyros's WhatsApp traffic, all of Wilnice's traffic, etc.).

    This function enumerates the relevant DBs via
    :func:`_enumerate_profile_state_db_paths`, runs the same SQL against each,
    and merges the results. The merge key for ``by_model`` includes the
    ``billing_provider`` so identical model names across providers do not
    collapse (the per-provider fix already in place for single-DB reads).

    Aux rows, ``daily``, ``totals``, ``by_task`` are simple sums across DBs.
    Skills and tools are reported from the first DB that yields non-empty
    data (InsightsEngine depends on a single store and we don't want
    duplicate / overlapping skill lists to appear in the UI).
    """
    from agent.insights import InsightsEngine
    from hermes_state import _default_db_path

    cutoff = time.time() - (days * 86400)
    db_paths = _enumerate_profile_state_db_paths(profile)
    if not db_paths:
        # Fallback: open the default store so the endpoint never errors
        db_paths = [Path(_default_db_path())]
    primary_db_path = _session_db_path_for_profile(profile)

    # Merged accumulators
    daily_by_day: Dict[str, Dict[str, Any]] = {}
    by_model_map: Dict[Tuple[str, Optional[str]], Dict[str, Any]] = {}
    aux_rows_all: List[Dict[str, Any]] = []
    totals: Dict[str, Any] = {
        "total_input": 0,
        "total_output": 0,
        "total_cache_read": 0,
        "total_reasoning": 0,
        "total_estimated_cost": 0.0,
        "total_actual_cost": 0.0,
        "total_sessions": 0,
        "total_api_calls": 0,
    }
    skills_payload: Optional[Dict[str, Any]] = None
    tools_payload: Optional[List[Any]] = None

    for db_path in db_paths:
        db = _open_session_db_read_only(db_path, required=(db_path.resolve() == primary_db_path.resolve()))
        if db is None:
            continue
        try:
            cur = db._conn.execute(
                """
                SELECT date(started_at, 'unixepoch') as day,
                       SUM(input_tokens) as input_tokens,
                       SUM(output_tokens) as output_tokens,
                       SUM(cache_read_tokens) as cache_read_tokens,
                       SUM(reasoning_tokens) as reasoning_tokens,
                       COALESCE(SUM(estimated_cost_usd), 0) as estimated_cost,
                       COALESCE(SUM(actual_cost_usd), 0) as actual_cost,
                       COUNT(*) as sessions,
                       SUM(COALESCE(api_call_count, 0)) as api_calls
                FROM sessions WHERE started_at > ?
                GROUP BY day ORDER BY day
                """,
                (cutoff,),
            )
            for row in cur.fetchall():
                d = dict(row)
                day = d.get("day")
                if day is None:
                    continue
                bucket = daily_by_day.setdefault(
                    day,
                    {
                        "day": day,
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "cache_read_tokens": 0,
                        "reasoning_tokens": 0,
                        "estimated_cost": 0.0,
                        "actual_cost": 0.0,
                        "sessions": 0,
                        "api_calls": 0,
                    },
                )
                for k in (
                    "input_tokens",
                    "output_tokens",
                    "cache_read_tokens",
                    "reasoning_tokens",
                    "estimated_cost",
                    "actual_cost",
                    "sessions",
                    "api_calls",
                ):
                    bucket[k] += d.get(k) or 0

            cur2 = db._conn.execute(
                """
                SELECT model,
                       billing_provider,
                       SUM(input_tokens) as input_tokens,
                       SUM(output_tokens) as output_tokens,
                       COALESCE(SUM(estimated_cost_usd), 0) as estimated_cost,
                       COUNT(*) as sessions,
                       SUM(COALESCE(api_call_count, 0)) as api_calls
                FROM sessions WHERE started_at > ? AND model IS NOT NULL
                GROUP BY model, billing_provider
                ORDER BY SUM(input_tokens) + SUM(output_tokens) DESC
                """,
                (cutoff,),
            )
            for row in cur2.fetchall():
                d = dict(row)
                key = (d.get("model") or "", d.get("billing_provider"))
                bucket = by_model_map.setdefault(
                    key,
                    {
                        "model": d.get("model"),
                        "billing_provider": d.get("billing_provider"),
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "estimated_cost": 0.0,
                        "sessions": 0,
                        "api_calls": 0,
                    },
                )
                for k in (
                    "input_tokens",
                    "output_tokens",
                    "estimated_cost",
                    "sessions",
                    "api_calls",
                ):
                    bucket[k] += d.get(k) or 0

            try:
                aux_rows = _aux_usage_rows(db, cutoff)
                aux_rows_all.extend(aux_rows)
            except Exception as aux_exc:
                _log.debug(
                    "analytics: aux_usage_rows failed for %s (%s); "
                    "continuing without that DB's aux rows",
                    db_path, aux_exc,
                )

            cur3 = db._conn.execute(
                """
                SELECT COALESCE(SUM(input_tokens), 0) as total_input,
                       COALESCE(SUM(output_tokens), 0) as total_output,
                       COALESCE(SUM(cache_read_tokens), 0) as total_cache_read,
                       COALESCE(SUM(reasoning_tokens), 0) as total_reasoning,
                       COALESCE(SUM(estimated_cost_usd), 0) as total_estimated_cost,
                       COALESCE(SUM(actual_cost_usd), 0) as total_actual_cost,
                       COUNT(*) as total_sessions,
                       COALESCE(SUM(api_call_count), 0) as total_api_calls
                FROM sessions WHERE started_at > ?
                """,
                (cutoff,),
            )
            t = dict(cur3.fetchone())
            for k in (
                "total_input",
                "total_output",
                "total_cache_read",
                "total_reasoning",
                "total_estimated_cost",
                "total_actual_cost",
                "total_sessions",
                "total_api_calls",
            ):
                totals[k] = (totals.get(k) or 0) + (t.get(k) or 0)

            # Skills + tools are read from the first DB whose InsightsEngine
            # returns a non-empty skill list — the insights store is
            # profile-scoped by design and we don't want every profile's
            # list concatenated (would inflate counts).
            if skills_payload is None:
                try:
                    usage = InsightsEngine(db).get_usage_breakdown(days=days)
                    skills_payload = usage.get("skills") or {"items": [], "total": 0}
                    tools_payload = usage.get("tools") or []
                except Exception as usage_exc:
                    _log.debug(
                        "analytics: InsightsEngine failed for %s (%s); "
                        "continuing without skills/tools",
                        db_path, usage_exc,
                    )
        finally:
            try:
                db.close()
            except Exception:
                pass

    daily = sorted(daily_by_day.values(), key=lambda r: r["day"])
    by_model = sorted(
        by_model_map.values(),
        key=lambda r: (r["input_tokens"] or 0) + (r["output_tokens"] or 0),
        reverse=True,
    )
    by_model = _merge_aux_into_by_model(by_model, aux_rows_all)

    return {
        "daily": daily,
        "by_model": by_model,
        "by_task": _aux_task_summary(aux_rows_all),
        "totals": totals,
        "period_days": days,
        "skills": skills_payload or {"items": [], "total": 0},
        "tools": tools_payload or [],
        # Diagnostic: let the UI show which DBs contributed so the operator
        # can confirm multiplexed sessions are being read (was previously
        # invisible — the dashboard only saw cron rows in the root DB).
        "sources": [str(p) for p in db_paths],
    }


@router.get("/api/analytics/usage")
async def get_usage_analytics(
    days: int = Query(30, ge=1, le=365),
    profile: Optional[str] = None,
):
    """``days`` is clamped to 1-365 (idea from #74778): huge or non-positive
    values would force expensive full-history SQL and InsightsEngine work, or
    produce empty/inverted time windows. The UI only offers 7/30/90-day
    presets."""
    with corrupt_store_as_status(_session_db_path_for_profile(profile)):
        return await asyncio.to_thread(_get_usage_analytics, days, profile)


def _aggregate_by_provider(combined_by_model: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Group the multi-DB ``by_model`` rows under their ``billing_provider``.

    Returns a list of provider rollups sorted by total tokens. Empty /
    NULL billing providers are grouped under a synthetic "(unassigned)"
    bucket so anonymous rows still show up in the analytics UI rather than
    being silently dropped.
    """
    bucket: Dict[str, Dict[str, Any]] = {}
    for row in combined_by_model:
        provider = row.get("billing_provider") or "(unassigned)"
        entry = bucket.setdefault(
            provider,
            {
                "billing_provider": provider,
                "display_name": provider,
                "input_tokens": 0,
                "output_tokens": 0,
                "estimated_cost": 0.0,
                "sessions": 0,
                "api_calls": 0,
                "models": [],
                "model_names": [],
            },
        )
        for k in (
            "input_tokens",
            "output_tokens",
            "estimated_cost",
            "sessions",
            "api_calls",
        ):
            entry[k] += row.get(k) or 0
        entry["models"].append(
            {
                "model": row.get("model"),
                "input_tokens": row.get("input_tokens") or 0,
                "output_tokens": row.get("output_tokens") or 0,
                "estimated_cost": row.get("estimated_cost") or 0,
                "sessions": row.get("sessions") or 0,
                "api_calls": row.get("api_calls") or 0,
            }
        )
        if row.get("model"):
            entry["model_names"].append(row["model"])

    for entry in bucket.values():
        entry["models"].sort(
            key=lambda r: (r["input_tokens"] or 0) + (r["output_tokens"] or 0),
            reverse=True,
        )
        entry["model_count"] = len(
            {m.get("model") for m in entry["models"] if m.get("model")}
        )

    result = list(bucket.values())
    result.sort(
        key=lambda r: (r["input_tokens"] or 0) + (r["output_tokens"] or 0),
        reverse=True,
    )
    return result


@router.get("/api/analytics/providers")
async def get_providers_analytics(
    days: int = Query(30, ge=1, le=365),
    profile: Optional[str] = None,
):
    """Per-provider rollup derived from the same multi-DB usage scan.

    Reuses the per-model merge and just buckets rows by ``billing_provider``.
    Same profile filter / clamp behaviour as ``/api/analytics/usage`` —
    ``profile`` None merges every profile DB, a named profile filters to
    its state.db only.
    """
    usage = await asyncio.to_thread(_get_usage_analytics, days, profile)
    return {
        "by_provider": _aggregate_by_provider(usage.get("by_model") or []),
        "period_days": days,
        "sources": usage.get("sources") or [],
    }


def _collect_model_rows_from_db(db, cutoff: float) -> List[Dict[str, Any]]:
    """Run the per-model query and aux-row synthesis for a single DB.

    Returns the raw list of model rows (session-aggregated + aux rows in the
    same shape) so :func:`_get_models_analytics` can merge across DBs before
    running its single-model dedup / provider-collapse pass.
    """
    cur = db._conn.execute("""
        SELECT model,
               billing_provider,
               SUM(input_tokens) as input_tokens,
               SUM(output_tokens) as output_tokens,
               SUM(cache_read_tokens) as cache_read_tokens,
               SUM(reasoning_tokens) as reasoning_tokens,
               COALESCE(SUM(estimated_cost_usd), 0) as estimated_cost,
               COALESCE(SUM(actual_cost_usd), 0) as actual_cost,
               COUNT(*) as sessions,
               SUM(COALESCE(api_call_count, 0)) as api_calls,
               SUM(tool_call_count) as tool_calls,
               MAX(started_at) as last_used_at,
               AVG(input_tokens + output_tokens) as avg_tokens_per_session
        FROM sessions WHERE started_at > ? AND model IS NOT NULL AND model != ''
        GROUP BY model, billing_provider
        ORDER BY SUM(input_tokens) + SUM(output_tokens) DESC
    """, (cutoff,))
    raw_rows = [dict(r) for r in cur.fetchall()]

    for aux in _aux_usage_rows(db, cutoff):
        raw_rows.append({
            "model": aux.get("model") or "unknown",
            "billing_provider": aux.get("billing_provider") or "",
            "input_tokens": aux.get("input_tokens") or 0,
            "output_tokens": aux.get("output_tokens") or 0,
            "cache_read_tokens": aux.get("cache_read_tokens") or 0,
            "reasoning_tokens": aux.get("reasoning_tokens") or 0,
            "estimated_cost": aux.get("estimated_cost") or 0,
            "actual_cost": 0,
            "sessions": aux.get("sessions") or 0,
            "api_calls": aux.get("api_calls") or 0,
            "tool_calls": 0,
            "last_used_at": aux.get("last_used_at"),
            "avg_tokens_per_session": 0,
            "aux_task": aux.get("task") or "",
        })
    return raw_rows


def _merge_model_rows_across_dbs(
    all_rows: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Collapse rows from multiple DBs into the dedup / provider-collapse shape.

    Mirrors the logic that used to live inline in :func:`_get_models_analytics`
    — folds session-only rows into the accounted provider row, then sorts
    descending by total tokens. Idempotent across DBs because the merge key
    is (model, billing_provider).
    """
    rows_by_model: Dict[str, List[Dict[str, Any]]] = {}
    for row in all_rows:
        rows_by_model.setdefault(row.get("model") or "", []).append(row)

    rows: List[Dict[str, Any]] = []
    for model_rows in rows_by_model.values():
        provider_rows = [r for r in model_rows if r.get("billing_provider")]
        if len(provider_rows) == 1:
            target = provider_rows[0]
            for row in model_rows:
                if row is target or row.get("billing_provider"):
                    continue
                has_usage = any(
                    (row.get(key) or 0) != 0
                    for key in (
                        "input_tokens",
                        "output_tokens",
                        "cache_read_tokens",
                        "reasoning_tokens",
                        "estimated_cost",
                        "actual_cost",
                        "api_calls",
                        "tool_calls",
                    )
                )
                if has_usage:
                    continue
                target["sessions"] = (target.get("sessions") or 0) + (row.get("sessions") or 0)
                target["last_used_at"] = max(target.get("last_used_at") or 0, row.get("last_used_at") or 0)
                total_tokens = (target.get("input_tokens") or 0) + (target.get("output_tokens") or 0)
                sessions = target.get("sessions") or 0
                target["avg_tokens_per_session"] = total_tokens / sessions if sessions else 0
            rows.append(target)
            rows.extend(
                r for r in model_rows
                if r is not target
                and (r.get("billing_provider") or any(
                    (r.get(key) or 0) != 0
                    for key in (
                        "input_tokens",
                        "output_tokens",
                        "cache_read_tokens",
                        "reasoning_tokens",
                        "estimated_cost",
                        "actual_cost",
                        "api_calls",
                        "tool_calls",
                    )
                ))
            )
        else:
            rows.extend(model_rows)

    rows.sort(
        key=lambda r: (r.get("input_tokens") or 0) + (r.get("output_tokens") or 0),
        reverse=True,
    )
    return rows


def _get_models_analytics(days: int = 30, profile: Optional[str] = None):
    """Rich per-model analytics for the Models dashboard page.

    Returns token/cost/session breakdown per model plus capability metadata
    from models.dev (context window, vision, tools, reasoning, etc.).

    Same multi-DB pattern as :func:`_get_usage_analytics`: each profile's
    state.db is queried separately, the per-DB rows are deduped with the
    standard session-only-row fold, and the totals are summed across DBs.
    """
    from hermes_state import _default_db_path

    cutoff = time.time() - (days * 86400)
    db_paths = _enumerate_profile_state_db_paths(profile)
    if not db_paths:
        db_paths = [Path(_default_db_path())]
    primary_db_path = _session_db_path_for_profile(profile)

    all_rows: List[Dict[str, Any]] = []
    totals = {
        "distinct_models": 0,
        "total_input": 0,
        "total_output": 0,
        "total_cache_read": 0,
        "total_reasoning": 0,
        "total_estimated_cost": 0.0,
        "total_actual_cost": 0.0,
        "total_sessions": 0,
        "total_api_calls": 0,
    }

    for db_path in db_paths:
        db = _open_session_db_read_only(db_path, required=(db_path.resolve() == primary_db_path.resolve()))
        if db is None:
            continue
        try:
            try:
                all_rows.extend(_collect_model_rows_from_db(db, cutoff))
            except Exception as qexc:
                _log.debug(
                    "models: per-DB query failed for %s (%s); "
                    "skipping that profile's rows",
                    db_path, qexc,
                )
                continue

            totals_cur = db._conn.execute("""
                SELECT COUNT(DISTINCT model) as distinct_models,
                       SUM(input_tokens) as total_input,
                       SUM(output_tokens) as total_output,
                       SUM(cache_read_tokens) as total_cache_read,
                       SUM(reasoning_tokens) as total_reasoning,
                       COALESCE(SUM(estimated_cost_usd), 0) as total_estimated_cost,
                       COALESCE(SUM(actual_cost_usd), 0) as total_actual_cost,
                       COUNT(*) as total_sessions,
                       SUM(COALESCE(api_call_count, 0)) as total_api_calls
                FROM sessions WHERE started_at > ? AND model IS NOT NULL AND model != ''
            """, (cutoff,))
            t = dict(totals_cur.fetchone())
            # distinct_models is COUNT(DISTINCT) per DB; the unioned answer
            # is captured later from the merged rows so we skip summing it.
            for k in (
                "total_input",
                "total_output",
                "total_cache_read",
                "total_reasoning",
                "total_estimated_cost",
                "total_actual_cost",
                "total_sessions",
                "total_api_calls",
            ):
                totals[k] = (totals.get(k) or 0) + (t.get(k) or 0)
        finally:
            try:
                db.close()
            except Exception:
                pass

    rows = _merge_model_rows_across_dbs(all_rows)
    totals["distinct_models"] = sum(
        1 for r in rows if r.get("billing_provider")
    )

    models = []
    for row in rows:
        provider = row.get("billing_provider") or ""
        model_name = row["model"]
        caps = {}
        try:
            from agent.models_dev import get_model_capabilities
            mc = get_model_capabilities(provider=provider, model=model_name)
            if mc is not None:
                caps = {
                    "supports_tools": mc.supports_tools,
                    "supports_vision": mc.supports_vision,
                    "supports_reasoning": mc.supports_reasoning,
                    "context_window": mc.context_window,
                    "max_output_tokens": mc.max_output_tokens,
                    "model_family": mc.model_family,
                }
        except Exception:
            pass

        models.append({
            "model": model_name,
            "provider": provider,
            "input_tokens": row["input_tokens"],
            "output_tokens": row["output_tokens"],
            "cache_read_tokens": row["cache_read_tokens"],
            "reasoning_tokens": row["reasoning_tokens"],
            "estimated_cost": row["estimated_cost"],
            "actual_cost": row["actual_cost"],
            "sessions": row["sessions"],
            "api_calls": row["api_calls"],
            "tool_calls": row["tool_calls"],
            "last_used_at": row["last_used_at"],
            "avg_tokens_per_session": row["avg_tokens_per_session"],
            "capabilities": caps,
        })

    return {
        "models": models,
        "totals": totals,
        "period_days": days,
        "sources": [str(p) for p in db_paths],
    }


@router.get("/api/analytics/models")
async def get_models_analytics(
    days: int = Query(30, ge=1, le=365),
    profile: Optional[str] = None,
):
    """Return model analytics without blocking the serving event loop."""
    with corrupt_store_as_status(_session_db_path_for_profile(profile)):
        return await asyncio.to_thread(_get_models_analytics, days, profile)
