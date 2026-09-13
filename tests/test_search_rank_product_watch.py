from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from product_watch import normalize_keyword_rank
from search_rank_worker import run_replay


class WatchMemory:
    def __init__(self, history=()):
        self.history = list(history); self.saved = []; self.events = []
    def load_observations(self, **kwargs): return list(self.history)
    def persist_observations(self, values): self.saved.extend(values); return len(list(values))
    def persist_events(self, values): self.events.extend(values); return len(list(values))


def _page(asin: str) -> str:
    return f'<div data-component-type="s-search-result" data-asin="{asin}" class="s-result-item">result</div>'


def test_comparable_rank_change_emits_one_event_but_unchanged_rank_does_not():
    earlier = datetime(2026, 9, 5, tzinfo=timezone.utc)
    evidence = {"evidence_id": "prior", "retrieved_at": earlier.isoformat(), "parser_version": "search-rank-v1"}
    prior = normalize_keyword_rank({"requested_asin": "B000000001", "observed_asin": "B000000001", "keyword": "ornament", "organic_position": 3, "searched_depth": 1, "coverage_complete": True, "search_conditions": {"language": "en_US"}}, evidence, observed_at=earlier)
    changed = WatchMemory([prior])
    comparable_config = {"language": "en_US", "rules": {"rank_change_threshold": 1}}
    result = run_replay(keyword="ornament", target_asins=["B000000001"], page_html={1: _page("B000000001")}, requested_depth=1, run_id="watch-change", config=comparable_config, watch_repository=changed, observed_at=earlier + timedelta(minutes=1))
    assert result["payloads"][0]["organic_position"] == 1
    assert [event.event_type for event in changed.events] == ["keyword_rank_improved"]
    unchanged = WatchMemory([prior])
    filler = '<div data-component-type="s-search-result" data-asin="B000000099" class="s-result-item">one</div><div data-component-type="s-search-result" data-asin="B000000098" class="s-result-item">two</div>'
    run_replay(keyword="ornament", target_asins=["B000000001"], page_html={1: filler + _page("B000000001")}, requested_depth=1, run_id="watch-same", config=comparable_config, watch_repository=unchanged, observed_at=earlier + timedelta(minutes=2))
    assert unchanged.events == []
