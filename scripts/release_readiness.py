"""Read-only production readiness and optional, non-overwriting cohort manifest."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
from pathlib import Path
import re


def write_manifest(path,asins):
    path=Path(path).resolve()
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('x',encoding='utf-8',newline='') as handle:
        writer=csv.DictWriter(handle,fieldnames=('asin','url','marketplace','source_site_label','source_workbook'))
        writer.writeheader()
        writer.writerows({'asin':asin,'url':'https://www.amazon.com/dp/'+asin,'marketplace':'US',
                          'source_site_label':'US','source_workbook':'postgres-owned-frozen-cohort'} for asin in asins)
    return {'manifest_path':str(path),'manifest_sha256':hashlib.sha256(path.read_bytes()).hexdigest()}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tenant-id',required=True)
    parser.add_argument('--limit',type=int,default=20)
    parser.add_argument('--manifest-out',type=Path)
    args=parser.parse_args()
    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,120}',args.tenant_id) or not 1<=args.limit<=1000:
        parser.error('invalid tenant or limit')
    dsn=os.environ.get('AMAZON_US_POSTGRES_DSN')
    if not dsn:
        print(json.dumps({'ok':False,'reason':'database_credentials_missing'})); return 2
    try:
        import psycopg
        from psycopg.rows import dict_row
        tables=('item_state','collection_evidence','product_snapshot','operation_run','proxy_capacity_reservation','recovery_job','recovery_egress','recovery_batch')
        with psycopg.connect(dsn,row_factory=dict_row,options='-c default_transaction_read_only=on',connect_timeout=5) as conn:
            missing=[]
            for table in tables:
                if conn.execute('SELECT to_regclass(%s) AS relation',('amazon_us.'+table,)).fetchone()['relation'] is None:
                    missing.append(table)
            if missing:
                print(json.dumps({'ok':False,'reason':'schema_migration_required','missing_tables':missing,'production_writes':0})); return 3
            rows=conn.execute('''SELECT s.asin FROM amazon_us.item_state s
                JOIN amazon_us.asin_master m USING(tenant_id,marketplace,asin,subject_type)
                LEFT JOIN amazon_us.recovery_job j ON j.tenant_id=s.tenant_id AND j.asin=s.asin AND j.subject_type=s.subject_type AND j.stage='product'
                WHERE s.tenant_id=%s AND s.subject_type='own' AND s.marketplace='US' AND s.status='pending'
                  AND s.task_stage='product' AND m.active_status='active'
                  AND (s.lease_expires_at IS NULL OR s.lease_expires_at<=CURRENT_TIMESTAMP)
                  AND j.tenant_id IS NULL
                  AND NOT EXISTS(SELECT 1 FROM amazon_us.collection_evidence e WHERE e.tenant_id=s.tenant_id AND e.asin=s.asin AND e.subject_type=s.subject_type)
                ORDER BY m.priority DESC,s.updated_at,s.asin LIMIT %s''',(args.tenant_id,args.limit)).fetchall()
            conn.rollback()
        asins=[row['asin'] for row in rows]
        result={'ok':len(asins)==args.limit,'reason':'ready' if len(asins)==args.limit else 'fresh_cohort_insufficient',
                'tenant_id':args.tenant_id,'requested':args.limit,'asins':asins,'production_writes':0}
        if args.manifest_out and result['ok']:
            result.update(write_manifest(args.manifest_out,asins))
        print(json.dumps(result,ensure_ascii=True)); return 0 if result['ok'] else 3
    except Exception as exc:
        print(json.dumps({'ok':False,'reason':'manifest_already_exists' if isinstance(exc,FileExistsError) else 'readiness_failed','sqlstate':getattr(exc,'sqlstate',None)}))
        return 2


if __name__=='__main__': raise SystemExit(main())
