#!/usr/bin/env python3
"""Host observer and verified DB backups. Never repairs/restarts/sends messages."""
from __future__ import annotations
import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tarfile
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent import db_connection as dbc, db_maintenance as dbm


def atomic_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp=path.with_name(path.name+'.'+str(os.getpid())+'.tmp')
    with open(tmp,'x') as f:
        os.chmod(tmp,0o600); json.dump(data,f,indent=2); f.flush(); os.fsync(f.fileno())
    os.replace(tmp,path); dbm.fsync_dir(path.parent)


def databases(home):
    # Explicitly include default, and every profile having configuration; a
    # missing configured database is an error, not silently omitted coverage.
    found={'default': home/'state.db'}
    for p in sorted((home/'profiles').glob('*')):
        if p.is_dir() and ((p/'config.yaml').exists() or (p/'state.db').exists()):
            found[p.name]=p/'state.db'
    return found


def validate_copy(path):
    with contextlib.closing(sqlite3.connect(path)) as c:
        if c.execute('pragma integrity_check').fetchall()!=[('ok',)]:
            raise ValueError('integrity check failed')
        if c.execute('pragma foreign_key_check').fetchall():
            raise ValueError('foreign key check failed')
        counts={t:c.execute('SELECT count(*) FROM '+t).fetchone()[0] for t in ('sessions','messages')}
        for t in ('messages_fts','messages_fts_trigram'):
            if not c.execute('select 1 from sqlite_master where name=?',(t,)).fetchone():
                raise ValueError('missing FTS table '+t)
            c.execute('BEGIN')
            try:c.execute(f'INSERT INTO "{t}"("{t}") VALUES (\'integrity-check\')')
            finally:c.rollback()
        return counts


def snapshot(src,dst,timeout=90):
    deadline=time.monotonic()+timeout
    def progress(status,remaining,total):
        if time.monotonic()>deadline:raise TimeoutError('backup exceeded deadline')
    with dbc.open_sqlite(src,role='reader',timeout=2) as reader:
        with contextlib.closing(sqlite3.connect(dst)) as target:
            reader.raw.backup(target,pages=256,progress=progress,sleep=0.05)
    return validate_copy(dst)


