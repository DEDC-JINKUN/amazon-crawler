from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import uuid

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
DSN = os.environ.get("AMAZON_TEST_POSTGRES_DSN", "")

from search_rank_storage import SearchRankStorage
from search_rank_worker import run_replay
from product_watch import ProductWatchRepository


@pytest.mark.skipif(not DSN, reason="AMAZON_TEST_POSTGRES_DSN is not configured")
def test_search_batches_and_page_checkpoints_are_additive_and_idempotent():
    import psycopg
    batch_id = "search-test-" + uuid.uuid4().hex
    with psycopg.connect(DSN) as connection:
        connection.execute((ROOT / "schema" / "postgres_schema.sql").read_text(encoding="utf-8"))
        migration = (ROOT / "schema" / "migrations" / "20260905_search_rank.sql").read_text(encoding="utf-8")
        connection.execute(migration); connection.execute(migration); connection.commit()
    storage = SearchRankStorage(DSN)
    storage.start_batch(batch_id=batch_id, run_id="search-run", tenant_id="search-integration", marketplace="US", keyword="fixture", search_conditions={"language": "en_US"}, requested_depth=1, target_asins=["B000000001"])
    first = storage.record_page(batch_id=batch_id, page=1, status="completed", url="https://example.invalid/search", http_status=200, transfer_bytes=20, raw_html_path="US/search/fake.html.gz", content_hash="a" * 64)
    second = storage.record_page(batch_id=batch_id, page=1, status="completed", url="https://example.invalid/search", http_status=200, transfer_bytes=20, raw_html_path="US/search/fake.html.gz", content_hash="a" * 64)
    assert first == second
    assert storage.completed_pages(batch_id) == {1}
    storage.finish_batch(batch_id, status="completed", metrics={"search_page_bytes": 20})


@pytest.mark.skipif(not DSN, reason="AMAZON_TEST_POSTGRES_DSN is not configured")
def test_replay_persists_search_evidence_and_detects_a_comparable_rank_change():
    import psycopg
    with psycopg.connect(DSN) as connection:
        connection.execute((ROOT / "schema" / "postgres_schema.sql").read_text(encoding="utf-8"))
        connection.execute((ROOT / "schema" / "migrations" / "20260905_product_watch.sql").read_text(encoding="utf-8"))
        connection.execute((ROOT / "schema" / "migrations" / "20260905_search_rank.sql").read_text(encoding="utf-8"))
        connection.commit()
    tenant = "search-replay-" + uuid.uuid4().hex
    config = {"tenant_id": tenant, "marketplace": "US", "language": "en_US", "rules": {"rank_change_threshold": 1}}
    storage = SearchRankStorage(DSN)
    watch = ProductWatchRepository(DSN)
    base = datetime(2026, 9, 5, tzinfo=timezone.utc)
    def card(asin: str) -> str:
        return f'<div data-component-type="s-search-result" data-asin="{asin}" class="s-result-item">result</div>'
    first = run_replay(keyword="fixture", target_asins=["B000000001"], page_html={1: card("B000000099") + card("B000000098") + card("B000000001")}, requested_depth=1, run_id="search-first", config=config, storage=storage, watch_repository=watch, observed_at=base)
    second = run_replay(keyword="fixture", target_asins=["B000000001"], page_html={1: card("B000000001")}, requested_depth=1, run_id="search-second", config=config, storage=storage, watch_repository=watch, observed_at=base + timedelta(minutes=1))
    assert first["coverage_complete"] and second["coverage_complete"]
    assert second["metrics"]["event_count"] == 1
    assert second["events"][0].event_type == "keyword_rank_improved"
