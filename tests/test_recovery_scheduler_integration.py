import os
from pathlib import Path
import sys
import uuid

import pytest

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
DSN = os.environ.get('AMAZON_TEST_POSTGRES_DSN')
pytestmark = pytest.mark.skipif(not DSN, reason='isolated PostgreSQL test DSN required')


@pytest.fixture(scope='module',autouse=True)
def isolated_schema():
    if not DSN:
        return
    import psycopg
    with psycopg.connect(DSN) as conn:
        assert conn.execute('SELECT current_database()').fetchone()[0] == 'amazon_recovery_test_20260904_13c0'
        conn.execute((ROOT / 'schema/postgres_schema.sql').read_text(encoding='utf-8'))
        conn.execute((ROOT / 'schema/migrations/20260904_recovery.sql').read_text(encoding='utf-8'))


def test_recovery_survives_restart_and_refresh_cannot_bypass_cooldown():
    import psycopg
    from postgres_worker_storage import PostgresWorkerStorage
    from recovery_scheduler import RecoveryScheduler
    tenant = 'recovery-' + uuid.uuid4().hex
    with psycopg.connect(DSN) as conn:
        assert conn.execute('SELECT current_database()').fetchone()[0] == 'amazon_recovery_test_20260904_13c0'
        conn.execute((ROOT / 'schema/postgres_schema.sql').read_text(encoding='utf-8'))
        conn.execute((ROOT / 'schema/migrations/20260904_recovery.sql').read_text(encoding='utf-8'))
    storage = PostgresWorkerStorage(DSN, tenant)
    storage.initialize_manifest([{'asin': 'B0CC2FRY3J', 'url': 'https://www.amazon.com/dp/B0CC2FRY3J'}])
    storage.configure_recovery({})
    task = storage.claim_task('worker-one')
    assert task['recovery']['attempts'] == 1
    storage.save_failure(task=task, reason='fetch_error', error='fetch_error')
    restarted = PostgresWorkerStorage(DSN, tenant)
    restarted.configure_recovery({})
    assert restarted.claim_task('worker-two') is None
    assert restarted.recovery_status('B0CC2FRY3J')['attempts'] == 1
    assert restarted.recovery_status('B0CC2FRY3J')['outcome'] == 'transport'
    with psycopg.connect(DSN) as conn:
        conn.execute("INSERT INTO amazon_us.refresh_request(job_id,tenant_id,marketplace,asin,subject_type,requested_by,reason,status) VALUES(%s,%s,'US','B0CC2FRY3J','own','test','test','queued')", (uuid.uuid4().hex,tenant))
    assert restarted.claim_refresh_task('refresh-worker') is None


def test_first_pass_scattered_captcha_does_not_preempt_untried_or_pause_cohort():
    import psycopg
    from datetime import datetime, timezone
    from postgres_worker_storage import PostgresWorkerStorage
    tenant = 'policy-' + uuid.uuid4().hex
    asins = [f'B0TST{i:05d}' for i in range(20)]
    storage = PostgresWorkerStorage(DSN, tenant)
    storage.initialize_manifest([{'asin':a, 'url':'https://www.amazon.com/dp/'+a} for a in asins])
    storage.configure_recovery({})
    storage._recovery_allowed_asins = asins
    seen = []
    for index in range(20):
        task = storage.claim_task('first-pass')
        assert task is not None
        assert task['asin'] not in seen
        seen.append(task['asin'])
        if index in {1,7,14}:
            before = datetime.now(timezone.utc)
            storage.save_failure(task=task,reason='captcha',error='captcha',next_status='blocked',state_fields={'block_reason':'captcha'})
            status = storage.recovery_status(task['asin'])
            delay = (datetime.fromisoformat(status['next_retry_at'])-before).total_seconds()
            assert 299 <= delay <= 305
            with psycopg.connect(DSN) as conn:
                conn.execute("UPDATE amazon_us.recovery_job SET next_retry_at=now()-interval '1 second' WHERE tenant_id=%s AND asin=%s",(tenant,task['asin']))
                conn.execute("UPDATE amazon_us.item_state SET next_retry_at=now()-interval '1 second',updated_at=now()-interval '1 day' WHERE tenant_id=%s AND asin=%s",(tenant,task['asin']))
        else:
            storage.save_failure(task=task,reason='variant_redirect',error=None,next_status='succeeded',increment_attempts=False)
    restarted = PostgresWorkerStorage(DSN, tenant); restarted.configure_recovery({})
    restarted._recovery_allowed_asins = asins
    retry = restarted.claim_task('recovery-after-first-pass')
    assert retry is not None and retry['recovery']['attempts'] == 2
    storage.save_failure(task=retry,reason='captcha',error='captcha',next_status='blocked',state_fields={'block_reason':'captcha'})
    with psycopg.connect(DSN) as conn:
        notes = conn.execute("SELECT reason FROM amazon_us.state_history WHERE tenant_id=%s AND reason LIKE %s",(tenant,'recovery_policy:%')).fetchall()
    assert notes and all('captcha-first-pass-v2' in row[0] for row in notes)