def backup(home,root):
    root.mkdir(parents=True,exist_ok=True)
    with open(root/'.backup.lock','a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        stamp=dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
        with tempfile.TemporaryDirectory(prefix='.staging-',dir=root) as temp:
            stage=Path(temp); manifest={'created_at':time.time(),'sqlite_version':sqlite3.sqlite_version,'profiles':{},'verified':False}
            for name,src in databases(home).items():
                dest=stage/(name+'.db'); counts=snapshot(src,dest)
                manifest['profiles'][name]={'member':name+'.db','counts':counts,'sha256':hashlib.sha256(dest.read_bytes()).hexdigest()}
            archive=stage/'databases.tar.gz'
            with tarfile.open(archive,'w:gz') as tar:
                for name in manifest['profiles']:tar.add(stage/(name+'.db'),arcname=name+'.db')
            # Read every exact member from the completed archive and validate
            # independent extracted copies before publishing a good pointer.
            with tarfile.open(archive,'r:gz') as tar:
                names=tar.getnames()
                expected=[v['member'] for v in manifest['profiles'].values()]
                if sorted(names)!=sorted(expected):raise ValueError('archive member mismatch')
                restore=stage/'restore';restore.mkdir()
                for name,info in manifest['profiles'].items():
                    dest=restore/info['member']
                    with tar.extractfile(info['member']) as source,open(dest,'xb') as out:shutil.copyfileobj(source,out)
                    if hashlib.sha256(dest.read_bytes()).hexdigest()!=info['sha256']:raise ValueError('archive hash mismatch')
                    if validate_copy(dest)!=info['counts']:raise ValueError('restored counts mismatch')
            manifest['verified']=True
            final=root/stamp;final.mkdir(mode=0o700)
            os.replace(archive,final/'databases.tar.gz')
            with open(final/'databases.tar.gz','rb') as f:os.fsync(f.fileno())
            atomic_json(final/'manifest.json',manifest)
            atomic_json(root/'latest-good.json',{'directory':str(final),**manifest})
            # Retain 48 latest snapshots and one daily snapshot for 14 days.
            snapshots=sorted([p for p in root.iterdir() if p.is_dir() and (p/'manifest.json').is_file()],reverse=True)
            keep=set(snapshots[:48]);days=set()
            for p in snapshots:
                day=p.name[:8]
                if day not in days and len(days)<14:keep.add(p);days.add(day)
            for p in snapshots:
                if p not in keep:shutil.rmtree(p)
            return {'ok':True,'directory':str(final),'profiles':manifest['profiles']}


def check(home,root,preflight=False):
    report={'checked_at':time.time(),'ok':True,'databases':{},'warnings':[]}
    for name,p in databases(home).items():
        row={}
        try:
            with dbc.open_sqlite(p,role='reader',timeout=2) as c:
                row['journal_mode']=c.raw.execute('pragma journal_mode').fetchone()[0]
                result=c.raw.execute('pragma quick_check(1)').fetchall()
                if result!=[('ok',)]:raise ValueError('quick_check: '+str(result))
                row['messages']=c.raw.execute('select count(*) from messages').fetchone()[0]
                row['sessions']=c.raw.execute('select count(*) from sessions').fetchone()[0]
                c.raw.execute('select * from gateway_routing limit 1').fetchall()
            holders=dbm.state_db_holders(p)
            row['unsafe_holders']=[h for h in holders if h.get('deleted') or h.get('detector_error')]
            if row['unsafe_holders']:raise ValueError('deleted or uninspectable database handles')
            import yaml
            config=yaml.safe_load((p.parent/'config.yaml').read_text()) or {}
            desired=config.get('database',{}).get('journal_mode','wal')
            if desired!=row['journal_mode']:report['warnings'].append(name+': configured journal mode differs from disk; offline maintenance required')
            row['ok']=True
        except Exception as e:
            row.update(ok=False,error=f'{type(e).__name__}: {e}'); report['ok']=False
        report['databases'][name]=row
    space=os.statvfs(home)
    if space.f_bavail*space.f_frsize<1024**3 or space.f_favail<1000:
        report['warnings'].append('low disk bytes or inodes')
    if not preflight:
        try:
            last=json.loads((root/'latest-good.json').read_text())
            report['backup_age_seconds']=time.time()-last['created_at']
            if not last.get('verified') or report['backup_age_seconds']>7200:report['warnings'].append('verified backup older than 2 hours')
        except Exception:report['warnings'].append('no verified backup metadata')
        # Bounded recent logs; avoid emitting message bodies or credentials.
        import re
        cutoff=dt.datetime.now()-dt.timedelta(minutes=10)
        for log in (home/'logs/errors.log',home/'logs/gateway.log'):
            if not log.exists():continue
            with log.open('rb') as f:
                f.seek(max(0,log.stat().st_size-512000)); lines=f.read().decode(errors='replace').splitlines()
            hits=0
            for line in lines:
                try:stamp=dt.datetime.strptime(line[:19],'%Y-%m-%d %H:%M:%S')
                except ValueError:continue
                if stamp>=cutoff and re.search('append_message failed|routing load failed|database disk image is malformed|SQLite session store unavailable',line):hits+=1
            if hits:report['warnings'].append(f'{log.name}: {hits} recent persistence failures')
    return report


def drill(home,root):
    latest=json.loads((root/'latest-good.json').read_text())
    with tempfile.TemporaryDirectory(prefix='hermes-restore-drill-') as temp:
        stage=Path(temp)
        with tarfile.open(Path(latest['directory'])/'databases.tar.gz') as tar:
            for name,info in latest['profiles'].items():
                dest=stage/(name+'.db')
                with tar.extractfile(info['member']) as f,open(dest,'xb') as out:shutil.copyfileobj(f,out)
                validate_copy(dest)
                # Exercise real SessionDB read + append + readback on copies.
                from hermes_state import SessionDB
                db=SessionDB(dest)
                sid='restore-drill-'+str(time.time_ns())
                try:
                    db.create_session(sid,'tool')
                    db.append_message(sid,'user','restore verification')
                    db.append_message(sid,'assistant','RESTORE_OK')
                finally:db.close()
                with contextlib.closing(sqlite3.connect(dest)) as c:
                    assert c.execute('select content from messages where session_id=? order by id',(sid,)).fetchall()==[('restore verification',),('RESTORE_OK',)]
                    c.execute('select * from gateway_routing limit 1').fetchall()
                validate_copy(dest)
    receipt={'ok':True,'checked_at':time.time(),'backup':latest['directory'],'profiles':list(latest['profiles'])}
    atomic_json(root/'latest-drill.json',receipt)
    return receipt


def main():
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=['check','preflight','backup','drill']);parser.add_argument('--home',type=Path,required=True);parser.add_argument('--output',type=Path,required=True)
    a=parser.parse_args()
    try:
        if a.action=='backup':result=backup(a.home,a.output)
        elif a.action=='drill':result=drill(a.home,a.output)
        else:result=check(a.home,a.output,a.action=='preflight')
        if a.action=='check':atomic_json(a.output/'health.json',result)
        print(json.dumps(result,sort_keys=True));return 0 if result['ok'] else 1
    except Exception as e:
        print(json.dumps({'ok':False,'error':f'{type(e).__name__}: {e}'}));return 1

if __name__=='__main__':raise SystemExit(main())
