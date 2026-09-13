# Systemd Hardening for the State-DB Reliability Layer

The state.db reliability layer interacts with systemd in three places:
the gateway / dashboard `ExecStartPre`, the cron watchdog, and the
optional `db-maintenance` marker file. This document describes the
unit-file changes that make the system safe to operate unattended.

## 1. Preflight before every writer starts

Add the following to `/etc/systemd/system/hermes-gateway.service` and
`/etc/systemd/system/hermes-dashboard.service` (and the wilnice
variants):

```ini
[Service]
ExecStartPre=/usr/local/bin/hermes db preflight --json
TimeoutStopSec=60
Restart=on-failure
RestartSec=10
KillMode=mixed
```

The `preflight` command returns:

- 0 — safe to start.
- 1 — maintenance lock is held; refuse.
- 2 — recovery required; refuse.
- 3 — DB missing; refuse.

`TimeoutStopSec=60` gives Hermes a full minute to commit in-flight
transactions, run `PRAGMA wal_checkpoint(TRUNCATE)`, and close
SQLite connections cleanly. Without it, systemd SIGKILLs the gateway
at the 90s default during a long write and leaves the WAL in a torn
state.

## 2. Watchdog cron

`/etc/cron.d/hermes-state-db-watchdog`:

```cron
*/5 * * * * root /usr/local/lib/hermes-agent/.venv/bin/python3 -m agent.db_watchdog tick /root/.hermes/state.db --snapshot /var/lib/hermes/state-db-health.json --event-log /var/lib/hermes/db-event-log.jsonl 2>&1 | logger -t hermes-state-db
```

Every 5 minutes. Each tick:
- Runs `PRAGMA quick_check` (~50ms on a 235MB DB).
- Reads `state_meta` for FTS high-water markers.
- Reads `/var/log/hermes/*.log` for the JSONL-fallback warning.
- Writes `/var/lib/hermes/state-db-health.json`.
- Appends one structured event to `/var/lib/hermes/db-event-log.jsonl`.

The tick NEVER modifies state.db. Escalation to recovery is a
separate decision by a human (or by a deliberate `hermes db recover`
invocation).

## 3. Maintenance marker (operator-set)

`/root/.hermes/db-maintenance` is a touch-file the operator creates
when entering maintenance mode. The systemd `ExecStartPre` of the
gateway should refuse to start when this file exists:

```bash
# /usr/local/bin/hermes-gateway-preflight
#!/usr/bin/env bash
if [[ -f /root/.hermes/db-maintenance ]]; then
    echo "BLOCKED: maintenance marker present" >&2
    exit 1
fi
exec /usr/local/bin/hermes db preflight --json "$@"
```

The `hermes db maintenance-on "reason"` command creates the marker
AND acquires the maintenance lock atomically (one syscall). The
`hermes db maintenance-off` command removes the marker after
verification.

## 4. Restart-after-failure policy

Hermes gateway / dashboard services use `Restart=on-failure` with
`RestartSec=10` to debounce crash loops. The fleet self-heal cron
runs every 6 hours to detect drift, but **only** restarts units that
fail the probe (active state + port bound for dashboards).

Do NOT add `Restart=always` to any state-db-touching unit without
explicit operator consent. A `Restart=always` policy on a unit that
keeps trying to write to a half-recovered DB will cause the failure
to repeat indefinitely.

## 5. KillMode

`KillMode=mixed` (the default in newer systemd) is the right choice.
`KillMode=process` would let child processes survive a SIGTERM and
hold the DB open after the main Hermes process exits. `KillMode=control-group`
would kill children but the cgroup might not have time to flush the
WAL. `mixed` (SIGTERM to main, SIGKILL to children after timeout)
gives Hermes a chance to close SQLite cleanly while preventing
orphaned child processes.

## 6. Cron install

```bash
sudo install -m 644 /tmp/hermes-state-db-watchdog.cron /etc/cron.d/hermes-state-db-watchdog
sudo systemctl daemon-reload
sudo systemctl restart hermes-gateway.service
```

Verify the cron is active:

```bash
ls -l /etc/cron.d/hermes-state-db-watchdog
systemctl list-timers | grep hermes
```

## 7. Roll-back

If the new layer causes a regression:

```bash
# Remove the cron
sudo rm /etc/cron.d/hermes-state-db-watchdog

# Revert the ExecStartPre by editing the unit file and removing the line
sudo systemctl daemon-reload
sudo systemctl restart hermes-gateway.service

# The maintenance lock can be cleared with:
hermes db maintenance-off

# Or manually:
rm -f /root/.hermes/state.db.maintenance.lock
```

The new `agent/db_*.py` modules are pure additive — none of them
modify the schema or existing write paths. Removing the cron and the
ExecStartPre returns the system to its pre-change behavior; the new
modules still work as standalone tools.
