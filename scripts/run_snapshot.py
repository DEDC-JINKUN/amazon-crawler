"""Read a final run projection directly from PostgreSQL, without Console HTTP."""
import argparse
import json
import os

from collection_console import PostgresConsoleRepository, TENANT_RE, _json_default


class SnapshotRepository(PostgresConsoleRepository):
    def _connect(self):
        import psycopg
        from psycopg.rows import dict_row
        return psycopg.connect(self.dsn, row_factory=dict_row, connect_timeout=3,
                               options='-c default_transaction_read_only=on -c statement_timeout=5000 -c lock_timeout=1000')


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tenant-id',required=True)
    parser.add_argument('--run-id',required=True)
    parser.add_argument('--dsn-env',default='AMAZON_US_POSTGRES_DSN')
    args=parser.parse_args(argv)
    try:
        if not TENANT_RE.fullmatch(args.tenant_id): raise ValueError('invalid tenant')
        dsn=os.environ.get(args.dsn_env)
        if not dsn: raise ValueError('missing connection')
        repository=SnapshotRepository(dsn,args.tenant_id)
        # Current evidence is authoritative. Never scan/reparse raw on the
        # polling/finalization path or mutate legacy identity evidence.
        result=repository.load_run(args.run_id)
        if result is None: raise ValueError('run not found')
        result['projection_source']='postgres_readonly'
        result['recovery']=repository.load_recovery_status()
        print(json.dumps(result,ensure_ascii=True,default=_json_default))
        return 0
    except Exception:
        print(json.dumps({'projection_source':'postgres_readonly','availability':'unknown',
                          'reason':'run_snapshot_unavailable','requested_actions':None,'recorded_actions':None}))
        return 2


if __name__=='__main__': raise SystemExit(main())
