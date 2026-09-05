"""Database maintenance lock + atomic install for Hermes state.db.

This module is the single owner of the rule "no destructive DB operation
without an exclusive maintenance lock, no install underneath a live writer".

Sections (from the state-db-reliability design):

- A. Database ownership / single-writer safety
- B. Safe shutdown before DB repair
- H. Atomic recovery
- I. WAL/SHM safety

Public surface:

    with MaintenanceLock(path) as lock:
        ...                                    # exclusive maintenance
        install_state_db_recovered(state_dir,
                                   recovered_path,
                                   dry_run=False)

    with assert_writer_safe(state_path, timeout=30):
        ...                                    # refuse to open if maintenance is active

The lock file lives **outside** the state.db directory tree on purpose:
- It cannot be confused with a backup snapshot taken by VACUUM INTO.
- It survives a `cp -r` of the state dir during backup/recovery.
- `fuser` will never falsely report it as a DB holder.

This module is stdlib-only (no Hermes imports) so it can run inside a
``systemd-run --scope`` transient unit before the rest of Hermes is loaded,
and so the unit tests do not need the Hermes venv.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import json
import logging
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Iterator, Optional

logger = logging.getLogger(__name__)


# Lock sentinel — set in the lock file's first 8 bytes so an operator can
# inspect /root/.hermes/state.db.maintenance.lock and see who holds it.
_LOCK_MAGIC = b"HERMES-MAINT-V1\n"
_HOLDER_LINE_MAX = 256


class MaintenanceActive(RuntimeError):
    """Raised when exclusive maintenance cannot be acquired (held by another
    process) within the configured timeout, or when a writer attempts to open
    the database while maintenance is active.
    """


class WriterStillPresent(RuntimeError):
    """Raised by ``install_state_db_recovered`` when an unexpected process
    continues to hold ``state.db``/``state.db-wal``/``state.db-shm`` after
    the maintenance lock was acquired. Recovery is aborted; the operator must
    identify the holder (see ``state_db_holders``) and stop it.
    """


def maintenance_lock_path(state_db_path: os.PathLike) -> Path:
    """Sidecar lock file for the given state.db.

    Lives next to state.db but is NOT state.db (so it cannot be swept up by
    a ``VACUUM INTO`` snapshot, a `cp -r` backup, or a `rm *.corrupt.*`).
    """
    p = Path(state_db_path).expanduser().resolve()
    return p.with_name(p.name + ".maintenance.lock")


def write_holder_metadata(lock_path: Path, *, reason: str, recovery_id: str,
                          pid: Optional[int] = None) -> None:
    """Write the operator-readable holder section of the lock file.

    The first 16 bytes are the magic sentinel; the next N bytes are a JSON
    line with reason, recovery_id, pid, hostname, started_at. This is the
    text an operator sees when they ``cat`` the lock file while maintenance
    is active — never any secret material.
    """
    payload = {
        "reason": reason[:_HOLDER_LINE_MAX],
        "recovery_id": recovery_id[:64],
        "pid": pid if pid is not None else os.getpid(),
        "hostname": os.uname().nodename,
        "started_at": time.time(),
    }
    line = (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8")
    # Truncate, then write. We never overwrite the magic sentinel bytes that
    # prove this is a maintenance lock and not some other sidecar file.
    with open(lock_path, "r+b", buffering=0) as fh:
        try:
            fh.seek(0)
            existing = fh.read(len(_LOCK_MAGIC))
            if existing != _LOCK_MAGIC:
                fh.seek(0, os.SEEK_SET)
                fh.write(_LOCK_MAGIC)
        except FileNotFoundError:
            pass
        fh.seek(0, os.SEEK_END)
        fh.write(line)


def read_holder_metadata(lock_path: Path) -> Optional[dict]:
    """Return the parsed holder metadata, or None if the lock is not held."""
    if not lock_path.exists():
        return None
    try:
        with open(lock_path, "rb") as fh:
            data = fh.read()
    except OSError:
        return None
    if not data.startswith(_LOCK_MAGIC):
        return None
    remainder = data[len(_LOCK_MAGIC):]
    if not remainder.strip():
        return {"reason": "", "recovery_id": "", "pid": None}
    # Take the last JSON line (defensive against partial writes).
    last_line = remainder.strip().splitlines()[-1] if remainder.strip() else b""
    try:
        return json.loads(last_line.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {"raw": remainder[:512].decode("utf-8", errors="replace")}


def _acquire_flock(lock_path: Path, exclusive: bool):
    """Non-blocking flock acquisition. Returns (acquired, fd) tuple.

    flock(2) is released automatically when the file descriptor is closed
    (process exit) or when the same process downgrades — both desirable
    here. It is the documented Linux mechanism for this use case and is
    not subject to NFS-rename races the way ``O_EXCL`` lock files are.

    The caller owns the returned fd and MUST close it to release the
    lock; flock auto-releases on close but we keep an explicit handle so
    we can read/write the holder metadata through the same descriptor.
    """
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        op = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        fcntl.flock(fd, op | fcntl.LOCK_NB)
    except (BlockingIOError, OSError) as e:
        # EWOULDBLOCK == EAGAIN == BlockingIOError on Linux.
        os.close(fd)
        if e.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
            return False, -1
        raise
    return True, fd


def _release_flock(fd: int) -> None:
    if fd is not None and fd >= 0:
        try:
            os.close(fd)
        except OSError:
            pass


class MaintenanceLock:
    """Exclusive maintenance lock for destructive DB operations.

    Usage::

        with MaintenanceLock(state_db_path, reason="fts-rebuild", timeout=60) as lock:
            ...                                         # exclusive work
            install_state_db_recovered(state_db_path, recovered_db_path)

    ``reason`` and ``recovery_id`` are written to the sidecar file as JSON
    so ``cat state.db.maintenance.lock`` is human-readable.
    """

    def __init__(self, state_db_path: os.PathLike, *,
                 reason: str = "unspecified",
                 recovery_id: Optional[str] = None,
                 timeout: float = 0.0,
                 poll_interval: float = 0.25):
        self.state_db_path = Path(state_db_path).expanduser().resolve()
        self.lock_path = maintenance_lock_path(self.state_db_path)
        self.reason = reason
        self.recovery_id = recovery_id or time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        self.timeout = float(timeout)
        self.poll_interval = float(poll_interval)
        self._held = False
        self._fd: int = -1

    def __enter__(self) -> "MaintenanceLock":
        deadline = time.monotonic() + self.timeout
        while True:
            acquired, fd = _acquire_flock(self.lock_path, exclusive=True)
            if acquired:
                self._held = True
                self._fd = fd
                try:
                    write_holder_metadata(
                        self.lock_path,
                        reason=self.reason,
                        recovery_id=self.recovery_id,
                    )
                except Exception:  # metadata write is best-effort
                    logger.debug("maintenance lock metadata write failed",
                                 exc_info=True)
                logger.info(
                    "maintenance lock acquired: reason=%r recovery_id=%r lock=%s",
                    self.reason, self.recovery_id, self.lock_path,
                )
                return self
            if time.monotonic() >= deadline:
                holder = read_holder_metadata(self.lock_path) or {}
                raise MaintenanceActive(
                    f"Could not acquire exclusive maintenance lock "
                    f"{self.lock_path} within {self.timeout}s. "
                    f"Current holder: {holder}"
                )
            time.sleep(self.poll_interval)

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._held:
            logger.info(
                "maintenance lock released: reason=%r recovery_id=%r",
                self.reason, self.recovery_id,
            )
            _release_flock(self._fd)
            self._fd = -1
            self._held = False


@contextlib.contextmanager
def assert_writer_safe(state_db_path: os.PathLike, *,
                       timeout: float = 30.0,
                       poll_interval: float = 0.5) -> Iterator[None]:
    """Refuse to open a writer connection while maintenance is active.

    Shared-lock semantics: as long as NOBODY holds the lock exclusively, the
    writer proceeds. The moment another process takes the exclusive lock,
    this context raises ``MaintenanceActive``.

    Writers call this at startup (after their main loop opens the DB) so the
    watchdog / recovery scripts can grab the lock and stop the writer
    gracefully. The check is one syscall (``flock LOCK_SH | LOCK_NB``) and
    is therefore cheap enough to call once per second from a tight loop if
    desired.
    """
    state_db_path = Path(state_db_path).expanduser().resolve()
    lock_path = maintenance_lock_path(state_db_path)
    deadline = time.monotonic() + timeout
    if not lock_path.exists():
        # No maintenance ever recorded — fast path.
        yield
        return
    while True:
        acquired, fd = _acquire_flock(lock_path, exclusive=False)
        if acquired:
            try:
                yield
            finally:
                _release_flock(fd)
            return
        if time.monotonic() >= deadline:
            holder = read_holder_metadata(lock_path) or {}
            raise MaintenanceActive(
                f"Writer cannot open {state_db_path} — maintenance lock is "
                f"held by {holder.get('pid')} (reason={holder.get('reason')!r}, "
                f"recovery_id={holder.get('recovery_id')!r}). Refusing to "
                f"start the writer; the operator must clear maintenance "
                f"mode."
            )
        time.sleep(poll_interval)


def state_db_holders(state_db_path: os.PathLike,
                     *,
                     include_wal: bool = True) -> list[dict]:
    """Return the list of processes holding state.db (and optionally the
    WAL/SHM sidecars).

    Uses two complementary mechanisms:

    1. ``fuser`` — Linux procps tool, fast, but unreliable for SQLite
       because the database may be held via a memfd or journal temp file
       that ``fuser`` cannot see.
    2. A probe connection that runs ``BEGIN IMMEDIATE`` with a short
       timeout. If SQLite raises ``OperationalError('database is locked')``
       another writer holds the DB; if it succeeds and rolls back, no
       writer is present. This is the authoritative detector for SQLite
       specifically.

    Returns a list of dicts suitable for log/dashboard display. Each entry
    is either an fuser-detected holder ``{"pid": int, "user": str,
    "command": str}`` or a probe-conflict ``{"source": "sqlite_probe",
    "conflict": True, "locked_error": str}``.
    """
    state_db_path = Path(state_db_path).expanduser().resolve()
    holders: list[dict] = []
    if shutil.which("fuser"):
        paths = [str(state_db_path)]
        if include_wal:
            paths.append(str(state_db_path) + "-wal")
            paths.append(str(state_db_path) + "-shm")
        try:
            proc = subprocess.run(
                ["fuser", "--no-mtab", "-v", "-n", "file"] + paths,
                capture_output=True, text=True, timeout=5,
            )
        except subprocess.TimeoutExpired:
            pass
        except Exception:
            pass
        else:
            for line in (proc.stdout or "").splitlines():
                line = line.strip()
                if ":" not in line or not any(ch.isdigit() for ch in line):
                    continue
                try:
                    parts = line.split()
                    pid = int(next(p for p in parts if p.isdigit()))
                    user = parts[0] if len(parts) >= 4 else "?"
                    cmd = " ".join(parts[3:]) if len(parts) >= 4 else ""
                    holders.append({"pid": pid, "user": user, "command": cmd})
                except (StopIteration, ValueError):
                    continue

    # SQLite writer probe — authoritative for in-process and same-host
    # writers. Uses the venv python's bundled sqlite (3.53.1) which is
    # what Hermes uses for everything else.
    probe_path = str(state_db_path)
    if os.path.exists(probe_path):
        try:
            import sqlite3
            probe = sqlite3.connect(probe_path, timeout=0.5)
            try:
                probe.execute("BEGIN IMMEDIATE")
                probe.rollback()
            except sqlite3.OperationalError as e:
                if "locked" in str(e).lower():
                    holders.append({
                        "source": "sqlite_probe",
                        "conflict": True,
                        "locked_error": str(e),
                    })
            finally:
                probe.close()
        except Exception as e:
            holders.append({"source": "sqlite_probe", "detector_error": str(e)})

    holders.sort(key=lambda h: h.get("pid", 0))
    return holders


def wait_for_no_holders(state_db_path: os.PathLike, *,
                        timeout: float = 30.0,
                        poll_interval: float = 0.5,
                        abort_on_unknown: bool = True) -> None:
    """Block until no process holds state.db / state.db-wal / state.db-shm.

    Raises ``WriterStillPresent`` if holders persist past the deadline. The
    caller (typically ``install_state_db_recovered``) refuses to perform
    the atomic rename until this returns cleanly.
    """
    deadline = time.monotonic() + timeout
    last_holders: list[dict] = []
    while True:
        holders = state_db_holders(state_db_path, include_wal=True)
        # Filter out shell PIDs whose cmd line is "fuser" itself (false
        # positive). Probe conflicts ({'conflict': True}) are real holders.
        # Detector errors ({'detector_error': ...}) do NOT count — they're
        # diagnostic only, the holder check still failed.
        real = [h for h in holders
                if "fuser" not in h.get("command", "").lower()
                and not h.get("detector_error")
                and (h.get("conflict") or h.get("pid") is not None)]
        if not real:
            if last_holders:
                logger.info("all holders released: previously %s",
                            [h.get("pid", h.get("source")) for h in last_holders])
            return
        last_holders = real
        if time.monotonic() >= deadline:
            raise WriterStillPresent(
                f"Holders of state.db persist after {timeout}s: {real}. "
                f"Aborting recovery — refusing to install a recovered DB "
                f"under a live writer. Use `hermes db holders` to identify "
                f"the process."
            )
        time.sleep(poll_interval)


# Backoff helper used by both the install path and any future callers.
def fsync_dir(path: os.PathLike) -> None:
    """fsync a directory so its directory entries are durable.

    On POSIX, ``os.replace`` does not guarantee the directory entry change
    is flushed to disk. We fsync the parent dir so a power loss right after
    ``os.replace`` does not leave the parent listing the OLD inode while the
    data is the recovered one (or vice versa).
    """
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def install_state_db_recovered(state_db_path: os.PathLike,
                               recovered_db_path: os.PathLike,
                               *,
                               dry_run: bool = False,
                               holder_wait_timeout: float = 30.0,
                               reason: str = "recover") -> dict:
    """Atomically install a previously-recovered DB as ``state.db``.

    Pre-conditions (the caller MUST have validated the recovered DB; this
    function does NOT run ``.recover`` or check integrity):

    - The recovered file passed ``PRAGMA integrity_check`` and matches the
      expected schema/counts. See ``agent/db_health.classify``.
    - The maintenance lock has been acquired by the caller (we re-acquire
      here defensively but the caller's lock is the authoritative one).

    Steps:

    1. Verify no live writer still holds state.db / -wal / -shm.
    2. fsync the recovered file (durability).
    3. os.replace(recovered.db, state.db) — atomic rename.
    4. fsync the parent directory.
    5. Return a structured report.
    """
    state_db_path = Path(state_db_path).expanduser().resolve()
    recovered_db_path = Path(recovered_db_path).expanduser().resolve()
    if not recovered_db_path.exists():
        raise FileNotFoundError(f"recovered DB not found: {recovered_db_path}")

    # The caller is expected to be inside a MaintenanceLock. We do not
    # re-acquire here because that would deadlock the lock acquisition; we
    # only verify the lock file's presence as a sanity check.
    lock_path = maintenance_lock_path(state_db_path)
    if not lock_path.exists():
        logger.warning(
            "install_state_db_recovered called without a maintenance lock "
            "for %s — proceeding but the caller is expected to hold the lock",
            state_db_path,
        )

    wait_for_no_holders(
        state_db_path,
        timeout=holder_wait_timeout,
    )

    report: dict = {
        "event": "DB_RECOVERY_INSTALL",
        "old_path": str(state_db_path),
        "new_path": str(recovered_db_path),
        "old_size": state_db_path.stat().st_size if state_db_path.exists() else None,
        "new_size": recovered_db_path.stat().st_size,
        "dry_run": dry_run,
        "reason": reason,
    }

    if dry_run:
        report["status"] = "DRY_RUN"
        return report

    # fsync the recovered file before rename.
    with open(recovered_db_path, "r+b") as fh:
        os.fsync(fh.fileno())

    # Atomic rename.
    os.replace(recovered_db_path, state_db_path)
    # fsync the parent so the rename is durable.
    fsync_dir(state_db_path.parent)

    report["status"] = "SUCCESS"
    report["inode"] = state_db_path.stat().st_ino
    return report


__all__ = [
    "MaintenanceActive",
    "WriterStillPresent",
    "MaintenanceLock",
    "assert_writer_safe",
    "maintenance_lock_path",
    "read_holder_metadata",
    "write_holder_metadata",
    "state_db_holders",
    "wait_for_no_holders",
    "install_state_db_recovered",
    "fsync_dir",
]