def test_total_budget_is_not_reset_by_crash_reclaim_or_explicit_refresh():
    import psycopg
    from postgres_worker_storage import PostgresWorkerStorage
    tenant = 'recovery-' + uuid.uuid4().hex
    storage = PostgresWorkerStorage(DSN, tenant)
    storage.configure_recovery({})
    storage.initialize_manifest([{'asin': 'B0CC2JBW2H', 'url': 'https://www.amazon.com/dp/B0CC2JBW2H'}])
    for attempt in range(1,4):
        task = storage.claim_task('worker')
        assert task['recovery']['attempts'] == attempt
        # Expire leases as a test clock boundary; no production data involved.
        with psycopg.connect(DSN) as conn:
            conn.execute("UPDATE amazon_us.item_state SET lease_expires_at=now()-INTERVAL '1 second' WHERE tenant_id=%s", (tenant,))
            conn.execute("UPDATE amazon_us.recovery_egress SET lease_expires_at=now()-INTERVAL '1 second' WHERE tenant_id=%s", (tenant,))
        storage.reclaim_expired_leases()
    assert storage.claim_task('new-process') is None
    assert storage.recovery_status('B0CC2JBW2H')['attempts'] == 3


def test_concurrent_consumers_share_one_active_egress_lease():
    from concurrent.futures import ThreadPoolExecutor
    from postgres_worker_storage import PostgresWorkerStorage
    tenant = 'recovery-' + uuid.uuid4().hex
    storage = PostgresWorkerStorage(DSN,tenant)
    storage.initialize_manifest([{'asin': asin, 'url': 'https://www.amazon.com/dp/'+asin} for asin in ('B0CC2FRY3J','B0CC2JBW2H')])
    def claim(index):
        consumer = PostgresWorkerStorage(DSN,tenant)
        consumer.configure_recovery({})
        return consumer.claim_task('worker-'+str(index))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, range(2)))
    assert sum(row is not None for row in results) == 1


def test_persistent_access_pause_allows_only_one_half_open_probe():
    import psycopg
    from postgres_worker_storage import PostgresWorkerStorage
    tenant = 'recovery-' + uuid.uuid4().hex
    storage = PostgresWorkerStorage(DSN,tenant)
    storage.configure_recovery({})
    storage.initialize_manifest([{'asin': asin,'url':'https://www.amazon.com/dp/'+asin} for asin in ('B0CC2FRY3J','B0CC2JBW2H','B0CJFNJCNV','B0DPMD58CN')])
    for _ in range(3):
        task = storage.claim_task('worker')
        storage.save_failure(task=task,reason='captcha',error='captcha',next_status='blocked',state_fields={'block_reason':'captcha'})
    assert storage.claim_task('new-run') is None
    with psycopg.connect(DSN) as conn:
        conn.execute("UPDATE amazon_us.recovery_egress SET paused_until=now()-INTERVAL '1 second' WHERE tenant_id=%s", (tenant,))
    probe = storage.claim_task('probe')
    assert probe is not None
    assert storage.claim_task('other-probe') is None
    storage.save_failure(task=probe,reason='variant_redirect',error=None,next_status='succeeded',increment_attempts=False)
    with psycopg.connect(DSN) as conn:
        paused,half_open = conn.execute('SELECT paused_until,half_open FROM amazon_us.recovery_egress WHERE tenant_id=%s',(tenant,)).fetchone()
    assert paused is None and half_open is False
    assert storage.recovery_status(probe['asin'])['outcome'] == 'variant'


