"""Release gate: exercise production batch reads against isolated persisted state.

This suite must not inherit production credentials. PostgreSQL tests accept only
the dedicated loopback cluster created for this matrix.
"""
import json
import os
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from operator_core import BatchManager, OperatorSettings
import operator_export


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding='utf-8')


@pytest.fixture
def batch(tmp_path):
    settings = OperatorSettings(ROOT, tmp_path / 'data', tmp_path / 'config.toml', tmp_path / 'unused-vault')
    manager = BatchManager(settings)
    batch_id = 'batch-' + uuid.uuid4().hex[:24]
    directory = settings.data_root / 'batches' / batch_id
    record = dict(batch_id=batch_id, tenant_id='matrix', subject_type='own',
                  status='failed', requested=1, completed=0, variant=0, failed=0,
                  blocked=0, pending=1, claimed=0, latest_error='runner_failed',
                  started_at='2026-09-07T00:00:00+00:00', stop_requested=False)
    write(directory / 'batch.json', record)
    return manager, directory, record


@pytest.mark.parametrize('terminal', ['failed', 'stopped', 'completed'])
def test_committed_batch_terminal_cannot_be_overwritten_by_projection(batch, terminal):
    manager, directory, record = batch
    record['status'] = terminal
    write(directory / 'batch.json', record)
    write(directory / 'progress.json', dict(status='completed' if terminal != 'completed' else 'failed'))
    assert manager.get_batch(record['batch_id'])['status'] == terminal


@pytest.mark.parametrize('stop_requested,exit_code', [(False,0),(False,2),(False,None),(True,130)])
def test_missing_cleanup_never_publishes_terminal_or_allows_next_batch(batch, stop_requested, exit_code):
    manager, directory, record = batch
    record.update(status='running', stop_requested=stop_requested)
    write(directory / 'batch.json', record)
    write(directory / 'control' / 'runs' / 'run-matrix' / 'capacity.authorization.json',
          dict(reservation_id='matrix-reservation', owner_id='matrix-worker'))
    write(directory / 'progress.json', dict(status='completed', completed=1, pending=0, claimed=0))
    manager._finish_record(record['batch_id'], exit_code)
    current = manager.get_batch(record['batch_id'])
    assert current['status'] == 'cleanup_pending'
    assert current['latest_error'] == 'capacity_cleanup_unconfirmed'
    assert manager._active_batch()['batch_id'] == record['batch_id']


@pytest.fixture
def database():
    import psycopg
    from psycopg.conninfo import conninfo_to_dict
    dsn = os.environ.get('OPERATOR_MATRIX_DSN', '')
    if not dsn:
        pytest.fail('matrix requires isolated PostgreSQL; a skip is not acceptance')
    info = conninfo_to_dict(dsn)
    assert info.get('host') == '127.0.0.1' and info.get('port') == '55444'
    assert info.get('dbname') == 'operator_matrix'
    with psycopg.connect(dsn) as conn:
        conn.execute((ROOT / 'schema' / 'postgres_schema.sql').read_text(encoding='utf-8'))
        conn.execute((ROOT / 'schema' / 'migrations' / '20260907_operator_batch.sql').read_text(encoding='utf-8'))
    return dsn


