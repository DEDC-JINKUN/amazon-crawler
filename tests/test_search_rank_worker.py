from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from search_rank_worker import run_replay


class MemoryStorage:
    def __init__(self):
        self.done = set(); self.pages = []; self.finished = []; self.records = {}
    def start_batch(self, **kwargs): self.batch = kwargs
    def completed_pages(self, batch_id): return set(self.done)
    def load_pages(self, batch_id): return dict(self.records)
    def record_page(self, **kwargs):
        self.pages.append(kwargs); self.done.add(kwargs["page"]) if kwargs["status"] == "completed" else None
        evidence = f"evidence-{kwargs['page']}"
        self.records[kwargs["page"]] = {"status": kwargs["status"], "source_evidence_id": evidence, "cards_json": kwargs.get("cards") or []}
        return evidence
    def finish_batch(self, batch_id, **kwargs): self.finished.append((batch_id, kwargs))


def _html(name: str) -> str:
    return (ROOT / "tests" / "fixtures" / name).read_text(encoding="utf-8")


def test_missing_page_is_partial_not_a_claim_of_not_found():
    result = run_replay(keyword="ornament", target_asins=["B000000001", "B000000009"], page_html={1: _html("search_rank_mixed.html")}, requested_depth=2, run_id="replay-1", config={"tenant_id": "test", "marketplace": "US"}, observed_at=datetime(2026, 9, 5, tzinfo=timezone.utc))
    by_asin = {item["requested_asin"]: item for item in result["payloads"]}
    assert result["coverage_complete"] is False
    assert by_asin["B000000001"]["organic_position"] == 1
    assert by_asin["B000000009"]["organic_position"] is None
    assert by_asin["B000000009"]["coverage_complete"] is False
    assert by_asin["B000000009"]["status"] == "partial"


def test_completed_page_checkpoint_is_not_replayed_or_charged_twice():
    storage = MemoryStorage()
    result = run_replay(keyword="ornament", target_asins=["B000000001"], page_html={1: _html("search_rank_mixed.html")}, requested_depth=1, run_id="replay-2", config={}, storage=storage)
    assert result["metrics"]["search_page_bytes"] > 0
    second = run_replay(keyword="ornament", target_asins=["B000000001"], page_html={1: _html("search_rank_mixed.html")}, requested_depth=1, run_id="replay-2", config={}, storage=storage)
    assert len(storage.pages) == 1
    assert second["coverage_complete"] is True
    assert second["metrics"]["search_page_bytes"] == 0
    assert second["payloads"][0]["source_evidence_ids"] == ["evidence-1"]


def test_failed_page_is_terminal_for_the_same_batch_and_never_recharged_automatically():
    storage = MemoryStorage()
    first = run_replay(keyword="ornament", target_asins=["B000000001"], page_html={}, requested_depth=1, run_id="failed-once", config={}, storage=storage)
    second = run_replay(keyword="ornament", target_asins=["B000000001"], page_html={1: _html("search_rank_mixed.html")}, requested_depth=1, run_id="failed-once", config={}, storage=storage)
    assert first["coverage_complete"] is False and second["coverage_complete"] is False
    assert len(storage.pages) == 1
