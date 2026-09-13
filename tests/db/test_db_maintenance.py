"""Standalone tests for ``agent.db_maintenance``.

Hermes's pytest isn't installed in this venv, so this module runs as a
unittest TestCase and can be executed directly:

    /usr/local/lib/hermes-agent/.venv/bin/python3 -m tests.db.test_db_maintenance

Covers spec sections B (safe shutdown), H (atomic recovery), and
I (WAL/SHM safety).
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, "/usr/local/lib/hermes-agent")

from agent import db_maintenance as dbm


def _make_state_dir() -> tempfile.TemporaryDirectory:
    td = tempfile.TemporaryDirectory()
    db = Path(td.name) / "state.db"
    c = sqlite3.connect(str(db))
    c.executescript(
        """
        CREATE TABLE sessions(id TEXT PRIMARY KEY, started_at REAL);
        CREATE TABLE messages(id INTEGER PRIMARY KEY, session_id TEXT,
                              content TEXT);
        INSERT INTO sessions VALUES ('s1', 1.0);
        INSERT INTO messages VALUES (1, 's1', 'hello');
        """
    )
    c.commit()
    c.close()
    return td


class MaintenanceLockTests(unittest.TestCase):
    def test_lock_path_is_outside_db_dir(self):
        with _make_state_dir() as td:
            db = Path(td) / "state.db"
            lock = dbm.maintenance_lock_path(db)
            self.assertEqual(lock, db.with_name("state.db.maintenance.lock"))
            self.assertEqual(lock.parent, db.parent)

    def test_exclusive_lock_blocks_writer(self):
        with _make_state_dir() as td:
            db = Path(td) / "state.db"
            with dbm.MaintenanceLock(db, reason="test", timeout=0):
                with self.assertRaises(dbm.MaintenanceActive):
                    with dbm.assert_writer_safe(db, timeout=0.5):
                        pass

    def test_writer_safe_when_no_lock(self):
        with _make_state_dir() as td:
            db = Path(td) / "state.db"
            with dbm.assert_writer_safe(db, timeout=1.0):
                self.assertTrue(os.path.exists(db))

    def test_atomic_install_replaces_db_atomically(self):
        with _make_state_dir() as td:
            db = Path(td) / "state.db"
            recovered = Path(td) / "recovered.db"
            rc = sqlite3.connect(str(recovered))
            rc.executescript(
                """
                CREATE TABLE sessions(id TEXT PRIMARY KEY, started_at REAL);
                CREATE TABLE messages(id INTEGER PRIMARY KEY, session_id TEXT,
                                      content TEXT);
                INSERT INTO sessions VALUES ('s1', 1.0);
                INSERT INTO messages VALUES (1, 's1', 'hello-recovered');
                INSERT INTO messages VALUES (2, 's1', 'extra');
                """
            )
            rc.commit()
            rc.close()
            with dbm.MaintenanceLock(db, reason="test-install", timeout=5):
                report = dbm.install_state_db_recovered(
                    db, recovered, dry_run=False, holder_wait_timeout=1.0,
                )
            self.assertEqual(report["status"], "SUCCESS")
            self.assertTrue(db.exists())
            self.assertFalse(recovered.exists(), "recovered should have been renamed")
            c = sqlite3.connect(str(db))
            rows = c.execute("SELECT id, content FROM messages ORDER BY id").fetchall()
            self.assertEqual(rows, [(1, "hello-recovered"), (2, "extra")])
            c.close()

    def test_install_aborts_with_live_holder(self):
        with _make_state_dir() as td:
            db = Path(td) / "state.db"
            recovered = Path(td) / "recovered.db"
            sqlite3.connect(str(recovered)).close()
            holder = sqlite3.connect(str(db), timeout=10)
            holder.execute("BEGIN IMMEDIATE")
            holder.execute("INSERT INTO messages VALUES (2, 's1', 'blocker')")
            try:
                with dbm.MaintenanceLock(db, reason="test-holder", timeout=5):
                    with self.assertRaises(dbm.WriterStillPresent):
                        dbm.install_state_db_recovered(
                            db, recovered, dry_run=False, holder_wait_timeout=2.0,
                        )
            finally:
                holder.rollback()
                holder.close()

    def test_install_dry_run(self):
        with _make_state_dir() as td:
            db = Path(td) / "state.db"
            recovered = Path(td) / "recovered.db"
            sqlite3.connect(str(recovered)).close()
            with dbm.MaintenanceLock(db, reason="dry-run", timeout=5):
                report = dbm.install_state_db_recovered(
                    db, recovered, dry_run=True, holder_wait_timeout=1.0,
                )
            self.assertEqual(report["status"], "DRY_RUN")
            self.assertTrue(db.exists())
            self.assertTrue(recovered.exists())

    def test_holder_metadata_roundtrip(self):
        with _make_state_dir() as td:
            db = Path(td) / "state.db"
            lock_path = dbm.maintenance_lock_path(db)
            with dbm.MaintenanceLock(db, reason="metadata-test",
                                     recovery_id="RID-12345", timeout=5):
                holder = dbm.read_holder_metadata(lock_path)
                self.assertIsNotNone(holder)
                self.assertEqual(holder["reason"], "metadata-test")
                self.assertEqual(holder["recovery_id"], "RID-12345")
                self.assertEqual(holder["pid"], os.getpid())

    def test_concurrent_lock_attempt_waits(self):
        with _make_state_dir() as td:
            db = Path(td) / "state.db"
            lock_path = dbm.maintenance_lock_path(db)
            lock_path.touch()
            acquired_at: list[float] = []

            def worker(idx: int, hold_for: float):
                with dbm.MaintenanceLock(db, reason=f"worker-{idx}", timeout=5):
                    acquired_at.append(time.monotonic())
                    time.sleep(hold_for)

            # Hold worker 1 longer than the poll_interval (0.25s) to make
            # the second worker's wait observable. We also assert the second
            # acquisition happens AFTER the first held window started.
            t1 = threading.Thread(target=worker, args=(1, 0.6))
            t1.start()
            time.sleep(0.1)  # give t1 time to acquire
            worker(2, 0.0)
            t1.join()
            self.assertEqual(len(acquired_at), 2)
            elapsed = acquired_at[1] - acquired_at[0]
            self.assertGreaterEqual(elapsed, 0.4,
                                    f"second worker waited only {elapsed:.3f}s")


if __name__ == "__main__":
    unittest.main(verbosity=2)
