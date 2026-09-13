"""``hermes db`` — state database reliability subcommands.

Implements Section T of the state-db-reliability design.

The subcommands are intentionally thin: they parse argv, call into the
agent.db_* modules, and emit either human-readable text or a single JSON
object (for cron / dashboard consumption).

Subcommands:

    hermes db status                    # one-screen summary
    hermes db check                     # quick_check + FTS (~50ms)
    hermes db check --full              # + integrity_check + disk diagnostics
    hermes db backup                    # VACUUM INTO, tiered retention
    hermes db repair-fts                # rebuild messages_fts (+trigram)
    hermes db recover [--strategy N]    # recovery under the maintenance lock
    hermes db restore <backup>          # restore from a validated snapshot
    hermes db holders                   # list processes with the DB open
    hermes db pending                   # list queued pending messages
    hermes db replay-pending            # idempotent replay into canonical
    hermes db maintenance-on "reason"   # set the maintenance marker
    hermes db maintenance-off           # clear the marker
    hermes db preflight                 # for systemd ExecStartPre

Every subcommand writes a structured event to the event log (handled by
the lower-level modules) and exits non-zero on failure.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


def _hermes_home() -> Path:
    from hermes_constants import get_hermes_home
    return Path(get_hermes_home()).expanduser().resolve()


def _state_db_path() -> Path:
    return _hermes_home() / "state.db"


def _print_json(payload: dict) -> None:
    json.dump(payload, sys.stdout, indent=2, sort_keys=True, default=str)
    sys.stdout.write("\n")


def _print_human(text: str) -> None:
    sys.stdout.write(text + "\n")


# ──────────────────────────────────────────────────────────────────────
# Subcommand handlers
# ──────────────────────────────────────────────────────────────────────

def cmd_status(args: argparse.Namespace) -> int:
    import agent.db_health as dbh
    report = dbh.classify(_state_db_path(), full=args.full)
    if args.json:
        _print_json(report.to_dict())
    else:
        _print_human(dbh.render_status(report))
    return 0 if report.severity in (dbh.OK, dbh.WARNING, dbh.DEGRADED_FK) else 2


def cmd_check(args: argparse.Namespace) -> int:
    import agent.db_health as dbh
    report = dbh.classify(_state_db_path(), full=args.full, persist_inode=False)
    if args.json:
        _print_json(report.to_dict())
    else:
        sev = report.severity
        marker = {
            dbh.OK: "OK",
            dbh.WARNING: "WARNING",
            dbh.DEGRADED_FK: "DEGRADED_FK",
            dbh.DEGRADED_FTS: "DEGRADED_FTS",
            dbh.DEGRADED_WAL: "DEGRADED_WAL",
            dbh.CORRUPT: "CORRUPT",
            dbh.RECOVERY_REQUIRED: "RECOVERY_REQUIRED",
        }.get(sev, sev)
        _print_human(
            f"sqlite core integrity: {report.quick_check}\n"
            f"fts messages: {'OK' if report.fts.get('messages_fts', {}).get('integrity') == 'ok' else report.fts.get('messages_fts', {}).get('integrity', 'unknown')}\n"
            f"fts messages_trigram: {'OK' if report.fts.get('messages_fts_trigram', {}).get('integrity') == 'ok' else report.fts.get('messages_fts_trigram', {}).get('integrity', 'unknown')}\n"
            f"foreign keys: {report.foreign_key_violations} violation(s)\n"
            f"summary: {marker} - {report.summary}\n"
            f"events: {', '.join(report.events) or '(none)'}"
        )
    # Exit codes:
    #   0 = OK / WARNING (continue, log only)
    #   1 = DEGRADED (any *; not blocking but alertable)
    #   2 = CORRUPT (structured alert)
    #   3 = RECOVERY_REQUIRED (must refuse to start)
    if report.severity == dbh.OK or report.severity == dbh.WARNING:
        return 0
    if report.severity in (dbh.DEGRADED_FK, dbh.DEGRADED_FTS, dbh.DEGRADED_WAL):
        return 1
    if report.severity == dbh.CORRUPT:
        return 2
    return 3


def cmd_backup(args: argparse.Namespace) -> int:
    import agent.db_connection as dbc
    import agent.db_maintenance as dbm
    p = _state_db_path()
    backup_root = _hermes_home() / "backups" / "snapshots"
    backup_root.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    dest = backup_root / f"state.db.{ts}.vacuum"
    # Use the existing VACUUM INTO; it's safe while the gateway is open.
    res = dbc.vacuum_into(p, dest)
    if not res.get("ok"):
        if args.json:
            _print_json({"status": "FAILED", "error": res.get("error")})
        else:
            _print_human(f"backup FAILED: {res.get('error')}")
        return 1
    # Verify the snapshot via quick_check.
    qc_ok, qc_detail = dbc.quick_check(dest)
    if not qc_ok:
        dest.unlink(missing_ok=True)
        if args.json:
            _print_json({"status": "FAILED", "verify": qc_detail,
                         "snapshot": str(dest)})
        else:
            _print_human(f"backup snapshot failed verification: {qc_detail}")
        return 1
    if args.json:
        _print_json({"status": "OK", "snapshot": str(dest), "size": res["size"],
                     "verify": "ok"})
    else:
        _print_human(f"backup OK: {dest} ({res['size']:,} bytes, integrity=ok)")
    return 0


def cmd_repair_fts(args: argparse.Namespace) -> int:
    import agent.db_maintenance as dbm
    import agent.db_health as dbh
    import agent.db_recover as dbr
    p = _state_db_path()
    expected = tuple(args.expected_fts) if args.expected_fts else (
        dbh.DEFAULT_EXPECTED_FTS_TABLES)
    if not args.no_lock:
        with dbm.MaintenanceLock(p, reason="repair-fts",
                                  timeout=args.timeout):
            dbm.wait_for_no_holders(p, timeout=args.timeout)
            report = dbr.repair_fts(p, include_trigram=not args.no_trigram,
                                     dry_run=args.dry_run,
                                     expected_fts=expected)
    else:
        report = dbr.repair_fts(p, include_trigram=not args.no_trigram,
                                 dry_run=args.dry_run,
                                 expected_fts=expected)
    if args.json:
        _print_json(report)
    else:
        _print_human(
            f"FTS rebuild status: {report.get('status')}\n"
            f"  events: {report.get('events', report.get('fts_pre', {}))}\n"
            f"  integrity: {report.get('fts_post', report.get('fts_pre', {}))}"
        )
    return 0 if report.get("status") in ("SUCCESS", "NO_REBUILD_NEEDED",
                                            "DRY_RUN_WOULD_REBUILD") else 1


def cmd_recover(args: argparse.Namespace) -> int:
    import agent.db_recover as dbr
    report = dbr.recover_state_db(
        _state_db_path(),
        strategy=args.strategy,
        reason=args.reason or "cli-recover",
        holder_wait_timeout=args.timeout,
    )
    if args.json:
        _print_json(report)
    else:
        _print_human(
            f"Recovery status: {report.get('status')}\n"
            f"  recovery_id : {report.get('recovery_id')}\n"
            f"  strategy    : {report.get('strategy')}\n"
            f"  quarantined : {report.get('quarantined', '-')}\n"
            f"  install     : {report.get('install', {}).get('status', '-')}\n"
            f"  ended_at    : {report.get('ended_at')}"
        )
    return 0 if report.get("status") in ("SUCCESS", "QUICK_CHECK_OK") else 1


def cmd_restore(args: argparse.Namespace) -> int:
    import agent.db_connection as dbc
    import agent.db_maintenance as dbm
    backup = Path(args.backup).expanduser().resolve()
    if not backup.exists():
        _print_human(f"backup not found: {backup}")
        return 2
    # Verify the backup before installing.
    qc_ok, qc_detail = dbc.quick_check(backup)
    if not qc_ok:
        _print_human(f"backup failed verification: {qc_detail}")
        return 1
    p = _state_db_path()
    with dbm.MaintenanceLock(p, reason=f"restore:{backup.name}",
                              timeout=args.timeout):
        dbm.wait_for_no_holders(p, timeout=args.timeout)
        # Copy to staging, then install via the atomic path.
        staging = p.parent / "recovery" / time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        staging.mkdir(parents=True, exist_ok=True)
        staged = staging / "restored.db"
        shutil.copy2(backup, staged)
        install = dbm.install_state_db_recovered(p, staged, dry_run=args.dry_run,
                                                 reason=f"restore:{backup.name}")
    if args.json:
        _print_json({"verify": qc_detail, "install": install})
    else:
        _print_human(
            f"Restore verify: {qc_detail}\n"
            f"Install status : {install.get('status')}\n"
            f"Install inode  : {install.get('inode')}"
        )
    return 0 if install.get("status") == "SUCCESS" else 1


def cmd_holders(args: argparse.Namespace) -> int:
    import agent.db_maintenance as dbm
    p = _state_db_path()
    holders = dbm.state_db_holders(p, include_wal=True)
    if args.json:
        _print_json({"path": str(p), "holders": holders})
    else:
        if not holders:
            _print_human("(no holders)")
        else:
            for h in holders:
                _print_human(f"pid={h.get('pid'):>6} user={h.get('user'):<10} cmd={h.get('command')}")
    return 0


def cmd_archive_validate(args: argparse.Namespace) -> int:
    """Validate an archive candidate against the operator-mandated invariants.

    Per operator spec 2026-09-04: an archive candidate is SUCCESS only when
    integrity_check=ok AND foreign_key_check=0 rows AND canonical reconciliation
    matches PRE. Anything else is reported honestly — never a one-line OK.
    """
    import sqlite3

    import agent.db_maintenance as dbm

    live = _state_db_path()
    archive = Path(args.archive).expanduser().resolve() if args.archive else \
              _hermes_home() / "state-archive.db"

    # Capture PRE counts from the current state.db (the candidate's "before"
    # state). The archive script records these before the move so we can
    # verify reconciliation; we do the same here so callers don't have to.
    pre_live: dict = {}
    pre_archive: dict = {}
    try:
        conn = sqlite3.connect(str(live))
        canonical = dbm._canonical_tables(conn)
        for t in canonical:
            try:
                pre_live[t] = conn.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0]
            except Exception:
                pre_live[t] = 0
        conn.close()
    except Exception as e:
        if args.json:
            _print_json({"status": "FAILED_VALIDATION",
                         "error": f"cannot read live: {e}"})
        else:
            _print_human(f"FAILED: cannot read live {live}: {e}")
        return 1

    if archive.exists():
        try:
            conn = sqlite3.connect(str(archive))
            canonical = dbm._canonical_tables(conn)
            for t in canonical:
                try:
                    pre_archive[t] = conn.execute(
                        f'SELECT count(*) FROM "{t}"').fetchone()[0]
                except Exception:
                    pre_archive[t] = 0
            conn.close()
        except Exception as e:
            if args.json:
                _print_json({"status": "FAILED_VALIDATION",
                             "error": f"cannot read archive: {e}"})
            else:
                _print_human(f"FAILED: cannot read archive {archive}: {e}")
            return 1

    report = dbm.validate_archive_candidate(
        live, archive,
        dry_run=True,
        pre_live_counts=pre_live,
        pre_archive_counts=pre_archive,
    )

    if args.json:
        _print_json(report)
    else:
        status = report.get("status", "UNKNOWN")
        if status == dbm.ArchiveStatus.SUCCESS:
            _print_human(f"STATUS: SUCCESS")
            _print_human(
                f"  live integrity={report['live_integrity']} "
                f"archive integrity={report['archive_integrity']}"
            )
            _print_human(
                f"  live FK={report['live_fk_count']} "
                f"archive FK={report['archive_fk_count']}"
            )
        elif status == dbm.ArchiveStatus.SUCCESS_WITH_WARNINGS:
            _print_human(f"STATUS: SUCCESS_WITH_WARNINGS")
            for w in report.get("warnings", []):
                _print_human(f"  WARN: {w}")
        else:
            _print_human(f"STATUS: {status}")
            for hf in report.get("hard_failures", []):
                _print_human(f"  FAIL: {hf}")

        # Print reconciliation table
        recon = report.get("canonical_reconciliation", {})
        rows = recon.get("rows", [])
        if rows:
            _print_human("")
            _print_human(
                f"  {'TABLE':<28} {'PRE L':>8} {'PRE A':>8} "
                f"{'POST L':>8} {'POST A':>8} {'dL':>5} {'dA':>5} STATUS"
            )
            for r in rows:
                _print_human(
                    f"  {r['table']:<28} {r['pre_live']:>8,} {r['pre_archive']:>8,} "
                    f"{r['post_live']:>8,} {r['post_archive']:>8,} "
                    f"{r['delta_live']:>+5,} {r['delta_archive']:>+5,} {r['status']}"
                )

    # Exit code reflects three-status outcome
    if report["status"] == dbm.ArchiveStatus.SUCCESS:
        return 0
    if report["status"] == dbm.ArchiveStatus.SUCCESS_WITH_WARNINGS:
        return 2
    return 1  # FAILED_VALIDATION


def cmd_pending(args: argparse.Namespace) -> int:
    import agent.pending_messages as pm
    msgs = pm.list_pending(_hermes_home(), state=args.state)
    if args.json:
        _print_json({"count": len(msgs), "messages": [m.to_dict() for m in msgs]})
    else:
        _print_human(f"{len(msgs)} pending message(s)")
        for m in msgs:
            _print_human(
                f"  {m.id} state={m.state} platform={m.platform} "
                f"profile={m.profile} sender={m.sender} attempts={m.attempt_count}"
            )
    return 0


def cmd_replay_pending(args: argparse.Namespace) -> int:
    import agent.pending_messages as pm
    def _noop(msg):
        return {"ok": True, "duplicate": False}
    summary = pm.replay_pending(_hermes_home(), process_fn=_noop,
                                  mark_replayed=not args.dry_run)
    if args.json:
        _print_json(summary)
    else:
        _print_human(
            f"replayed={summary['replayed']} duplicates={summary['duplicates']} "
            f"failed={summary['failed']} skipped={summary['skipped']}"
        )
    return 0 if summary["failed"] == 0 else 1


def cmd_maintenance_on(args: argparse.Namespace) -> int:
    import agent.db_maintenance as dbm
    p = _state_db_path()
    lock = dbm.MaintenanceLock(p, reason=args.reason, recovery_id=args.recovery_id,
                                timeout=args.timeout)
    # Acquire then immediately hold (don't exit the context manager).
    lock.__enter__()
    if args.json:
        _print_json({"status": "ON", "reason": args.reason,
                     "recovery_id": args.recovery_id, "lock": str(lock.lock_path)})
    else:
        _print_human(
            f"Maintenance ON: reason={args.reason!r} recovery_id={args.recovery_id!r}\n"
            f"Lock file: {lock.lock_path}\n"
            f"Use `hermes db maintenance-off` to clear (after verification)."
        )
    # NOTE: this command BLOCKS until the user runs maintenance-off, by design.
    # For a non-blocking variant, pass --hold=false (not implemented here; the
    # operator is expected to know what they are doing when they go into
    # maintenance mode).
    if args.hold:
        try:
            while True:
                time.sleep(60)
        except KeyboardInterrupt:
            lock.__exit__(None, None, None)
    return 0


def cmd_maintenance_off(args: argparse.Namespace) -> int:
    import agent.db_maintenance as dbm
    p = _state_db_path()
    lock_path = dbm.maintenance_lock_path(p)
    if lock_path.exists():
        lock_path.unlink()
    if args.json:
        _print_json({"status": "OFF", "lock": str(lock_path)})
    else:
        _print_human(f"Maintenance OFF (removed {lock_path})")
    return 0


def cmd_preflight(args: argparse.Namespace) -> int:
    """For systemd ExecStartPre: refuse to start if maintenance is active
    OR the DB is unrecoverable. Returns:
        0  → safe to start
        1  → maintenance is active; refuse
        2  → recovery required; refuse
        3  → DB missing; refuse
    """
    import agent.db_health as dbh
    import agent.db_maintenance as dbm
    p = _state_db_path()
    lock_path = dbm.maintenance_lock_path(p)
    if lock_path.exists():
        holder = dbm.read_holder_metadata(lock_path)
        if holder:
            if args.json:
                _print_json({"status": "BLOCKED", "reason": "maintenance-active",
                             "holder": holder})
            else:
                _print_human(
                    f"BLOCKED: maintenance lock active (pid={holder.get('pid')} "
                    f"reason={holder.get('reason')!r}). Refusing to start."
                )
            return 1
    if not p.exists():
        if args.json:
            _print_json({"status": "BLOCKED", "reason": "db-missing", "path": str(p)})
        else:
            _print_human(f"BLOCKED: state.db does not exist at {p}")
        return 3
    report = dbh.classify(p, full=False, persist_inode=False)
    if report.severity == dbh.RECOVERY_REQUIRED:
        if args.json:
            _print_json({"status": "BLOCKED", "reason": "recovery-required",
                         "summary": report.summary})
        else:
            _print_human(
                f"BLOCKED: state.db requires recovery. {report.summary}\n"
                f"Run: hermes db recover --strategy 2"
            )
        return 2
    return 0


# ──────────────────────────────────────────────────────────────────────
# Parser builder
# ──────────────────────────────────────────────────────────────────────

def build_db_parser(subparsers) -> argparse.ArgumentParser:
    """Attach `hermes db` to the main parser. Returns the parser for
    caller-side default-func wiring (main.py needs to dispatch subcommands
    because the subcommand handler functions live in agent.db_admin)."""
    parser: argparse.ArgumentParser = subparsers.add_parser(
        "db",
        help="State database reliability and recovery subcommands (Section T).",
    )
    sub = parser.add_subparsers(dest="db_cmd", required=True)

    p_status = sub.add_parser("status", help="One-screen health summary.")
    p_status.add_argument("--full", action="store_true",
                          help="Use full integrity_check.")
    p_status.add_argument("--json", action="store_true")
    p_status.set_defaults(func=cmd_status)

    p_check = sub.add_parser("check", help="Run quick_check + FTS only (or full).")
    p_check.add_argument("--full", action="store_true",
                         help="Add integrity_check and disk diagnostics.")
    p_check.add_argument("--json", action="store_true")
    p_check.set_defaults(func=cmd_check)

    p_backup = sub.add_parser("backup",
                              help="VACUUM INTO snapshot, verify, write under backups/snapshots.")
    p_backup.add_argument("--json", action="store_true")
    p_backup.set_defaults(func=cmd_backup)

    p_fts = sub.add_parser("repair-fts",
                            help="Rebuild messages_fts (+ trigram) under the maintenance lock.")
    p_fts.add_argument("--no-trigram", action="store_true",
                        help="Skip the trigram vtable rebuild.")
    p_fts.add_argument("--no-lock", action="store_true",
                        help="DO NOT acquire the maintenance lock (operator override).")
    p_fts.add_argument("--dry-run", action="store_true")
    p_fts.add_argument("--timeout", type=float, default=60.0)
    p_fts.add_argument("--expected-fts", nargs="+", default=None,
                        help="Override the expected FTS vtable names "
                             "(default: messages_fts messages_fts_trigram).")
    p_fts.add_argument("--json", action="store_true")
    p_fts.set_defaults(func=cmd_repair_fts)

    p_rec = sub.add_parser("recover",
                            help="Run a recovery strategy under the maintenance lock.")
    p_rec.add_argument("--strategy", type=int, default=0,
                        choices=[0, 1, 2],
                        help="0=quick_check+reopen, 1=VACUUM INTO, 2=header-splice+.recover")
    p_rec.add_argument("--reason", default="cli-recover")
    p_rec.add_argument("--timeout", type=float, default=60.0)
    p_rec.add_argument("--json", action="store_true")
    p_rec.set_defaults(func=cmd_recover)

    p_rest = sub.add_parser("restore",
                             help="Restore from a VACUUM INTO snapshot, after verification.")
    p_rest.add_argument("backup", help="Path to the verified snapshot")
    p_rest.add_argument("--dry-run", action="store_true")
    p_rest.add_argument("--timeout", type=float, default=60.0)
    p_rest.add_argument("--json", action="store_true")
    p_rest.set_defaults(func=cmd_restore)

    p_holders = sub.add_parser("holders",
                                help="List processes with state.db (or WAL/SHM) open.")
    p_holders.add_argument("--json", action="store_true")
    p_holders.set_defaults(func=cmd_holders)

    p_arc = sub.add_parser("archive-validate",
                           help="Validate an archive candidate against FK + "
                                "reconciliation invariants. Returns SUCCESS / "
                                "SUCCESS_WITH_WARNINGS / FAILED_VALIDATION "
                                "(operator-mandated 2026-09-04).")
    p_arc.add_argument("--archive", default=None,
                       help="Path to the archive DB (default: <HERMES_HOME>/state-archive.db)")
    p_arc.add_argument("--json", action="store_true")
    p_arc.set_defaults(func=cmd_archive_validate)

    p_pending = sub.add_parser("pending",
                                help="List queued pending_messages.")
    p_pending.add_argument("--state", default=None,
                            choices=["queued", "replaying", "replayed", "failed"])
    p_pending.add_argument("--json", action="store_true")
    p_pending.set_defaults(func=cmd_pending)

    p_replay = sub.add_parser("replay-pending",
                               help="Idempotently replay queued messages.")
    p_replay.add_argument("--dry-run", action="store_true")
    p_replay.add_argument("--json", action="store_true")
    p_replay.set_defaults(func=cmd_replay_pending)

    p_on = sub.add_parser("maintenance-on",
                           help="Acquire the maintenance lock and HOLD it (blocks).")
    p_on.add_argument("reason", help="Human-readable reason for the maintenance window.")
    p_on.add_argument("--recovery-id", default=None)
    p_on.add_argument("--timeout", type=float, default=0.0)
    p_on.add_argument("--no-hold", dest="hold", action="store_false")
    p_on.add_argument("--json", action="store_true")
    p_on.set_defaults(func=cmd_maintenance_on, hold=True)

    p_off = sub.add_parser("maintenance-off",
                            help="Clear the maintenance marker after verification.")
    p_off.add_argument("--json", action="store_true")
    p_off.set_defaults(func=cmd_maintenance_off)

    p_pre = sub.add_parser("preflight",
                            help="For systemd ExecStartPre: refuse to start if unsafe.")
    p_pre.add_argument("--json", action="store_true")
    p_pre.set_defaults(func=cmd_preflight)

    return parser


# ──────────────────────────────────────────────────────────────────────
# Standalone entry point
# ──────────────────────────────────────────────────────────────────────

def _run_argv(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="hermes db", description=__doc__)
    sub = parser.add_subparsers(dest="db_cmd", required=True)
    # Don't call build_db_parser(sub) here because that would create a
    # second "db" level. Instead, lift the inner subcommands out so the
    # standalone CLI matches the wired-in `hermes db <subcommand>` shape.
    # We re-build the subcommands inline to avoid the nesting.
    _add_db_subcommands(sub)
    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


def _add_db_subcommands(sub) -> None:
    """Duplicate the inner subcommands so `python -m hermes_cli.subcommands.db_admin <cmd>`
    works without needing the outer `db` parser."""
    p_status = sub.add_parser("status", help="One-screen health summary.")
    p_status.add_argument("--full", action="store_true")
    p_status.add_argument("--json", action="store_true")
    p_status.set_defaults(func=cmd_status)

    p_check = sub.add_parser("check", help="Quick_check + FTS only (or full).")
    p_check.add_argument("--full", action="store_true")
    p_check.add_argument("--json", action="store_true")
    p_check.set_defaults(func=cmd_check)

    p_pre = sub.add_parser("preflight", help="Refuse to start if unsafe (for ExecStartPre).")
    p_pre.add_argument("--json", action="store_true")
    p_pre.set_defaults(func=cmd_preflight)

    p_holders = sub.add_parser("holders", help="List processes with state.db open.")
    p_holders.add_argument("--json", action="store_true")
    p_holders.set_defaults(func=cmd_holders)

    p_pending = sub.add_parser("pending", help="List queued pending_messages.")
    p_pending.add_argument("--state", default=None,
                            choices=["queued", "replaying", "replayed", "failed"])
    p_pending.add_argument("--json", action="store_true")
    p_pending.set_defaults(func=cmd_pending)

    p_backup = sub.add_parser("backup", help="VACUUM INTO snapshot.")
    p_backup.add_argument("--json", action="store_true")
    p_backup.set_defaults(func=cmd_backup)

    p_fts = sub.add_parser("repair-fts", help="Rebuild FTS5 vtables.")
    p_fts.add_argument("--no-trigram", action="store_true")
    p_fts.add_argument("--no-lock", action="store_true")
    p_fts.add_argument("--dry-run", action="store_true")
    p_fts.add_argument("--timeout", type=float, default=60.0)
    p_fts.add_argument("--expected-fts", nargs="+", default=None)
    p_fts.add_argument("--json", action="store_true")
    p_fts.set_defaults(func=cmd_repair_fts)

    p_rec = sub.add_parser("recover", help="Run a recovery strategy.")
    p_rec.add_argument("--strategy", type=int, default=0, choices=[0, 1, 2])
    p_rec.add_argument("--reason", default="cli-recover")
    p_rec.add_argument("--timeout", type=float, default=60.0)
    p_rec.add_argument("--json", action="store_true")
    p_rec.set_defaults(func=cmd_recover)

    p_rest = sub.add_parser("restore", help="Restore from a verified snapshot.")
    p_rest.add_argument("backup")
    p_rest.add_argument("--dry-run", action="store_true")
    p_rest.add_argument("--timeout", type=float, default=60.0)
    p_rest.add_argument("--json", action="store_true")
    p_rest.set_defaults(func=cmd_restore)

    p_replay = sub.add_parser("replay-pending", help="Idempotent replay.")
    p_replay.add_argument("--dry-run", action="store_true")
    p_replay.add_argument("--json", action="store_true")
    p_replay.set_defaults(func=cmd_replay_pending)

    p_on = sub.add_parser("maintenance-on", help="Acquire and HOLD the maintenance lock.")
    p_on.add_argument("reason")
    p_on.add_argument("--recovery-id", default=None)
    p_on.add_argument("--timeout", type=float, default=0.0)
    p_on.add_argument("--no-hold", dest="hold", action="store_false")
    p_on.add_argument("--json", action="store_true")
    p_on.set_defaults(func=cmd_maintenance_on, hold=True)

    p_off = sub.add_parser("maintenance-off", help="Clear the maintenance marker.")
    p_off.add_argument("--json", action="store_true")
    p_off.set_defaults(func=cmd_maintenance_off)


if __name__ == "__main__":
    sys.exit(_run_argv(sys.argv[1:]))
