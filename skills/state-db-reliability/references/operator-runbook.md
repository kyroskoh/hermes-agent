# State-DB Operator Runbook

When `/root/.hermes/state.db` is unhealthy, this runbook tells you which
`hermes db` command to run. The commands are designed to be safe by
default: they acquire the maintenance lock, refuse to write if the DB
is unhealthy, and emit structured reports.

## Step 1 — Identify the problem

```bash
hermes db status          # one-screen summary, fast (~50ms)
hermes db check --json    # full structured report, ~3-8s on 235MB
hermes db holders         # who has the DB open right now
```

If `status` reports `Severity: RECOVERY_REQUIRED`, jump to Step 4.
If `Severity: DEGRADED_FTS`, jump to Step 3.
If `Severity: WARNING` or `DEGRADED_FK`, the system is operational; the
report lists which check failed. Most cases of `DEGRADED_FK` are
historical orphans that survived prior recoveries — they do NOT block
new writes (the connection factory now sets `foreign_keys=ON`, so new
orphan writes are refused).

## Step 2 — Free the DB

Before any destructive operation, all Hermes processes that touch
state.db must be stopped:

```bash
# Identify holders
hermes db holders

# Stop them gracefully
systemctl stop hermes-gateway.service
systemctl stop hermes-dashboard.service hermes-dashboard-wilnice.service
```

The maintenance lock enforces this: any `hermes db recover ...` will
poll `fuser` AND probe with `BEGIN IMMEDIATE` for up to 30 seconds; if
a writer is still present, the recovery aborts. Do NOT use `--force`
or other bypasses.

## Step 3 — Repair FTS only (no DB-level damage)

```bash
# Dry-run first
hermes db repair-fts --dry-run --json

# Real rebuild
hermes db repair-fts
```

This is the right call when:
- `messages_fts` or `messages_fts_trigram` is missing / corrupted.
- `quick_check` is OK.
- `integrity_check` is OK.

The command will NOT run if `integrity_check` fails; it returns
`ABORT_CORE_INTEGRITY_FAILED` and exits 1. In that case, escalate to
Step 4.

## Step 4 — Full recovery

```bash
# Quick check + reopen — for stale-WAL-only issues
hermes db recover --strategy 0

# VACUUM INTO install — for a healthy DB that needs defrag
hermes db recover --strategy 1

# Header-splice + .recover — for a destroyed page 1 (post-crash)
hermes db recover --strategy 2
```

Strategy 2 is the proven path from the 2026-09-03 incident. It:

1. Quarantines the corrupt DB to `state.db.<recovery_id>`.
2. Header-splices a donor SQLite header with the correct page count.
3. Runs `sqlite3 ... .recover` into a staging file.
4. Drops unconstructible FTS5 vtables from the recovered DB.
5. Validates with `PRAGMA quick_check` + `PRAGMA integrity_check`.
6. Atomic `os.replace` install.
7. fsync the parent directory.
8. Writes `/var/lib/hermes/last-recovery-report.json` with the full
   report.

The recovery report shows counts of every canonical table, the FTS
status, the install inode, and any error.

## Step 5 — Replay pending messages

If messages came in during the recovery window, replay them
idempotently:

```bash
hermes db pending                  # list queued messages
hermes db replay-pending --dry-run # preview what would be replayed
hermes db replay-pending           # commit
```

The replay path is keyed on `(platform, profile, sender,
platform_message_id)` so the same inbound message never produces two
database rows even if replay is run multiple times.

## Step 6 — Bring services back up

```bash
# Start dashboards (port :9119)
systemctl start hermes-dashboard.service hermes-dashboard-wilnice.service

# Start the gateway
systemctl start hermes-gateway.service

# Verify
hermes db status
```

If a unit refuses to start with "BLOCKED: maintenance lock active",
you forgot Step 2. If a unit refuses with "BLOCKED: state.db requires
recovery", Step 4 failed silently — re-run with `--json` and check
`/var/lib/hermes/last-recovery-report.json`.

## Postmortem

After the recovery:

- Read `/var/lib/hermes/db-event-log.jsonl` for the structured event
  timeline.
- Read `/var/lib/hermes/last-recovery-report.json` for the full
  install report.
- Cross-check the new `state.db` row counts against the last known
  good count (the prior `last-recovery-report.json` has the numbers).
- Update the runbook if a new failure mode surfaced.
- If the recovery was triggered by a self-heal loop, fix the loop
  (the 2026-09-03 incident was caused by
  `hermes-restart-all.service` being repeatedly SIGTERM'd by the
  fleet-self-heal cron because the oneshot unit's healthy state is
  `inactive`, not `active`).
