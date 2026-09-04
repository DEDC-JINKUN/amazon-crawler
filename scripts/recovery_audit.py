"""Read-only recovery audit. Produces guarded proposals, never applies repairs."""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from raw_html_store import read_raw_html


def build_dry_run(tenant_id, rows, raw_root):
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,120}", tenant_id):
        raise ValueError("invalid_tenant")
    root = Path(raw_root).resolve(strict=True)
    candidates = []
    for row in rows:
        verified = False
        try:
            path = Path(row.get('raw_html_path') or '')
            path = (path if path.is_absolute() else root / path).resolve(strict=True)
            path.relative_to(root)
            body = read_raw_html(path)
            verified = hashlib.sha256(body.encode('utf-8')).hexdigest() == row.get('content_hash')
        except (OSError, ValueError):
            pass
        category = 'unknown'
        if verified:
            from amazon_us_worker import parse_product_html, _is_sibling_variant_redirect
            parsed = parse_product_html(body, 'https://www.amazon.com/dp/' + row['asin'])
            if _is_sibling_variant_redirect(parsed, row['asin']):
                category = 'strict_variant'
            elif row.get('block_reason') in {'captcha', 'robot_check', 'http_403', 'http_429', 'waf'} or row.get('http_status') in {403, 429}:
                category = 'access_control'
            elif row.get('http_status') in {404, 410}:
                category = 'deterministic_product_issue'
        elif not row.get('content_hash') and row.get('error_code') in {'fetch_error', 'review_fetch_error', 'transport_error'}:
            category = 'retryable_transport'
        if verified and row.get('snapshot_at') and row.get('evidence_at') and row['snapshot_at'] >= row['evidence_at'] and not row.get('error_code') and not row.get('block_reason'):
            category = 'stale_recovered'
        guard = {key: row.get(key) for key in ('asin', 'status', 'evidence_id', 'content_hash', 'state_updated_at', 'snapshot_id', 'lease_expires_at')}
        guard['tenant_id'] = tenant_id
        guard = json.loads(json.dumps(guard, default=str))
        key = hashlib.sha256(json.dumps(guard, sort_keys=True).encode()).hexdigest()
        candidates.append({**guard, 'category': category, 'raw_hash_verified': verified,
                           'idempotency_key': key, 'proposal': 'manual_review' if category == 'unknown' else 'guarded_reconciliation' if category in {'strict_variant', 'stale_recovered'} else 'bounded_recovery_review',
                           'apply_allowed': False})
    return {'tenant_id': tenant_id, 'dry_run': True, 'production_writes': 0,
            'apply_contract': 'separate approval; transaction locks exact tenant/ASIN; compare latest evidence/hash, state and lease; deduplicate idempotency key; append audit; never rewrite history',
            'counts': dict(Counter(row['category'] for row in candidates)), 'candidates': candidates}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tenant-id', required=True)
    parser.add_argument('--raw-root', type=Path, required=True)
    args = parser.parse_args()
    try:
        import psycopg
        from psycopg.rows import dict_row
        with psycopg.connect(os.environ['AMAZON_US_POSTGRES_DSN'], row_factory=dict_row,
                              options='-c default_transaction_read_only=on', connect_timeout=5) as conn:
            conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            rows = conn.execute('''
                SELECT s.asin,s.status,s.updated_at AS state_updated_at,s.lease_expires_at,
                       e.id AS evidence_id,e.content_hash,e.raw_html_path,e.block_reason,
                       e.http_status,e.error_code,e.retrieved_at AS evidence_at,
                       p.snapshot_id,p.collected_at AS snapshot_at
                FROM amazon_us.item_state s
                LEFT JOIN LATERAL (
                    SELECT * FROM amazon_us.collection_evidence e
                    WHERE e.tenant_id=s.tenant_id AND e.marketplace=s.marketplace
                      AND e.asin=s.asin AND e.subject_type=s.subject_type ORDER BY e.id DESC LIMIT 1
                ) e ON true
                LEFT JOIN LATERAL (
                    SELECT snapshot_id,collected_at FROM amazon_us.product_snapshot p
                    WHERE p.tenant_id=s.tenant_id AND p.marketplace=s.marketplace
                      AND p.asin=s.asin AND p.subject_type=s.subject_type ORDER BY snapshot_id DESC LIMIT 1
                ) p ON true
                WHERE s.tenant_id=%s AND s.status IN ('failed','blocked') ORDER BY s.asin
            ''', (args.tenant_id,)).fetchall()
            result = build_dry_run(args.tenant_id, rows, args.raw_root)
            conn.rollback()
        print(json.dumps(result, ensure_ascii=True))
        return 0
    except Exception as exc:
        print(json.dumps({'status': 'audit_failed', 'sqlstate': getattr(exc, 'sqlstate', None)}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