def test_request_budget_survives_new_process():
    import psycopg
    from postgres_worker_storage import PostgresWorkerStorage
    from recovery_scheduler import RecoveryDenied
    tenant = 'recovery-' + uuid.uuid4().hex
    storage = PostgresWorkerStorage(DSN,tenant)
    storage.configure_recovery({})
    storage.initialize_manifest([{'asin':'B0CC2FRY3J','url':'https://www.amazon.com/dp/B0CC2FRY3J'}])
    task = storage.claim_task('worker')
    for _ in range(12):
        assert storage.before_recovery_request(task) > 0
        with psycopg.connect(DSN) as conn:
            conn.execute('UPDATE amazon_us.recovery_egress SET next_request_at=NULL WHERE tenant_id=%s',(tenant,))
    new = PostgresWorkerStorage(DSN,tenant)
    new.configure_recovery({})
    with pytest.raises(RecoveryDenied):
        new.before_recovery_request(task)
    assert new.recovery_status(task['asin'])['request_count'] == 12


def test_console_and_agent_api_project_the_same_recovery_fact_with_unknown_bytes():
    from postgres_worker_storage import PostgresWorkerStorage
    from collection_storage import PostgresCollectionRepository
    from collection_console import PostgresConsoleRepository
    tenant = 'recovery-' + uuid.uuid4().hex
    storage = PostgresWorkerStorage(DSN,tenant)
    storage.configure_recovery({})
    storage.initialize_manifest([{'asin':'B0CC2FRY3J','url':'https://www.amazon.com/dp/B0CC2FRY3J'}])
    task = storage.claim_task('worker')
    storage.save_failure(task=task,reason='fetch_error',error='fetch_error')
    api = PostgresCollectionRepository(DSN,tenant_id=tenant).load_recovery_status()
    console = PostgresConsoleRepository(DSN,tenant).load_recovery_status()
    assert api == console
    assert api['jobs'][0]['total_bytes'] is None
    assert api['jobs'][0]['unknown_byte_attempts'] == 1
    assert api['jobs'][0]['outcome'] == 'transport'
    assert 'lease_token' not in str(api)


def test_legacy_exhausted_state_cannot_gain_fresh_budget_via_refresh():
    import psycopg
    from postgres_worker_storage import PostgresWorkerStorage
    tenant = 'recovery-' + uuid.uuid4().hex
    storage = PostgresWorkerStorage(DSN,tenant)
    storage.initialize_manifest([{'asin':'B0CC2FRY3J','url':'https://www.amazon.com/dp/B0CC2FRY3J'}])
    with psycopg.connect(DSN) as conn:
        conn.execute("UPDATE amazon_us.item_state SET status='failed',attempts=3 WHERE tenant_id=%s",(tenant,))
        conn.execute("INSERT INTO amazon_us.refresh_request(job_id,tenant_id,marketplace,asin,subject_type,requested_by,reason,status) VALUES(%s,%s,'US','B0CC2FRY3J','own','test','test','queued')",(uuid.uuid4().hex,tenant))
    storage.configure_recovery({})
    assert storage.claim_refresh_task('worker') is None
    assert storage.recovery_status('B0CC2FRY3J')['terminal'] is True


