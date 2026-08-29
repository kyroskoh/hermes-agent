#!/usr/bin/env python3
"""
Honcho peer roster scanner.

Runs Steps 2-4 of the honcho-peer-roster skill:

  2. POST /v3/workspaces/{ws}/peers/list  → full peer list
  3. Partition against the known allowlist
  4. For each fresh peer, pull first 3 inbound messages from state.db

Usage:
  python3 scan_peers.py                     # default workspace=hermes, days=7
  python3 scan_peers.py --workspace foo     # different workspace
  python3 scan_peers.py --days 30           # widen the freshness window
  python3 scan_peers.py --json              # machine-readable output
  python3 scan_peers.py --state-db /path    # point at a specific state.db

Honcho endpoint is hard-coded to http://localhost:8000 — this is for Kyros's
local deployment. Adjust HONCHO_URL below for other environments.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import urllib.request
from datetime import datetime, timezone

HONCHO_URL = "http://localhost:8000"
DEFAULT_WORKSPACE = "hermes"
DEFAULT_DAYS = 7

# Per-deployment allowlist. Edit for your fleet.
KNOWN = {
    # Internal AI peers
    "kyroskoh_bot", "WilniceBot", "KyrosBot",
    # Operator + girlfriend human peers (canonical names)
    "Kyros", "Wilnice",
    # Webhook peers
    "webhook-github-prs", "webhook-github-pr-review",
}

# Identity aliasing: one human can have many Honcho peer IDs (phone JID,
# WhatsApp privacy-LID, Telegram UID, Discord UID). Map them here.
KNOWN_ALIASES = {
    "Kyros": {
        "phones":   ["6580323587"],
        "lids":     [
            "5927843410163-lid",              # main WhatsApp privacy-LID
            "199999480688782-lid",            # secondary phone (Vivo x70 Pro+)
            "188661404582023-lid",            # platform=whatsapp, operator (secondary WhatsApp LID; primary is 5927843410163-lid)
        ],
    },
    "Wilnice": {
        "phones":   ["6581103465"],
        "telegram": ["7233071505"],
        "lids":     ["171666202210553-lid"], # WhatsApp privacy-LID for Wilnice
    },
    "Wai Loong": {
        "lids":     ["226576889331767-lid"], # kawaii-personality trigger peer
    },
    "Bille": {
        "lids":     ["113048287211723-lid"], # cosplay partner, KL trip companion
    },
    "Stephy": {
        "lids":     ["266356792533046-lid"], # new contact, first chatted 2026-08-28
    },
    "Shiva": {
        "lids":     ["267056872190171-lid"], # ex-colleague, introduced by Kyros 2026-08-28
    },
    "Kingsfield": {
        "lids":     ["62758129242347-lid"],  # new WhatsApp contact, first chatted 2026-08-28
    },
}

def is_known(pid: str) -> bool:
    if pid in KNOWN:
        return True
    for aliases in KNOWN_ALIASES.values():
        for ids in aliases.values():
            if pid in ids:
                return True
    return False


def list_peers(workspace: str) -> list[dict]:
    """POST /v3/workspaces/{ws}/peers/list — body must be {}."""
    url = f"{HONCHO_URL}/v3/workspaces/{workspace}/peers/list"
    req = urllib.request.Request(
        url,
        data=b"{}",
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        body = json.load(r)
    return body.get("items", body) if isinstance(body, dict) else body


def first_inbound(state_db: str, peer_id: str, limit: int = 3) -> list[tuple]:
    """
    Surface context about a peer from state.db.

    Lookup order:
      1. sessions table: match by user_id OR chat_id for the @lid-suffixed form.
         Returns display_name + started_at + message_count.
      2. messages table: substring search by peer_id (catches older formats).
    """
    if not os.path.exists(state_db):
        return []
    conn = sqlite3.connect(state_db)
    conn.row_factory = sqlite3.Row
    try:
        results: list[dict] = []
        # Try @lid-suffixed variants — state.db stores them as 113048287211723@lid
        for candidate in (peer_id, f"{peer_id}@lid", peer_id.replace("-lid", "@lid")):
            rows = conn.execute(
                """
                SELECT id, source, user_id, chat_id, display_name, started_at, message_count
                FROM sessions
                WHERE user_id = ? OR chat_id = ?
                ORDER BY started_at DESC
                LIMIT 5
                """,
                (candidate, candidate),
            ).fetchall()
            for r in rows:
                d = dict(r)
                d["_via"] = "sessions"
                results.append(d)

        # Fall back: substring search of messages
        rows = conn.execute(
            """
            SELECT timestamp, session_id, content
            FROM messages
            WHERE role = 'user'
              AND (session_id LIKE ? OR content LIKE ?)
            ORDER BY timestamp ASC
            LIMIT ?
            """,
            (f"%{peer_id}%", f"%{peer_id}%", limit),
        ).fetchall()
        for r in rows:
            d = dict(r)
            d["_via"] = "messages"
            results.append(d)

        # Deduplicate by session_id where possible
        seen = set()
        out = []
        for r in results:
            key = r.get("id") or r.get("session_id")
            if key in seen:
                continue
            seen.add(key)
            out.append(r)
        return out
    finally:
        conn.close()


def resolve_human(pid: str) -> str | None:
    """Return the canonical human name if this peer ID is an alias of one."""
    for human, aliases in KNOWN_ALIASES.items():
        for ids in aliases.values():
            if pid in ids:
                return human
    return None


def partition(peers: list[dict], days: int) -> tuple[list, list, list]:
    now = datetime.now(timezone.utc)
    known, fresh, unidentified = [], [], []
    for p in peers:
        pid = p["id"]
        created = p.get("created_at", "")
        try:
            created_dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
        except ValueError:
            created_dt = now
        age_days = (now - created_dt).total_seconds() / 86400
        has_name = bool(p.get("metadata", {}).get("display_name"))

        if is_known(pid):
            known.append((p, age_days, has_name, resolve_human(pid)))
        elif age_days <= days or not has_name:
            fresh.append((p, age_days, has_name, None))
        else:
            unidentified.append((p, age_days, has_name, None))
    return known, fresh, unidentified


def fmt_plain(known, fresh, unidentified, state_dbs, days):
    print(f"HONCHO PEER ROSTER — workspace peers={len(known)+len(fresh)+len(unidentified)} "
          f"known={len(known)} fresh(≤{days}d)={len(fresh)} unidentified={len(unidentified)}\n")

    # Group known by canonical human for readability
    print("KNOWN PEERS")
    by_human = {"_system_": []}
    for p, age, has_name, human in sorted(known, key=lambda x: x[0].get("created_at", "")):
        by_human.setdefault(human or "_system_", []).append(p)
    for human, plist in sorted(by_human.items()):
        if human == "_system_":
            for p in plist:
                name = p.get("metadata", {}).get("display_name", "") or "-"
                print(f"  [system]  {p['id']:<35} {p.get('created_at','')[:19]}  display_name={name}")
        else:
            print(f"  {human}:")
            for p in plist:
                name = p.get("metadata", {}).get("display_name", "") or "-"
                kind = "phone" if p["id"].isdigit() else (
                    "lid" if p["id"].endswith("-lid") else (
                    "telegram" if p["id"].isdigit() and len(p["id"]) > 10 else "other"))
                print(f"      [{kind:<8}] {p['id']:<35} {p.get('created_at','')[:19]}  display_name={name}")

    print(f"\nFRESH PEERS (last {days} days, or no display_name)")
    for p, age, has_name, _ in fresh:
        print(f"  {p['id']:<35} {p.get('created_at','')[:19]}  age={age:.1f}d")
        # Try to pull context from each state.db
        ctx = []
        for db in state_dbs:
            ctx.extend(first_inbound(db, p["id"], limit=1))
        # dedupe by session id / message id
        seen = set()
        unique = []
        for c in ctx:
            k = c.get("id") or c.get("session_id")
            if k in seen:
                continue
            seen.add(k)
            unique.append(c)

        shown_sessions = 0
        for c in unique:
            if c.get("_via") == "sessions":
                ts = datetime.fromtimestamp(c["started_at"], tz=timezone.utc).isoformat()[:19]
                print(f"    [{ts}] session={c['id'][:50]}  msgs={c['message_count']}  "
                      f"display_name={c.get('display_name') or '-'}")
                shown_sessions += 1
                if shown_sessions >= 2:
                    break
            elif c.get("_via") == "messages":
                snippet = (c["content"] or "")[:120].replace("\n", " ")
                ts = datetime.fromtimestamp(c["timestamp"], tz=timezone.utc).isoformat()[:19]
                print(f"    [{ts}] session={c['session_id'][:50]}  \"{snippet}\"")
                shown_sessions += 1
                if shown_sessions >= 2:
                    break
        if shown_sessions == 0:
            print(f"    No inbound context found in state.db — NEEDS_OPERATOR_INPUT")

    print(f"\nUNIDENTIFIED (older, no display_name)")
    for p, age, has_name, _ in unidentified:
        print(f"  {p['id']:<35} {p.get('created_at','')[:19]}  age={age:.1f}d  NEEDS_OPERATOR_INPUT")


def fmt_json(peers, known, fresh, unidentified, state_dbs):
    out = {
        "total": len(peers),
        "known": [],
        "fresh": [],
        "unidentified": [],
    }
    # group known by human
    by_human = {"_system_": []}
    for p, age, has_name, human in known:
        by_human.setdefault(human or "_system_", []).append(p)
    for human, plist in by_human.items():
        for p in plist:
            out["known"].append({
                "id": p["id"],
                "canonical_human": None if human == "_system_" else human,
                "created_at": p.get("created_at"),
                "display_name": p.get("metadata", {}).get("display_name", ""),
            })
    for p, age, has_name, _ in fresh:
        ctx = []
        for db in state_dbs:
            ctx.extend(first_inbound(db, p["id"], limit=3))
        seen = set()
        unique_ctx = []
        for c in ctx:
            k = c.get("id") or c.get("session_id")
            if k in seen:
                continue
            seen.add(k)
            unique_ctx.append(c)
        out["fresh"].append({
            "id": p["id"],
            "created_at": p["created_at"],
            "age_days": round(age, 1),
            "context": [
                {
                    "via": c.get("_via"),
                    "session_id": c.get("id") or c.get("session_id"),
                    "started_at": c.get("started_at") or c.get("timestamp"),
                    "display_name": c.get("display_name"),
                    "message_count": c.get("message_count"),
                    "content": (c.get("content") or "")[:500],
                }
                for c in unique_ctx[:5]
            ],
        })
    for p, age, has_name, _ in unidentified:
        out["unidentified"].append({
            "id": p["id"],
            "created_at": p.get("created_at"),
            "age_days": round(age, 1),
        })
    print(json.dumps(out, indent=2, default=str))


def main():
    ap = argparse.ArgumentParser(description="Honcho peer roster scanner")
    ap.add_argument("--workspace", default=DEFAULT_WORKSPACE)
    ap.add_argument("--days", type=int, default=DEFAULT_DAYS, help="freshness window")
    ap.add_argument("--state-db", action="append", help="state.db path (repeatable)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()

    state_dbs = args.state_db or [
        "/root/.hermes/state.db",
        "/root/.hermes/profiles/kyros/state.db",
        "/root/.hermes/profiles/wilnice/state.db",
    ]
    state_dbs = [d for d in state_dbs if os.path.exists(d)]
    if not state_dbs:
        print("WARNING: no state.db files found, skipping inbound-message enrichment", file=sys.stderr)

    peers = list_peers(args.workspace)
    known, fresh, unidentified = partition(peers, args.days)

    if args.json:
        fmt_json(peers, known, fresh, unidentified, state_dbs)
    else:
        fmt_plain(known, fresh, unidentified, state_dbs, args.days)


if __name__ == "__main__":
    main()
