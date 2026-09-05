"""Centralized SQLite connection factory + health check primitives.

This module solves Sections C, F, and J from the state-db-reliability
design by owning the rules for *every* SQLite connection Hermes opens:

- Apply a safe default PRAGMA set on connect (WAL, FULL, foreign_keys=ON,
  busy_timeout, temp_store=MEMORY).
- Block concurrent writer opens from the same process (refuses to silently
  double-write to the same path).
- Surface the existing ``agent/db_maintenance.assert_writer_safe`` check at
  open time, so the gateway can never write to a database whose maintenance
  marker is held by another process.
- Provide the wrapper functions used by ``agent/db_health.classify`` and
  by the CLI: ``quick_check``, ``integrity_check``, ``foreign_key_check``,
  ``wal_checkpoint``, ``vacuum_into``, ``fts_integrity_check``.

This module is stdlib-only. ``agent/db_maintenance`` is the only in-tree
dependency. The module is deliberately importable from a ``systemd-run
--scope`` transient unit so preflight checks can run before the gateway
loads.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterator, Optional

import agent.db_maintenance as dbm

logger = logging.getLogger(__name__)


# Per-process writer registry: path -> open connection. Prevents a single
# Python process from opening two writer connections to the same DB (which
# would be the surest way to corrupt state.db through interleaved commits).
# Reader connections are tracked separately and may have multiple.
_WRITER_SINGLETON: dict[str, "ManagedConnection"] = {}
_WRITER_SINGLETON_LOCK = threading.Lock()
_READER_LOCK = threading.RLock()

# Defaults — deliberately conservative. The hardened PRAGMAs were chosen
# after observing the live state.db has synchronous=FULL (good),
# journal_mode=WAL (good), but foreign_keys=0 (bad — the cause of 99 live
# orphan rows) and temp_store=0 (default; benign but more disk churn).
DEFAULT_PRAGMAS = (
    ("journal_mode", "WAL"),
    ("synchronous", "FULL"),
    ("foreign_keys", "ON"),
    ("busy_timeout", "5000"),
    ("temp_store", "MEMORY"),
)


class WriterAlreadyOpen(RuntimeError):
    """A writer connection to this DB is already open in this process."""


class MaintenanceHeld(RuntimeError):
    """Refused to open because maintenance lock is held elsewhere."""


def _assert_writer_safe_or_raise(state_db_path: Path) -> None:
    """Bail out early if maintenance is currently active.

    We don't acquire the shared lock here (the writer will hold the DB open
    for its whole lifetime); we just probe ``flock LOCK_SH | LOCK_NB`` to
    refuse to write while maintenance is active.
    """
    lock_path = dbm.maintenance_lock_path(state_db_path)
    if not lock_path.exists():
        return
    # If the lock file exists but is NOT flock-held, we should clear the
    # stale sentinel so it does not deadlock the next maintenance attempt.
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        try:
            import fcntl as _fcntl
            _fcntl.flock(fd, _fcntl.LOCK_SH | _fcntl.LOCK_NB)
            _fcntl.flock(fd, _fcntl.LOCK_UN)
            # No exclusive holder. Stale file? Leave it; another process may
            # be about to acquire it. Real maintenance activity will rewrite
            # the metadata quickly.
        except (BlockingIOError, OSError):
            holder = dbm.read_holder_metadata(lock_path) or {}
            raise MaintenanceHeld(
                f"Refusing to open writer for {state_db_path}: maintenance "
                f"lock is held (reason={holder.get('reason')!r}, "
                f"recovery_id={holder.get('recovery_id')!r}, "
                f"pid={holder.get('pid')})."
            )
    finally:
        os.close(fd)


class ManagedConnection:
    """A sqlite3.Connection wrapper that applies the safe PRAGMA set on
    open, holds a strong reference so the connection cannot be GC'd out
    from under active statements, and exposes ``close()`` and a context
    manager.
    """

    __slots__ = ("_conn", "_path", "_role", "_closed")

    def __init__(self, conn: sqlite3.Connection, path: Path, role: str):
        self._conn = conn
        self._path = path
        self._role = role
        self._closed = False

    @property
    def raw(self) -> sqlite3.Connection:
        return self._conn

    @property
    def path(self) -> Path:
        return self._path

    @property
    def role(self) -> str:
        return self._role

    @property
    def closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._conn.close()
        except Exception:  # pragma: no cover — close() is best-effort
            logger.debug("sqlite close raised for %s", self._path,
                         exc_info=True)

    def __enter__(self) -> "ManagedConnection":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


def _apply_default_pragmas(conn: sqlite3.Connection) -> None:
    """Apply the hardened PRAGMA set in order. Idempotent."""
    cur = conn.cursor()
    try:
        for pragma, value in DEFAULT_PRAGMAS:
            try:
                cur.execute(f"PRAGMA {pragma}={value}")
            except sqlite3.OperationalError as e:
                logger.warning(
                    "PRAGMA %s=%s failed: %s (continuing)",
                    pragma, value, e,
                )
    finally:
        cur.close()


def open_sqlite(path: os.PathLike, *,
                role: str = "reader",
                timeout: float = 30.0,
                isolation_level: Optional[Any] = None,
                check_same_thread: bool = True,
                apply_pragmas: bool = True,
                trust_maintenance_lock: bool = False) -> ManagedConnection:
    """Open a SQLite connection with the hardened defaults applied.

    Args:
        path: filesystem path to the SQLite file.
        role: ``"writer"`` (single-instance per process; enforces the safe
            PRAGMA set; refuses to open if maintenance is held) or
            ``"reader"`` (concurrent-safe; for diagnostics and snapshot
            creation).
        timeout: SQLite-level busy timeout in seconds (re-applied on top of
            the busy_timeout PRAGMA so threads waiting on a writer see the
            same timeout window).
        isolation_level: passed through to sqlite3; defaults to None which
            means "implicit transactions" (the recommended mode for WAL).
        check_same_thread: passed through; defaults to True. If a tool needs
            cross-thread access it must wrap the connection in a
            ``threading.RLock`` itself.
        apply_pragmas: when False (used by tests), the factory only opens
            the file and returns without changing its PRAGMAs.
        trust_maintenance_lock: ONLY for callers that legitimately need to
            write while holding the maintenance lock (recovery scripts,
            FTS rebuild). The default ``False`` is the safe value; the
            factory refuses to open a writer when maintenance is held.

    Returns a ``ManagedConnection`` that wraps the underlying ``sqlite3.Connection``.

    Raises:
        WriterAlreadyOpen: a writer is already open in this process for
            the same path.
        MaintenanceHeld: ``role="writer"`` and the maintenance lock is held
            (and ``trust_maintenance_lock`` is not set).
        sqlite3.OperationalError: e.g. file is not a database, disk full.
    """
    if role not in ("writer", "reader"):
        raise ValueError(f"role must be 'writer' or 'reader', got {role!r}")
    p = Path(path).expanduser().resolve()

    if role == "writer":
        with _WRITER_SINGLETON_LOCK:
            existing = _WRITER_SINGLETON.get(str(p))
            if existing is not None and not existing.closed:
                raise WriterAlreadyOpen(
                    f"Writer connection already open for {p} in this process "
                    f"(role={existing.role}). Close it before opening a new one."
                )
            if not trust_maintenance_lock:
                _assert_writer_safe_or_raise(p)

    # Open.
    conn = sqlite3.connect(
        str(p),
        timeout=timeout,
        isolation_level=isolation_level,
        check_same_thread=check_same_thread,
    )

    if apply_pragmas:
        _apply_default_pragmas(conn)

    mc = ManagedConnection(conn, p, role)

    if role == "writer":
        with _WRITER_SINGLETON_LOCK:
            # Re-check after acquiring the lock to handle a race with another
            # thread that opened between our first check and now.
            existing = _WRITER_SINGLETON.get(str(p))
            if existing is not None and not existing.closed:
                conn.close()
                raise WriterAlreadyOpen(
                    f"Writer connection already open for {p} (race)."
                )
            _WRITER_SINGLETON[str(p)] = mc
    return mc


# ──────────────────────────────────────────────────────────────────────
# Health-check primitives
# ──────────────────────────────────────────────────────────────────────

def quick_check(path: os.PathLike) -> tuple[bool, str]:
    """Run ``PRAGMA quick_check(1)`` — the cheap gate.

    Returns (ok, detail). ok=True iff the result is exactly ``"ok"``. detail
    is the raw first row otherwise so the caller can log/display it.
    """
    p = Path(path).expanduser().resolve()
    with open_sqlite(p, role="reader", apply_pragmas=False) as mc:
        try:
            row = mc.raw.execute("PRAGMA quick_check(1)").fetchone()
        except sqlite3.DatabaseError as e:
            return False, f"PRAGMA quick_check raised: {e}"
    if row is None:
        return False, "PRAGMA quick_check returned no rows"
    val = row[0]
    return (val == "ok"), str(val)


def integrity_check(path: os.PathLike, *, full: bool = True) -> tuple[bool, list[str]]:
    """Run ``PRAGMA integrity_check`` (full) or ``PRAGMA integrity_check(1)`` (fast).

    Returns (ok, rows). ok=True iff every row reads ``"ok"``. rows is the
    raw list of strings the pragma returned, useful for the dashboard card.
    """
    p = Path(path).expanduser().resolve()
    pragma = "PRAGMA integrity_check" if full else "PRAGMA integrity_check(1)"
    with open_sqlite(p, role="reader", apply_pragmas=False) as mc:
        try:
            rows = [r[0] for r in mc.raw.execute(pragma).fetchall()]
        except sqlite3.DatabaseError as e:
            return False, [f"PRAGMA integrity_check raised: {e}"]
    return (all(r == "ok" for r in rows) and bool(rows)), rows


def foreign_key_check(path: os.PathLike) -> tuple[int, list[dict]]:
    """Run ``PRAGMA foreign_key_check`` and return the count + a few rows.

    Only the first 50 rows are returned (the count is the live number).
    Foreign-key violations are an integrity-class issue, NOT a physical
    SQLite corruption: ``.recover`` is the wrong fix.
    """
    p = Path(path).expanduser().resolve()
    with open_sqlite(p, role="reader", apply_pragmas=True) as mc:
        try:
            mc.raw.execute("PRAGMA foreign_keys=ON")  # must be on for check to mean anything
            rows = mc.raw.execute("PRAGMA foreign_key_check").fetchall()
        except sqlite3.DatabaseError as e:
            return -1, [{"error": str(e)}]
    sample = []
    for r in rows[:50]:
        if isinstance(r, (tuple, list)):
            sample.append({"table": r[0], "rowid": r[1],
                           "parent": r[2], "fkid": r[3]})
        elif isinstance(r, dict):
            sample.append(r)
    return len(rows), sample


def wal_checkpoint(path: os.PathLike, mode: str = "TRUNCATE") -> dict:
    """Run ``PRAGMA wal_checkpoint(<mode>)``. Returns the result triple as a
    dict. Safe to call only when no writer holds the DB (callers are
    expected to be inside a maintenance lock).
    """
    p = Path(path).expanduser().resolve()
    if mode not in ("PASSIVE", "FULL", "RESTART", "TRUNCATE"):
        raise ValueError(f"invalid wal_checkpoint mode: {mode!r}")
    with open_sqlite(p, role="writer", apply_pragmas=False) as mc:
        # Temporarily switch out of WAL to allow checkpoint on a non-WAL
        # file (the safe default is to leave WAL on — the checkpoint still
        # works because we keep journal_mode=WAL).
        try:
            cur = mc.raw.execute(f"PRAGMA wal_checkpoint({mode})")
            row = cur.fetchone()
        except sqlite3.DatabaseError as e:
            return {"ok": False, "error": str(e)}
    if not row:
        return {"ok": False, "error": "no result"}
    # Columns: busy(int), log(int), checkpointed(int)
    return {"ok": bool(row[0] == 0), "busy": row[0], "log": row[1],
            "checkpointed": row[2]}


def vacuum_into(path: os.PathLike, dest_path: os.PathLike) -> dict:
    """Run ``VACUUM INTO <dest>`` to produce a clean, defragmented snapshot.

    VACUUM INTO acquires a brief read lock on the source; it is safe to
    run while the gateway is writing, but it is slower than
    ``Connection.backup()`` for a 235MB WAL DB. Both are valid; this one
    is used by the watchdog / doctor because it is a single SQL statement.
    """
    p = Path(path).expanduser().resolve()
    d = Path(dest_path).expanduser().resolve()
    d.parent.mkdir(parents=True, exist_ok=True)
    # If dest exists, VACUUM INTO refuses; remove first.
    if d.exists():
        d.unlink()
    with open_sqlite(p, role="reader", apply_pragmas=False) as mc:
        try:
            mc.raw.execute(f"VACUUM INTO {str(d)!r}")
        except sqlite3.OperationalError as e:
            return {"ok": False, "error": str(e)}
    return {"ok": True, "size": d.stat().st_size}


def fts_integrity_check(path: os.PathLike,
                         *, fts_names: Optional[list[str]] = None) -> dict:
    """Probe each FTS5 virtual table for (a) existence in sqlite_master,
    (b) ability to be queried, (c) ability to run the ``integrity-check``
    command.

    Does NOT mutate the database; the 'integrity-check' command is read-only
    on FTS5 vtables. For trigram vtables the row-count check is skipped
    because they intentionally cover a subset of the source.

    If ``fts_names`` is provided, missing tables are still reported (with
    ``exists=False``) so the classifier can flag them. When ``fts_names``
    is None, only the vtables currently in ``sqlite_master`` are reported.
    """
    p = Path(path).expanduser().resolve()
    with open_sqlite(p, role="reader", apply_pragmas=False) as mc:
        result: dict[str, dict] = {}
        if fts_names is None:
            fts_names = [
                r[0] for r in mc.raw.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='table' AND sql LIKE '%VIRTUAL TABLE%fts%'"
                ).fetchall()
            ]
        for name in fts_names:
            entry: dict[str, Any] = {"exists": False, "queryable": False,
                                     "integrity": None, "count": None,
                                     "error": None}
            try:
                row = mc.raw.execute(
                    "SELECT sql FROM sqlite_master "
                    "WHERE type='table' AND name = ?", (name,),
                ).fetchone()
                entry["exists"] = bool(row)
                if not entry["exists"]:
                    result[name] = entry
                    continue
                # Query-ability probe.
                try:
                    cnt = mc.raw.execute(f"SELECT count(*) FROM {name}").fetchone()
                    entry["queryable"] = True
                    entry["count"] = cnt[0] if cnt else 0
                except sqlite3.DatabaseError as e:
                    entry["queryable"] = False
                    entry["error"] = f"SELECT count(*) raised: {e}"
                # Integrity-check command (FTS5-specific).
                try:
                    ic = mc.raw.execute(
                        f"INSERT INTO {name}({name}) VALUES('integrity-check')"
                    )
                    # INSERT returns no rows; on failure, FTS5 raises.
                    entry["integrity"] = "ok"
                except sqlite3.OperationalError as e:
                    entry["integrity"] = f"failed: {e}"
                except sqlite3.DatabaseError as e:
                    entry["integrity"] = f"failed: {e}"
            except sqlite3.DatabaseError as e:
                entry["error"] = str(e)
            result[name] = entry
    return result


def db_header_bytes(path: os.PathLike, n: int = 16) -> bytes:
    """Return the first ``n`` bytes of the DB file — used by ``classify``
    to detect a destroyed page 1 (which starts with 0x0d instead of the
    SQLite magic string).
    """
    p = Path(path).expanduser().resolve()
    with open(p, "rb") as fh:
        return fh.read(n)


__all__ = [
    "DEFAULT_PRAGMAS",
    "ManagedConnection",
    "MaintenanceHeld",
    "WriterAlreadyOpen",
    "open_sqlite",
    "quick_check",
    "integrity_check",
    "foreign_key_check",
    "wal_checkpoint",
    "vacuum_into",
    "fts_integrity_check",
    "db_header_bytes",
]
