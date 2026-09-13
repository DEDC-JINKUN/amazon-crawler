"""Persistence contract tests for the isolated operator batch coordinator."""
import os
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))


@pytest.fixture
def database():
    import psycopg
    from psycopg.conninfo import conninfo_to_dict

    dsn = os.environ.get(
        'OPERATOR_MATRIX_DSN',
        'host=127.0.0.1 port=55444 dbname=operator_matrix user=matrix_owner',
    )
    info = conninfo_to_dict(dsn)
    if info.get('host') != '127.0.0.1' or info.get('port') != '55444' or info.get('dbname') != 'operator_matrix':
        pytest.fail('operator batch tests require the dedicated loopback database')
    with psycopg.connect(dsn) as conn:
        conn.execute((ROOT / 'schema' / 'postgres_schema.sql').read_text(encoding='utf-8'))
        conn.execute((ROOT / 'schema' / 'migrations' / '20260907_operator_batch.sql').read_text(encoding='utf-8'))
    return dsn


def new_store(database):
    from operator_batch_store import PostgresBatchStore

    return PostgresBatchStore(database, 'store-v2-' + uuid.uuid4().hex)


def test_stage_stop_cleanup_and_snapshot_are_persisted(database):
    store = new_store(database)
    batch_id = 'batch-' + uuid.uuid4().hex[:24]
    asin = 'B0V2000001'
    store.publish(batch_id, [asin])
    store.set_stage(batch_id, 'canary')
    store.set_stage(batch_id, 'collecting')
    store.register_run(batch_id, 'run-1', 'worker-1')
    with __import__('psycopg').connect(database) as conn:
        conn.execute(
            "UPDATE amazon_us.operator_batch SET status='collecting' WHERE tenant_id=%s AND batch_id=%s",
            (store.tenant_id, batch_id),
        )
    member = store.claim(batch_id, 'run-1', 'worker-1')
    store.request_stop(batch_id)
    assert store.stop_requested(batch_id)
    store.finish_member(batch_id, asin, member['lease_token'], 'stopped', 'worker token=secret')
    result = store.finish(batch_id, 'stopped', 'operator stopped')
    assert result['status'] == 'stopped'
    snapshot = store.snapshot(batch_id)
    assert snapshot['phase'] == 'stopped'
    assert snapshot['counts']['stopped'] == 1
    assert snapshot['stopped'] == 1
    assert 'secret' not in (snapshot['last_error'] or '')


def test_finish_rejects_claims_and_completed_requires_all_members(database):
    store = new_store(database)
    batch_id = 'batch-' + uuid.uuid4().hex[:24]
    asins = ['B0V2000002', 'B0V2000003']
    store.publish(batch_id, asins)
    store.register_run(batch_id, 'run-2', 'worker-2')
    with __import__('psycopg').connect(database) as conn:
        conn.execute(
            "UPDATE amazon_us.operator_batch SET status='collecting' WHERE tenant_id=%s AND batch_id=%s",
            (store.tenant_id, batch_id),
        )
    member = store.claim(batch_id, 'run-2', 'worker-2')
    with pytest.raises(ValueError, match='claimed_members_not_cleaned'):
        store.finish(batch_id, 'failed')
    store.release_members(batch_id)
    with pytest.raises(ValueError, match='batch_members_incomplete'):
        store.finish(batch_id, 'completed')
    assert store.finish(batch_id, 'failed', 'cleanup complete')['status'] == 'failed'
    assert member['asin'] == asins[0]


def test_register_run_is_idempotent_but_conflicts_are_rejected(database):
    store = new_store(database)
    batch_id = 'batch-' + uuid.uuid4().hex[:24]
    other_batch = 'batch-' + uuid.uuid4().hex[:24]
    store.publish(batch_id, ['B0V2000004'])
    store.publish(other_batch, ['B0V2000005'])
    assert store.register_run(batch_id, 'run-idempotent', 'worker-a') == 'run-idempotent'
    assert store.register_run(batch_id, 'run-idempotent', 'worker-a') == 'run-idempotent'
    with pytest.raises(ValueError, match='batch_run_identity_conflict'):
        store.register_run(other_batch, 'run-idempotent', 'worker-a')
    with pytest.raises(ValueError, match='batch_run_identity_conflict'):
        store.register_run(batch_id, 'run-idempotent', 'worker-b')
