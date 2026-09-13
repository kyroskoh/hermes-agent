"""Standalone tests for ``agent.db_health`` and ``agent.db_connection``.

Covers spec sections C (PRAGMA hardening), D (FTS5 health), E
(distinguish FTS from core), F (preflight).
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, "/usr/local/lib/hermes-agent")

from agent import db_connection as dbc
from agent import db_health as dbh


def _make_db_with_fts(tmp: Path) -> Path:
    db = tmp / "test.db"
    c = sqlite3.connect(str(db))
    c.executescript(
        """
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY,
            content TEXT,
            tool_name TEXT,
            tool_calls TEXT
        );
        CREATE VIRTUAL TABLE messages_fts USING fts5(
            content, tool_name, tool_calls,
            content='messages', content_rowid='id'
        );
        CREATE TRIGGER messages_ai AFTER INSERT ON messages BEGIN
            INSERT INTO messages_fts(rowid, content, tool_name, tool_calls)
            VALUES (new.id, new.content, new.tool_name, new.tool_calls);
        END;
        INSERT INTO messages VALUES (1, 'hello world', 'echo', '[]');
        INSERT INTO messages VALUES (2, 'second message', 'bash', '[]');
        INSERT INTO messages VALUES (3, 'third one', 'echo', '[]');
        """
    )
    c.commit()
    c.close()
    return db


class ConnectionFactoryTests(unittest.TestCase):
    def test_default_pragmas_applied(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "test.db"
            sqlite3.connect(str(db)).close()  # create empty
            with dbc.open_sqlite(db, role="writer") as mc:
                cur = mc.raw
                self.assertEqual(cur.execute("PRAGMA journal_mode").fetchone()[0],
                                 "wal")
                self.assertEqual(cur.execute("PRAGMA synchronous").fetchone()[0], 2)
                self.assertEqual(cur.execute("PRAGMA foreign_keys").fetchone()[0], 1)
                self.assertGreaterEqual(
                    cur.execute("PRAGMA busy_timeout").fetchone()[0], 5000)

    def test_writer_already_open_raises(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "test.db"
            sqlite3.connect(str(db)).close()
            with dbc.open_sqlite(db, role="writer") as first:
                with self.assertRaises(dbc.WriterAlreadyOpen):
                    dbc.open_sqlite(db, role="writer")

    def test_reader_concurrent_ok(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "test.db"
            sqlite3.connect(str(db)).close()
            with dbc.open_sqlite(db, role="reader"):
                with dbc.open_sqlite(db, role="reader"):
                    pass  # second reader is fine


class HealthClassifierTests(unittest.TestCase):
    def test_healthy_db_returns_ok(self):
        with tempfile.TemporaryDirectory() as td:
            db = _make_db_with_fts(Path(td))
            report = dbh.classify(db, full=True, persist_inode=False,
                                   expected_fts=())
            self.assertEqual(report.severity, dbh.OK,
                             f"expected OK, got {report.severity}: {report.summary}")
            self.assertEqual(report.quick_check, "ok")
            self.assertTrue(report.header_ok)

    def test_fk_violation_classified_as_degraded_fk(self):
        """The current live state.db has 99 FK violations; we synthesize
        one and verify the classifier flags it without raising."""
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "test.db"
            c = sqlite3.connect(str(db))
            c.executescript(
                """
                CREATE TABLE parent(id INTEGER PRIMARY KEY);
                CREATE TABLE child(id INTEGER PRIMARY KEY, parent_id INTEGER
                    REFERENCES parent(id));
                INSERT INTO child VALUES (1, 999);  -- orphan
                """
            )
            c.commit()
            c.close()
            report = dbh.classify(db, full=True, persist_inode=False,
                                  expected_fts=())
            self.assertEqual(report.severity, dbh.DEGRADED_FK)
            self.assertGreaterEqual(report.foreign_key_violations, 1)

    def test_corrupt_header_returns_recovery_required(self):
        with tempfile.TemporaryDirectory() as td:
            db = _make_db_with_fts(Path(td))
            # Truncate to simulate a destroyed page 1 (the prior incident).
            with open(db, "r+b") as fh:
                fh.truncate(16)
                fh.write(b"\x0d" * 16)  # leaf b-tree page magic, not SQLite header
            report = dbh.classify(db, full=False, persist_inode=False,
                                  expected_fts=())
            self.assertEqual(report.severity, dbh.RECOVERY_REQUIRED)
            self.assertIn("DB_HEADER_INVALID", report.events)

    def test_fts_unqueryable_returns_degraded_fts(self):
        with tempfile.TemporaryDirectory() as td:
            db = _make_db_with_fts(Path(td))
            # Drop the FTS table to simulate a vtable that won't construct.
            c = sqlite3.connect(str(db))
            c.execute("DROP TABLE messages_fts")
            c.commit()
            c.close()
            report = dbh.classify(db, full=True, persist_inode=False,
                                  expected_fts=("messages_fts",))
            # After DROP TABLE, classify's fts_integrity_check should report
            # messages_fts as not-existing → DEGRADED_FTS, NOT
            # RECOVERY_REQUIRED.
            self.assertEqual(report.severity, dbh.DEGRADED_FTS)
            self.assertIn("FTS_MISSING:messages_fts", report.events)

    def test_expected_fts_missing_flagged(self):
        """The classifier flags missing canonical FTS tables."""
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "test.db"
            c = sqlite3.connect(str(db))
            c.executescript("CREATE TABLE messages(id INTEGER PRIMARY KEY);")
            c.commit()
            c.close()
            report = dbh.classify(db, full=True, persist_inode=False,
                                  expected_fts=("messages_fts",
                                                "messages_fts_trigram"))
            self.assertEqual(report.severity, dbh.DEGRADED_FTS)
            self.assertIn("FTS_MISSING:messages_fts", report.events)
            self.assertIn("FTS_MISSING:messages_fts_trigram", report.events)


class FtsIntegrityCheckTests(unittest.TestCase):
    def test_fts_integrity_check_passes(self):
        with tempfile.TemporaryDirectory() as td:
            db = _make_db_with_fts(Path(td))
            res = dbc.fts_integrity_check(db)
            self.assertIn("messages_fts", res)
            self.assertTrue(res["messages_fts"]["queryable"])
            self.assertEqual(res["messages_fts"]["integrity"], "ok")

    def test_fts_integrity_check_missing(self):
        with tempfile.TemporaryDirectory() as td:
            db = _make_db_with_fts(Path(td))
            c = sqlite3.connect(str(db))
            c.execute("DROP TABLE messages_fts")
            c.commit()
            c.close()
            # Probe with explicit fts_names so the missing table is reported.
            res = dbc.fts_integrity_check(db, fts_names=["messages_fts"])
            self.assertFalse(res["messages_fts"]["exists"])


class VacuumAndCheckpointTests(unittest.TestCase):
    def test_vacuum_into_creates_snapshot(self):
        with tempfile.TemporaryDirectory() as td:
            src = _make_db_with_fts(Path(td))
            dest = Path(td) / "snap.db"
            res = dbc.vacuum_into(src, dest)
            self.assertTrue(res["ok"])
            self.assertTrue(dest.exists())
            # Verify the snapshot is openable.
            c = sqlite3.connect(str(dest))
            row = c.execute("SELECT COUNT(*) FROM messages").fetchone()
            self.assertEqual(row[0], 3)
            c.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
