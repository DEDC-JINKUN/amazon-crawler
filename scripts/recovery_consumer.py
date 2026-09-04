"""A bounded, live consumer for one immutable product cohort, not a daemon platform."""
from __future__ import annotations

from datetime import datetime,timezone
import time


def prepare_batch(storage,run_id,target,manifest_asins,max_seconds):
    if not 1<=int(target)<=10000 or not 1<=int(max_seconds)<=5400:
        raise ValueError('invalid_recovery_batch_budget')
    supplied=list(dict.fromkeys(manifest_asins or []))
    with storage._connect_factory() as conn, conn.cursor() as cur:
        cur.execute('SELECT * FROM amazon_us.recovery_batch WHERE tenant_id=%s AND run_id=%s FOR UPDATE',(storage.tenant_id,run_id))
        row=cur.fetchone()
        if row:
            batch=dict(row)
            if len(batch['asins'])!=target or (supplied and not set(batch['asins']).issubset(supplied)):
                raise ValueError('cohort_identity_conflict')
        else:
            if len(supplied)==target:
                selected=supplied
            else:
                cur.execute('''SELECT s.asin FROM amazon_us.item_state s
                    JOIN amazon_us.asin_master m USING(tenant_id,marketplace,asin,subject_type)
                    LEFT JOIN amazon_us.recovery_job j ON j.tenant_id=s.tenant_id AND j.asin=s.asin
                      AND j.subject_type=s.subject_type AND j.stage='product'
                    WHERE s.tenant_id=%s AND s.subject_type=%s AND s.marketplace='US' AND s.task_stage='product'
                      AND (%s::text[] IS NULL OR s.asin=ANY(%s))
                      AND (s.status='pending' OR (s.status IN ('failed','blocked','running') AND j.terminal=false))
                    ORDER BY m.priority DESC,s.updated_at,s.asin LIMIT %s''',
                    (storage.tenant_id,storage.subject_type,supplied or None,supplied or None,target))
                selected=[row['asin'] for row in cur.fetchall()]
            if len(selected)!=target:
                raise ValueError('cohort_scope_insufficient')
            cur.execute('''INSERT INTO amazon_us.recovery_batch(tenant_id,run_id,subject_type,asins,deadline)
                           VALUES(%s,%s,%s,%s,CURRENT_TIMESTAMP+(%s*INTERVAL '1 second')) RETURNING *''',
                        (storage.tenant_id,run_id,storage.subject_type,selected,max_seconds))
            batch=dict(cur.fetchone())
        metadata={key:value.isoformat() if hasattr(value,'isoformat') else value for key,value in batch.items()}
        cur.execute('''UPDATE amazon_us.collection_run SET receipt_json=receipt_json || %s
                       WHERE tenant_id=%s AND run_id=%s''',
                    (storage._jsonb({'recovery_batch':metadata}),storage.tenant_id,run_id))
    storage._recovery_allowed_asins=list(batch['asins'])
    return metadata


def batch_progress(storage,run_id):
    with storage._connect_factory() as conn, conn.cursor() as cur:
        cur.execute('SELECT *,CURRENT_TIMESTAMP AS now FROM amazon_us.recovery_batch WHERE tenant_id=%s AND run_id=%s', (storage.tenant_id,run_id))
        batch=dict(cur.fetchone())
        now=batch['now']
        cur.execute('''SELECT s.asin,s.status,s.lease_expires_at,s.next_retry_at AS state_retry,
                       j.attempts,j.max_attempts,j.request_count,j.max_requests,j.browser_request_count,j.relay_payload_bytes,
                       j.deadline,j.next_retry_at,j.terminal,j.outcome
                       FROM amazon_us.item_state s LEFT JOIN amazon_us.recovery_job j
                         ON j.tenant_id=s.tenant_id AND j.asin=s.asin AND j.subject_type=s.subject_type AND j.stage='product'
                       WHERE s.tenant_id=%s AND s.subject_type=%s AND s.asin=ANY(%s)''',
                    (storage.tenant_id,storage.subject_type,batch['asins']))
        jobs=[dict(row) for row in cur.fetchall()]
        cur.execute('SELECT paused_until,manually_paused,lease_expires_at FROM amazon_us.recovery_egress WHERE tenant_id=%s AND egress_id=%s', (storage.tenant_id,'paid-residential'))
        gate=dict(cur.fetchone() or {})
    terminal=due=resolved=0
    waits=[]
    for job in jobs:
        exhausted=bool(job['terminal']) or (job['attempts'] is not None and (
            job['attempts']>=job['max_attempts'] or job['request_count']>=job['max_requests']
            or job['browser_request_count']>=240 or job['relay_payload_bytes']>=67108864 or job['deadline']<=now))
        unavailable=job['attempts'] is None and job['status'] not in {'pending','running'}
        if exhausted or unavailable:
            terminal+=1
            resolved+=int(job['outcome'] in {'completed','partial','variant'})
            continue
        ready_at=max([now]+[value for value in (job['next_retry_at'],job['state_retry'],job['lease_expires_at'],gate.get('paused_until'),gate.get('lease_expires_at')) if value])
        wait=max(0,(ready_at-now).total_seconds())
        waits.append(wait)
        if not wait and not gate.get('manually_paused'): due+=1
    return {'run_id':run_id,'asins':batch['asins'],'requested':len(batch['asins']),
            'terminal_count':terminal,'resolved_count':resolved,'due_count':due,
            'wait_seconds':min(waits) if waits else 0,'deadline_expired':batch['deadline']<=now,
            'manual_pause':bool(gate.get('manually_paused')),'remaining_seconds':max(0,(batch['deadline']-now).total_seconds())}