def test_retry_after_pauses_new_runs_and_other_asins():
    from datetime import datetime,timedelta,timezone
    from postgres_worker_storage import PostgresWorkerStorage
    tenant = 'recovery-' + uuid.uuid4().hex
    storage = PostgresWorkerStorage(DSN,tenant)
    storage.configure_recovery({})
    storage.initialize_manifest([{'asin':asin,'url':'https://www.amazon.com/dp/'+asin} for asin in ('B0CC2FRY3J','B0CC2JBW2H')])
    task = storage.claim_task('worker')
    due = datetime.now(timezone.utc)+timedelta(hours=2)
    storage.save_failure(task=task,reason='too_many_requests',error='too_many_requests',next_status='pending',
                         state_fields={'block_reason':'too_many_requests','next_retry_at':due.isoformat()})
    restarted = PostgresWorkerStorage(DSN,tenant)
    restarted.configure_recovery({})
    assert restarted.claim_task('different-run') is None
    assert restarted.recovery_denial_reason() == 'recovery_global_pause'
    actual = datetime.fromisoformat(restarted.recovery_status(task['asin'])['next_retry_at'])
    assert actual >= due-timedelta(seconds=1)


def test_real_python_process_restart_preserves_cooldown_without_network():
    import json
    import subprocess
    tenant = 'recovery-' + uuid.uuid4().hex
    code = '''
import json,os,sys
sys.path.insert(0,'scripts')
from postgres_worker_storage import PostgresWorkerStorage
storage=PostgresWorkerStorage(os.environ['AMAZON_TEST_POSTGRES_DSN'],sys.argv[1])
storage.configure_recovery({})
storage.initialize_manifest([{'asin':'B0CC2FRY3J','url':'https://www.amazon.com/dp/B0CC2FRY3J'}])
task=storage.claim_task('fixture-'+str(os.getpid()))
if task:
    storage.save_failure(task=task,reason='fetch_error',error='fetch_error')
print(json.dumps({'claimed':task is not None,'attempts':storage.recovery_status('B0CC2FRY3J')['attempts']}))
'''
    command = [sys.executable,'-c',code,tenant]
    first = subprocess.run(command,cwd=ROOT,capture_output=True,text=True,check=True)
    second = subprocess.run(command,cwd=ROOT,capture_output=True,text=True,check=True)
    assert json.loads(first.stdout) == {'claimed':True,'attempts':1}
    assert json.loads(second.stdout) == {'claimed':False,'attempts':1}


def test_repeated_same_asin_is_not_two_independent_global_block_samples():
    import psycopg
    from postgres_worker_storage import PostgresWorkerStorage
    from collection_storage import PostgresCollectionRepository
    tenant = 'recovery-' + uuid.uuid4().hex
    storage = PostgresWorkerStorage(DSN,tenant)
    storage.configure_recovery({})
    storage.initialize_manifest([{'asin':'B0CC2FRY3J','url':'https://www.amazon.com/dp/B0CC2FRY3J'}])
    for _ in range(2):
        task=storage.claim_task('worker')
        storage.save_failure(task=task,reason='captcha',error='captcha',next_status='blocked',state_fields={'block_reason':'captcha'})
        with psycopg.connect(DSN) as conn:
            conn.execute('UPDATE amazon_us.item_state SET next_retry_at=NULL WHERE tenant_id=%s',(tenant,))
            conn.execute('UPDATE amazon_us.recovery_job SET next_retry_at=NULL WHERE tenant_id=%s',(tenant,))
    projection=PostgresCollectionRepository(DSN,tenant_id=tenant).load_recovery_status()
    assert projection['egress'][0]['paused_until'] is None


