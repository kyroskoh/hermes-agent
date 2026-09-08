#!/usr/bin/env python3
"""Detached, deliberate database installation with lifetime admission exclusion."""
import argparse
import contextlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from agent import db_maintenance as dbm
from scripts.db_reliability_host import validate_copy


def main():
    p=argparse.ArgumentParser();p.add_argument('candidate',nargs='?');p.add_argument('--db',type=Path,default=Path('/root/.hermes/state.db'));p.add_argument('--holders',action='store_true');a=p.parse_args()
    if a.holders:
        holders=dbm.state_db_holders(a.db)
        for h in holders:print(json.dumps(h))
        return 0
    if not a.candidate:raise ValueError('candidate required')
    candidate=Path(a.candidate).resolve()
    validate_copy(candidate)
    units=['hermes-gateway.service','hermes-dashboard.service','hermes-dashboard-wilnice.service']
    active=[u for u in units if subprocess.run(['systemctl','is-active','--quiet',u]).returncode==0]
    subprocess.run(['systemctl','stop',*units],check=True)
    installed=False
    try:
        with dbm.MaintenanceLock(a.db,reason='verified-offline-install',timeout=0):
            result=dbm.install_state_db_recovered(a.db,candidate,holder_wait_timeout=0)
            installed=True
            print(json.dumps(result))
    finally:
        # Failed admission never changed the live family. After installation,
        # preserve the validated replacement even if service startup fails.
        if active:subprocess.run(['systemctl','start',*active],check=True)
    return 0

if __name__=='__main__':raise SystemExit(main())