def finish_batch(storage,run_id,status,reason=None):
    with storage._connect_factory() as conn,conn.cursor() as cur:
        cur.execute('''UPDATE amazon_us.recovery_batch SET status=%s,reason=%s,finished_at=CURRENT_TIMESTAMP
                       WHERE tenant_id=%s AND run_id=%s AND status='running' RETURNING *''',(status,reason,storage.tenant_id,run_id))
        value=cur.fetchone()
        if value is None:
            cur.execute('SELECT * FROM amazon_us.recovery_batch WHERE tenant_id=%s AND run_id=%s',(storage.tenant_id,run_id))
            value=cur.fetchone()
        row=dict(value)
        metadata={key:value.isoformat() if hasattr(value,'isoformat') else value for key,value in row.items()}
        cur.execute('UPDATE amazon_us.collection_run SET receipt_json=receipt_json || %s WHERE tenant_id=%s AND run_id=%s',
                    (storage._jsonb({'recovery_batch':metadata}),storage.tenant_id,run_id))


def consume_batch(storage,adapter,config,*,run_id,target,worker_id,lease_seconds,max_seconds,reservation_id,run_once):
    from proxy_capacity_gate import ProxyCapacityGateDenied,capacity_batch_actions
    storage.configure_recovery(config)
    batch=storage.prepare_recovery_batch(run_id,target,config.get('manifest_asins'),max_seconds)
    scoped={**config,'_recovery_batch_deadline':batch['deadline'],
            'context':{**(config.get('context') or {}),'recovery_batch_id':run_id}}
    wait=config.get('_recovery_wait') or time.sleep
    waitable={'capacity_evidence_stale','capacity_evidence_missing','capacity_reserved_elsewhere',
              'capacity_reservation_expired','capacity_authorization_expired','recovery_authorization_expired'}
    last_notice=0.0
    try:
        while True:
            storage.reclaim_expired_leases()
            progress=storage.recovery_batch_progress(run_id)
            if progress['manual_pause']:
                storage.finish_recovery_batch(run_id,'interrupted','manual_pause')
                return -1
            if progress['terminal_count']==target:
                ok=progress['resolved_count']==target
                storage.finish_recovery_batch(run_id,'completed' if ok else 'exhausted','cohort_resolved' if ok else 'terminal_or_budget_exhausted')
                return target if ok else -1
            if progress['deadline_expired']:
                storage.finish_recovery_batch(run_id,'deadline','consumer_deadline_exhausted')
                return -1
            delay=max(0.05,min(5.0,progress['wait_seconds'] or 1.0,progress['remaining_seconds']))
            if progress['due_count']:
                fact=storage.load_latest_proxy_capacity(max_age_seconds=int(config.get('proxy_canary_max_age_seconds') or 3600))
                size=capacity_batch_actions(config,progress['due_count'],fact)
                storage.begin_recovery_pass()
                try:
                    result=run_once(storage,adapter,scoped,limit=size,run_id=run_id,worker_id=worker_id,
                                    lease_seconds=lease_seconds,product_only=True,capacity_reservation_id=reservation_id)
                except ProxyCapacityGateDenied as exc:
                    if exc.reason not in waitable:
                        storage.finish_recovery_batch(run_id,'exhausted',exc.reason)
                        raise
                    delay=min(5.0,progress['remaining_seconds'])
                else:
                    if result != 0:
                        continue
                finally:
                    reservation_id=None
            if time.monotonic()-last_notice>=30:
                print(f"phase=recovery_wait; run_id={run_id}; terminal={progress['terminal_count']}/{target}; consumer_alive=true",flush=True)
                last_notice=time.monotonic()
            wait(max(0.01,delay))
    except BaseException:
        storage.finish_recovery_batch(run_id,'interrupted','consumer_interrupted')
        raise
