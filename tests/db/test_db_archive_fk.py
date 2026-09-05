"""Tests for the archive FK-enforcement and canonical-reconciliation layer
added 2026-09-04 to agent/db_maintenance.py.

Background (the 3rd state.db archive incident):

  - Previous archive script's phase 5d copied system_prompts from the
    archive's freshly-wiped table (0 rows) instead of from live. Result:
    416 dangling FKs in the archive. The script reported ``[archive-py]
    OK`` and the swap installed the broken image.
  - FK violations were reported as warnings, not hard-failures.
  - 41 pre-existing FK violations in live (orphan rows referencing
    missing parent sessions) were carried into the archive as additional
    dangling references.

Operator-mandated invariants (2026-09-04):

  1. archive SUCCESS requires integrity_check=ok AND foreign_key_check=0
  2. canonical reconciliation matches PRE on every canonical table
  3. FTS rebuilds are deferred to Hermes boot (derived indexes; excluded)
  4. three statuses: SUCCESS / SUCCESS_WITH_WARNINGS / FAILED_VALIDATION
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

_WORKTREE = Path(__file__).resolve().parents[2]
if str(_WORKTREE) not in sys.path:
    sys.path.insert(0, str(_WORKTREE))

from agent.db_maintenance import (  # noqa: E402
    ArchiveStatus,
    _canonical_tables,
    _count_or_none,
    _fk_graph,
    _fts5_virtual_tables,
    _synthesize_parent,
    canonical_reconciliation,
    heal_fk_orphans,
    validate_archive_candidate,
)


# -----------------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------------


@pytest.fixture()
def live_with_orphans(tmp_path) -> Path:
    """Build a minimal SQLite DB that mimics Hermes state.db with FK orphans.

    Schema is reduced for test isolation — only the columns we need to
    exercise the orphan-heal + FK-enforcement logic. We model:
      - sessions (parent of messages, parent of session_model_usage,
        parent of system_prompts via system_prompt_hash)
      - system_prompts (parent of sessions via system_prompt_hash)
      - messages (FK to sessions)
      - session_model_usage (FK to sessions)

    Pre-seeded orphans:
      - 3 messages + 1 session_model_usage reference missing session
        ``cron_missing_20260825_010013``
      - 1 session references missing system_prompts hash
        ``orphaned_hash_42``

    Total expected FK violations: 4 + 1 = 5 (4 messages+usage, 1 session)
    """
    db_path = tmp_path / "live_with_orphans.db"
    conn = sqlite3.connect(str(db_path))
    # FK=OFF during fixture setup so we can insert orphans; tests re-enable.
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            system_prompt_hash TEXT REFERENCES system_prompts(hash),
            started_at REAL NOT NULL,
            message_count INTEGER DEFAULT 0,
            archived INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE system_prompts (
            hash TEXT PRIMARY KEY,
            prompt TEXT NOT NULL
        );
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL REFERENCES sessions(id),
            role TEXT NOT NULL,
            timestamp REAL NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            compacted INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE session_model_usage (
            session_id TEXT NOT NULL REFERENCES sessions(id),
            model TEXT NOT NULL,
            api_call_count INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (session_id, model)
        );
        CREATE TABLE state_meta (key TEXT PRIMARY KEY, value TEXT);
        INSERT INTO state_meta(key, value) VALUES('schema_version', '1');
        INSERT INTO system_prompts(hash, prompt) VALUES('real_hash_1', 'p1');
        INSERT INTO sessions(id, source, system_prompt_hash, started_at)
          VALUES ('s_real_1', 'test', 'real_hash_1', 1000.0);
        INSERT INTO sessions(id, source, system_prompt_hash, started_at)
          VALUES ('s_real_2', 'test', 'orphaned_hash_42', 1100.0);
        INSERT INTO messages(session_id, role, timestamp)
          VALUES ('cron_missing_20260825_010013', 'user', 1200.0);
        INSERT INTO messages(session_id, role, timestamp)
          VALUES ('cron_missing_20260825_010013', 'assistant', 1201.0);
        INSERT INTO messages(session_id, role, timestamp)
          VALUES ('cron_missing_20260825_010013', 'user', 1202.0);
        INSERT INTO session_model_usage(session_id, model)
          VALUES ('cron_missing_20260825_010013', 'm1');
        """
    )
    conn.commit()
    conn.close()
    return db_path