def test_agent_refresh_view_shares_same_run_exclusions_with_actual_storage():
    import psycopg
    from postgres_worker_storage import PostgresWorkerStorage
    from agent_collection_service import RefreshOnlyStorageView
    tenant = 'recovery-' + uuid.uuid4().hex
    storage = PostgresWorkerStorage(DSN,tenant)
    storage.configure_recovery({})
    storage.initialize_manifest([{'asin':'B0CC2FRY3J','url':'https://www.amazon.com/dp/B0CC2FRY3J'}])
    job_id = uuid.uuid4().hex
    with psycopg.connect(DSN) as conn:
        conn.execute("INSERT INTO amazon_us.refresh_request(job_id,tenant_id,marketplace,asin,subject_type,requested_by,reason,status) VALUES(%s,%s,'US','B0CC2FRY3J','own','test','test','queued')",(job_id,tenant))
    view = RefreshOnlyStorageView(storage)
    view.begin_recovery_run()
    task = view.claim_refresh_task('worker')
    view.save_failure(task=task,reason='fetch_error',error='fetch_error')
    view.finish_refresh_request(job_id,'failed')
    with psycopg.connect(DSN) as conn:
        conn.execute('UPDATE amazon_us.item_state SET next_retry_at=NULL WHERE tenant_id=%s',(tenant,))
        conn.execute('UPDATE amazon_us.recovery_job SET next_retry_at=NULL WHERE tenant_id=%s',(tenant,))
    assert view.claim_refresh_task('same-run') is None
    view.begin_recovery_run()
    retry = view.claim_refresh_task('next-run')
    assert retry['recovery']['attempts'] == 2
    assert view.claim_task('ordinary-queue') is None


def test_budget_abort_keeps_unknown_bytes_and_records_failure_evidence():
    from postgres_worker_storage import PostgresWorkerStorage
    from collection_storage import PostgresCollectionRepository
    tenant='recovery-'+uuid.uuid4().hex
    storage=PostgresWorkerStorage(DSN,tenant)
    storage.configure_recovery({})
    storage.initialize_manifest([{'asin':'B0CC2FRY3J','url':'https://www.amazon.com/dp/B0CC2FRY3J'}])
    storage.claim_task('worker')
    storage.abort_recovery({'run_id':'fixture-abort','transfer_bytes':None,'source_type':'http_html'})
    api=PostgresCollectionRepository(DSN,tenant_id=tenant)
    projection=api.load_recovery_status()
    assert projection['jobs'][0]['total_bytes'] is None
    assert projection['jobs'][0]['terminal'] is True
    evidence=api.load_evidence('US','B0CC2FRY3J')
    assert evidence[0]['error_code']=='recovery_budget_or_lease_denied'
    assert evidence[0]['transfer_bytes'] is None


@pytest.mark.parametrize(('reason','next_status','expected'),[('variant_redirect','succeeded','completed'),('fetch_error','failed','queued')])
def test_refresh_status_commits_atomically_with_recovery_outcome(reason,next_status,expected):
    import psycopg
    from postgres_worker_storage import PostgresWorkerStorage
    from collection_storage import PostgresCollectionRepository
    tenant='recovery-'+uuid.uuid4().hex
    storage=PostgresWorkerStorage(DSN,tenant); storage.configure_recovery({})
    storage.initialize_manifest([{'asin':'B0CC2FRY3J','url':'https://www.amazon.com/dp/B0CC2FRY3J'}])
    job_id=uuid.uuid4().hex
    with psycopg.connect(DSN) as conn:
        conn.execute("INSERT INTO amazon_us.refresh_request(job_id,tenant_id,marketplace,asin,subject_type,requested_by,reason,status) VALUES(%s,%s,'US','B0CC2FRY3J','own','test','test','queued')",(job_id,tenant))
    task=storage.claim_refresh_task('worker')
    storage.save_failure(task=task,reason=reason,error=None if reason=='variant_redirect' else reason,
                         next_status=next_status,increment_attempts=reason!='variant_redirect')
    # Simulate process exit before the separate receipt/metrics callback.
    status=PostgresCollectionRepository(DSN,tenant_id=tenant).load_refresh_request(job_id)
    assert status['status']==expected


