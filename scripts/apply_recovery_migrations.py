"""Explicit additive migration. Dry-run is restricted to the marked test DB."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess

ROOT=Path(__file__).resolve().parents[1]
FILES=('20260904_recovery.sql','20260904_bounded_consumer.sql')
TEST_DB='amazon_recovery_test_20260904_13c0'
MARKER='amazon-recovery-isolated-tests:20260904:13c0'


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode',choices=('dry-run','apply'),default='dry-run')
    parser.add_argument('--confirm-production',action='store_true')
    parser.add_argument('--expected-head')
    args=parser.parse_args()
    if args.mode=='apply' and (not args.confirm_production or not args.expected_head):
        print(json.dumps({'ok':False,'reason':'explicit_production_approval_and_head_required'})); return 2
    try:
        import psycopg
        from psycopg.conninfo import make_conninfo,conninfo_to_dict
        source=os.environ.get('AMAZON_US_POSTGRES_DSN')
        if not source: raise ValueError('database_credentials_missing')
        options=conninfo_to_dict(source)
        if options.get('host') not in {'localhost','127.0.0.1','::1'} or options.get('hostaddr','') not in {'','127.0.0.1','::1'}:
            raise ValueError('local_database_required')
        git=['git','-c','safe.directory='+ROOT.as_posix()]
        head=subprocess.check_output([*git,'rev-parse','HEAD'],cwd=ROOT,text=True,stderr=subprocess.PIPE).strip()
        if args.expected_head and head!=args.expected_head: raise ValueError('head_mismatch')
        if args.mode=='apply':
            dirty=subprocess.check_output([*git,'status','--porcelain=v1','--untracked-files=no'],cwd=ROOT,text=True,stderr=subprocess.PIPE)
            if dirty.strip(): raise ValueError('worktree_not_clean')
            subprocess.check_output([*git,'ls-files','--error-unmatch',*[f'schema/migrations/{name}' for name in FILES]],cwd=ROOT,stderr=subprocess.PIPE)
        dsn=make_conninfo(source,dbname=TEST_DB) if args.mode=='dry-run' else source
        files=[]
        with psycopg.connect(dsn,connect_timeout=5) as conn:
            database=conn.execute('SELECT current_database()').fetchone()[0]
            if args.mode=='dry-run':
                marker=conn.execute("SELECT shobj_description(oid,'pg_database') FROM pg_database WHERE datname=current_database()").fetchone()[0]
                if database!=TEST_DB or marker!=MARKER: raise ValueError('isolated_database_identity_mismatch')
            elif database==TEST_DB: raise ValueError('production_mode_cannot_target_test_database')
            conn.execute("SET LOCAL lock_timeout='5s'")
            conn.execute("SET LOCAL statement_timeout='30s'")
            for name in FILES:
                path=ROOT/'schema/migrations'/name
                payload=path.read_bytes(); conn.execute(payload.decode('utf-8'))
                files.append({'file':name,'sha256':hashlib.sha256(payload).hexdigest()})
            if args.mode=='apply': conn.commit()
            else: conn.rollback()
        print(json.dumps({'ok':True,'mode':args.mode,'head':head,'committed':args.mode=='apply',
                          'database_scope':'isolated_test' if args.mode=='dry-run' else 'approved_production',
                          'data_backfill':False,'files':files})); return 0
    except Exception as exc:
        print(json.dumps({'ok':False,'reason':str(exc) if type(exc) is ValueError else 'migration_failed','sqlstate':getattr(exc,'sqlstate',None)})); return 2


if __name__=='__main__': raise SystemExit(main())
