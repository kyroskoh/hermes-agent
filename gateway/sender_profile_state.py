"""Per-sender active-profile persistence.

Stores a single ``profile`` name per ``(platform, chat_id)`` row in a
sidecar SQLite database at ``<hermes_home>/profiles/sender_profile.db``.
This is intentionally NOT in the canonical ``state.db`` — the per-sender
profile is a gateway-only state, not a session message.

Schema (single table, idempotent CREATE):

    CREATE TABLE IF NOT EXISTS sender_profile_active (
        platform  TEXT NOT NULL,
        chat_id   TEXT NOT NULL,
        user_id   TEXT,
        profile   TEXT NOT NULL,
        updated_at REAL NOT NULL,
        PRIMARY KEY (platform, chat_id)
    );

Concurrency:

- Single-process gateway writers only; SQLite WAL mode is enabled so a
  dashboard ``GET`` does not block an inbound message write.
- All reads return a ``str`` profile name or ``None`` when absent.

Backward compatibility:

- Operators without this file just see ``get_active_profile() -> None``
  and the policy defaults take over. There is no migration.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path
from typing import Optional

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)


_DB_FILENAME = "sender_profile.db"
_TABLE = "sender_profile_active"


def _db_path() -> Path:
    base = get_hermes_home()
    profiles_dir = base / "profiles"
    profiles_dir.mkdir(parents=True, exist_ok=True)
    return profiles_dir / _DB_FILENAME


def _connect() -> sqlite3.Connection:
    """Open a connection in WAL mode with row factory."""
    path = _db_path()
    conn = sqlite3.connect(str(path), timeout=5.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
    except sqlite3.DatabaseError as exc:  # pragma: no cover
        logger.warning("sender_profile DB pragma failed (%s); continuing.", exc)
    _ensure_schema(conn)
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        f"""
        CREATE TABLE IF NOT EXISTS {_TABLE} (
            platform   TEXT NOT NULL,
            chat_id    TEXT NOT NULL,
            user_id    TEXT,
            profile    TEXT NOT NULL,
            updated_at REAL NOT NULL,
            PRIMARY KEY (platform, chat_id)
        );

        CREATE INDEX IF NOT EXISTS idx_{_TABLE}_user
            ON {_TABLE}(platform, user_id);
        """
    )


def _profile_dir_exists(name: str) -> bool:
    """True when a profile directory exists on disk for *name*.

    ``default`` lives at the Hermes home root and has ``SOUL.md`` there.
    Other profiles live at ``<hermes_home>/profiles/<name>/``.
    """
    base = get_hermes_home()
    if name == "default":
        return (base / "SOUL.md").exists()
    return (base / "profiles" / name).is_dir()


def _validate_profile(value: str) -> Optional[str]:
    """Normalise, sanitise, AND require the profile to exist on disk.

    Unlike the upstream ``hermes_cli.profiles.validate_profile_name``
    (which is a format/path-traversal guard), this guard rejects names
    that have no on-disk ``SOUL.md``. Operators cannot accidentally
    persist a profile that does not exist.
    """
    if not isinstance(value, str):
        return None
    v = value.strip()
    if not v:
        return None
    try:
        from hermes_cli.profiles import normalize_profile_name, validate_profile_name

        v = normalize_profile_name(v)
        validate_profile_name(v)
    except (ValueError, ImportError):
        return None
    if not _profile_dir_exists(v):
        return None
    return v


def get_active_profile(
    platform: Optional[str],
    chat_id: Optional[str],
) -> Optional[str]:
    """Return the saved profile for ``(platform, chat_id)`` or ``None``."""
    if not platform or not chat_id:
        return None
    try:
        conn = _connect()
    except sqlite3.DatabaseError as exc:  # pragma: no cover
        logger.warning("sender_profile DB open failed (%s); returning None.", exc)
        return None
    try:
        row = conn.execute(
            f"SELECT profile FROM {_TABLE} WHERE platform = ? AND chat_id = ?",
            (str(platform), str(chat_id)),
        ).fetchone()
        if row is None:
            return None
        return row["profile"]
    finally:
        conn.close()


def set_active_profile(
    platform: Optional[str],
    chat_id: Optional[str],
    user_id: Optional[str],
    profile: Optional[str],
) -> bool:
    """Persist (or clear) the active profile for ``(platform, chat_id)``.

    ``profile=None`` deletes the row. Returns True when the side-effect
    landed, False when the call was a no-op (missing args, invalid name,
    or DB error).
    """
    if not platform or not chat_id:
        return False
    if profile is None:
        return _delete_row(platform, chat_id)
    normalised = _validate_profile(profile)
    if not normalised:
        return False
    try:
        conn = _connect()
    except sqlite3.DatabaseError as exc:  # pragma: no cover
        logger.warning("sender_profile DB open failed (%s); write skipped.", exc)
        return False
    try:
        conn.execute(
            f"""
            INSERT INTO {_TABLE} (platform, chat_id, user_id, profile, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(platform, chat_id) DO UPDATE SET
                user_id    = excluded.user_id,
                profile    = excluded.profile,
                updated_at = excluded.updated_at
            """,
            (
                str(platform),
                str(chat_id),
                str(user_id) if user_id else None,
                normalised,
                time.time(),
            ),
        )
        return True
    finally:
        conn.close()


def _delete_row(platform: str, chat_id: str) -> bool:
    try:
        conn = _connect()
    except sqlite3.DatabaseError as exc:  # pragma: no cover
        logger.warning("sender_profile DB open failed (%s); delete skipped.", exc)
        return False
    try:
        conn.execute(
            f"DELETE FROM {_TABLE} WHERE platform = ? AND chat_id = ?",
            (str(platform), str(chat_id)),
        )
        return True
    finally:
        conn.close()


def clear_active_profile(platform: Optional[str], chat_id: Optional[str]) -> bool:
    """Delete the persisted profile for ``(platform, chat_id)``."""
    if not platform or not chat_id:
        return False
    return _delete_row(platform, chat_id)


def list_active_profiles() -> list[dict]:
    """Return every persisted sender profile. Used by the dashboard."""
    try:
        conn = _connect()
    except sqlite3.DatabaseError as exc:  # pragma: no cover
        logger.warning("sender_profile DB open failed (%s); list skipped.", exc)
        return []
    try:
        rows = conn.execute(
            f"SELECT platform, chat_id, user_id, profile, updated_at FROM {_TABLE} ORDER BY updated_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()
