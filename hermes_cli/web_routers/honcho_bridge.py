"""Honcho bridging — read-only endpoints that let the operator's dashboard
correlate Honcho peer/session state with the corresponding state.db row.

The Honcho REST API is unauthenticated on localhost (local Honcho at
http://127.0.0.1:8000, workspace "hermes"). The match heuristic for
``state_db_match`` is intentionally conservative:
  1. chat_id == chat_id AND same profile's state.db
  2. within a ±60 minute window of the Honcho session's created_at
  3. prefer the session whose started_at is closest to created_at
Operator-side: never send third-party chat content through here — this
endpoint is operator-scoped (admin only) and is intended for accuracy /
debugging work, not for surfacing one peer's messages to another.

Extracted from ``hermes_cli.web_server``; app state and helpers are late-bound through
:mod:`hermes_cli.web_deps` (cycle-safe, monkeypatch-friendly).
"""

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Query

from hermes_cli.web_routers.analytics import _enumerate_profile_state_db_paths, _open_session_db_read_only

router = APIRouter()

_HONCHO_DEFAULT_BASE = "http://127.0.0.1:8000"
_HONCHO_DEFAULT_WORKSPACE = "hermes"
_HONCHO_MATCH_WINDOW_SECONDS = 3600  # ±60 min


def _honcho_fetch(base: str, workspace: str, path: str, *, method: str = "POST", body: Optional[dict] = None) -> Optional[Any]:
    """Best-effort fetch from the local Honcho REST API. Returns None on any error."""
    import urllib.request
    import urllib.error
    try:
        data = json.dumps(body or {}).encode()
        req = urllib.request.Request(
            f"{base.rstrip('/')}{path}",
            data=data if method == "POST" else None,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=8) as r:
            raw = r.read()
        try:
            return json.loads(raw)
        except Exception:
            return None
    except Exception:
        return None


def urllib_parse_quote(value: str) -> str:
    """Local alias to avoid pulling urllib.parse at module import time."""
    import urllib.parse
    return urllib.parse.quote(value, safe="")


def _honcho_sessions_for_peer(base: str, workspace: str, peer: str, limit: int) -> List[Dict[str, Any]]:
    """Fetch a peer's Honcho sessions, newest-first.

    Returns the raw item list from the REST API; each item has ``id``,
    ``created_at`` (ISO 8601), ``is_active``, and ``metadata`` (a JSON
    blob including ``owner_peer`` and ``chat_id`` when present).
    """
    if not peer:
        return []
    payload = _honcho_fetch(
        base, workspace,
        f"/v3/workspaces/{workspace}/peers/{urllib_parse_quote(peer)}/sessions",
        body={},
    )
    if not isinstance(payload, dict):
        return []
    items = payload.get("items") or []
    if not isinstance(items, list):
        return []
    items.sort(key=lambda s: s.get("created_at") or "", reverse=True)
    return items[:max(1, min(int(limit or 25), 200))]


def _honcho_extract_chat_id(session_id: str, metadata: Optional[Dict[str, Any]]) -> str:
    """Best-effort: recover the gateway chat_id from a Honcho session.

    Gateway-written session ids encode the chat_id in their tail, e.g.
    ``agent-main-whatsapp-dm-171666202210553`` → ``171666202210553``.
    ``agent-main-telegram-dm-7233071505`` → ``7233071505``.
    The dashboard / state.db stores the canonical ``<id>@<suffix>`` form
    (e.g. ``171666202210553@lid``) — we still need to LIKE-match, not
    equality-match.
    """
    sid = (session_id or "").strip()
    if not sid:
        return ""
    # Strip the leading ``agent-<profile>-<platform>-`` prefix; the chat id
    # is whatever remains after the last hyphen. Telegram uses numeric user
    # ids, WhatsApp uses phone numbers / @lid long-ids, Discord/Slack use
    # alphanumerics — all are valid match candidates for a LIKE in state.db.
    if sid.startswith("agent-"):
        # Strip known channel prefixes
        for prefix in ("agent-main-whatsapp-dm-", "agent-main-whatsapp-",
                       "agent-main-telegram-dm-", "agent-main-telegram-",
                       "agent-main-discord-dm-", "agent-main-discord-",
                       "agent-main-slack-dm-", "agent-main-slack-",
                       "agent-kyros-whatsapp-dm-", "agent-wilnice-whatsapp-dm-",
                       "agent-kyros-telegram-dm-", "agent-wilnice-telegram-dm-"):
            if sid.startswith(prefix):
                return sid[len(prefix):]
        # Fallback: last hyphen-separated segment
        if "-" in sid:
            return sid.rsplit("-", 1)[-1]
    return sid


