import os
from pathlib import Path
import sys
import uuid

import pytest

ROOT=Path(__file__).parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
DSN=os.environ.get('AMAZON_TEST_POSTGRES_DSN')
pytestmark=pytest.mark.skipif(not DSN,reason='isolated PostgreSQL DSN required')


def test_frozen_cohort_survives_reentry_without_replacing_asins_or_extending_deadline():
    from postgres_worker_storage import PostgresWorkerStorage
    tenant='consumer-'+uuid.uuid4().hex
    storage=PostgresWorkerStorage(DSN,tenant); storage.configure_recovery({})
    asins=['B0CC2FRY3J','B0CC2JBW2H','B0CJFNJCNV']
    storage.initialize_manifest([{'asin':asin,'url':'https://www.amazon.com/dp/'+asin} for asin in asins])
    batch=storage.prepare_recovery_batch('fixture-batch',2,asins[:2],300)
    again=storage.prepare_recovery_batch('fixture-batch',2,asins[:2],1000)
    assert batch['asins']==asins[:2]
    assert again['deadline']==batch['deadline']
    with pytest.raises(ValueError,match='cohort_identity_conflict'):
        storage.prepare_recovery_batch('fixture-batch',2,asins[1:],300)
    progress=storage.recovery_batch_progress('fixture-batch')
    assert progress['requested']==2 and progress['due_count']==2 and progress['terminal_count']==0


def build_fixture(monkeypatch,tmp_path):
    import io,socket
    import psycopg
    from amazon_us_worker import DEFAULTS,HttpFirstAdapter
    from postgres_worker_storage import PostgresWorkerStorage
    from proxy_canary import capacity_config_hash
    import operation_ledger
    tenant='consumer-'+uuid.uuid4().hex
    asins=['B0CC2FRY3J','B0CC2JBW2H']
    storage=PostgresWorkerStorage(DSN,tenant)
    storage.initialize_manifest([{'asin':asin,'url':'https://www.amazon.com/dp/'+asin} for asin in asins])
    config={**DEFAULTS,'max_actions_per_run':5,'http_max_attempts':1,'http_retry_backoff_seconds':0,
            'context':{},'raw_html_dir':tmp_path,'manifest_asins':asins[:1],
            'proxy_url':'http://proxy.example:10000','proxy_session_ports':[10000,10001,10002,10003],
            'proxy_session_max_asins':1,'proxy_credential_generation':'consumer-fixture-generation',
            'global_requests_per_second':0,'egress_requests_per_second':0,'proxy_request_jitter_seconds':0}
    fact={'schema_version':'amazon-us-proxy-canary-v1','canary_status':'succeeded','planned_slots':4,'tested_slots':4,
          'available_slots':4,'unique_egress_count':4,'duplicate_egress_count':0,'requested_capacity':2,'required_slots':2,
          'slot_budget':1,'slot_capacity':4,'capacity_gate_status':'allowed','capacity_gate_reason':'capacity_sufficient',
          'credential_generation':'consumer-fixture-generation','p95_latency_ms':1,'config_hash':capacity_config_hash(config),
          'sessions':[{'session_id':f'session-{i:02d}','status':'available','usable':True,'auth_status':'succeeded',
                       'connect_tls_status':'succeeded','latency_ms':1,'http_status':200,'error_class':None} for i in range(1,5)]}
    connect=lambda:psycopg.connect(DSN)
    operation_ledger.ensure_schema(connect)
    operation='fixture-canary-'+uuid.uuid4().hex
    operation_ledger.start_operation(operation,tenant,'canary','fixture',None,connect=connect)
    operation_ledger.finish_operation(operation,tenant,'succeeded',None,None,capacity_fact=fact,connect=connect)
    calls=[]; waits=[]
    def forbidden(*args,**kwargs): raise AssertionError('non-database network forbidden')
    monkeypatch.setattr(socket,'getaddrinfo',forbidden); monkeypatch.setattr(socket,'create_connection',forbidden)
    class Response(io.BytesIO):
        headers={}
        def getcode(self): return 200
    class Transport:
        def open(self,request,timeout):
            import urllib.error
            asin=request.full_url.rsplit('/',1)[-1]; calls.append(asin)
            if len(calls)<=2: raise urllib.error.URLError('fixture transport failure')
            return Response((f'<link rel="canonical" href="https://www.amazon.com/dp/{asin}">'
                             f'<input id="ASIN" value="{asin}"><span id="productTitle">Recovered fixture</span>').encode())
    class Adapter(HttpFirstAdapter):
        def _rebuild_opener(self): self.opener=Transport()
        def fetch_browser(self,*args,**kwargs): return forbidden()
    def advance(seconds=0):
        waits.append(len(calls))
        with psycopg.connect(DSN) as conn:
            conn.execute('UPDATE amazon_us.item_state SET next_retry_at=NULL WHERE tenant_id=%s',(tenant,))
            conn.execute('UPDATE amazon_us.recovery_job SET next_retry_at=NULL WHERE tenant_id=%s',(tenant,))
            conn.execute('UPDATE amazon_us.recovery_egress SET next_request_at=NULL,paused_until=NULL WHERE tenant_id=%s',(tenant,))
    return storage,config,Adapter,calls,waits,advance


