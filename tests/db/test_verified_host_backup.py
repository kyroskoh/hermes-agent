import json
import sqlite3
from pathlib import Path
import pytest
from scripts.db_reliability_host import backup,check,drill
from hermes_state import SessionDB


def make_home(p):
    p.mkdir()
    (p/'config.yaml').write_text('database:\n  journal_mode: delete\n')
    db=SessionDB(p/'state.db');db.create_session('s','tool');db.append_message('s','user','hello');db.close()


def test_backup_extract_verify_and_restore_drill(tmp_path):
    home=tmp_path/'home';make_home(home);root=tmp_path/'backups'
    result=backup(home,root)
    assert result['ok']
    assert drill(home,root)['ok']
    assert check(home,root)['ok']
    c=sqlite3.connect(home/'state.db')
    assert c.execute('select count(*) from sessions').fetchone()[0]==1
    c.close()


def test_failed_profile_keeps_latest_good(tmp_path):
    home=tmp_path/'home';make_home(home);root=tmp_path/'backups'
    backup(home,root);old=(root/'latest-good.json').read_bytes()
    bad=home/'profiles'/'broken';bad.mkdir(parents=True);(bad/'config.yaml').write_text('{}')
    with pytest.raises(sqlite3.OperationalError):backup(home,root)
    assert (root/'latest-good.json').read_bytes()==old


def test_corrupt_source_never_publishes(tmp_path):
    home=tmp_path/'home';make_home(home);root=tmp_path/'backups'
    (home/'state.db').write_bytes(b'corrupt'*100)
    with pytest.raises(sqlite3.DatabaseError):backup(home,root)
    assert not (root/'latest-good.json').exists()