@pytest.mark.parametrize('size,old_count', [(1,1),(3,3),(10,10),(20,10)])
def test_historical_results_do_not_count_as_fresh_batch_evidence(batch, database, monkeypatch, size, old_count):
    import psycopg
    from postgres_worker_storage import PostgresWorkerStorage
    manager, directory, record = batch
    tenant = 'matrix-' + uuid.uuid4().hex
    asins = [f'B0MX{i:06d}' for i in range(size)]
    storage = PostgresWorkerStorage(database, tenant)
    storage.initialize_manifest([dict(asin=a, url='https://www.amazon.com/dp/'+a) for a in asins])
    with psycopg.connect(database) as conn:
        conn.execute("UPDATE amazon_us.item_state SET status='reviews_pending' WHERE tenant_id=%s AND asin=ANY(%s)", (tenant, asins[:old_count]))
    record.update(tenant_id=tenant, status='starting', requested=size, pending=size)
    write(directory / 'batch.json', record)
    manifest = directory / 'manifest.csv'
    manifest.write_text('asin\n'+'\n'.join(asins)+'\n', encoding='utf-8')
    monkeypatch.setenv('OPERATOR_MATRIX_DSN', database)
    assert operator_export.main(['status','--tenant-id',tenant,'--subject-type','own',
        '--batch-id',record['batch_id'],'--batch-dir',str(directory),'--manifest',str(manifest),
        '--dsn-env','OPERATOR_MATRIX_DSN']) == 0
    projected = json.loads((directory / 'progress.json').read_text(encoding='utf-8'))
    assert projected['completed'] == 0
    assert projected['pending'] == size
    assert all(item['status'] == 'pending' for item in projected['items'])
    assert operator_export.main(['finalize','--tenant-id',tenant,'--subject-type','own',
        '--batch-id',record['batch_id'],'--batch-dir',str(directory),'--manifest',str(manifest),
        '--dsn-env','OPERATOR_MATRIX_DSN']) == 3
    assert (directory/'results.csv').is_file()
    results=json.loads((directory/'results.json').read_text(encoding='utf-8'))
    assert all(row['status']=='pending' for row in results['items'])


@pytest.mark.parametrize('workers,size', [(w,s) for w in (1,2) for s in (1,3,10,20)])
def test_database_membership_is_idempotent_and_claims_do_not_overlap(database, workers, size):
    import psycopg
    from concurrent.futures import ThreadPoolExecutor
    from operator_batch_store import PostgresBatchStore
    tenant = 'matrix-' + uuid.uuid4().hex
    store = PostgresBatchStore(database, tenant)
    asins = [f'B0DB{i:06d}' for i in range(size)]
    batch_id = 'batch-' + uuid.uuid4().hex[:24]
    store.publish(batch_id, asins)
    store.publish(batch_id, asins)
    assert len(store.snapshot(batch_id)['members']) == size
    for worker in range(workers):
        store.register_run(batch_id, f'run-{worker}', f'worker-{worker}')
    with psycopg.connect(database) as conn:
        conn.execute("UPDATE amazon_us.operator_batch SET status='collecting' WHERE tenant_id=%s AND batch_id=%s", (tenant,batch_id))
    def consume(worker):
        claimed = []
        while (member := store.claim(batch_id, f'run-{worker}', f'worker-{worker}')) is not None:
            claimed.append(member['asin'])
        return claimed
    with ThreadPoolExecutor(max_workers=workers) as pool:
        groups = list(pool.map(consume, range(workers)))
    claimed = [asin for group in groups for asin in group]
    assert sorted(claimed) == sorted(asins)
    assert len(claimed) == len(set(claimed))
    next_id = 'batch-' + uuid.uuid4().hex[:24]
    store.publish(next_id, asins)
    assert all(m['status'] == 'pending' for m in store.snapshot(next_id)['members'])


def test_database_rejects_foreign_run_evidence_and_preserves_claim(database):
    import psycopg
    from operator_batch_store import PostgresBatchStore
    tenant = 'matrix-' + uuid.uuid4().hex
    store = PostgresBatchStore(database, tenant)
    batch_id = 'batch-' + uuid.uuid4().hex[:24]
    asin = 'B0DB000001'
    store.publish(batch_id, [asin])
    store.register_run(batch_id, 'current-run', 'worker')
    with psycopg.connect(database) as conn:
        conn.execute("UPDATE amazon_us.operator_batch SET status='collecting' WHERE tenant_id=%s AND batch_id=%s", (tenant,batch_id))
        evidence_id = conn.execute("INSERT INTO amazon_us.collection_evidence(tenant_id,marketplace,asin,subject_type,run_id,url) VALUES(%s,'US',%s,'own','old-run','https://example.invalid') RETURNING id", (tenant,asin)).fetchone()[0]
    member = store.claim(batch_id, 'current-run', 'worker')
    with pytest.raises(ValueError, match='evidence_outside_batch'):
        store.complete(batch_id, asin, member['lease_token'], evidence_id)
    assert store.snapshot(batch_id)['members'][0]['status'] == 'claimed'