def test_live_bounded_batch_automatically_recovers_same_cohort_after_cooldown(monkeypatch,tmp_path):
    from amazon_us_worker import run_postgres_actions,classify_block
    from proxy_session_pool import ProxySessionPool
    storage,config,factory,calls,waits,advance=build_fixture(monkeypatch,tmp_path)
    config['_recovery_wait']=advance
    pool=ProxySessionPool(config,factory,classify_block)
    run_id='fixture-live-batch'
    assert run_postgres_actions(storage,pool,config,limit=1,run_id=run_id,product_only=True,
                                keep_alive=True,consumer_max_seconds=300)==1
    assert calls==['B0CC2FRY3J']*3 and waits==[2]
    status=storage.recovery_status('B0CC2FRY3J')
    assert status['attempts']==2 and status['outcome']=='completed'
    assert storage.recovery_status('B0CC2JBW2H') is None
    assert storage.recovery_batch_progress(run_id)['terminal_count']==1
    from collection_console import PostgresConsoleRepository
    result=PostgresConsoleRepository(DSN,storage.tenant_id).load_run(run_id,tmp_path)
    assert result['requested_actions']==result['recorded_actions']==1
    assert result['attempt_actions']==2 and len(result['items'])==1
    assert len(result['items'][0]['attempt_evidence'])==2
    assert result['items'][0]['content_hash'] and result['items'][0]['raw_html_path']
    assert len(result['capacity_authorizations'])==2
    summary=PostgresConsoleRepository(DSN,storage.tenant_id).list_runs(raw_html_dir=tmp_path)[0]
    assert summary['requested_actions']==summary['recorded_actions']==1 and summary['attempt_actions']==2


def test_live_agent_refresh_recovers_after_cooldown_without_requeue(monkeypatch,tmp_path):
    import time
    from agent_collection_service import AgentRefreshWorker
    from collection_storage import PostgresCollectionRepository
    from amazon_us_worker import classify_block
    from proxy_session_pool import ProxySessionPool
    storage,config,factory,calls,waits,advance=build_fixture(monkeypatch,tmp_path)
    api=PostgresCollectionRepository(DSN,tenant_id=storage.tenant_id)
    job=api.request_refresh('US','B0CC2FRY3J','fixture','bounded-recovery')
    consumer=AgentRefreshWorker(storage=storage,adapter_factory=lambda:ProxySessionPool(config,factory,classify_block),config=config,poll_seconds=0.05)
    consumer.start()
    try:
        deadline=time.monotonic()+15
        while time.monotonic()<deadline:
            status=storage.recovery_status('B0CC2FRY3J')
            if status and status['outcome']=='transport': break
            time.sleep(0.05)
        assert api.load_refresh_request(job['job_id'])['status']=='queued'
        assert calls==['B0CC2FRY3J']*2
        advance()
        while time.monotonic()<deadline and api.load_refresh_request(job['job_id'])['status']!='completed': time.sleep(0.05)
        assert api.load_refresh_request(job['job_id'])['status']=='completed'
        assert storage.recovery_status('B0CC2FRY3J')['attempts']==2
        assert calls==['B0CC2FRY3J']*3
    finally:
        consumer.stop()


def test_stale_gate_keeps_consumer_alive_without_fetch_until_new_fact(monkeypatch,tmp_path):
    import psycopg
    from amazon_us_worker import run_postgres_actions,classify_block
    from proxy_session_pool import ProxySessionPool
    storage,config,factory,calls,waits,advance=build_fixture(monkeypatch,tmp_path)
    with psycopg.connect(DSN) as conn:
        conn.execute("UPDATE amazon_us.operation_run SET finished_at=now()-INTERVAL '2 hours' WHERE tenant_id=%s AND operation_type='canary'",(storage.tenant_id,))
    observed=[]
    def wait(seconds):
        observed.append(len(calls))
        if not calls:
            with psycopg.connect(DSN) as conn:
                conn.execute("UPDATE amazon_us.operation_run SET finished_at=now() WHERE tenant_id=%s AND operation_type='canary'",(storage.tenant_id,))
        else: advance()
    config['_recovery_wait']=wait
    assert run_postgres_actions(storage,ProxySessionPool(config,factory,classify_block),config,limit=1,
                                product_only=True,keep_alive=True,consumer_max_seconds=300)==1
    assert observed[0]==0 and observed[-1]==2 and len(calls)==3


def test_batch_deadline_stops_waiting_without_resetting_task_budget(monkeypatch,tmp_path):
    import psycopg
    from amazon_us_worker import run_postgres_actions,classify_block
    from proxy_session_pool import ProxySessionPool
    storage,config,factory,calls,waits,advance=build_fixture(monkeypatch,tmp_path)
    def expire(seconds):
        with psycopg.connect(DSN) as conn:
            conn.execute("UPDATE amazon_us.recovery_batch SET deadline=now()-INTERVAL '1 second' WHERE tenant_id=%s",(storage.tenant_id,))
    config['_recovery_wait']=expire
    assert run_postgres_actions(storage,ProxySessionPool(config,factory,classify_block),config,limit=1,
                                product_only=True,keep_alive=True,consumer_max_seconds=300)==-1
    assert len(calls)==2 and storage.recovery_status('B0CC2FRY3J')['attempts']==1
