import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from agent import db_connection as dbc, db_maintenance as dbm
from hermes_cli.sqlite_safe_read import connect_tracked


def make_db(p):
    c = sqlite3.connect(p)
    c.executescript('CREATE TABLE sessions(id TEXT PRIMARY KEY); CREATE TABLE messages(id INTEGER PRIMARY KEY, session_id TEXT REFERENCES sessions(id), content TEXT);')
    c.close()
    return p


@pytest.mark.parametrize('mode', ['delete', 'wal'])
def test_reader_cannot_write_or_change_mode(tmp_path, mode):
    p = make_db(tmp_path/'state.db')
    c = sqlite3.connect(p); c.execute('PRAGMA journal_mode='+mode); c.close()
    with dbc.open_sqlite(p, role='reader') as r:
        assert r.raw.execute('pragma journal_mode').fetchone()[0] == mode
        with pytest.raises(sqlite3.OperationalError):
            r.raw.execute("insert into sessions values ('bad')")
    assert dbc.quick_check(p)[0]
    assert dbc.foreign_key_check(p)[0] == 0
    c = sqlite3.connect(p); assert c.execute('pragma journal_mode').fetchone()[0] == mode; c.close()


def test_reader_missing_does_not_create(tmp_path):
    p=tmp_path/'missing.db'
    with pytest.raises(sqlite3.OperationalError): dbc.open_sqlite(p)
    assert not p.exists()


@pytest.mark.linux_only
def test_lifetime_admission_both_directions(tmp_path):
    p=make_db(tmp_path/'state.db')
    c=connect_tracked(p)
    with pytest.raises(dbm.MaintenanceActive):
        with dbm.MaintenanceLock(p): pass
    c.close()
    with dbm.MaintenanceLock(p):
        with pytest.raises(dbm.MaintenanceActive): connect_tracked(p)
    connect_tracked(p).close()


@pytest.mark.linux_only
def test_idle_external_holder_blocks_install(tmp_path):
    p=make_db(tmp_path/'state.db'); candidate=make_db(tmp_path/'candidate.db')
    child=subprocess.Popen([sys.executable,'-c',"import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); c.execute('select * from sessions').fetchall(); print('ready',flush=True); sys.stdin.read()",str(p)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True)
    try:
        assert child.stdout.readline().strip()=='ready'
        assert any(h.get('pid')==child.pid for h in dbm.state_db_holders(p))
        with dbm.MaintenanceLock(p):
            with pytest.raises(dbm.WriterStillPresent): dbm.install_state_db_recovered(p,candidate,holder_wait_timeout=0)
    finally:
        child.communicate('',timeout=10)


@pytest.mark.linux_only
def test_deleted_journal_detected(tmp_path):
    p=make_db(tmp_path/'state.db'); journal=Path(str(p)+'-journal'); journal.write_bytes(b'x')
    with journal.open('rb'):
        journal.unlink()
        assert any(h.get('deleted') for h in dbm.state_db_holders(p))


@pytest.mark.linux_only
def test_install_requires_actual_lease_and_preserves_predecessor(tmp_path):
    p=make_db(tmp_path/'state.db'); candidate=make_db(tmp_path/'candidate.db')
    dbm.maintenance_lock_path(p).touch()
    with pytest.raises(dbm.MaintenanceActive): dbm.install_state_db_recovered(p,candidate)
    with dbm.MaintenanceLock(p):
        result=dbm.install_state_db_recovered(p,candidate)
    assert (Path(result['predecessor'])/'state.db').exists()
    assert candidate.exists()
    assert dbc.integrity_check(p)[0]


def test_backup_never_overwrites_existing(tmp_path):
    p=make_db(tmp_path/'state.db'); dest=tmp_path/'snapshot.db'
    assert dbc.vacuum_into(p,dest)['ok']
    with pytest.raises(FileExistsError): dbc.vacuum_into(p,dest)
    assert dbc.integrity_check(dest)[0]
