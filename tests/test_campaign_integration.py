import os,sys,uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import pytest

ROOT=Path(__file__).parents[1]; sys.path.insert(0,str(ROOT/'scripts'))
DSN=os.environ.get('AMAZON_TEST_POSTGRES_DSN')
pytestmark=pytest.mark.skipif(not DSN,reason='isolated PostgreSQL required')

def test_twenty_member_campaign_is_idempotent_and_dynamically_claimed_by_workers():
    import psycopg
    from postgres_worker_storage import PostgresWorkerStorage
    from collection_storage import PostgresCollectionRepository
    tenant='campaign-'+uuid.uuid4().hex
    with psycopg.connect(DSN) as conn:
        conn.execute((ROOT/'schema/postgres_schema.sql').read_text(encoding='utf-8'))
        conn.execute((ROOT/'schema/migrations/20260904_recovery.sql').read_text(encoding='utf-8'))
        conn.execute((ROOT/'schema/migrations/20260906_campaign.sql').read_text(encoding='utf-8'))
    asins=[f'B0CMP{i:05d}' for i in range(20)]
    storage=PostgresWorkerStorage(DSN,tenant); storage.initialize_manifest([{'asin':asin,'url':'https://www.amazon.com/dp/'+asin} for asin in asins]); storage.configure_recovery({})
    repository=PostgresCollectionRepository(DSN,tenant_id=tenant)
    first=repository.submit_campaign(asins=asins,idempotency_key='idem-campaign-0001',requested_by='refresh-agent',max_members=1093)
    again=repository.submit_campaign(asins=asins,idempotency_key='idem-campaign-0001',requested_by='refresh-agent',max_members=1093)
    assert first['campaign_id']==again['campaign_id'] and first['total']==20 and first['counts']['pending']==20
    worker1=PostgresWorkerStorage(DSN,tenant); worker1.configure_recovery({})
    worker2=PostgresWorkerStorage(DSN,tenant); worker2.configure_recovery({})
    claims=[worker1.claim_refresh_task('worker-1'),worker2.claim_refresh_task('worker-2')]
    assert all(claims) and claims[0]['asin']!=claims[1]['asin']
    # Complete both leases atomically enough for queue visibility; product data
    # is irrelevant to this scheduler-only fixture.
    with psycopg.connect(DSN) as conn:
        for task in claims:
            conn.execute("UPDATE amazon_us.item_state SET status='succeeded',lease_token=NULL,lease_owner=NULL,lease_expires_at=NULL WHERE tenant_id=%s AND asin=%s",(tenant,task['asin']))
            conn.execute("UPDATE amazon_us.recovery_job SET terminal=true,outcome='completed',lease_token=NULL WHERE tenant_id=%s AND asin=%s",(tenant,task['asin']))
            conn.execute("UPDATE amazon_us.refresh_request SET status='completed',completed_at=now() WHERE tenant_id=%s AND job_id=%s",(tenant,task['job_id']))
    fast=[worker1.claim_refresh_task('worker-1'),worker1.claim_refresh_task('worker-1')]
    assert all(fast) and len({task['asin'] for task in claims+fast})==4
    deferred=fast[0]
    with psycopg.connect(DSN) as conn:
        conn.execute("UPDATE amazon_us.item_state SET status='pending',next_retry_at=now()+interval '1 hour',lease_token=NULL,lease_owner=NULL,lease_expires_at=NULL WHERE tenant_id=%s AND asin=%s",(tenant,deferred['asin']))
        conn.execute("UPDATE amazon_us.recovery_job SET next_retry_at=now()+interval '1 hour',lease_token=NULL WHERE tenant_id=%s AND asin=%s",(tenant,deferred['asin']))
        conn.execute("UPDATE amazon_us.refresh_request SET status='queued',claimed_at=NULL WHERE tenant_id=%s AND job_id=%s",(tenant,deferred['job_id']))
    next_task=worker2.claim_refresh_task('worker-2')
    assert next_task is not None and next_task['asin']!=deferred['asin']
    progress=repository.load_campaign(first['campaign_id'])
    assert progress['counts']['succeeded']==2 and progress['counts']['deferred']==1 and progress['counts']['running']==2 and progress['total']==20

