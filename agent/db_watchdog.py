"""Database health watchdog — periodic severity-graded observer.

This module implements Sections F (preflight), G (periodic health
watchdog), R (severity escalation), and S (structured observability).

The watchdog is designed to be called from:

1. A 5-minute cron (one tick per run).
2. The systemd ``ExecStartPre`` of hermes-gateway / hermes-dashboard units.
3. ``hermes db check`` CLI subcommand.

It MUST NOT do destructive work. The watchdog writes its findings to:

- ``/var/lib/hermes/state-db-health.json`` (machine-readable snapshot).
- A structured event log at ``<state_dir>/db-event-log.jsonl`` (append-only).
- The systemd journal (when run under systemd).

Telegram / dashboard notifications are handled by the caller (the cron
wrapper), not by this module — keeps the watchdog pure-Python and easy to
unit-test without network dependencies.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Optional

import agent.db_connection as dbc
import agent.db_health as dbh
import agent.db_maintenance as dbm

logger = logging.getLogger(__name__)


DEFAULT_EVENT_LOG = "/var/lib/hermes/db-event-log.jsonl"
DEFAULT_HEALTH_SNAPSHOT = "/var/lib/hermes/state-db-health.json"


def _append_event(event_log: Path, payload: dict) -> None:
    """Append one structured event line. fsync after the write."""
    event_log.parent.mkdir(parents=True, exist_ok=True)
    with open(event_log, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, sort_keys=True) + "\n")
        fh.flush()
        try:
            os.fsync(fh.fileno())
        except OSError:
            pass  # journal might be on a non-fsyncable tmpfs


def _write_health_snapshot(snapshot_path: Path, payload: dict) -> None:
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = snapshot_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, snapshot_path)


def _disk_diagnostics(state_dir: Path) -> dict:
    """Capture df -h / df -i / dmesg tail snapshot for the report."""
    out: dict[str, Any] = {}
    try:
        st = os.statvfs(str(state_dir))
        out["free_bytes"] = st.f_bavail * st.f_frsize
        out["total_bytes"] = st.f_blocks * st.f_frsize
    except OSError:
        pass

    def _try(cmd: list[str]) -> Optional[str]:
        import subprocess
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
            return r.stdout[:4096]
        except Exception:
            return None

    out["df_h"] = _try(["df", "-h", str(state_dir)])
    out["df_i"] = _try(["df", "-i", str(state_dir)])
    out["dmesg_tail"] = _try(["dmesg", "-T", "--since=-2 hours"])
    return out


def tick(state_db_path: os.PathLike, *,
         event_log: Optional[Path] = None,
         snapshot_path: Optional[Path] = None,
         full: bool = False,
         persist_inode: bool = True) -> dict:
    """Run one watchdog tick. Returns the JSON snapshot written to disk.

    ``full=False`` is the periodic tick (quick_check + FTS only, fast). Use
    ``full=True`` for daily or post-recovery ticks (adds integrity_check
    and disk diagnostics).

    NEVER mutates the database.
    """
    p = Path(state_db_path).expanduser().resolve()
    event_log = Path(event_log) if event_log else Path(DEFAULT_EVENT_LOG)
    snapshot_path = Path(snapshot_path) if snapshot_path else Path(DEFAULT_HEALTH_SNAPSHOT)

    report = dbh.classify(p, full=full, persist_inode=persist_inode)

    snapshot: dict[str, Any] = {
        "ts": time.time(),
        "event": "DB_HEALTH_TICK",
        "severity": report.severity,
        "summary": report.summary,
        "path": report.path,
        "header_ok": report.header_ok,
        "quick_check": report.quick_check,
        "integrity_check": report.integrity_check[:3],
        "foreign_key_violations": report.foreign_key_violations,
        "fts": report.fts,
        "wal": report.wal,
        "disk": report.disk,
        "inode": report.inode,
        "events": report.events,
        "holders": dbm.state_db_holders(p, include_wal=True),
    }
    if full:
        snapshot["diagnostics"] = _disk_diagnostics(p.parent)

    _write_health_snapshot(snapshot_path, snapshot)

    # Structured event log: emit one DB_HEALTH_TICK line plus a
    # severity-classified event line for each severity.
    _append_event(event_log, {
        "ts": snapshot["ts"],
        "event": "DB_HEALTH_TICK",
        "severity": report.severity,
        "path": report.path,
        "events": report.events,
    })
    if report.severity in (dbh.CORRUPT, dbh.RECOVERY_REQUIRED):
        _append_event(event_log, {
            "ts": snapshot["ts"],
            "event": "DB_HEALTH_FAILED",
            "severity": report.severity,
            "path": report.path,
            "summary": report.summary,
        })
    elif report.severity in (dbh.DEGRADED_FTS, dbh.DEGRADED_WAL):
        _append_event(event_log, {
            "ts": snapshot["ts"],
            "event": "DB_DEGRADED",
            "severity": report.severity,
            "path": report.path,
            "events": report.events,
        })
    elif report.severity == dbh.WARNING:
        _append_event(event_log, {
            "ts": snapshot["ts"],
            "event": "DB_HEALTH_WARNING",
            "severity": report.severity,
            "path": report.path,
            "events": report.events,
        })

    return snapshot


def should_block_writes(report_dict: dict) -> bool:
    """Return True if the watchdog severity means the gateway must refuse to
    open a writer connection.

    Severity ladder (from Section R of the design):

        WARNING             → continue, log only
        DEGRADED_*          → continue, log + dashboard notification
        CORRUPT             → continue, structured alert
        RECOVERY_REQUIRED   → STOP WRITES; refuse to open the writer
    """
    return report_dict.get("severity") == dbh.RECOVERY_REQUIRED


__all__ = [
    "tick",
    "should_block_writes",
    "DEFAULT_EVENT_LOG",
    "DEFAULT_HEALTH_SNAPSHOT",
]