@pytest.mark.parametrize('delayed_callback',[False,True])
def test_real_runner_reused_slot_charges_two_distinct_postgres_task_leases(tmp_path,monkeypatch,delayed_callback):
    import io
    import socket
    import psycopg
    from amazon_us_worker import DEFAULTS,HttpFirstAdapter,run_postgres_actions,classify_block
    from proxy_session_pool import ProxySessionPool
    from proxy_canary import capacity_config_hash
    from postgres_worker_storage import PostgresWorkerStorage
    from collection_storage import PostgresCollectionRepository
    import operation_ledger
    tenant='recovery-'+uuid.uuid4().hex
    asins=['B0CC2FRY3J','B0CC2JBW2H']
    storage=PostgresWorkerStorage(DSN,tenant)
    storage.initialize_manifest([{'asin':asin,'url':'https://www.amazon.com/dp/'+asin} for asin in asins])
    config={**DEFAULTS,'max_actions_per_run':2,'context':{},'raw_html_dir':tmp_path,
            'proxy_url':'http://proxy.example:10000','proxy_session_ports':[10000,10001,10002,10003],
            'proxy_product_session_scope':'bounded','proxy_session_max_asins':2,
            'proxy_credential_generation':'fixture-generation-reused','global_requests_per_second':0,
            'egress_requests_per_second':0}
    fact={'schema_version':'amazon-us-proxy-canary-v1','canary_status':'succeeded','planned_slots':4,'tested_slots':4,'available_slots':4,
          'unique_egress_count':4,'duplicate_egress_count':0,'requested_capacity':2,'required_slots':1,
          'slot_budget':2,'slot_capacity':8,'capacity_gate_status':'allowed','capacity_gate_reason':'capacity_sufficient',
          'credential_generation':'fixture-generation-reused','p95_latency_ms':1,'config_hash':capacity_config_hash(config),
          'sessions':[{'session_id':f'session-{index:02d}','status':'available','usable':True,
                       'auth_status':'succeeded','connect_tls_status':'succeeded','latency_ms':1,
                       'http_status':200,'error_class':None} for index in range(1,5)]}
    connect=lambda:psycopg.connect(DSN)
    operation_ledger.ensure_schema(connect)
    operation='fixture-canary-'+uuid.uuid4().hex
    operation_ledger.start_operation(operation,tenant,'canary','fixture',None,connect=connect)
    operation_ledger.finish_operation(operation,tenant,'succeeded',None,None,capacity_fact=fact,connect=connect)
    class Response(io.BytesIO):
        headers={}
        def getcode(self): return 200
    saved_hooks=[]
    class OfflineTransport:
        def __init__(self,config): self.config=config
        def open(self,request,timeout):
            if not saved_hooks:
                saved_hooks.append(self.config['_recovery_before_request'])
            elif delayed_callback:
                from recovery_scheduler import RecoveryDenied
                with pytest.raises(RecoveryDenied): saved_hooks[0]()
            asin=request.full_url.rsplit('/',1)[-1]
            return Response((f'<html><link rel="canonical" href="https://www.amazon.com/dp/{asin}">'
                             f'<input id="ASIN" value="{asin}"><span id="productTitle">Fixture</span></html>').encode())
    def network_forbidden(*args,**kwargs):
        raise AssertionError('fixture attempted non-database network')
    monkeypatch.setattr(socket,'create_connection',network_forbidden)
    monkeypatch.setattr(socket,'getaddrinfo',network_forbidden)
    class OfflineHttpAdapter(HttpFirstAdapter):
        def _rebuild_opener(self): self.opener=OfflineTransport(self.config)
        def fetch_browser(self,*args,**kwargs): return network_forbidden()
    created=[]
    def factory(slot_config):
        adapter=OfflineHttpAdapter(slot_config)
        created.append(adapter)
        return adapter
    pool=ProxySessionPool(config,factory,classify_block)
    assert run_postgres_actions(storage,pool,config,limit=2,product_only=True,run_id='fixture-reused-'+uuid.uuid4().hex)==2
    assert len(created)==1
    api=PostgresCollectionRepository(DSN,tenant_id=tenant)
    used_slots=[]
    for asin in asins:
        status=storage.recovery_status(asin)
        assert status['attempts']==1 and status['request_count']==1 and status['outcome']=='completed'
        evidence=api.load_evidence('US',asin)[0]
        used_slots.append(evidence['context_json']['proxy_session_pool']['current_session_id'])
    assert len(set(used_slots))==1 and used_slots[0] is not None
