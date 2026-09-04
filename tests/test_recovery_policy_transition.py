import os
from pathlib import Path
import sys
import uuid

import pytest

sys.path.insert(0,str(Path(__file__).parents[1]/'scripts'))
DSN=os.environ.get('AMAZON_TEST_POSTGRES_DSN')
pytestmark=pytest.mark.skipif(not DSN,reason='isolated PostgreSQL DSN required')


def test_policy_transition_preserves_by_default_and_audits_explicit_reschedule():
    import psycopg
    from psycopg.rows import dict_row
    from postgres_worker_storage import PostgresWorkerStorage
    from recovery_policy_transition import transition
    tenant='transition-'+uuid.uuid4().hex; asin='B0CC2FRY3J'
    storage=PostgresWorkerStorage(DSN,tenant); storage.configure_recovery({})
    storage.initialize_manifest([{'asin':asin,'url':'https://www.amazon.com/dp/'+asin}])
    task=storage.claim_task('fixture')
    storage.save_failure(task=task,reason='captcha',error='captcha',next_status='blocked',state_fields={'block_reason':'captcha'})
    with psycopg.connect(DSN,row_factory=dict_row) as conn:
        conn.execute("UPDATE amazon_us.recovery_job SET next_retry_at=updated_at+interval '1 hour' WHERE tenant_id=%s",(tenant,))
        conn.execute("UPDATE amazon_us.item_state s SET next_retry_at=j.next_retry_at FROM amazon_us.recovery_job j WHERE s.tenant_id=j.tenant_id AND s.asin=j.asin AND s.tenant_id=%s",(tenant,))
        conn.execute("UPDATE amazon_us.recovery_egress SET paused_until=now()+interval '1 hour',outcomes=%s WHERE tenant_id=%s",(storage._jsonb([{'key':asin+'|product','blocked':True}]),tenant))
        conn.execute("INSERT INTO amazon_us.collection_evidence(tenant_id,marketplace,asin,subject_type,run_id,url,http_status,block_reason,error_code) VALUES(%s,'US',%s,'own','legacy-test',%s,200,'captcha','captcha')",(tenant,asin,task['url']))
    with psycopg.connect(DSN,row_factory=dict_row) as conn:
        original=transition(conn,tenant,[asin])
        assert original['changes']==[] and original['committed'] is False
    with psycopg.connect(DSN,row_factory=dict_row) as conn:
        with pytest.raises(ValueError,match='retry_after_attestation_required'):
            transition(conn,tenant,[asin],reschedule=True)
    with psycopg.connect(DSN,row_factory=dict_row) as conn:
        plan=transition(conn,tenant,[asin],reschedule=True,legacy_no_retry_after=True)
        assert len(plan['changes'])==2 and plan['committed'] is False
    with psycopg.connect(DSN,row_factory=dict_row) as conn:
        conn.execute('UPDATE amazon_us.recovery_egress SET manually_paused=true WHERE tenant_id=%s',(tenant,))
    with psycopg.connect(DSN,row_factory=dict_row) as conn:
        with pytest.raises(ValueError,match='active_or_manually_paused_scope'):
            transition(conn,tenant,[asin],reschedule=True,legacy_no_retry_after=True,apply=True,expected_hash=plan['plan_hash'],confirm_stopped=True)
    with psycopg.connect(DSN,row_factory=dict_row) as conn:
        conn.execute('UPDATE amazon_us.recovery_egress SET manually_paused=false WHERE tenant_id=%s',(tenant,))
    with psycopg.connect(DSN,row_factory=dict_row) as conn:
        result=transition(conn,tenant,[asin],reschedule=True,legacy_no_retry_after=True,apply=True,expected_hash=plan['plan_hash'],confirm_stopped=True)
        assert result['committed'] is True
    with psycopg.connect(DSN,row_factory=dict_row) as conn:
        row=conn.execute('SELECT attempts,request_count,deadline,next_retry_at FROM amazon_us.recovery_job WHERE tenant_id=%s',(tenant,)).fetchone()
        assert row['attempts']==1 and row['request_count']==0
        assert row['deadline'].isoformat()==plan['jobs'][0]['deadline']
        assert conn.execute('SELECT paused_until FROM amazon_us.recovery_egress WHERE tenant_id=%s',(tenant,)).fetchone()['paused_until'] is None
        assert conn.execute('SELECT COUNT(*) AS n FROM amazon_us.state_history WHERE tenant_id=%s AND reason LIKE %s',(tenant,'recovery_policy_transition:%')).fetchone()['n']==1
    with psycopg.connect(DSN,row_factory=dict_row) as conn:
        with pytest.raises(ValueError,match='plan_drift'):
            transition(conn,tenant,[asin],reschedule=True,legacy_no_retry_after=True,apply=True,expected_hash=plan['plan_hash'],confirm_stopped=True)


def test_transition_cannot_shorten_known_server_retry_after_even_with_attestation():
    import psycopg
    from psycopg.rows import dict_row
    from datetime import datetime,timedelta,timezone
    from postgres_worker_storage import PostgresWorkerStorage
    from recovery_policy_transition import transition
    tenant='transition-'+uuid.uuid4().hex; asin='B0CC2FRY3J'
    storage=PostgresWorkerStorage(DSN,tenant); storage.configure_recovery({})
    storage.initialize_manifest([{'asin':asin,'url':'https://www.amazon.com/dp/'+asin}])
    task=storage.claim_task('fixture')
    storage.save_failure(task=task,reason='captcha',error='captcha',next_status='blocked',
                         state_fields={'block_reason':'captcha','next_retry_at':(datetime.now(timezone.utc)+timedelta(hours=2)).isoformat()})
    with psycopg.connect(DSN,row_factory=dict_row) as conn:
        assert conn.execute("SELECT 'recovery_policy_transition:' LIKE 'recovery_policy:%' AS matches").fetchone()['matches'] is False
        preserved=transition(conn,tenant,[asin])
    with psycopg.connect(DSN,row_factory=dict_row) as conn:
        transition(conn,tenant,[asin],apply=True,confirm_stopped=True,expected_hash=preserved['plan_hash'])
    with psycopg.connect(DSN,row_factory=dict_row) as conn:
        with pytest.raises(ValueError,match='known_retry_after_preserved'):
            transition(conn,tenant,[asin],reschedule=True,legacy_no_retry_after=True)