def test_campaign_quota_registration_and_idempotency_conflict_fail_closed():
    import psycopg
    from postgres_worker_storage import PostgresWorkerStorage
    from collection_storage import PostgresCollectionRepository
    tenant='campaign-'+uuid.uuid4().hex; repository=PostgresCollectionRepository(DSN,tenant_id=tenant)
    with psycopg.connect(DSN) as conn:
        conn.execute((ROOT/'schema/postgres_schema.sql').read_text(encoding='utf-8'))
        conn.execute((ROOT/'schema/migrations/20260904_recovery.sql').read_text(encoding='utf-8'))
        conn.execute((ROOT/'schema/migrations/20260906_campaign.sql').read_text(encoding='utf-8'))
    storage=PostgresWorkerStorage(DSN,tenant); storage.initialize_manifest([{'asin':'B0CMP00000','url':'https://www.amazon.com/dp/B0CMP00000'}])
    with pytest.raises(KeyError): repository.submit_campaign(asins=['B0CMP99999'],idempotency_key='idem-campaign-0002',requested_by='refresh-agent')
    first=repository.submit_campaign(asins=['B0CMP00000'],idempotency_key='idem-campaign-0003',requested_by='refresh-agent')
    with pytest.raises(ValueError,match='conflicts'): repository.submit_campaign(asins=['B0CMP00000'],idempotency_key='idem-campaign-0003',requested_by='refresh-agent',priority=99)
    assert first['total']==1


def test_campaign_after_success_starts_a_new_logical_recovery_budget():
    import psycopg
    from collection_storage import PostgresCollectionRepository
    from postgres_worker_storage import PostgresWorkerStorage

    tenant='campaign-reset-'+uuid.uuid4().hex; asin='B0CMP00060'
    try:
        storage=PostgresWorkerStorage(DSN,tenant); storage.initialize_manifest([{'asin':asin,'url':'https://www.amazon.com/dp/'+asin}]); storage.configure_recovery({})
        assert storage.claim_task('campaign-reset-prime') is not None
        with psycopg.connect(DSN) as conn:
            conn.execute("UPDATE amazon_us.recovery_job SET terminal=true,outcome='completed',attempts=2,request_count=4,browser_request_count=7,known_bytes=1234,next_retry_at=now()+interval '1 day',lease_token=NULL WHERE tenant_id=%s AND asin=%s AND stage='product'",(tenant,asin))
            conn.execute("UPDATE amazon_us.item_state SET status='reviews_pending',next_retry_at=now()+interval '1 day',lease_token=NULL,lease_owner=NULL,lease_expires_at=NULL WHERE tenant_id=%s AND asin=%s",(tenant,asin))

        repository=PostgresCollectionRepository(DSN,tenant_id=tenant)
        campaign=repository.submit_campaign(asins=[asin],idempotency_key='idem-campaign-reset-success',requested_by='refresh-agent')
        with psycopg.connect(DSN) as conn:
            reset=conn.execute("SELECT terminal,outcome,attempts,request_count,browser_request_count,known_bytes,next_retry_at FROM amazon_us.recovery_job WHERE tenant_id=%s AND asin=%s AND stage='product'",(tenant,asin)).fetchone()
        assert reset == (False,'new',0,0,0,0,None)
        claimed=storage.claim_refresh_task('campaign-reset-worker')
        assert claimed is not None and claimed['job_id']==campaign['members'][0]['refresh_job_id']
    finally:
        with psycopg.connect(DSN) as conn:
            for table in ('collection_campaign_member','collection_campaign','refresh_request','recovery_job','recovery_egress','state_history','item_state','asin_master'):
                conn.execute(f'DELETE FROM amazon_us.{table} WHERE tenant_id=%s',(tenant,))


