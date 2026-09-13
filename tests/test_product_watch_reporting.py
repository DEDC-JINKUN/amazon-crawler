from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from product_watch import Observation, WatchPipeline
from product_watch.delivery import LocalReceiptChannel
from product_watch.reporting import report_for_events


def _event() -> dict:
    return {
        "event_id": "evt-fixture", "event_type": "price_increase", "severity": "important", "watch_id": "watch-1",
        "old_value_json": {"amount": "10.00", "currency": "USD"}, "new_value_json": {"amount": "12.00", "currency": "USD"},
        "delta_json": {"amount": "2.00"}, "comparison_context_json": {"requested_asin": "B000000001", "currency": "USD"},
        "observed_window_json": {"before": "2026-09-05T00:00:00+00:00", "after": "2026-09-05T01:00:00+00:00"},
        "evidence_ids": ["evidence:hash-1"], "reason": "comparable currency and offer context",
    }


def test_report_is_deterministic_evidence_first_and_never_uses_raw_path():
    report = report_for_events([_event()], report_kind="immediate", timezone_name="Asia/Shanghai")
    assert report["events"][0]["subject"] == "B000000001"
    assert report["events"][0]["evidence_ids"] == ["evidence:hash-1"]
    assert "raw_html_path" not in report["content_text"]
    assert "observed window" in report["content_text"]


def test_local_receipt_channel_confirms_only_after_a_durable_receipt(tmp_path: Path):
    channel = LocalReceiptChannel(tmp_path)
    receipt = channel.send({"delivery_id": "dly-1", "dedupe_key": "dedupe-1"}, {"report_id": "rpt-1", "content_json": {"safe": True}})
    assert receipt["confirmed"] is True
    assert (tmp_path / receipt["receipt_path"]).is_file()


class _WatchRepository:
    def __init__(self): self.history = []; self.persisted = []; self.events = []
    def load_observations(self, **_kwargs): return list(self.history)
    def persist_observations(self, values): self.persisted.extend(values); self.history.extend(values); return len(values)
    def persist_events(self, values): self.events.extend(values); return len(values)


class _DeliveryRepository:
    def __init__(self): self.syncs = 0
    def sync_event_reports(self, **_kwargs): self.syncs += 1; return 1


def test_pipeline_creates_baseline_then_event_and_reconciles_crash_safe_outbox():
    watches, deliveries = _WatchRepository(), _DeliveryRepository()
    pipeline = WatchPipeline(watch_repository=watches, delivery_repository=deliveries, tenant_id="test-tenant", rules={"price_change_threshold": "0"})
    first = {"asin": "B000000001", "price": "$10.00", "currency": "USD", "specs": {}}
    second = {"asin": "B000000001", "price": "$12.00", "currency": "USD", "specs": {}}
    first_evidence = {"id": "one", "retrieved_at": datetime(2026, 9, 5, tzinfo=timezone.utc)}
    second_evidence = {"id": "two", "retrieved_at": datetime(2026, 9, 5, tzinfo=timezone.utc) + timedelta(minutes=1)}
    assert pipeline.process_snapshot(snapshot=first, evidence=first_evidence, subject_type="competitor", requested_asin="B000000001")["events"] == 0
    result = pipeline.process_snapshot(snapshot=second, evidence=second_evidence, subject_type="competitor", requested_asin="B000000001")
    assert result["events"] == 1
    assert watches.events[0].event_type == "price_increase"
    assert deliveries.syncs == 2
