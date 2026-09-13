from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from product_watch import Observation, ProductWatchRepository


class Cursor:
    def __init__(self, responses):
        self.responses = list(responses)
        self.executed = []

    def __enter__(self): return self
    def __exit__(self, *_): return False
    def execute(self, sql, params=()): self.executed.append((sql, tuple(params)))
    def fetchone(self): return self.responses.pop(0) if self.responses else None
    def fetchall(self): return []


class Connection:
    def __init__(self, responses):
        self.cursor_value = Cursor(responses); self.commits = 0; self.rollbacks = 0
    def __enter__(self): return self
    def __exit__(self, *_): return False
    def cursor(self): return self.cursor_value
    def commit(self): self.commits += 1
    def rollback(self): self.rollbacks += 1


def observation():
    return Observation(tenant_id="tenant-a", marketplace="US", subject_type="own", requested_asin="B000000000", observed_asin="B000000000", kind="price", observed_at=datetime(2026, 9, 5, tzinfo=timezone.utc), source_evidence_ids=("hash-a",), value={"amount": "9.99", "currency": "USD"}, context={"currency": "USD"})


def test_persist_is_additive_idempotent_and_tenant_scoped():
    connection = Connection([{"observation_id": "first"}, None])
    repo = ProductWatchRepository("postgresql://unused", connect=lambda: connection)
    assert repo.persist_observations([observation(), observation()]) == 1
    assert connection.commits == 1 and connection.rollbacks == 0
    sql, params = connection.cursor_value.executed[0]
    assert "ON CONFLICT (observation_id) DO NOTHING" in sql
    assert params[1:4] == ("tenant-a", "US", "own")


def test_latest_effective_excludes_failed_refresh_but_latest_attempt_does_not():
    effective_connection = Connection([])
    repo = ProductWatchRepository("postgresql://unused", connect=lambda: effective_connection)
    identity = {"tenant_id": "tenant-a", "marketplace": "US", "subject_type": "own", "requested_asin": "B000000000", "observed_asin": "B000000000"}
    repo.load_latest_effective(identity=identity, kind="price")
    effective_sql = effective_connection.cursor_value.executed[0][0]
    assert "quality='valid'" in effective_sql and "presence IN ('observed','confirmed_absent')" in effective_sql
    attempt_connection = Connection([])
    ProductWatchRepository("postgresql://unused", connect=lambda: attempt_connection).load_latest_attempt(identity=identity, kind="price")
    attempt_sql = attempt_connection.cursor_value.executed[0][0]
    assert "quality='valid'" not in attempt_sql and "presence IN ('observed','confirmed_absent')" not in attempt_sql


def test_latest_state_marks_the_old_valid_value_stale_after_a_failed_attempt(monkeypatch):
    repo = ProductWatchRepository("postgresql://unused", connect=lambda: Connection([]))
    valid = observation()
    failed = Observation(tenant_id="tenant-a", marketplace="US", subject_type="own", requested_asin="B000000000", observed_asin="B000000000", kind="price", observed_at=datetime(2026, 9, 6, tzinfo=timezone.utc), source_evidence_ids=("hash-b",), quality="unknown", presence="not_observed")
    monkeypatch.setattr(repo, "load_latest_effective", lambda **_: valid)
    monkeypatch.setattr(repo, "load_latest_attempt", lambda **_: failed)
    state = repo.load_latest_state(identity={"tenant_id": "tenant-a", "marketplace": "US", "subject_type": "own", "requested_asin": "B000000000", "observed_asin": "B000000000"}, kind="price")
    assert state["effective"] is valid and state["latest_attempt"] is failed
    assert state["stale"] is True and state["stale_since"] == valid.observed_at.isoformat()


def test_keyword_effective_query_defaults_to_organic_and_can_select_ad_position():
    identity = {"tenant_id": "tenant-a", "marketplace": "US", "subject_type": "own", "requested_asin": "B000000000", "observed_asin": "B000000000"}
    organic_connection = Connection([])
    ProductWatchRepository("postgresql://unused", connect=lambda: organic_connection).load_latest_effective(identity=identity, kind="keyword_rank")
    organic_sql, organic_params = organic_connection.cursor_value.executed[0]
    assert "value_json ? %s" in organic_sql and organic_params[-2:] == ("organic_position", "organic_position")
    ad_connection = Connection([])
    ProductWatchRepository("postgresql://unused", connect=lambda: ad_connection).load_latest_effective(identity=identity, kind="keyword_rank", rank_field="ad")
    assert ad_connection.cursor_value.executed[0][1][-2:] == ("ad_position", "ad_position")
