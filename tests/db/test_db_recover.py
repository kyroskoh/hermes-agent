"""Standalone tests for ``agent.db_recover`` and the FTS rebuild path.

Covers spec sections D (FTS rebuild) and H (atomic recovery).
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, "/usr/local/lib/hermes-agent")

from agent import db_connection as dbc
from agent import db_health as dbh
from agent import db_maintenance as dbm
from agent import db_recover as dbr


def _make_db_with_fts(tmp: Path, rows: int = 100) -> Path:
    db = tmp / "state.db"
    c = sqlite3.connect(str(db))
    c.executescript(
        """
        CREATE TABLE sessions(id TEXT PRIMARY KEY, started_at REAL);
        CREATE TABLE messages(id INTEGER PRIMARY KEY, session_id TEXT,
                              content TEXT, role TEXT NOT NULL DEFAULT 'user',
                              tool_name TEXT, tool_calls TEXT);
        CREATE TABLE state_meta(key TEXT PRIMARY KEY, value TEXT);
        CREATE VIRTUAL TABLE messages_fts USING fts5(
            content, tool_name, tool_calls,
            content='messages', content_rowid='id'
        );
        CREATE TRIGGER messages_ai AFTER INSERT ON messages BEGIN
            INSERT INTO messages_fts(rowid, content, tool_name, tool_calls)
            VALUES (new.id, new.content, new.tool_name, new.tool_calls);
        END;
        """
    )
    c.execute("INSERT INTO sessions VALUES ('s1', 1.0)")
    for i in range(rows):
        c.execute(
            "INSERT INTO messages(session_id, content, tool_name) "
            "VALUES (?, ?, ?)",
            ("s1", f"row {i}", f"tool{i % 5}"),
        )
    c.commit()
    c.close()
    return db


class RepairFtsTests(unittest.TestCase):
    def test_no_rebuild_needed_when_fts_healthy(self):
        with tempfile.TemporaryDirectory() as td:
            db = _make_db_with_fts(Path(td))
            report = dbr.repair_fts(db, dry_run=False)
            self.assertIn(report["status"],
                          ("NO_REBUILD_NEEDED", "SUCCESS"))

    def test_rebuild_when_fts_dropped(self):
        with tempfile.TemporaryDirectory() as td:
            db = _make_db_with_fts(Path(td))
            # Drop the FTS table to simulate a destroyed vtable.
            c = sqlite3.connect(str(db))
            c.execute("DROP TABLE messages_fts")
            c.commit()
            c.close()

            with dbm.MaintenanceLock(db, reason="fts-test", timeout=5):
                dbm.wait_for_no_holders(db, timeout=2.0)
                report = dbr.repair_fts(db, dry_run=False,
                                         expected_fts=("messages_fts",))
            self.assertEqual(report["status"], "SUCCESS")
            # Verify FTS is back.
            res = dbc.fts_integrity_check(db, fts_names=["messages_fts"])
            self.assertTrue(res["messages_fts"]["exists"])
            self.assertEqual(res["messages_fts"]["integrity"], "ok")

    def test_abort_when_core_integrity_failed(self):
        with tempfile.TemporaryDirectory() as td:
            db = _make_db_with_fts(Path(td))
            # Corrupt page 1.
            with open(db, "r+b") as fh:
                fh.truncate(16)
                fh.write(b"\x0d" * 16)
            report = dbr.repair_fts(db, dry_run=False)
            # repair_fts must refuse to touch FTS when core integrity is
            # broken — that's the whole point of Section D / E separation.
            self.assertEqual(report["status"], "ABORT_CORE_INTEGRITY_FAILED")


class RecoverStateDbTests(unittest.TestCase):
    def test_strategy_0_quick_check_only(self):
        with tempfile.TemporaryDirectory() as td:
            db = _make_db_with_fts(Path(td))
            report = dbr.recover_state_db(db, strategy=0, reason="test")
            self.assertEqual(report["status"], "QUICK_CHECK_OK")
            self.assertEqual(report["strategy"], 0)

    def test_strategy_1_vacuum_install(self):
        with tempfile.TemporaryDirectory() as td:
            db = _make_db_with_fts(Path(td))
            report = dbr.recover_state_db(db, strategy=1,
                                          reason="test-strategy-1",
                                          holder_wait_timeout=2.0)
            self.assertEqual(report["status"], "SUCCESS")
            self.assertEqual(report["strategy"], 1)
            # Verify the install was atomic.
            self.assertTrue(db.exists())
            c = sqlite3.connect(str(db))
            row = c.execute("SELECT COUNT(*) FROM messages").fetchone()
            self.assertEqual(row[0], 100)
            c.close()

    def test_recovery_writes_event_log(self):
        with tempfile.TemporaryDirectory() as td:
            db = _make_db_with_fts(Path(td))
            # Redirect the report snapshot to a tempdir.
            with mock.patch.object(dbr, "_write_recovery_report") as wm:
                report = dbr.recover_state_db(db, strategy=0, reason="log-test")
                wm.assert_called_once()


class _ImportMock:
    """Stub for the optional unittest.mock if it's not installed."""
    @staticmethod
    def patch_object(*a, **kw):
        from unittest import mock
        return mock.patch.object(*a, **kw)


if not hasattr(unittest, "mock"):
    import unittest.mock as _um
    mock = _um

import unittest.mock as mock  # noqa: E402


if __name__ == "__main__":
    unittest.main(verbosity=2)