@pytest.fixture()
def clean_live(tmp_path) -> Path:
    """Build a SQLite DB with no FK violations (control fixture)."""
    db_path = tmp_path / "clean.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            started_at REAL NOT NULL
        );
        INSERT INTO sessions(id, source, started_at) VALUES ('s1', 't', 1.0);
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL REFERENCES sessions(id),
            role TEXT NOT NULL,
            timestamp REAL NOT NULL
        );
        INSERT INTO messages(session_id, role, timestamp) VALUES ('s1', 'u', 1.0);
        """
    )
    conn.commit()
    conn.close()
    return db_path


@pytest.fixture()
def fts5_live(tmp_path) -> Path:
    """Build a SQLite DB with a real FTS5 virtual table for FTS-detection tests."""
    db_path = tmp_path / "fts.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(
        """
        CREATE TABLE docs (
            id INTEGER PRIMARY KEY,
            body TEXT
        );
        CREATE VIRTUAL TABLE docs_fts USING fts5(body, content='docs',
                                                content_rowid='id');
        CREATE TABLE state_meta (key TEXT PRIMARY KEY, value TEXT);
        INSERT INTO docs(id, body) VALUES (1, 'hello');
        """
    )
    conn.commit()
    conn.close()
    return db_path


# -----------------------------------------------------------------------------
# _canonical_tables / _fts5_virtual_tables / _fk_graph
# -----------------------------------------------------------------------------


def test_canonical_tables_excludes_fts_shadow_tables(tmp_path):
    """Canonical list excludes FTS5 virtual + FTS shadow tables + sqlite_*."""
    db = tmp_path / "canon.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE sessions (id TEXT PRIMARY KEY);
        CREATE TABLE messages (id INTEGER PRIMARY KEY, body TEXT);
        -- FTS5 vtable; this auto-creates messages_fts_data + messages_fts_idx
        -- as content-table shadow tables.
        CREATE VIRTUAL TABLE messages_fts USING fts5(body, content='messages',
                                                      content_rowid='id');
        CREATE TABLE state_meta (key TEXT PRIMARY KEY, value TEXT);
        """
    )
    conn.commit()
    conn.close()

    # Sanity-check the FTS vtable created its shadow tables in sqlite_schema
    raw = sqlite3.connect(str(db))
    raw_tables = [r[0] for r in raw.execute(
        "SELECT name FROM sqlite_schema WHERE type='table' ORDER BY name"
    ).fetchall()]
    raw.close()
    assert "messages_fts" in raw_tables
    assert any(t.startswith("messages_fts_") for t in raw_tables), (
        f"FTS shadow tables missing from {raw_tables}"
    )

    conn = sqlite3.connect(str(db))
    canonical = _canonical_tables(conn)
    conn.close()
    assert "sessions" in canonical
    assert "messages" in canonical
    assert "state_meta" in canonical
    # FTS-related must NOT appear in canonical list
    assert "messages_fts" not in canonical
    fts_shadow = [t for t in canonical if "fts" in t.lower()]
    assert fts_shadow == [], (
        f"FTS shadow tables leaked into canonical: {fts_shadow}"
    )


def test_fts5_virtual_tables_detects_real_vtables_only(fts5_live):
    """Detection via sqlite_schema (sql LIKE %VIRTUAL TABLE%) not name LIKE."""
    conn = sqlite3.connect(str(fts5_live))
    fts = _fts5_virtual_tables(conn)
    conn.close()
    assert fts == ["docs_fts"]


def test_fk_graph_discovers_sessions_to_system_prompts(live_with_orphans):
    """The bug fix: previous script missed this FK because it was hardcoded.

    The FK graph walker MUST find sessions -> system_prompts dynamically.
    """
    conn = sqlite3.connect(str(live_with_orphans))
    graph = _fk_graph(conn)
    conn.close()

    # sessions has FK to system_prompts via system_prompt_hash
    sessions_edges = graph.get("sessions", [])
    sp_edges = [e for e in sessions_edges if e[1] == "system_prompts"]
    assert len(sp_edges) == 1
    assert sp_edges[0] == ("system_prompt_hash", "system_prompts", "hash")

    # messages has FK to sessions
    msg_edges = graph.get("messages", [])
    sess_edges = [e for e in msg_edges if e[1] == "sessions"]
    assert len(sess_edges) == 1
    assert sess_edges[0][0] == "session_id"

    # session_model_usage has FK to sessions
    smu_edges = graph.get("session_model_usage", [])
    sess_edges = [e for e in smu_edges if e[1] == "sessions"]
    assert len(sess_edges) == 1


# -----------------------------------------------------------------------------
# heal_fk_orphans
# -----------------------------------------------------------------------------


def test_heal_fk_orphans_synthesizes_missing_session(live_with_orphans):
    """Heal must synthesize the missing session referenced by orphan messages."""
    conn = sqlite3.connect(str(live_with_orphans))
    conn.execute("PRAGMA foreign_keys=ON")
    pre_violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    assert len(pre_violations) == 5  # 4 from cron_missing + 1 system_prompts

    result = heal_fk_orphans(conn, dry_run=False)
    conn.close()

    assert result["pre"] == 5
    assert result["post"] == 0
    assert result["synthesized"]["sessions"] == 1
    assert result["synthesized"]["system_prompts"] == 1

    # Confirm the synthesized rows actually exist
    conn = sqlite3.connect(str(live_with_orphans))
    assert conn.execute(
        "SELECT 1 FROM sessions WHERE id='cron_missing_20260825_010013'"
    ).fetchone() is not None
    assert conn.execute(
        "SELECT 1 FROM system_prompts WHERE hash='orphaned_hash_42'"
    ).fetchone() is not None
    conn.close()