def test_campaign_does_not_reset_access_control_terminal_budget():
    import psycopg
    from collection_storage import PostgresCollectionRepository
    from postgres_worker_storage import PostgresWorkerStorage

    tenant='campaign-no-reset-'+uuid.uuid4().hex; asin='B0CMP00061'
    try:
        storage=PostgresWorkerStorage(DSN,tenant); storage.initialize_manifest([{'asin':asin,'url':'https://www.amazon.com/dp/'+asin}]); storage.configure_recovery({})
        assert storage.claim_task('campaign-no-reset-prime') is not None
        with psycopg.connect(DSN) as conn:
            conn.execute("UPDATE amazon_us.recovery_job SET terminal=true,outcome='access_control',attempts=max_attempts,request_count=5,browser_request_count=20,lease_token=NULL WHERE tenant_id=%s AND asin=%s AND stage='product'",(tenant,asin))
            conn.execute("UPDATE amazon_us.item_state SET status='blocked',block_reason='captcha',lease_token=NULL,lease_owner=NULL,lease_expires_at=NULL WHERE tenant_id=%s AND asin=%s",(tenant,asin))

        repository=PostgresCollectionRepository(DSN,tenant_id=tenant)
        campaign=repository.submit_campaign(asins=[asin],idempotency_key='idem-campaign-no-reset-access',requested_by='refresh-agent')
        with psycopg.connect(DSN) as conn:
            recovery=conn.execute("SELECT terminal,outcome,attempts,max_attempts FROM amazon_us.recovery_job WHERE tenant_id=%s AND asin=%s AND stage='product'",(tenant,asin)).fetchone()
        assert recovery == (True,'access_control',3,3)
        assert storage.claim_refresh_task('campaign-no-reset-worker') is None
        assert campaign['members'][0]['job_status']=='queued'
    finally:
        with psycopg.connect(DSN) as conn:
            for table in ('collection_campaign_member','collection_campaign','refresh_request','recovery_job','recovery_egress','state_history','item_state','asin_master'):
                conn.execute(f'DELETE FROM amazon_us.{table} WHERE tenant_id=%s',(tenant,))


def test_database_recovery_requeues_owned_refresh_without_resetting_budget():
    import psycopg
    from collection_storage import PostgresCollectionRepository
    from postgres_worker_storage import PostgresWorkerStorage

    tenant='db-requeue-'+uuid.uuid4().hex; asin='B0DBR00001'
    try:
        storage=PostgresWorkerStorage(DSN,tenant); storage.initialize_manifest([{'asin':asin,'url':'https://www.amazon.com/dp/'+asin}]); storage.configure_recovery({})
        job=PostgresCollectionRepository(DSN,tenant_id=tenant).request_refresh('US',asin,'refresh-agent','db-recovery-test')
        first=storage.claim_refresh_task('agent-refresh-requeue')
        assert first is not None and first['job_id']==job['job_id']
        with psycopg.connect(DSN) as conn:
            before=conn.execute("SELECT attempts,request_count,browser_request_count FROM amazon_us.recovery_job WHERE tenant_id=%s AND asin=%s AND stage='product'",(tenant,asin)).fetchone()
        assert storage.requeue_claimed_refreshes('agent-refresh-requeue')==1
        with psycopg.connect(DSN) as conn:
            after=conn.execute("SELECT j.attempts,j.request_count,j.browser_request_count,j.lease_token,r.status,s.status FROM amazon_us.recovery_job j JOIN amazon_us.refresh_request r ON r.tenant_id=j.tenant_id AND r.asin=j.asin AND r.subject_type=j.subject_type JOIN amazon_us.item_state s ON s.tenant_id=j.tenant_id AND s.asin=j.asin AND s.subject_type=j.subject_type WHERE j.tenant_id=%s AND j.asin=%s AND j.stage='product'",(tenant,asin)).fetchone()
        assert after[:3]==before and after[3:] == (None,'queued','pending')
        second=storage.claim_refresh_task('agent-refresh-requeue')
        assert second is not None and second['job_id']==job['job_id']
    finally:
        with psycopg.connect(DSN) as conn:
            for table in ('refresh_request','recovery_job','recovery_egress','state_history','item_state','asin_master'):
                conn.execute(f'DELETE FROM amazon_us.{table} WHERE tenant_id=%s',(tenant,))


def test_concurrent_idempotency_is_atomic_for_same_and_different_payloads():
    import psycopg
    from collection_storage import CampaignConflictError, PostgresCollectionRepository
    from postgres_worker_storage import PostgresWorkerStorage
    tenant='campaign-'+uuid.uuid4().hex
    with psycopg.connect(DSN) as conn:
        conn.execute((ROOT/'schema/postgres_schema.sql').read_text(encoding='utf-8'))
        conn.execute((ROOT/'schema/migrations/20260904_recovery.sql').read_text(encoding='utf-8'))
        conn.execute((ROOT/'schema/migrations/20260906_campaign.sql').read_text(encoding='utf-8'))
    asins=['B0CMP00010','B0CMP00011']
    PostgresWorkerStorage(DSN,tenant).initialize_manifest([{'asin':asin,'url':'https://www.amazon.com/dp/'+asin} for asin in asins])
    def submit(priority=50):
        return PostgresCollectionRepository(DSN,tenant_id=tenant).submit_campaign(
            asins=asins,idempotency_key='idem-concurrent-same',requested_by='agent',priority=priority)
    with ThreadPoolExecutor(max_workers=2) as pool:
        same=list(pool.map(lambda _value: submit(),range(2)))
    assert same[0]['campaign_id']==same[1]['campaign_id']
    with psycopg.connect(DSN) as conn:
        assert conn.execute("SELECT count(*) FROM amazon_us.collection_campaign WHERE tenant_id=%s AND idempotency_key=%s",(tenant,'idem-concurrent-same')).fetchone()[0]==1
        assert conn.execute("SELECT count(*) FROM amazon_us.collection_campaign_member WHERE tenant_id=%s AND campaign_id=%s",(tenant,same[0]['campaign_id'])).fetchone()[0]==2
    def conflicting(priority):
        try:
            value=PostgresCollectionRepository(DSN,tenant_id=tenant).submit_campaign(
                asins=asins,idempotency_key='idem-concurrent-conflict',requested_by='agent',priority=priority)
            return ('ok',value['campaign_id'])
        except CampaignConflictError:
            return ('conflict',None)
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes=list(pool.map(conflicting,(20,80)))
    assert sorted(value[0] for value in outcomes)==['conflict','ok']


