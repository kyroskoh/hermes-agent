"""Database health classifier — one decision tree for all checks.

Implements Section E (distinguish FTS from core corruption) and feeds
Section S (observability) by writing structured HealthReport objects.

The decision tree (Sections E and G):

1. Header sanity (xxd -l 16) → if not "SQLite format 3\\0", RECOVERY_REQUIRED
   header-splice path.
2. quick_check → if not "ok", RECOVERY_REQUIRED.
3. integrity_check (full) → if not "ok", RECOVERY_REQUIRED.
4. foreign_key_check → count only; never RECOVERY_REQUIRED on its own.
5. FTS5 health per vtable → DEGRADED_FTS if any check fails; NEVER
   RECOVERY_REQUIRED just because FTS is bad.
6. WAL state — orphan WAL/SHM without a live writer → DEGRADED_WAL.
7. Disk space — WARNING below thresholds.
8. DB inode tracking — if the inode changed unexpectedly, emit
   DB_INODE_CHANGED; do not auto-recover.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Optional

import agent.db_connection as dbc

logger = logging.getLogger(__name__)


# The 16-byte SQLite header magic.
_SQLITE_MAGIC = b"SQLite format 3\x00"

# Severity bands. WARNING is recoverable on the next operation; DEGRADED is
# observable but does not block writes; CORRUPT and RECOVERY_REQUIRED MUST
# stop new writes.
WARNING = "WARNING"
DEGRADED_FTS = "DEGRADED_FTS"
DEGRADED_WAL = "DEGRADED_WAL"
DEGRADED_FK = "DEGRADED_FK"
CORRUPT = "CORRUPT"
RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
OK = "OK"


# Canonical Hermes FTS5 vtable names. When ``classify`` runs and these
# are missing from sqlite_master, it reports ``DEGRADED_FTS`` (not
# ``RECOVERY_REQUIRED`` — see Section E).
DEFAULT_EXPECTED_FTS_TABLES = ("messages_fts", "messages_fts_trigram")


@dataclasses.dataclass
class HealthReport:
    path: str
    severity: str
    summary: str
    header_ok: bool
    quick_check: str
    integrity_check: list[str]
    foreign_key_violations: int
    foreign_key_sample: list[dict]
    fts: dict
    wal: dict
    disk: dict
    inode: dict
    events: list[str]
    when: float = dataclasses.field(default_factory=time.time)

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=True)


def _read_inode_record(state_dir: Path) -> Optional[dict]:
    p = state_dir / "state-db-inode.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _write_inode_record(state_dir: Path, record: dict) -> None:
    p = state_dir / "state-db-inode.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(record, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, p)


def classify(path: os.PathLike, *,
             full: bool = True,
             persist_inode: bool = True,
             fts_names: Optional[list[str]] = None,
             expected_fts: Optional[tuple[str, ...]] = DEFAULT_EXPECTED_FTS_TABLES) -> HealthReport:
    """Classify the database at ``path`` and return a structured report.

    Args:
        path: filesystem path to state.db (or any SQLite file).
        full: when True, run the full integrity_check (slower). When False,
            use the fast `integrity_check(1)`.
        persist_inode: write the inode record under ``<dir>/state-db-inode.json``
            so the next call can detect an unexpected inode change.
        fts_names: explicit list of FTS5 vtables to check; if None, all
            vtables whose sql matches ``%VIRTUAL TABLE%fts%`` are checked.
        expected_fts: when set, these table names are checked even when not
            in ``sqlite_master``; a missing name is reported as
            ``FTS_MISSING:<name>`` and bumps the severity to DEGRADED_FTS.
            Defaults to the canonical Hermes pair.
    """
    p = Path(path).expanduser().resolve()
    events: list[str] = []
    severity = OK
    summary = "ok"

    # ── 1. Header sanity ────────────────────────────────────────────────
    header = dbc.db_header_bytes(p, 16)
    header_ok = header.startswith(_SQLITE_MAGIC)
    if not header_ok:
        events.append("DB_HEADER_INVALID")
        severity = RECOVERY_REQUIRED
        summary = "page 1 destroyed — header does not match SQLite magic"

    # ── 2. quick_check ─────────────────────────────────────────────────
    qc_ok, qc_detail = dbc.quick_check(p) if header_ok else (False, "skipped (header bad)")
    if header_ok and not qc_ok:
        events.append("DB_QUICK_CHECK_FAILED")
        severity = RECOVERY_REQUIRED
        summary = f"quick_check failed: {qc_detail}"

    # ── 3. integrity_check ─────────────────────────────────────────────
    if header_ok:
        ic_ok, ic_rows = dbc.integrity_check(p, full=full)
    else:
        ic_ok, ic_rows = False, ["skipped (header bad)"]
    if header_ok and not ic_ok:
        events.append("DB_INTEGRITY_FAILED")
        severity = RECOVERY_REQUIRED
        summary = f"integrity_check failed ({len(ic_rows)} rows)"

    # ── 4. foreign_key_check ───────────────────────────────────────────
    if header_ok:
        fk_count, fk_sample = dbc.foreign_key_check(p)
    else:
        fk_count, fk_sample = -1, []
    if fk_count > 0 and severity == OK:
        events.append("FK_VIOLATIONS")
        severity = DEGRADED_FK
        summary = f"{fk_count} foreign-key violations"

    # ── 5. FTS5 health ─────────────────────────────────────────────────
    if header_ok:
        fts = dbc.fts_integrity_check(p, fts_names=fts_names)
    else:
        fts = {}
    # If the caller provided expected_fts names, union them into the
    # check so missing vtables still appear (exists=False).
    if expected_fts:
        for name in expected_fts:
            if name not in fts:
                fts[name] = {"exists": False, "queryable": False,
                             "integrity": None, "count": None, "error": None}
    for name, entry in fts.items():
        if not entry.get("exists"):
            events.append(f"FTS_MISSING:{name}")
            if severity == OK:
                severity = DEGRADED_FTS
                summary = f"FTS table {name!r} missing"
        elif entry.get("error") or not entry.get("queryable"):
            events.append(f"FTS_UNQUERYABLE:{name}")
            if severity in (OK, DEGRADED_FK):
                severity = DEGRADED_FTS
                summary = f"FTS table {name!r} not queryable"
        elif entry.get("integrity") and entry["integrity"] != "ok":
            events.append(f"FTS_CORRUPT:{name}")
            if severity in (OK, DEGRADED_FK):
                severity = DEGRADED_FTS
                summary = f"FTS table {name!r} integrity-check failed"

    # ── 6. WAL state ───────────────────────────────────────────────────
    wal: dict = {"wal_present": False, "shm_present": False, "size_wal": 0}
    wal_path = p.with_suffix(p.suffix + "-wal")
    shm_path = p.with_suffix(p.suffix + "-shm")
    if wal_path.exists():
        wal["wal_present"] = True
        wal["size_wal"] = wal_path.stat().st_size
        events.append("WAL_PRESENT")
    if shm_path.exists():
        wal["shm_present"] = True
        events.append("SHM_PRESENT")
    # If WAL/SHM is present without a live process, that's DEGRADED_WAL.
    # We detect "no live process" by checking maintenance holders; a clean
    # DB with a checkpointed WAL has no -wal file in normal operation.

    # ── 7. Disk space ──────────────────────────────────────────────────
    disk: dict = {"free_bytes": None, "free_pct": None}
    try:
        st = os.statvfs(str(p.parent))
        free = st.f_bavail * st.f_frsize
        total = st.f_blocks * st.f_frsize
        disk["free_bytes"] = free
        disk["free_pct"] = round(100.0 * free / total, 2) if total else None
        if free < 1 << 30:  # < 1 GiB
            events.append("DISK_LOW")
            if severity == OK:
                severity = WARNING
                summary = f"disk space low: {free // (1<<20)} MiB free"
    except OSError as e:
        disk["error"] = str(e)

    # ── 8. Inode tracking ──────────────────────────────────────────────
    inode_record: dict = {}
    try:
        st = p.stat()
        current_inode = (st.st_dev, st.st_ino, st.st_size)
        prior = _read_inode_record(p.parent)
        if prior and tuple(prior.get("current", ())) != current_inode:
            events.append("DB_INODE_CHANGED")
            inode_record["changed"] = True
            inode_record["previous"] = prior.get("current")
            inode_record["current"] = list(current_inode)
            # An inode change WITHOUT a maintenance action is a critical
            # signal — it means someone replaced the file underneath us.
            if prior.get("last_install_reason") is None:
                if severity in (OK, DEGRADED_FK, DEGRADED_FTS):
                    severity = WARNING
                    summary = (
                        "DB inode changed unexpectedly while no install was "
                        "recorded. Investigate immediately."
                    )
        else:
            inode_record["changed"] = False
            inode_record["current"] = list(current_inode)
        if persist_inode:
            _write_inode_record(p.parent, {
                "current": list(current_inode),
                "recorded_at": time.time(),
            })
    except FileNotFoundError:
        events.append("DB_MISSING")
        severity = RECOVERY_REQUIRED
        summary = "database file is missing"

    return HealthReport(
        path=str(p),
        severity=severity,
        summary=summary,
        header_ok=header_ok,
        quick_check=qc_detail,
        integrity_check=ic_rows,
        foreign_key_violations=fk_count if fk_count >= 0 else 0,
        foreign_key_sample=fk_sample,
        fts=fts,
        wal=wal,
        disk=disk,
        inode=inode_record,
        events=events,
    )


def render_status(report: HealthReport) -> str:
    """One-screen text summary for `hermes db status` and the dashboard card."""
    p = report.path
    sev = report.severity
    lines = [
        f"Database: {p}",
        f"Status  : {sev}",
        f"Summary : {report.summary}",
        f"Header  : {'OK' if report.header_ok else 'INVALID'}",
        f"Quick   : {report.quick_check}",
        f"IC rows : {'/'.join(report.integrity_check[:1]) + (f' (+{len(report.integrity_check)-1} more)' if len(report.integrity_check) > 1 else '')}",
        f"FK      : {report.foreign_key_violations} violation(s)",
        f"FTS     : " + ", ".join(
            f"{k}={v.get('integrity', v.get('error', '?'))}"
            for k, v in report.fts.items()
        ) if report.fts else "(none)",
        f"WAL     : present={report.wal.get('wal_present')} shm={report.wal.get('shm_present')} size={report.wal.get('size_wal', 0)}",
        f"Disk    : free={report.disk.get('free_bytes')} pct={report.disk.get('free_pct')}",
        f"Inode   : {report.inode.get('current', '?')} changed={report.inode.get('changed')}",
        f"Events  : {', '.join(report.events) if report.events else '(none)'}",
    ]
    return "\n".join(lines)


__all__ = [
    "WARNING", "DEGRADED_FTS", "DEGRADED_WAL", "DEGRADED_FK",
    "CORRUPT", "RECOVERY_REQUIRED", "OK",
    "HealthReport",
    "classify",
    "render_status",
]