def test_heal_fk_orphans_dry_run_does_not_persist(live_with_orphans):
    """dry_run=True must NOT write the synthesized rows to disk."""
    conn = sqlite3.connect(str(live_with_orphans))
    conn.execute("PRAGMA foreign_keys=ON")
    result = heal_fk_orphans(conn, dry_run=True)
    conn.close()

    assert result["pre"] == 5
    assert result["post"] == 5  # not actually healed

    # Confirm nothing was persisted
    conn = sqlite3.connect(str(live_with_orphans))
    assert conn.execute(
        "SELECT 1 FROM sessions WHERE id='cron_missing_20260825_010013'"
    ).fetchone() is None
    conn.close()


def test_heal_fk_orphans_no_op_on_clean_db(clean_live):
    """Heal on a clean DB must be a no-op."""
    conn = sqlite3.connect(str(clean_live))
    conn.execute("PRAGMA foreign_keys=ON")
    result = heal_fk_orphans(conn, dry_run=False)
    conn.close()
    assert result == {"pre": 0, "post": 0, "synthesized": {}}


# -----------------------------------------------------------------------------
# canonical_reconciliation
# -----------------------------------------------------------------------------


def test_canonical_reconciliation_ok_on_unchanged_db(clean_live):
    """Reconciliation must return all_reconciled=True when pre==post."""
    conn = sqlite3.connect(str(clean_live))
    conn2 = sqlite3.connect(str(clean_live))
    # Capture PRE counts so reconciliation sees stable state
    pre_live = {t: _count_or_none(conn, t) or 0 for t in _canonical_tables(conn)}
    pre_archive = {t: _count_or_none(conn2, t) or 0 for t in _canonical_tables(conn2)}
    all_reconciled, rows = canonical_reconciliation(
        conn, conn2, pre_live=pre_live, pre_archive=pre_archive,
    )
    conn.close()
    conn2.close()

    assert all_reconciled is True
    assert all(r["status"] == "OK" for r in rows)


def test_canonical_reconciliation_flags_archive_shrank(clean_live):
    """Reconciliation must FAIL when archive lost rows it shouldn't have."""
    live = sqlite3.connect(str(clean_live))
    archive = sqlite3.connect(str(clean_live))

    # Capture PRE counts while both DBs are full
    pre_archive = {t: _count_or_none(archive, t) or 0
                   for t in _canonical_tables(archive)}

    # Now delete a row from archive to simulate a bug
    archive.execute("DELETE FROM messages")
    archive.commit()

    all_reconciled, rows = canonical_reconciliation(
        live, archive, pre_archive=pre_archive,
    )
    live.close()
    archive.close()

    assert all_reconciled is False
    msgs_row = next(r for r in rows if r["table"] == "messages")
    assert "archive_shrank" in msgs_row["status"]