def test_expiry_and_cancel_prevent_future_claims_but_leave_running_member_owned():
    import psycopg
    from collection_storage import PostgresCollectionRepository
    from postgres_worker_storage import PostgresWorkerStorage
    tenant='campaign-'+uuid.uuid4().hex
    with psycopg.connect(DSN) as conn:
        conn.execute((ROOT/'schema/postgres_schema.sql').read_text(encoding='utf-8'))
        conn.execute((ROOT/'schema/migrations/20260904_recovery.sql').read_text(encoding='utf-8'))
        conn.execute((ROOT/'schema/migrations/20260906_campaign.sql').read_text(encoding='utf-8'))
    asins=['B0CMP00020','B0CMP00021','B0CMP00022']
    storage=PostgresWorkerStorage(DSN,tenant); storage.initialize_manifest([{'asin':asin,'url':'https://www.amazon.com/dp/'+asin} for asin in asins]); storage.configure_recovery({})
    repository=PostgresCollectionRepository(DSN,tenant_id=tenant)
    expired=repository.submit_campaign(asins=[asins[0]],idempotency_key='idem-expired',requested_by='agent')
    with psycopg.connect(DSN) as conn:
        conn.execute("UPDATE amazon_us.collection_campaign SET deadline=now()-interval '1 second' WHERE tenant_id=%s AND campaign_id=%s",(tenant,expired['campaign_id']))
    assert storage.claim_refresh_task('worker-expired') is None
    expired_status=repository.load_campaign(expired['campaign_id'])
    assert expired_status['status']=='expired' and expired_status['counts']['cancelled']==1
    active=repository.submit_campaign(asins=asins[1:],idempotency_key='idem-cancel',requested_by='agent')
    running=storage.claim_refresh_task('worker-running')
    assert running is not None and running['asin'] in asins[1:]
    first=repository.cancel_campaign(active['campaign_id']); second=repository.cancel_campaign(active['campaign_id'])
    assert first['status']==second['status']=='cancelled'
    assert first['counts']['running']==1 and first['counts']['cancelled']==1
    assert storage.claim_refresh_task('worker-after-cancel') is None


def test_campaign_subject_scope_is_explicit_and_claimable():
    import psycopg
    from collection_storage import PostgresCollectionRepository
    from postgres_worker_storage import PostgresWorkerStorage
    tenant='campaign-'+uuid.uuid4().hex; asin='B0CMP00030'
    with psycopg.connect(DSN) as conn:
        conn.execute((ROOT/'schema/postgres_schema.sql').read_text(encoding='utf-8'))
        conn.execute((ROOT/'schema/migrations/20260904_recovery.sql').read_text(encoding='utf-8'))
        conn.execute((ROOT/'schema/migrations/20260906_campaign.sql').read_text(encoding='utf-8'))
    storage=PostgresWorkerStorage(DSN,tenant,subject_type='competitor'); storage.initialize_manifest([{'asin':asin,'url':'https://www.amazon.com/dp/'+asin}]); storage.configure_recovery({})
    repository=PostgresCollectionRepository(DSN,tenant_id=tenant,subject_type='competitor')
    campaign=repository.submit_campaign(asins=[asin],idempotency_key='idem-subject',requested_by='agent',subject_type='competitor')
    assert campaign['subject_type']=='competitor'
    task=storage.claim_refresh_task('competitor-worker')
    assert task is not None and task['asin']==asin and task['subject_type']=='competitor'
    with pytest.raises(ValueError,match='worker scope'):
        repository.submit_campaign(asins=[asin],idempotency_key='idem-wrong-subject',requested_by='agent',subject_type='own')


