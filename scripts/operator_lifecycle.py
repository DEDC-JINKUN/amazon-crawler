"""Batch authority shared by the Operator and its read projections.

Product history cannot commit a batch terminal. Capacity cleanup is a separate
fact and must cover every reservation, including nested two-worker runs.
"""
import json
from pathlib import Path

TERMINAL = frozenset({'completed', 'failed', 'stopped'})


def read_object(path):
    try:
        value = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def capacity_cleanup_confirmed(batch_dir):
    root = Path(batch_dir)
    ids = set()
    for path in (root / 'control' / 'runs').rglob('capacity.authorization.json'):
        value = read_object(path)
        if not value or not isinstance(value.get('reservation_id'), str):
            return False
        ids.add(value['reservation_id'])
    if not ids:
        return True
    marker = read_object(root / 'control' / 'operator-capacity-cleanup.json') or {}
    released = marker.get('reservations')
    return (marker.get('schema') == 'amazon-operator-capacity-cleanup-v1'
            and marker.get('status') == 'released'
            and isinstance(released, list) and ids.issubset(set(released)))


def batch_run_ids(batch_dir):
    """Only runs registered inside this batch, never all runs for a tenant."""
    root = Path(batch_dir) / 'control' / 'runs'
    ids = set()
    for path in root.rglob('worker.request.json'):
        request = read_object(path) or {}
        run_id = (request.get('lifecycle') or {}).get('run_id')
        if isinstance(run_id, str):
            ids.add(run_id)
    for path in root.rglob('receipt.json'):
        receipt = read_object(path) or {}
        run_id = receipt.get('run_id')
        if isinstance(run_id, str):
            ids.add(run_id)
    return sorted(ids)
