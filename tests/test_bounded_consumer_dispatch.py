from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).parents[1]/'scripts'))
from recovery_consumer import consume_batch


def test_batch_lifetime_cannot_exceed_release_5400_second_boundary():
    import pytest
    from recovery_consumer import prepare_batch
    with pytest.raises(ValueError,match='invalid_recovery_batch_budget'):
        prepare_batch(object(),'fixture',1,['B0CC2FRY3J'],5401)


def test_one_hundred_product_cohort_never_requests_whole_cohort_capacity():
    class Database:
        done=0
        def configure_recovery(self,config): pass
        def prepare_recovery_batch(self,*args): return {'deadline':'2099-01-01T00:00:00+00:00'}
        def reclaim_expired_leases(self): pass
        def begin_recovery_pass(self): pass
        def load_latest_proxy_capacity(self,**kwargs): return {'unique_egress_count':10,'requested_capacity':5}
        def recovery_batch_progress(self,run): return {'manual_pause':False,'terminal_count':self.done,'resolved_count':self.done,
            'deadline_expired':False,'due_count':100-self.done,'wait_seconds':0,'remaining_seconds':100}
        def finish_recovery_batch(self,run,status,reason): self.status=status
    database=Database(); limits=[]
    def run_once(storage,adapter,config,**kwargs):
        limits.append(kwargs['limit']); storage.done+=kwargs['limit']; return kwargs['limit']
    result=consume_batch(database,object(),{'proxy_session_ports':list(range(10000,10050))},run_id='fixture',target=100,
                         worker_id='fixture',lease_seconds=600,max_seconds=100,reservation_id=None,run_once=run_once)
    assert result==100 and limits==[5]*20 and database.status=='completed'