def test_cancelled_campaign_does_not_cancel_or_hide_preexisting_atomic_refresh():
    import psycopg
    from collection_storage import PostgresCollectionRepository
    from postgres_worker_storage import PostgresWorkerStorage
    tenant='campaign-'+uuid.uuid4().hex; asin='B0CMP00040'
    with psycopg.connect(DSN) as conn:
        conn.execute((ROOT/'schema/postgres_schema.sql').read_text(encoding='utf-8'))
        conn.execute((ROOT/'schema/migrations/20260904_recovery.sql').read_text(encoding='utf-8'))
        conn.execute((ROOT/'schema/migrations/20260906_campaign.sql').read_text(encoding='utf-8'))
    storage=PostgresWorkerStorage(DSN,tenant); storage.initialize_manifest([{'asin':asin,'url':'https://www.amazon.com/dp/'+asin}]); storage.configure_recovery({})
    repository=PostgresCollectionRepository(DSN,tenant_id=tenant)
    atomic=repository.request_refresh('US',asin,'agent','on_demand')
    campaign=repository.submit_campaign(asins=[asin],idempotency_key='idem-shared-atomic',requested_by='agent')
    assert campaign['members'][0]['refresh_job_id']==atomic['job_id']
    cancelled=repository.cancel_campaign(campaign['campaign_id'])
    assert cancelled['status']=='cancelled'
    with psycopg.connect(DSN) as conn:
        assert conn.execute("SELECT status FROM amazon_us.refresh_request WHERE tenant_id=%s AND job_id=%s",(tenant,atomic['job_id'])).fetchone()[0]=='queued'
    task=storage.claim_refresh_task('atomic-worker')
    assert task is not None and task['job_id']==atomic['job_id']


def test_atomic_refresh_added_after_campaign_survives_campaign_cancel():
    import psycopg
    from collection_storage import PostgresCollectionRepository
    from postgres_worker_storage import PostgresWorkerStorage
    tenant='campaign-'+uuid.uuid4().hex; asin='B0CMP00041'
    with psycopg.connect(DSN) as conn:
        conn.execute((ROOT/'schema/postgres_schema.sql').read_text(encoding='utf-8'))
        conn.execute((ROOT/'schema/migrations/20260904_recovery.sql').read_text(encoding='utf-8'))
        conn.execute((ROOT/'schema/migrations/20260906_campaign.sql').read_text(encoding='utf-8'))
    storage=PostgresWorkerStorage(DSN,tenant); storage.initialize_manifest([{'asin':asin,'url':'https://www.amazon.com/dp/'+asin}]); storage.configure_recovery({})
    repository=PostgresCollectionRepository(DSN,tenant_id=tenant)
    campaign=repository.submit_campaign(asins=[asin],idempotency_key='idem-campaign-first',requested_by='campaign-agent')
    direct=repository.request_refresh('US',asin,'direct-agent','on_demand')
    assert direct['job_id']==campaign['members'][0]['refresh_job_id']
    assert direct['reason']=='direct:on_demand'
    cancelled=repository.cancel_campaign(campaign['campaign_id'])
    assert cancelled['status']=='cancelled' and cancelled['counts']['pending']==1
    with psycopg.connect(DSN) as conn:
        assert conn.execute("SELECT status FROM amazon_us.refresh_request WHERE tenant_id=%s AND job_id=%s",(tenant,direct['job_id'])).fetchone()[0]=='queued'
    task=storage.claim_refresh_task('direct-worker')
    assert task is not None and task['job_id']==direct['job_id']
    with psycopg.connect(DSN) as conn:
        conn.execute("UPDATE amazon_us.item_state SET status='succeeded',lease_token=NULL,lease_owner=NULL,lease_expires_at=NULL WHERE tenant_id=%s AND asin=%s",(tenant,asin))
        conn.execute("UPDATE amazon_us.recovery_job SET terminal=true,outcome='completed',lease_token=NULL WHERE tenant_id=%s AND asin=%s",(tenant,asin))
        conn.execute("UPDATE amazon_us.refresh_request SET status='completed',completed_at=now() WHERE tenant_id=%s AND job_id=%s",(tenant,direct['job_id']))
    final=repository.load_campaign(campaign['campaign_id'])
    assert final['status']=='cancelled' and final['counts']['succeeded']==1