def test_overlapping_active_batches_cannot_claim_same_product(database):
    import psycopg
    from operator_batch_store import PostgresBatchStore
    store = PostgresBatchStore(database, 'matrix-' + uuid.uuid4().hex)
    first, second = ['batch-' + uuid.uuid4().hex[:24] for _ in range(2)]
    for batch_id in (first, second):
        store.publish(batch_id, ['B0DB000001'])
        store.register_run(batch_id, batch_id + '-run', batch_id + '-worker')
    with psycopg.connect(database) as conn:
        conn.execute("UPDATE amazon_us.operator_batch SET status='collecting' WHERE tenant_id=%s", (store.tenant_id,))
    assert store.claim(first, first+'-run', first+'-worker') is not None
    assert store.claim(second, second+'-run', second+'-worker') is None


def test_cleanup_marker_must_cover_nested_worker_authorizations(batch):
    from operator_lifecycle import capacity_cleanup_confirmed
    _, directory, _ = batch
    for worker in (1,2):
        write(directory/'control'/'runs'/'run-parent'/f'worker-{worker}'/'capacity.authorization.json',
              {'reservation_id':f'reservation-{worker}'})
    marker = directory/'control'/'operator-capacity-cleanup.json'
    write(marker, {'schema':'amazon-operator-capacity-cleanup-v1','status':'released','reservations':['reservation-1']})
    assert not capacity_cleanup_confirmed(directory)
    write(marker, {'schema':'amazon-operator-capacity-cleanup-v1','status':'released','reservations':['reservation-1','reservation-2']})
    assert capacity_cleanup_confirmed(directory)


@pytest.mark.parametrize('stop_requested,exit_code,expected', [(False,0,'completed'),(False,2,'failed'),(True,130,'stopped')])
def test_restart_after_delayed_cleanup_preserves_original_outcome(batch, stop_requested, exit_code, expected):
    manager, directory, record = batch
    record.update(status='running', stop_requested=stop_requested)
    write(directory/'batch.json', record)
    write(directory/'control'/'runs'/'run-matrix'/'capacity.authorization.json',
          {'reservation_id':'matrix-reservation','owner_id':'matrix-worker'})
    write(directory/'progress.json', {'status':'completed','requested':1,'completed':1,'pending':0,'claimed':0})
    manager._finish_record(record['batch_id'],exit_code)
    assert manager.get_batch(record['batch_id'])['status'] == 'cleanup_pending'
    write(directory/'control'/'operator-capacity-cleanup.json',
          {'schema':'amazon-operator-capacity-cleanup-v1','status':'released','reservations':['matrix-reservation']})
    recovered = BatchManager(manager.settings)
    result = recovered.get_batch(record['batch_id'])
    assert result['status'] == expected
    assert result['latest_error'] != 'capacity_cleanup_unconfirmed'
    receipt = json.loads((directory/'receipt.json').read_text(encoding='utf-8'))
    assert receipt['runner_exit_code'] == exit_code


def test_completed_batch_is_not_rewritten_on_duplicate_finalization(batch):
    manager,directory,record = batch
    record.update(status='completed',latest_error=None,completed=1,pending=0)
    write(directory/'batch.json',record)
    before=(directory/'batch.json').read_bytes()
    manager._finish_record(record['batch_id'],None)
    assert (directory/'batch.json').read_bytes() == before