def _state_db_match_for_chat(chat_id: str, honcho_created_at: Optional[str], *, honcho_session_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Find the closest state.db session row for ``chat_id`` in any profile DB.

    Returns a small dict with id, source, profile_name, db_path, started_at
    when a row is found; otherwise None. Used by the Honcho UI panel so the
    operator can navigate from a Honcho session to the matching Hermes row.
    """
    if not chat_id:
        return None

    cutoff_dt = None
    if honcho_created_at:
        try:
            cutoff_dt = datetime.fromisoformat(honcho_created_at.replace("Z", "+00:00"))
        except Exception:
            cutoff_dt = None

    best: Optional[Tuple[float, Dict[str, Any]]] = None
    for db_path in _enumerate_profile_state_db_paths():
        db = _open_session_db_read_only(db_path)
        if db is None:
            continue
        try:
            # Hermes gateway multiplexed sessions carry their state.db id as
            # the Honcho session id verbatim (e.g. ``20260822_135536_a2940a``),
            # so the simplest reliable match is by ``sessions.id``. The
            # ``chat_id`` LIKE is the fallback for older sessions where the
            # gateway wrote a different id scheme but the chat_id is still
            # populated on the row.
            sql = """
                SELECT id, source, user_id, session_key, chat_id, profile_name,
                       display_name, model, started_at, ended_at, end_reason,
                       message_count
                FROM sessions
                WHERE 1=1
            """
            params: List[Any] = []
            clauses: List[str] = []
            if honcho_session_id:
                clauses.append("id = ?")
                params.append(honcho_session_id)
            if chat_id:
                clauses.append("(chat_id = ? OR chat_id LIKE ?)")
                params.extend([chat_id, f"%{chat_id}%"])
            if clauses:
                sql += " AND (" + " OR ".join(clauses) + ")"
            sql += " ORDER BY started_at DESC LIMIT 25"

            cur = db._conn.execute(sql, params)
            for row in cur.fetchall():
                d = dict(row)
                ts = d.get("started_at") or 0
                if cutoff_dt is not None:
                    ts_dt = datetime.fromtimestamp(ts, tz=timezone.utc)
                    delta = abs((ts_dt - cutoff_dt).total_seconds())
                    if delta > _HONCHO_MATCH_WINDOW_SECONDS:
                        continue
                    score = delta
                else:
                    score = 0.0
                if best is None or score < best[0]:
                    best = (score, {
                        "id": d.get("id"),
                        "source": d.get("source"),
                        "user_id": d.get("user_id"),
                        "session_key": d.get("session_key"),
                        "chat_id": d.get("chat_id"),
                        "profile_name": d.get("profile_name"),
                        "display_name": d.get("display_name"),
                        "model": d.get("model"),
                        "started_at": ts,
                        "ended_at": d.get("ended_at"),
                        "end_reason": d.get("end_reason"),
                        "message_count": d.get("message_count"),
                        "db_path": str(db_path),
                    })
        finally:
            try:
                db.close()
            except Exception:
                pass
    return best[1] if best else None


@router.get("/api/honcho/sessions")
async def honcho_sessions(
    peer: str = Query(..., description="Honcho peer id (e.g. 'Wilnice', 'Kyros')"),
    limit: int = Query(25, ge=1, le=200),
    base: Optional[str] = Query(None, description="Honcho base URL (default local)"),
    workspace: Optional[str] = Query(None, description="Honcho workspace (default 'hermes')"),
    match_state_db: bool = Query(True, description="Link each Honcho session to its state.db row"),
):
    """Honcho sessions for a peer with optional state.db correlation.

    Used by the dashboard's Honcho bridge page so the operator can see a
    unified view of one peer's activity across Honcho's semantic memory
    store and Hermes's durable session DB.
    """
    honcho_base = base or _HONCHO_DEFAULT_BASE
    honcho_ws = workspace or _HONCHO_DEFAULT_WORKSPACE
    sessions = _honcho_sessions_for_peer(honcho_base, honcho_ws, peer, limit)

    out = []
    for s in sessions:
        metadata = s.get("metadata") or {}
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except Exception:
                metadata = {}
        # Honcho stores chat_id encoded in the session id (e.g.
        # ``agent-main-whatsapp-dm-171666202210553``); explicit metadata
        # ``chat_id`` wins, otherwise we recover it from the suffix.
        chat_id = (
            metadata.get("chat_id")
            or metadata.get("gateway_chat_id")
            or _honcho_extract_chat_id(s.get("id") or "", metadata)
        )
        state_match = None
        if match_state_db:
            try:
                state_match = _state_db_match_for_chat(
                    chat_id or "",
                    s.get("created_at"),
                    honcho_session_id=s.get("id"),
                )
            except Exception:
                state_match = None
        out.append({
            "honcho_session_id": s.get("id"),
            "peer": peer,
            "created_at": s.get("created_at"),
            "is_active": s.get("is_active"),
            "metadata": metadata,
            "state_db_match": state_match,
        })

    return {
        "peer": peer,
        "honcho_base": honcho_base,
        "workspace": honcho_ws,
        "sessions": out,
        "matched_count": sum(1 for o in out if o["state_db_match"] is not None),
        "total_count": len(out),
    }


@router.get("/api/honcho/peers")
async def honcho_peers(
    base: Optional[str] = Query(None),
    workspace: Optional[str] = Query(None),
):
    """Honcho peers for the dashboard Honcho panel.

    Mirrors the skill honcho-session-retrieval's session listing so the UI
    doesn't have to know the Honcho REST shape.
    """
    honcho_base = base or _HONCHO_DEFAULT_BASE
    honcho_ws = workspace or _HONCHO_DEFAULT_WORKSPACE
    payload = _honcho_fetch(
        honcho_base, honcho_ws,
        f"/v3/workspaces/{honcho_ws}/peers/list",
        body={},
    )
    if not isinstance(payload, dict):
        return {"honcho_base": honcho_base, "workspace": honcho_ws, "peers": []}
    items = payload.get("items") or []
    return {
        "honcho_base": honcho_base,
        "workspace": honcho_ws,
        "peers": [
            {"id": p.get("id"), "created_at": p.get("created_at"), "metadata": p.get("metadata")}
            for p in items if isinstance(p, dict)
        ],
    }