def test_canonical_reconciliation_system_prompts_allows_intentional_dup(clean_live):
    """system_prompts may be intentionally duplicated across live+archive."""
    # Build a DB with system_prompts in both live and archive
    db_path = clean_live.parent / "with_sp.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT NOT NULL,
                               system_prompt_hash TEXT, started_at REAL NOT NULL);
        CREATE TABLE system_prompts (hash TEXT PRIMARY KEY, prompt TEXT NOT NULL);
        CREATE TABLE state_meta (key TEXT PRIMARY KEY, value TEXT);
        INSERT INTO sessions(id, source, system_prompt_hash, started_at)
          VALUES ('s1', 't', 'shared_hash', 1.0);
        INSERT INTO system_prompts(hash, prompt) VALUES ('shared_hash', 'p1');
        """
    )
    conn.commit()
    conn.close()

    live = sqlite3.connect(str(db_path))
    archive = sqlite3.connect(str(db_path))
    all_reconciled, rows = canonical_reconciliation(live, archive)
    live.close()
    archive.close()

    assert all_reconciled is True
    sp_row = next(r for r in rows if r["table"] == "system_prompts")
    assert sp_row["status"] == "OK"


# -----------------------------------------------------------------------------
# validate_archive_candidate — the operator-mandated three-status API
# -----------------------------------------------------------------------------


def test_validate_archive_candidate_success_on_clean_pair(clean_live):
    """Clean live + clean archive → SUCCESS (no warnings, no failures)."""
    # Capture PRE counts so reconciliation doesn't see "live grew from 0"
    pre_live = {t: _count_or_none(sqlite3.connect(str(clean_live)), t) or 0
                for t in ["sessions", "messages"]}
    pre_archive = {t: _count_or_none(sqlite3.connect(str(clean_live)), t) or 0
                   for t in ["sessions", "messages"]}

    report = validate_archive_candidate(
        clean_live, clean_live,
        dry_run=True,
        pre_live_counts=pre_live,
        pre_archive_counts=pre_archive,
    )
    assert report["status"] == ArchiveStatus.SUCCESS
    assert report["live_integrity"] == "ok"
    assert report["archive_integrity"] == "ok"
    assert report["live_fk_count"] == 0
    assert report["archive_fk_count"] == 0
    assert "hard_failures" not in report
    assert "warnings" not in report


def test_validate_archive_candidate_failed_validation_on_fk_violations(
    live_with_orphans,
):
    """Archive with FK violations must be FAILED_VALIDATION, not SUCCESS."""
    report = validate_archive_candidate(
        live_with_orphans, live_with_orphans, dry_run=True
    )
    assert report["status"] == ArchiveStatus.FAILED_VALIDATION
    # Both live and archive have 5 FK violations (it's the same file)
    assert report["live_fk_count"] == 5
    assert report["archive_fk_count"] == 5
    assert any("FK violations" in f for f in report["hard_failures"])


def test_validate_archive_candidate_does_not_silently_pass_when_fk_nonzero(
    live_with_orphans,
):
    """Regression test for the 2026-09-04 bug: previously reported OK
    despite 416 FK violations in the archive."""
    report = validate_archive_candidate(
        live_with_orphans, live_with_orphans, dry_run=True
    )
    # The old script would have printed "[archive-py] OK" — we must NOT.
    assert report["status"] != ArchiveStatus.SUCCESS
    assert report["live_fk_count"] > 0


def test_validate_archive_candidate_handles_missing_files(tmp_path):
    """Missing files must produce FAILED_VALIDATION, not a crash."""
    # Create a directory at the path — sqlite3.connect refuses to open a
    # directory as a DB, raising OperationalError("not a database").
    # This tests the error-handling path without relying on sqlite's
    # auto-create behavior for nonexistent files.
    fake_dir = tmp_path / "fake.db"
    fake_dir.mkdir()
    real_live = tmp_path / "real.db"
    sqlite3.connect(str(real_live)).close()

    report = validate_archive_candidate(
        fake_dir, real_live, dry_run=True
    )
    assert report["status"] == ArchiveStatus.FAILED_VALIDATION
    assert "error" in report


def test_validate_archive_candidate_surfaces_reconciliation_table(
    clean_live,
):
    """Report must include per-table reconciliation rows."""
    report = validate_archive_candidate(
        clean_live, clean_live, dry_run=True
    )
    recon = report["canonical_reconciliation"]
    assert "rows" in recon
    assert "all_reconciled" in recon
    assert isinstance(recon["rows"], list)
    assert len(recon["rows"]) > 0
    # Each row has the expected columns
    expected_keys = {
        "table", "pre_live", "pre_archive", "post_live", "post_archive",
        "delta_live", "delta_archive", "status",
    }
    for row in recon["rows"]:
        assert expected_keys.issubset(row.keys())


def test_validate_archive_candidate_detects_fts5(fts5_live):
    """FTS5 vtables are detected via sqlite_schema (not LIKE '%_fts%')."""
    report = validate_archive_candidate(
        fts5_live, fts5_live, dry_run=True
    )
    assert "docs_fts" in report["fts_live"]


# -----------------------------------------------------------------------------
# _synthesize_parent
# -----------------------------------------------------------------------------


def test_synthesize_parent_for_known_tables(live_with_orphans):
    """sessions + system_prompts get explicit stubs; other tables no-op."""
    conn = sqlite3.connect(str(live_with_orphans))
    conn.execute("PRAGMA foreign_keys=ON")

    # sessions stub
    assert _synthesize_parent(conn, "sessions", "test_session_xyz", dry_run=False) is True
    # idempotent — second call returns False (already exists)
    assert _synthesize_parent(conn, "sessions", "test_session_xyz", dry_run=False) is False

    # system_prompts stub
    assert _synthesize_parent(conn, "system_prompts", "test_hash_xyz", dry_run=False) is True
    # idempotent
    assert _synthesize_parent(conn, "system_prompts", "test_hash_xyz", dry_run=False) is False

    conn.close()


def test_synthesize_parent_unknown_table_is_noop(live_with_orphans):
    """Unknown parent tables cannot be synthesized generically — no-op."""
    conn = sqlite3.connect(str(live_with_orphans))
    # 'state_meta' has no FK edges; the generic branch is a no-op (returns False)
    assert _synthesize_parent(conn, "state_meta", "test", dry_run=False) is False
    conn.close()
