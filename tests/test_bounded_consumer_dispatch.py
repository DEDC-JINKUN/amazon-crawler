from pathlib import Path
import sys
import pytest

sys.path.insert(0,str(Path(__file__).parents[1]/'scripts'))
from recovery_consumer import consume_batch


def test_batch_lifetime_cannot_exceed_release_5400_second_boundary():
    import pytest
    from recovery_consumer import prepare_batch
    with pytest.raises(ValueError,match='invalid_recovery_batch_budget'):
        prepare_batch(object(),'fixture',1,['B0CC2FRY3J'],5401)


@pytest.mark.parametrize('target', [50, 100])
def test_product_cohort_never_requests_whole_cohort_capacity(target):
    class Database:
        done=0
        def configure_recovery(self,config): pass
        def prepare_recovery_batch(self,*args): return {'deadline':'2099-01-01T00:00:00+00:00'}
        def reclaim_expired_leases(self): pass
        def begin_recovery_pass(self): pass
        def load_latest_proxy_capacity(self,**kwargs): return {'unique_egress_count':10,'requested_capacity':5}
        def recovery_batch_progress(self,run): return {'manual_pause':False,'terminal_count':self.done,'resolved_count':self.done,
            'deadline_expired':False,'due_count':target-self.done,'wait_seconds':0,'remaining_seconds':100}
        def finish_recovery_batch(self,run,status,reason): self.status=status
    database=Database(); limits=[]
    def run_once(storage,adapter,config,**kwargs):
        limits.append(kwargs['limit']); storage.done+=kwargs['limit']; return kwargs['limit']
    result=consume_batch(database,object(),{'proxy_session_ports':list(range(10000,10050))},run_id='fixture',target=target,
                         worker_id='fixture',lease_seconds=600,max_seconds=100,reservation_id=None,run_once=run_once)
    assert result==target and limits==[5]*(target//5) and database.status=='completed'


def test_expired_controller_reservation_ends_batch_instead_of_retrying_same_id():
    from proxy_capacity_gate import ProxyCapacityGateDenied
    class Database:
        def configure_recovery(self, config): pass
        def prepare_recovery_batch(self, *args): return {'deadline': '2099-01-01T00:00:00+00:00'}
        def validate_proxy_capacity_reservation(self, *args, **kwargs):
            return {'status': 'denied', 'reason': 'capacity_reservation_expired'}
        def reclaim_expired_leases(self): raise AssertionError('expired reservation must stop before claiming')
        def finish_recovery_batch(self, run, status, reason): self.finished=(status,reason)
    database=Database()
    with pytest.raises(ProxyCapacityGateDenied, match='capacity_reservation_expired'):
        consume_batch(database, object(), {'proxy_session_ports': [10000]}, run_id='fixture', target=1,
                      worker_id='worker-a', lease_seconds=600, max_seconds=900,
                      reservation_id='controller-reservation', run_once=lambda *args, **kwargs: 0)
    assert database.finished == ('exhausted','capacity_reservation_expired')


def test_terminal_product_failure_finishes_its_cohort_without_becoming_consumer_failure():
    class Database:
        def configure_recovery(self, config): pass
        def prepare_recovery_batch(self, *args): return {'deadline': '2099-01-01T00:00:00+00:00'}
        def reclaim_expired_leases(self): pass
        def recovery_batch_progress(self, run):
            return {'manual_pause': False, 'terminal_count': 1, 'resolved_count': 0,
                    'deadline_expired': False, 'due_count': 0, 'wait_seconds': 0, 'remaining_seconds': 10}
        def finish_recovery_batch(self, run, status, reason): self.finished=(status,reason)
    database=Database()
    assert consume_batch(database, object(), {'proxy_session_ports': [10000]}, run_id='fixture', target=1,
                         worker_id='worker-a', lease_seconds=600, max_seconds=900,
                         reservation_id=None, run_once=lambda *args, **kwargs: 0) == 1
    assert database.finished == ('exhausted','terminal_or_budget_exhausted')
