---
name: state-db-reliability
description: Use when investigating /root/.hermes/state.db corruption, FTS5 issues, multi-profile secret-scope errors, or when planning recovery. Permanent reliability layer; one-shot scripts are recovery fallbacks only.
trigger: state.db corruption OR FTS5 rebuild OR UnscopedSecretError OR maintenance lock OR "hermes db" CLI OR atomic recovery OR VACUUM INTO under contention.
category: devops
provenance: shipped 2026-09-04 after the 3rd state.db corruption incident within 11 days. Replaces ad-hoc recovery scripts with a permanent layered architecture.
---

# State-DB Reliability Skill

Permanent reliability layer for `/root/.hermes/state.db`. Replaces ad-hoc
recovery scripts with a layered, testable architecture:

1. **Maintenance lock** (`agent/db_maintenance.py`) — `flock` on
   `/root/.hermes/state.db.maintenance.lock`. Every destructive operation
   (recovery, FTS rebuild, VACUUM INTO, restore) MUST hold the exclusive
   lock; every writer MUST refuse to open while the lock is held.

2. **Atomic recovery** (`agent/db_maintenance.install_state_db_recovered`)
   — never `cp` over `state.db`. Always: quarantine → recover into
   staging → validate → `os.replace` → `fsync` parent dir → release lock.

3. **Connection factory** (`agent/db_connection.py`) — every SQLite
   connection goes through `open_sqlite(...)` which applies WAL,
   synchronous=FULL, `foreign_keys=ON`, busy_timeout=5000, temp_store=MEMORY.
   Prevents new corruption from accumulating (the FK=ON fix alone would
   have caught the orphan rows that survived 2 prior recoveries).

4. **Health classifier** (`agent/db_health.py`) — one decision tree for
   `classify(path)`. Returns severity bands (OK / WARNING / DEGRADED_FK /
   DEGRADED_FTS / DEGRADED_WAL / CORRUPT / RECOVERY_REQUIRED). Never
   confuses FTS corruption with core DB corruption.

5. **Watchdog** (`agent/db_watchdog.py`) — `tick(path)` writes a JSON
   snapshot + structured event log. Cron-friendly; never mutates the DB.

6. **Recovery orchestration** (`agent/db_recover.py`) — strategy table
   (0 = quick_check only, 1 = VACUUM INTO install, 2 = header-splice +
   `.recover`). Repair-fts is separate and refuses to run when core
   integrity is broken.

7. **Pending messages** (`agent/pending_messages.py`) — atomic durable
   queue with idempotent replay keyed on
   `(platform, profile, sender, platform_message_id)`.

8. **Deferred check_fn** (`tools/registry.py`) — secret-bound tool
   availability is per-profile, not global. `CHECK_FN_UNRESOLVED` sentinel
   short-circuits `_check_fn_cached` to return `None` (unknown) when
   multiplex is active but no profile is installed. Tools are evaluated
   per turn, never falsely marked unavailable for the lifetime of the
   process.

9. **CLI** (`hermes_cli/subcommands/db_admin.py`) — `hermes db {status,
   check, backup, repair-fts, recover, restore, holders, pending,
   replay-pending, maintenance-on, maintenance-off, preflight}`. Wired
   into `hermes_cli/main.py` at the same level as `hermes doctor`.

## When to use this skill

- State.db corruption, repeated `file is not a database` errors, or
  gateway falling back to JSONL because SQLite is unreachable.
- FTS5 vtable errors (e.g. `vtable constructor failed: messages_fts`).
- `agent.secret_scope.UnscopedSecretError` raised at startup or in the
  multiplexer before profile resolution.
- Building systemd `ExecStartPre=` / ExecStop handlers for any Hermes
  unit (see `references/systemd-hardening.md`).
- Recovery planning: deciding which strategy to run, when to quarantine,
  when to restore from backup.
- Adding a new FTS5 vtable to the canonical schema — the classifier
  baseline must be updated (`DEFAULT_EXPECTED_FTS_TABLES`).

## How to use this skill

1. Read `references/state-db-reliability-design.md` for the design rationale
   and the per-section root-cause map.
2. Read `references/operator-runbook.md` for the operator-facing
   recovery playbook (which command to run in which state).
3. Read `references/systemd-hardening.md` for the systemd-level changes
   (ExecStartPre, TimeoutStopSec, maintenance marker).
4. Run `hermes db status` to get the current severity in one screen.
5. Run `hermes db check --full --json` for the full structured report
   (3-8s on a 235MB WAL DB).
6. If `repair-fts` is needed, run `hermes db repair-fts` — it acquires
   the maintenance lock itself, then validates core integrity before
   touching the FTS vtables.
7. If recovery is needed, run `hermes db recover --strategy 2` — that
   uses the proven header-splice + `.recover` path from the 2026-09-03
   incident.

## Anti-patterns this skill prevents

- ❌ `cp state.db state.db.recovered && mv state.db.recovered state.db`
  (replaces underneath a live writer; "FATAL: state.db was replaced
  underneath the process").
- ❌ `rm -f state.db-wal state.db-shm` while a writer is connected
  (orphans the WAL; next open panics with `database disk image is
  malformed`).
- ❌ Running `.recover` for an FTS-only problem (drops data when an FTS
  rebuild would have sufficed).
- ❌ Disabling `UnscopedSecretError` globally (leaks one profile's
  credentials into another's gateway turn).
- ❌ Disabling `PRAGMA foreign_keys` because "it makes writes slow"
  (lets orphans accumulate; we now set it ON in the factory).
- ❌ A `hermes-restart-all.service` loop that SIGTERMs a gateway holding
  a WAL-mode DB (this is what caused the 2026-09-03 incident).
