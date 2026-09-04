"""Durable bounded recovery shared by Controller and refresh-only Agent.

All admission and finalization run under PostgreSQL locks. A new process/run
does not create a new budget. Schema installation is an explicit deployment step.
"""
from __future__ import annotations

from datetime import datetime, timedelta
import time
import uuid


class RecoveryDenied(RuntimeError):
    pass


def read_projection(connect, tenant):
    """One read-only projection for Console, Agent API and derived receipts."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT to_regclass('amazon_us.recovery_job') AS relation")
        if not cur.fetchone()['relation']:
            return {'availability':'unknown','reason':'recovery_schema_required','counts':None,'jobs':None,'egress':None}
        cur.execute('''SELECT outcome,COUNT(*) AS count FROM amazon_us.recovery_job
                       WHERE tenant_id=%s GROUP BY outcome ORDER BY outcome''',(tenant,))
        counts = {row['outcome']:row['count'] for row in cur.fetchall()}
        cur.execute('''SELECT asin,subject_type,stage,attempts,max_attempts,request_count,max_requests,
                       deadline,next_retry_at,outcome,terminal,known_bytes,unknown_byte_attempts,browser_request_count,relay_payload_bytes,
                       CASE WHEN unknown_byte_attempts=0 THEN known_bytes ELSE NULL END AS total_bytes,
                       CASE WHEN terminal THEN 'terminal_manual_or_resolved'
                            WHEN deadline<=CURRENT_TIMESTAMP THEN 'deadline_exhausted'
                            WHEN attempts>=max_attempts OR request_count>=max_requests OR browser_request_count>=240 THEN 'budget_exhausted'
                            WHEN next_retry_at>CURRENT_TIMESTAMP THEN 'cooldown' ELSE 'due' END AS next_claim_reason
                       FROM amazon_us.recovery_job WHERE tenant_id=%s ORDER BY updated_at DESC,asin LIMIT 100''',(tenant,))
        jobs = [dict(row) for row in cur.fetchall()]
        cur.execute('''SELECT egress_id,paused_until,manually_paused,half_open,consecutive_blocks,next_request_at,outcomes,
                       lease_expires_at FROM amazon_us.recovery_egress WHERE tenant_id=%s ORDER BY egress_id''',(tenant,))
        gates = [dict(row) for row in cur.fetchall()]
        for gate in gates:
            outcomes = gate.pop('outcomes')
            flags = [bool(value.get('blocked')) if isinstance(value,dict) else bool(value) for value in outcomes]
            gate['sample_count'] = len(flags)
            gate['blocked_count'] = sum(flags)
            gate['blocked_rate'] = sum(flags)/len(flags) if flags else None
    def public(row):
        return {key:value.isoformat() if hasattr(value,'isoformat') else value for key,value in row.items()}
    return {'availability':'available','tenant_id':tenant,'counts':counts,'jobs':[public(row) for row in jobs],
            'egress':[public(row) for row in gates], 'job_limit':100,'scope':'current_tenant_not_historical_run',
            'proxy_billed_bytes':None}


class RecoveryScheduler:
    def __init__(self, storage, config=None):
        self.storage = storage
        self.config = dict(config or {})
        # Deliberately independent of ports, credentials, config hashes and run IDs.
        self.egress_id = 'paid-residential'
        self.interval = max(5.0, float(self.config.get('recovery_request_interval_seconds', 5)))
        self.last_denial = None
        self.authorization_expires_at = None

    def get(self, asin, stage='product'):
        with self.storage._connect_factory() as conn, conn.cursor() as cur:
            cur.execute('SELECT * FROM amazon_us.recovery_job WHERE tenant_id=%s AND asin=%s AND subject_type=%s AND stage=%s',
                        (self.storage.tenant_id, asin, self.storage.subject_type, stage))
            row = cur.fetchone()
            return {key: value.isoformat() if hasattr(value, 'isoformat') else value
                    for key,value in dict(row).items() if key != 'lease_token'} if row else None

    def due_count(self, limit=5):
        with self.storage._connect_factory() as conn, conn.cursor() as cur:
            cur.execute('''SELECT COUNT(*) AS count FROM (
                SELECT DISTINCT s.asin FROM amazon_us.refresh_request r
                JOIN amazon_us.item_state s USING(tenant_id,marketplace,asin,subject_type)
                LEFT JOIN amazon_us.recovery_job j ON j.tenant_id=s.tenant_id AND j.asin=s.asin
                  AND j.subject_type=s.subject_type AND j.stage='product'
                WHERE r.tenant_id=%s AND r.subject_type=%s AND r.status='queued'
                  AND (s.lease_expires_at IS NULL OR s.lease_expires_at<=CURRENT_TIMESTAMP)
                  AND (s.next_retry_at IS NULL OR s.next_retry_at<=CURRENT_TIMESTAMP)
                  AND (j.tenant_id IS NULL OR (NOT j.terminal AND j.attempts<j.max_attempts
                       AND j.request_count<j.max_requests AND j.browser_request_count<240 AND j.deadline>CURRENT_TIMESTAMP
                       AND (j.next_retry_at IS NULL OR j.next_retry_at<=CURRENT_TIMESTAMP)))
                  AND NOT EXISTS(SELECT 1 FROM amazon_us.recovery_egress e WHERE e.tenant_id=r.tenant_id
                       AND e.egress_id=%s AND (e.manually_paused OR e.paused_until>CURRENT_TIMESTAMP OR e.lease_expires_at>CURRENT_TIMESTAMP))
                LIMIT %s) due''', (self.storage.tenant_id,self.storage.subject_type,self.egress_id,limit))
            return int(cur.fetchone()['count'])

    def abort(self,evidence=None):
        task = getattr(self,'current_task',None)
        if not task:
            return
        tenant, subject = self.storage.tenant_id,self.storage.subject_type
        with self.storage._connect_factory() as conn, conn.cursor() as cur:
            cur.execute('SELECT 1 FROM amazon_us.recovery_egress WHERE tenant_id=%s AND egress_id=%s FOR UPDATE', (tenant,self.egress_id))
            transfer=(evidence or {}).get('transfer_bytes')
            cur.execute("UPDATE amazon_us.recovery_job SET terminal=true,outcome='budget_or_lease_denied',lease_token=NULL,known_bytes=known_bytes+%s,unknown_byte_attempts=unknown_byte_attempts+%s WHERE tenant_id=%s AND asin=%s AND subject_type=%s AND lease_token=%s", (max(0,int(transfer or 0)),int(transfer is None),tenant,task['asin'],subject,task['lease_token']))
            cur.execute("UPDATE amazon_us.item_state SET status='failed',last_error='recovery_budget_or_lease_denied',lease_token=NULL,lease_owner=NULL,lease_expires_at=NULL WHERE tenant_id=%s AND asin=%s AND subject_type=%s AND lease_token=%s", (tenant,task['asin'],subject,task['lease_token']))
            if cur.rowcount:
                cur.execute("UPDATE amazon_us.refresh_request SET status='failed',completed_at=CURRENT_TIMESTAMP WHERE tenant_id=%s AND asin=%s AND subject_type=%s AND status='claimed'", (tenant,task['asin'],subject))
                cur.execute("INSERT INTO amazon_us.state_history(tenant_id,marketplace,asin,subject_type,from_status,to_status,reason) VALUES(%s,'US',%s,%s,'running','failed','recovery_budget_or_lease_denied')", (tenant,task['asin'],subject))
                if evidence:
                    cur.execute('''INSERT INTO amazon_us.collection_evidence
                        (tenant_id,marketplace,asin,subject_type,run_id,url,transfer_bytes,source_type,error_code,context_json)
                        VALUES(%s,'US',%s,%s,%s,%s,%s,%s,'recovery_budget_or_lease_denied',%s)''',
                        (tenant,task['asin'],subject,evidence['run_id'],task['url'],evidence.get('transfer_bytes'),
                         evidence.get('source_type'),self.storage._jsonb(evidence.get('context_json') or {})))
            cur.execute('UPDATE amazon_us.recovery_egress SET lease_token=NULL,lease_expires_at=NULL WHERE tenant_id=%s AND egress_id=%s AND lease_token=%s', (tenant,self.egress_id,task['lease_token']))

    def claim(self, worker_id, *, lease_seconds=600, stage=None, refresh_only=False):
        if not worker_id or not 1 <= lease_seconds <= 3600:
            raise ValueError('invalid_recovery_lease')
        tenant, subject = self.storage.tenant_id, self.storage.subject_type
        token = uuid.uuid4().hex
        with self.storage._connect_factory() as conn, conn.cursor() as cur:
            cur.execute('INSERT INTO amazon_us.recovery_egress(tenant_id,egress_id) VALUES (%s,%s) ON CONFLICT DO NOTHING', (tenant, self.egress_id))
            cur.execute('SELECT *,CURRENT_TIMESTAMP AS now FROM amazon_us.recovery_egress WHERE tenant_id=%s AND egress_id=%s FOR UPDATE', (tenant,self.egress_id))
            gate = dict(cur.fetchone())
            now = gate['now']
            if self.authorization_expires_at and datetime.fromisoformat(self.authorization_expires_at) <= now:
                self.last_denial = 'recovery_authorization_expired'
                return None
            if gate['manually_paused'] or (gate['paused_until'] and gate['paused_until'] > now) or (gate['lease_expires_at'] and gate['lease_expires_at'] > now):
                self.last_denial = 'recovery_manual_pause' if gate['manually_paused'] else 'recovery_global_pause' if gate['paused_until'] and gate['paused_until'] > now else 'recovery_egress_busy'
                return None
            cur.execute('''
                SELECT s.*,r.job_id,j.attempts AS recovery_attempts
                FROM amazon_us.item_state s
                LEFT JOIN amazon_us.recovery_job j ON j.tenant_id=s.tenant_id AND j.asin=s.asin
                  AND j.subject_type=s.subject_type AND j.stage=CASE WHEN %s THEN 'product' ELSE s.task_stage END
                LEFT JOIN LATERAL (
                    SELECT job_id FROM amazon_us.refresh_request r WHERE r.tenant_id=s.tenant_id
                    AND r.marketplace=s.marketplace AND r.asin=s.asin AND r.subject_type=s.subject_type
                    AND r.status='queued' ORDER BY requested_at,job_id LIMIT 1
                ) r ON true
                WHERE s.tenant_id=%s AND s.subject_type=%s AND s.marketplace='US'
                  AND NOT(s.asin=ANY(%s))
                  AND (%s::text IS NULL OR s.task_stage=%s)
                  AND (s.lease_expires_at IS NULL OR s.lease_expires_at <= CURRENT_TIMESTAMP)
                  AND (s.next_retry_at IS NULL OR s.next_retry_at <= CURRENT_TIMESTAMP)
                  AND ((%s AND r.job_id IS NOT NULL) OR (NOT %s AND
                       (s.status IN ('pending','reviews_pending','running') OR
                        (s.status IN ('failed','blocked') AND j.terminal=false))))
                  AND (j.tenant_id IS NULL OR (NOT j.terminal AND j.attempts < j.max_attempts
                       AND j.request_count < j.max_requests AND j.browser_request_count<240 AND j.deadline>CURRENT_TIMESTAMP
                       AND (j.next_retry_at IS NULL OR j.next_retry_at<=CURRENT_TIMESTAMP)))
                ORDER BY s.updated_at,s.asin FOR UPDATE OF s SKIP LOCKED LIMIT 1
            ''', (refresh_only, tenant, subject, sorted(getattr(self.storage,'_recovery_excluded_asins',set())), stage, stage, refresh_only, refresh_only))
            row = cur.fetchone()
            if not row:
                self.last_denial = 'recovery_no_due_work'
                return None
            task = dict(row)
            job_stage = 'product' if refresh_only else task['task_stage']
            if task['recovery_attempts'] is None:
                cur.execute('''SELECT COUNT(*) AS count FROM amazon_us.collection_evidence
                               WHERE tenant_id=%s AND asin=%s AND subject_type=%s''',(tenant,task['asin'],subject))
                legacy_evidence = int(cur.fetchone()['count'])
                legacy_attempts = int(task.get('attempts') or 0)
                legacy_unknown = legacy_evidence > 0
                exhausted = legacy_unknown or legacy_attempts >= 3
                cur.execute('''INSERT INTO amazon_us.recovery_job(tenant_id,asin,subject_type,stage,deadline,egress_id,attempts,terminal,outcome)
                               VALUES(%s,%s,%s,%s,CURRENT_TIMESTAMP+INTERVAL '24 hours',%s,%s,%s,%s) ON CONFLICT DO NOTHING''',
                            (tenant,task['asin'],subject,job_stage,self.egress_id,legacy_attempts,exhausted,
                             'legacy_budget_unknown' if legacy_unknown else 'budget_exhausted' if exhausted else 'new'))
                if exhausted:
                    return None
            cur.execute('''UPDATE amazon_us.recovery_job SET attempts=attempts+1,lease_token=%s,updated_at=CURRENT_TIMESTAMP
                           WHERE tenant_id=%s AND asin=%s AND subject_type=%s AND stage=%s RETURNING *''',
                        (token,tenant,task['asin'],subject,job_stage))
            budget = dict(cur.fetchone())
            cur.execute('''UPDATE amazon_us.item_state SET status='running',resume_status=%s,task_stage=%s,
                           lease_token=%s,lease_owner=%s,lease_expires_at=LEAST(CURRENT_TIMESTAMP+(%s*INTERVAL '1 second'),COALESCE(%s::timestamptz,'infinity'::timestamptz))
                           WHERE tenant_id=%s AND asin=%s AND subject_type=%s AND marketplace='US' RETURNING *''',
                        ('pending' if refresh_only or task['status']=='running' else task['status'],job_stage,token,worker_id,lease_seconds,self.authorization_expires_at,tenant,task['asin'],subject))
            claimed = dict(cur.fetchone())
            if refresh_only:
                cur.execute("UPDATE amazon_us.refresh_request SET status='claimed',claimed_at=CURRENT_TIMESTAMP WHERE job_id=%s AND tenant_id=%s", (task['job_id'],tenant))
                claimed['job_id'] = task['job_id']
            cur.execute('''UPDATE amazon_us.recovery_egress SET lease_token=%s,lease_expires_at=%s,
                           half_open=(paused_until IS NOT NULL) WHERE tenant_id=%s AND egress_id=%s''',
                        (token,claimed['lease_expires_at'],tenant,self.egress_id))
            cur.execute("INSERT INTO amazon_us.state_history(tenant_id,marketplace,asin,subject_type,from_status,to_status,reason) VALUES(%s,'US',%s,%s,%s,'running','recovery_claimed')", (tenant,task['asin'],subject,task['status']))
            claimed['recovery'] = budget
            if hasattr(self.storage,'_recovery_excluded_asins'):
                self.storage._recovery_excluded_asins.add(task['asin'])
            self.current_task = claimed
            return claimed

    def finish(self, cursor, task, outcome, *, retry_after=None, transfer_bytes=None):
        if not task.get('recovery'):
            return
        tenant, subject = self.storage.tenant_id, self.storage.subject_type
        key = (tenant, task['asin'], subject, task['recovery']['stage'])
        cursor.execute('SELECT *,CURRENT_TIMESTAMP AS now FROM amazon_us.recovery_egress WHERE tenant_id=%s AND egress_id=%s FOR UPDATE', (tenant,self.egress_id))
        gate = dict(cursor.fetchone())
        cursor.execute('SELECT * FROM amazon_us.recovery_job WHERE tenant_id=%s AND asin=%s AND subject_type=%s AND stage=%s FOR UPDATE', key)
        job = dict(cursor.fetchone())
        if job['lease_token'] != task['lease_token']:
            raise RecoveryDenied('recovery_lease_lost')
        terminal = outcome in {'completed','partial','variant','identity_terminal','unknown'} or job['attempts'] >= job['max_attempts'] or job['request_count'] >= job['max_requests'] or job['browser_request_count'] >= 240 or job['relay_payload_bytes'] >= 64*1024*1024 or job['deadline'] <= gate['now']
        cooldown = max(3600 if outcome == 'access_control' else 0 if outcome == 'review_page' else 60, float(retry_after or 0))
        due = None if terminal else gate['now'] + timedelta(seconds=cooldown)
        cursor.execute('''UPDATE amazon_us.recovery_job SET outcome=%s,terminal=%s,next_retry_at=%s,lease_token=NULL,
                          known_bytes=known_bytes+%s,unknown_byte_attempts=unknown_byte_attempts+%s,updated_at=CURRENT_TIMESTAMP
                          WHERE tenant_id=%s AND asin=%s AND subject_type=%s AND stage=%s''',
                       (outcome,terminal,due,max(0,int(transfer_bytes or 0)),int(transfer_bytes is None),*key))
        cursor.execute("UPDATE amazon_us.item_state SET next_retry_at=%s WHERE tenant_id=%s AND asin=%s AND subject_type=%s AND marketplace='US'", (due,tenant,task['asin'],subject))
        if task.get('job_id'):
            refresh_status='completed' if outcome in {'completed','partial','variant'} else 'failed' if terminal else 'queued'
            cursor.execute('''UPDATE amazon_us.refresh_request SET status=%s,
                              completed_at=CASE WHEN %s='queued' THEN NULL ELSE CURRENT_TIMESTAMP END
                              WHERE tenant_id=%s AND job_id=%s AND status IN ('claimed','queued')''',
                           (refresh_status,refresh_status,tenant,task['job_id']))
        sample_key = task['asin']+'|'+task['recovery']['stage']
        prior = [value if isinstance(value,dict) else {'key':'legacy-'+str(index),'blocked':bool(value)}
                 for index,value in enumerate(gate['outcomes'])]
        window = [value for value in prior if value['key'] != sample_key][-19:]
        window.append({'key':sample_key,'blocked':outcome == 'access_control'})
        if gate['half_open'] and outcome in {'completed','partial','variant'}:
            window = [{'key':sample_key,'blocked':False}]
        consecutive = 0
        for value in reversed(window):
            if not value['blocked']: break
            consecutive += 1
        block_count = sum(value['blocked'] for value in window)
        rolling_pause = len(window) >= 3 and block_count >= 3 and block_count/len(window) >= 0.15
        pause = bool(retry_after and retry_after > 0) or (gate['half_open'] and outcome not in {'completed','partial','variant'}) or (outcome == 'access_control' and (consecutive >= 2 or rolling_pause))
        cursor.execute('''UPDATE amazon_us.recovery_egress SET outcomes=%s,consecutive_blocks=%s,
                          paused_until=%s,half_open=false,lease_token=NULL,lease_expires_at=NULL
                          WHERE tenant_id=%s AND egress_id=%s AND lease_token=%s''',
                       (self.storage._jsonb(window),consecutive,gate['now']+timedelta(seconds=max(3600,cooldown)) if pause else None,tenant,self.egress_id,task['lease_token']))

    def before_request(self, task):
        """Charge each HTTP retry / browser navigation before transport, not after."""
        while True:
            with self.storage._connect_factory() as conn, conn.cursor() as cur:
                cur.execute('SELECT *,CURRENT_TIMESTAMP AS now FROM amazon_us.recovery_egress WHERE tenant_id=%s AND egress_id=%s FOR UPDATE', (self.storage.tenant_id,self.egress_id))
                gate = dict(cur.fetchone())
                cur.execute('SELECT * FROM amazon_us.recovery_job WHERE tenant_id=%s AND asin=%s AND subject_type=%s AND stage=%s FOR UPDATE',
                            (self.storage.tenant_id,task['asin'],self.storage.subject_type,task['recovery']['stage']))
                job = dict(cur.fetchone())
                now = gate['now']
                if gate['manually_paused'] or gate['lease_token'] != task['lease_token'] or gate['lease_expires_at'] <= now or job['terminal'] or job['deadline'] <= now or job['request_count'] >= job['max_requests'] or job['known_bytes'] >= 50_000_000 or job['relay_payload_bytes'] >= 64*1024*1024:
                    raise RecoveryDenied('recovery_budget_or_lease_denied')
                wait = max(0,(gate['next_request_at']-now).total_seconds()) if gate['next_request_at'] else 0
                if not wait:
                    cur.execute('UPDATE amazon_us.recovery_job SET request_count=request_count+1 WHERE tenant_id=%s AND asin=%s AND subject_type=%s AND stage=%s',
                                (self.storage.tenant_id,task['asin'],self.storage.subject_type,task['recovery']['stage']))
                    cur.execute("UPDATE amazon_us.recovery_egress SET next_request_at=CURRENT_TIMESTAMP+(%s*INTERVAL '1 second') WHERE tenant_id=%s AND egress_id=%s", (self.interval,self.storage.tenant_id,self.egress_id))
                    return min(60.0,(job['deadline']-now).total_seconds(),(gate['lease_expires_at']-now).total_seconds())
            time.sleep(min(wait,1))

    def before_browser_request(self, task):
        with self.storage._connect_factory() as conn, conn.cursor() as cur:
            cur.execute('SELECT *,CURRENT_TIMESTAMP AS now FROM amazon_us.recovery_egress WHERE tenant_id=%s AND egress_id=%s FOR UPDATE', (self.storage.tenant_id,self.egress_id))
            gate = dict(cur.fetchone())
            if gate['manually_paused'] or gate['lease_token'] != task['lease_token'] or gate['lease_expires_at'] <= gate['now']:
                raise RecoveryDenied('recovery_browser_lease_denied')
            cur.execute('''UPDATE amazon_us.recovery_job SET browser_request_count=browser_request_count+1
                           WHERE tenant_id=%s AND asin=%s AND subject_type=%s AND stage=%s
                           AND lease_token=%s AND NOT terminal AND deadline>CURRENT_TIMESTAMP
                           AND browser_request_count<240 AND relay_payload_bytes<67108864 RETURNING browser_request_count''',
                        (self.storage.tenant_id,task['asin'],self.storage.subject_type,task['recovery']['stage'],task['lease_token']))
            if cur.fetchone() is None:
                raise RecoveryDenied('recovery_browser_budget_denied')

    def consume_relay_bytes(self, task, count):
        count = int(count)
        if not 0 < count <= 65536:
            return False
        with self.storage._connect_factory() as conn, conn.cursor() as cur:
            cur.execute('SELECT *,CURRENT_TIMESTAMP AS now FROM amazon_us.recovery_egress WHERE tenant_id=%s AND egress_id=%s FOR UPDATE', (self.storage.tenant_id,self.egress_id))
            gate = dict(cur.fetchone())
            if gate['manually_paused'] or gate['lease_token'] != task['lease_token'] or gate['lease_expires_at'] <= gate['now']:
                return False
            cur.execute('''UPDATE amazon_us.recovery_job SET relay_payload_bytes=relay_payload_bytes+%s
                           WHERE tenant_id=%s AND asin=%s AND subject_type=%s AND stage=%s
                           AND lease_token=%s AND NOT terminal AND deadline>CURRENT_TIMESTAMP
                           RETURNING relay_payload_bytes''',
                        (count,self.storage.tenant_id,task['asin'],self.storage.subject_type,task['recovery']['stage'],task['lease_token']))
            row = cur.fetchone()
            return row is not None and row['relay_payload_bytes'] <= 64*1024*1024
