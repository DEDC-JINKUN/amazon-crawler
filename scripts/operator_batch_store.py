"""Candidate PostgreSQL batch publication/claim interface, independent of history.

Schema is migrated by the deployment/test harness, never by this repository.
All reads and mutations are tenant+batch scoped. The caller owns process and
capacity orchestration; this module rejects evidence belonging to another run.
"""
import re
import uuid


_STAGES = frozenset({
    'queued', 'canary', 'reserving', 'collecting', 'finalizing',
    'releasing', 'cleanup_pending',
})
_TERMINAL_STATUSES = frozenset({'completed', 'failed', 'stopped'})
_MEMBER_TERMINAL_STATUSES = frozenset({
    'completed', 'variant', 'reused', 'failed', 'blocked', 'stopped',
})
_PHASE_MAP = {stage: stage for stage in (*_STAGES, *_TERMINAL_STATUSES)}


def _safe_error(value):
    """Return a short, non-sensitive error suitable for a dashboard snapshot."""
    if value is None:
        return None
    text = re.sub(r'[\x00-\x1f\x7f]+', ' ', str(value)).strip()
    text = re.sub(r'(?i)(password|token|cookie|authorization|secret)\s*[:=]\s*[^\s,;]+', r'\1=[redacted]', text)
    return text[:240] or None


class PostgresBatchStore:
    def __init__(self, dsn, tenant_id):
        self.dsn = dsn
        self.tenant_id = tenant_id

    def connect(self):
        import psycopg
        from psycopg.rows import dict_row
        return psycopg.connect(self.dsn, row_factory=dict_row, connect_timeout=3)

    def publish(self, batch_id, asins, subject_type='own'):
        members = list(dict.fromkeys(asins))
        if not members or any(not re.fullmatch('[A-Z0-9]{10}', a) for a in members):
            raise ValueError('invalid_batch_members')
        with self.connect() as conn:
            conn.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',
                         (self.tenant_id + ':' + batch_id,))
            existing = conn.execute('SELECT * FROM amazon_us.operator_batch WHERE tenant_id=%s AND batch_id=%s',
                                    (self.tenant_id,batch_id)).fetchone()
            if existing:
                prior = conn.execute('SELECT asin FROM amazon_us.operator_batch_member WHERE tenant_id=%s AND batch_id=%s ORDER BY ordinal',
                                     (self.tenant_id,batch_id)).fetchall()
                if existing['subject_type'] != subject_type or [r['asin'] for r in prior] != members:
                    raise ValueError('batch_identity_conflict')
                return batch_id
            conn.execute('INSERT INTO amazon_us.operator_batch(tenant_id,batch_id,subject_type,requested) VALUES(%s,%s,%s,%s)',
                         (self.tenant_id,batch_id,subject_type,len(members)))
            for ordinal, asin in enumerate(members):
                conn.execute('INSERT INTO amazon_us.operator_batch_member(tenant_id,batch_id,asin,ordinal) VALUES(%s,%s,%s,%s)',
                             (self.tenant_id,batch_id,asin,ordinal))
        return batch_id

    def register_run(self, batch_id, run_id, worker_id):
        with self.connect() as conn:
            batch = conn.execute(
                'SELECT 1 FROM amazon_us.operator_batch WHERE tenant_id=%s AND batch_id=%s',
                (self.tenant_id, batch_id),
            ).fetchone()
            if batch is None:
                raise ValueError('batch_not_found')
            existing = conn.execute(
                'SELECT batch_id,worker_id FROM amazon_us.operator_batch_run '
                'WHERE tenant_id=%s AND run_id=%s',
                (self.tenant_id, run_id),
            ).fetchone()
            if existing:
                if existing['batch_id'] == batch_id and existing['worker_id'] == worker_id:
                    return run_id
                raise ValueError('batch_run_identity_conflict')
            conn.execute(
                'INSERT INTO amazon_us.operator_batch_run(tenant_id,batch_id,run_id,worker_id) VALUES(%s,%s,%s,%s)',
                (self.tenant_id, batch_id, run_id, worker_id),
            )
        return run_id

    def set_stage(self, batch_id, stage):
        if stage not in _STAGES:
            raise ValueError('invalid_batch_stage')
        with self.connect() as conn:
            row = conn.execute(
                'SELECT status FROM amazon_us.operator_batch WHERE tenant_id=%s AND batch_id=%s FOR UPDATE',
                (self.tenant_id, batch_id),
            ).fetchone()
            if row is None:
                raise ValueError('batch_not_found')
            if row['status'] in _TERMINAL_STATUSES:
                raise ValueError('batch_already_finished')
            return conn.execute(
                'UPDATE amazon_us.operator_batch SET status=%s '
                'WHERE tenant_id=%s AND batch_id=%s RETURNING *',
                (stage, self.tenant_id, batch_id),
            ).fetchone()

    def request_stop(self, batch_id):
        with self.connect() as conn:
            row = conn.execute(
                'UPDATE amazon_us.operator_batch SET stop_requested=true '
                'WHERE tenant_id=%s AND batch_id=%s RETURNING *',
                (self.tenant_id, batch_id),
            ).fetchone()
            if row is None:
                raise ValueError('batch_not_found')
            return row

    def stop_requested(self, batch_id):
        with self.connect() as conn:
            row = conn.execute(
                'SELECT stop_requested FROM amazon_us.operator_batch '
                'WHERE tenant_id=%s AND batch_id=%s',
                (self.tenant_id, batch_id),
            ).fetchone()
            if row is None:
                raise ValueError('batch_not_found')
            return bool(row['stop_requested'])

    def claim(self, batch_id, run_id, worker_id, lease_seconds=60):
        if not 1 <= lease_seconds <= 600:
            raise ValueError('invalid_lease')
        with self.connect() as conn:
            conn.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',
                         ('operator-claim:' + self.tenant_id,))
            registered = conn.execute('SELECT 1 FROM amazon_us.operator_batch_run WHERE tenant_id=%s AND batch_id=%s AND run_id=%s AND worker_id=%s',
                                      (self.tenant_id,batch_id,run_id,worker_id)).fetchone()
            if not registered:
                raise ValueError('unregistered_batch_run')
            row = conn.execute('''SELECT m.* FROM amazon_us.operator_batch_member m
                JOIN amazon_us.operator_batch b USING(tenant_id,batch_id)
                WHERE m.tenant_id=%s AND m.batch_id=%s AND m.status='pending'
                  AND NOT b.stop_requested AND b.status='collecting'
                  AND NOT EXISTS (
                    SELECT 1 FROM amazon_us.operator_batch_member busy
                    JOIN amazon_us.operator_batch other USING(tenant_id,batch_id)
                    WHERE busy.tenant_id=m.tenant_id AND busy.asin=m.asin
                      AND other.subject_type=b.subject_type AND busy.status='claimed'
                      AND busy.lease_expires_at>now())
                ORDER BY m.ordinal FOR UPDATE OF m SKIP LOCKED LIMIT 1''',
                (self.tenant_id,batch_id)).fetchone()
            if row is None:
                return None
            return conn.execute('''UPDATE amazon_us.operator_batch_member SET status='claimed',
                lease_token=%s,lease_owner=%s,lease_expires_at=now()+(%s*INTERVAL '1 second')
                WHERE tenant_id=%s AND batch_id=%s AND asin=%s RETURNING *''',
                (uuid.uuid4().hex,worker_id,lease_seconds,self.tenant_id,batch_id,row['asin'])).fetchone()

    def complete(self, batch_id, asin, token, evidence_id):
        from collection_console import classify_evidence_outcome
        with self.connect() as conn:
            evidence = conn.execute('''SELECT e.* FROM amazon_us.collection_evidence e
                JOIN amazon_us.operator_batch_run r ON e.tenant_id=r.tenant_id AND e.run_id=r.run_id
                JOIN amazon_us.operator_batch b ON b.tenant_id=r.tenant_id AND b.batch_id=r.batch_id
                WHERE r.tenant_id=%s AND r.batch_id=%s AND e.asin=%s AND e.id=%s
                  AND e.subject_type=b.subject_type AND e.marketplace='US' ''',
                (self.tenant_id,batch_id,asin,evidence_id)).fetchone()
            if evidence is None:
                raise ValueError('evidence_outside_batch')
            outcome = classify_evidence_outcome(evidence)
            status = 'variant' if outcome == 'variant_redirect' else outcome
            updated = conn.execute('''UPDATE amazon_us.operator_batch_member
                SET status=%s,evidence_id=%s,lease_token=NULL,lease_owner=NULL,lease_expires_at=NULL
                WHERE tenant_id=%s AND batch_id=%s AND asin=%s AND status='claimed'
                  AND lease_token=%s AND lease_expires_at>now() RETURNING asin''',
                (status,evidence_id,self.tenant_id,batch_id,asin,token)).fetchone()
            if not updated:
                raise ValueError('batch_lease_lost')

    def finish_member(self, batch_id, asin, token, status, error=None):
        if status not in {'failed', 'blocked', 'stopped'}:
            raise ValueError('invalid_member_terminal_status')
        with self.connect() as conn:
            updated = conn.execute(
                '''UPDATE amazon_us.operator_batch_member
                   SET status=%s,last_error=%s,lease_token=NULL,lease_owner=NULL,lease_expires_at=NULL
                   WHERE tenant_id=%s AND batch_id=%s AND asin=%s
                     AND status='claimed' AND lease_token=%s
                   RETURNING *''',
                (status, _safe_error(error), self.tenant_id, batch_id, asin, token),
            ).fetchone()
            if not updated:
                raise ValueError('batch_lease_lost')
            if error:
                conn.execute(
                    'UPDATE amazon_us.operator_batch SET last_error=%s '
                    'WHERE tenant_id=%s AND batch_id=%s',
                    (_safe_error(error), self.tenant_id, batch_id),
                )
            return updated

    def finish(self, batch_id, status, error=None):
        if status not in _TERMINAL_STATUSES:
            raise ValueError('invalid_batch_terminal_status')
        with self.connect() as conn:
            batch = conn.execute(
                'SELECT * FROM amazon_us.operator_batch '
                'WHERE tenant_id=%s AND batch_id=%s FOR UPDATE',
                (self.tenant_id, batch_id),
            ).fetchone()
            if batch is None:
                raise ValueError('batch_not_found')
            if batch['status'] in _TERMINAL_STATUSES:
                if batch['status'] == status:
                    return batch
                raise ValueError('batch_already_finished')
            claimed = conn.execute(
                "SELECT 1 FROM amazon_us.operator_batch_member "
                "WHERE tenant_id=%s AND batch_id=%s AND status='claimed' LIMIT 1",
                (self.tenant_id, batch_id),
            ).fetchone()
            if claimed:
                raise ValueError('claimed_members_not_cleaned')
            if status == 'completed':
                incomplete = conn.execute(
                    "SELECT 1 FROM amazon_us.operator_batch_member "
                    "WHERE tenant_id=%s AND batch_id=%s "
                    "AND status NOT IN ('completed','variant','reused') LIMIT 1",
                    (self.tenant_id, batch_id),
                ).fetchone()
                if incomplete:
                    raise ValueError('batch_members_incomplete')
            return conn.execute(
                'UPDATE amazon_us.operator_batch SET status=%s,finished_at=now(),last_error=%s '
                'WHERE tenant_id=%s AND batch_id=%s RETURNING *',
                (status, _safe_error(error), self.tenant_id, batch_id),
            ).fetchone()

    def release_members(self, batch_id):
        """Release claims after the caller has confirmed every worker exited."""
        with self.connect() as conn:
            updated = conn.execute(
                '''UPDATE amazon_us.operator_batch_member
                   SET status='stopped',lease_token=NULL,lease_owner=NULL,lease_expires_at=NULL
                   WHERE tenant_id=%s AND batch_id=%s AND status='claimed'
                   RETURNING asin''',
                (self.tenant_id, batch_id),
            ).fetchall()
            if not updated:
                exists = conn.execute(
                    'SELECT 1 FROM amazon_us.operator_batch WHERE tenant_id=%s AND batch_id=%s',
                    (self.tenant_id, batch_id),
                ).fetchone()
                if exists is None:
                    raise ValueError('batch_not_found')
            return len(updated)

    def snapshot(self, batch_id):
        with self.connect() as conn:
            batch = conn.execute('SELECT * FROM amazon_us.operator_batch WHERE tenant_id=%s AND batch_id=%s',
                                 (self.tenant_id,batch_id)).fetchone()
            if batch is None:
                raise ValueError('batch_not_found')
            members = conn.execute(
                'SELECT * FROM amazon_us.operator_batch_member '
                'WHERE tenant_id=%s AND batch_id=%s ORDER BY ordinal',
                (self.tenant_id, batch_id),
            ).fetchall()
            counts = {status: 0 for status in ('pending', 'claimed', *_MEMBER_TERMINAL_STATUSES)}
            for member in members:
                counts[member['status']] = counts.get(member['status'], 0) + 1
                if 'last_error' in member:
                    member['last_error'] = _safe_error(member.get('last_error'))
            batch['members'] = members
            batch['counts'] = counts
            batch.update(counts)
            batch['phase'] = _PHASE_MAP.get(batch['status'], 'unknown')
            batch['safe_error'] = _safe_error(batch.get('last_error'))
            batch['last_error'] = batch['safe_error']
            return batch
