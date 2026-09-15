from __future__ import annotations

import csv
import json
import sys
import threading
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


def write_manifest(path: Path, asins: list[str]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["asin"])
        writer.writeheader()
        for asin in asins:
            writer.writerow({"asin": asin})


class CapacityLimitedClient:
    def __init__(self):
        self.submissions = 0

    def get_readiness(self):
        return {
            "tenant_id": "owned-products",
            "subject_type": "own",
            "refresh_worker": {
                "state": "running",
                "requested_workers": 2,
                "effective_workers": 1,
                "alive_workers": 1,
                "capacity_backpressure": True,
            },
        }

    def submit_campaign(self, *_args, **_kwargs):
        self.submissions += 1
        raise AssertionError("capacity failure must happen before campaign submission")


class CompletingCampaignClient:
    def __init__(self):
        self.submissions = []
        self.polls = 0
        self.campaign_id = "campaign-" + "2" * 32

    def get_readiness(self):
        return {
            "tenant_id": "owned-products",
            "subject_type": "own",
            "refresh_worker": {
                "state": "running",
                "requested_workers": 2,
                "effective_workers": 2,
                "alive_workers": 2,
                "capacity_backpressure": False,
            },
        }

    def submit_campaign(self, asins, idempotency_key, **options):
        self.submissions.append((list(asins), idempotency_key, dict(options)))
        return {"campaign": self._campaign("active", ["pending", "pending", "pending"])}

    def get_campaign(self, campaign_id):
        assert campaign_id == self.campaign_id
        self.polls += 1
        if self.polls == 1:
            return {"campaign": self._campaign("active", ["running", "pending", "pending"])}
        return {"campaign": self._campaign("completed", ["succeeded", "variant", "failed"])}

    def _campaign(self, status, member_statuses):
        asins = ["B0TEST0001", "B0TEST0002", "B0TEST0003"]
        terminal = sum(value in {"succeeded", "variant", "failed", "cancelled"} for value in member_statuses)
        counts = {name: member_statuses.count(name) for name in (
            "pending", "running", "deferred", "succeeded", "variant", "failed", "cancelled"
        )}
        return {
            "campaign_id": self.campaign_id,
            "status": status,
            "total": len(asins),
            "terminal": terminal,
            "counts": counts,
            "members": [
                {"asin": asin, "status": member_statuses[index], "refresh_job_id": f"job-{index}"}
                for index, asin in enumerate(asins)
            ],
        }


class CancelledCampaignClient(CompletingCampaignClient):
    def __init__(self):
        super().__init__()
        self.cancelled = 0

    def get_campaign(self, campaign_id):
        assert campaign_id == self.campaign_id
        self.polls += 1
        return {"campaign": self._campaign("active", ["running", "pending", "pending"])}

    def cancel_campaign(self, campaign_id):
        assert campaign_id == self.campaign_id
        self.cancelled += 1
        return {"campaign": self._campaign("cancelled", ["cancelled", "cancelled", "cancelled"])}


class ExpiredCampaignClient(CompletingCampaignClient):
    def get_campaign(self, campaign_id):
        assert campaign_id == self.campaign_id
        return {"campaign": self._campaign("expired", ["cancelled", "cancelled", "cancelled"])}


class FailingPollCampaignClient(CompletingCampaignClient):
    def __init__(self):
        super().__init__()
        self.cancelled = 0

    def get_campaign(self, campaign_id):
        assert campaign_id == self.campaign_id
        self.polls += 1
        raise OSError("loopback unavailable with secret detail")

    def cancel_campaign(self, campaign_id):
        assert campaign_id == self.campaign_id
        self.cancelled += 1
        return {"campaign": self._campaign("cancelled", ["cancelled", "cancelled", "cancelled"])}


class BlockedCampaignClient(CompletingCampaignClient):
    def get_campaign(self, campaign_id):
        assert campaign_id == self.campaign_id
        campaign = self._campaign("completed", ["succeeded", "variant", "failed"])
        campaign["members"][2]["item_status"] = "blocked"
        return {"campaign": campaign}


class SubjectMismatchClient(CompletingCampaignClient):
    def get_readiness(self):
        readiness = super().get_readiness()
        readiness["subject_type"] = "competitor"
        return readiness


class CompletedBeforeStopClient(CompletingCampaignClient):
    def get_campaign(self, campaign_id):
        assert campaign_id == self.campaign_id
        return {"campaign": self._campaign("completed", ["succeeded", "variant", "succeeded"])}
    def cancel_campaign(self, _campaign_id):
        raise AssertionError("terminal campaign must not be cancelled")


class CompletedDuringCancelClient(CancelledCampaignClient):
    def cancel_campaign(self, campaign_id):
        assert campaign_id == self.campaign_id
        self.cancelled += 1
        return {"campaign": self._campaign("completed", ["succeeded", "variant", "succeeded"])}


class TerminalMemberFailureClient(CompletingCampaignClient):
    def __init__(self):
        super().__init__()
        self.member_calls = 0

    def get_campaign_summary(self, campaign_id):
        assert campaign_id == self.campaign_id
        campaign = self._campaign("completed", ["succeeded", "variant", "failed"])
        campaign.pop("members")
        return {"campaign": campaign}

    def get_campaign_with_members(self, campaign_id, *, summary=None):
        assert campaign_id == self.campaign_id
        assert summary and "members" not in summary
        self.member_calls += 1
        raise OSError("terminal member page unavailable with secret detail")


class RetryingCancelCampaignClient(CancelledCampaignClient):
    def cancel_campaign(self, campaign_id):
        assert campaign_id == self.campaign_id
        self.cancelled += 1
        if self.cancelled < 3:
            raise OSError("temporary cancel failure")
        return {"campaign": self._campaign("cancelled", ["cancelled", "cancelled", "cancelled"])}


class HttpCampaignRepository:
    tenant_id = "owned-products"
    subject_type = "own"

    def __init__(self):
        self.campaign_id = "campaign-" + "8" * 32
        self.load_views = []
        self.summary_polls = 0

    def load_schema_contract(self):
        return {
            "item_state": ["lease_expires_at", "lease_owner", "lease_token", "next_retry_at"],
            "collection_evidence": ["context_json", "transfer_bytes"],
        }

    def load_campaign_contract(self):
        return True

    def load_job_status(self):
        return {"counts": {}}

    def record_api_audit(self, **_event):
        return None

    def submit_campaign(self, *, asins, **_options):
        return self._campaign("active", asins, ["pending"] * len(asins))

    def load_campaign(self, campaign_id, **member_view):
        assert campaign_id == self.campaign_id
        self.load_views.append(dict(member_view))
        asins = ["B0TEST0001", "B0TEST0002"]
        if not member_view.get("include_members", True):
            self.summary_polls += 1
            if self.summary_polls < 3:
                campaign = self._campaign("active", asins, ["running", "pending"])
            else:
                campaign = self._campaign("completed", asins, ["succeeded", "variant"])
            campaign.pop("members", None)
        else:
            campaign = self._campaign("completed", asins, ["succeeded", "variant"])
        if member_view.get("include_members", True) and member_view.get("member_limit") is not None:
            offset = int(member_view.get("member_offset") or 0)
            limit = int(member_view["member_limit"])
            campaign["members"] = campaign["members"][offset:offset + limit]
        return campaign

    def cancel_campaign(self, campaign_id):
        assert campaign_id == self.campaign_id
        return self._campaign("cancelled", ["B0TEST0001", "B0TEST0002"], ["cancelled", "cancelled"])

    def _campaign(self, status, asins, statuses):
        return {
            "campaign_id": self.campaign_id,
            "status": status,
            "total": len(asins),
            "terminal": sum(value in {"succeeded", "variant", "failed", "cancelled"} for value in statuses),
            "counts": {name: statuses.count(name) for name in (
                "pending", "running", "deferred", "succeeded", "variant", "failed", "cancelled"
            )},
            "members": [
                {"asin": asin, "status": statuses[index], "refresh_job_id": f"http-job-{index}"}
                for index, asin in enumerate(asins)
            ],
        }


def test_requested_two_workers_fail_before_submit_when_capacity_effective_pool_is_one(tmp_path):
    from operator_campaign_runner import CampaignRunSpec, run_campaign

    batch_dir = tmp_path / ("batch-" + "1" * 24)
    batch_dir.mkdir()
    manifest = batch_dir / "manifest.csv"
    write_manifest(manifest, ["B0TEST0001", "B0TEST0002"])
    client = CapacityLimitedClient()

    result = run_campaign(
        CampaignRunSpec(
            batch_dir=batch_dir,
            manifest=manifest,
            tenant_id="owned-products",
            subject_type="own",
            worker_count=2,
            deadline_seconds=3600,
        ),
        client,
        sleep=lambda _seconds: None,
    )

    assert result == 3
    assert client.submissions == 0
    progress = json.loads((batch_dir / "progress.json").read_text(encoding="utf-8"))
    assert progress["status"] == "failed"
    assert progress["latest_error"] == "worker_capacity_unavailable"
    assert progress["requested_workers"] == 2
    assert progress["effective_workers"] == 1
    assert not (batch_dir / "campaign.json").exists()


def test_operator_progress_and_results_are_projected_only_from_its_campaign(tmp_path):
    from operator_campaign_runner import CampaignRunSpec, run_campaign

    batch_dir = tmp_path / ("batch-" + "2" * 24)
    batch_dir.mkdir()
    manifest = batch_dir / "manifest.csv"
    write_manifest(manifest, ["B0TEST0001", "B0TEST0002", "B0TEST0003"])
    client = CompletingCampaignClient()

    result = run_campaign(
        CampaignRunSpec(
            batch_dir=batch_dir,
            manifest=manifest,
            tenant_id="owned-products",
            subject_type="own",
            worker_count=2,
            deadline_seconds=3600,
            poll_seconds=0,
        ),
        client,
        sleep=lambda _seconds: None,
    )

    assert result == 0
    assert client.submissions == [(
        ["B0TEST0001", "B0TEST0002", "B0TEST0003"],
        "operator:batch-" + "2" * 24,
        {"subject_type": "own", "deadline_seconds": 3600},
    )]
    campaign = json.loads((batch_dir / "campaign.json").read_text(encoding="utf-8"))
    assert campaign["campaign_id"] == client.campaign_id
    progress = json.loads((batch_dir / "progress.json").read_text(encoding="utf-8"))
    assert progress["status"] == "completed"
    assert progress["completed"] == 1
    assert progress["variant"] == 1
    assert progress["failed"] == 1
    assert progress["pending"] == 0
    assert [item["status"] for item in progress["items"]] == ["completed", "variant", "failed"]
    results = json.loads((batch_dir / "results.json").read_text(encoding="utf-8"))
    assert results["campaign_id"] == client.campaign_id
    assert [item["status"] for item in results["items"]] == ["completed", "variant", "failed"]


def test_operator_stop_cancels_the_same_campaign_and_waits_for_terminal_projection(tmp_path):
    from operator_campaign_runner import CampaignRunSpec, run_campaign

    batch_dir = tmp_path / ("batch-" + "3" * 24)
    batch_dir.mkdir()
    manifest = batch_dir / "manifest.csv"
    write_manifest(manifest, ["B0TEST0001", "B0TEST0002", "B0TEST0003"])
    client = CancelledCampaignClient()
    checks = iter([False, True])

    result = run_campaign(
        CampaignRunSpec(
            batch_dir=batch_dir,
            manifest=manifest,
            tenant_id="owned-products",
            subject_type="own",
            worker_count=2,
            poll_seconds=0,
        ),
        client,
        sleep=lambda _seconds: None,
        stop_requested=lambda: next(checks, True),
    )

    assert result == 130
    assert client.cancelled == 1
    progress = json.loads((batch_dir / "progress.json").read_text(encoding="utf-8"))
    assert progress["status"] == "stopped"
    assert progress["stopped"] == 3
    receipt = json.loads((batch_dir / "receipt.json").read_text(encoding="utf-8"))
    assert receipt["status"] == "stopped"
    assert receipt["campaign_id"] == client.campaign_id


def test_cli_uses_scoped_collection_adapter_without_leaking_the_master_key(tmp_path, monkeypatch, capsys):
    import operator_campaign_runner as runner

    batch_dir = tmp_path / ("batch-" + "4" * 24)
    batch_dir.mkdir()
    manifest = batch_dir / "manifest.csv"
    write_manifest(manifest, ["B0TEST0001", "B0TEST0002", "B0TEST0003"])
    client = CompletingCampaignClient()
    captured = {}
    master_key = "master-key-must-not-leak"
    monkeypatch.setenv("AMAZON_COLLECTION_API_KEY", master_key)

    def build_client(base_url, supplied_master):
        captured.update(base_url=base_url, master=supplied_master)
        return client

    monkeypatch.setattr(runner, "build_collection_client", build_client)
    exit_code = runner.main([
        "--batch-dir", str(batch_dir),
        "--manifest", str(manifest),
        "--tenant-id", "owned-products",
        "--subject-type", "own",
        "--worker-count", "2",
        "--expected-count", "3",
        "--deadline-seconds", "3600",
        "--base-url", "http://127.0.0.1:8865",
    ])

    assert exit_code == 0
    assert captured == {"base_url": "http://127.0.0.1:8865", "master": master_key}
    emitted = capsys.readouterr()
    assert master_key not in emitted.out + emitted.err
    assert all(master_key not in path.read_text(encoding="utf-8-sig") for path in batch_dir.glob("*.json"))


def test_campaign_expiry_is_a_failed_operator_terminal_not_a_success(tmp_path):
    from operator_campaign_runner import CampaignRunSpec, run_campaign

    batch_dir = tmp_path / ("batch-" + "5" * 24)
    batch_dir.mkdir()
    manifest = batch_dir / "manifest.csv"
    write_manifest(manifest, ["B0TEST0001", "B0TEST0002", "B0TEST0003"])

    result = run_campaign(
        CampaignRunSpec(
            batch_dir=batch_dir,
            manifest=manifest,
            tenant_id="owned-products",
            subject_type="own",
            worker_count=2,
            poll_seconds=0,
        ),
        ExpiredCampaignClient(),
        sleep=lambda _seconds: None,
    )

    assert result == 4
    progress = json.loads((batch_dir / "progress.json").read_text(encoding="utf-8"))
    assert progress["status"] == "failed"
    assert progress["campaign_status"] == "expired"
    assert progress["failed"] == 3


def test_terminal_member_page_failure_retries_then_persists_safe_failure(tmp_path):
    from operator_campaign_runner import CampaignRunSpec, run_campaign

    batch_dir = tmp_path / ("batch-" + "f" * 24)
    batch_dir.mkdir()
    manifest = batch_dir / "manifest.csv"
    write_manifest(manifest, ["B0TEST0001", "B0TEST0002", "B0TEST0003"])
    client = TerminalMemberFailureClient()

    result = run_campaign(
        CampaignRunSpec(
            batch_dir=batch_dir,
            manifest=manifest,
            tenant_id="owned-products",
            subject_type="own",
            worker_count=2,
            poll_seconds=0,
            poll_failure_limit=2,
        ),
        client,
        sleep=lambda _seconds: None,
    )

    assert result == 4
    assert client.member_calls == 2
    progress = json.loads((batch_dir / "progress.json").read_text(encoding="utf-8"))
    receipt = json.loads((batch_dir / "receipt.json").read_text(encoding="utf-8"))
    assert progress["latest_error"] == receipt["latest_error"] == "campaign_members_fetch_failed"
    assert "secret detail" not in json.dumps(progress) + json.dumps(receipt)
    assert not (batch_dir / "results.json").exists()


def test_repeated_poll_failure_cancels_campaign_and_persists_a_safe_failure(tmp_path):
    from operator_campaign_runner import CampaignRunSpec, run_campaign

    batch_dir = tmp_path / ("batch-" + "6" * 24)
    batch_dir.mkdir()
    manifest = batch_dir / "manifest.csv"
    write_manifest(manifest, ["B0TEST0001", "B0TEST0002", "B0TEST0003"])
    client = FailingPollCampaignClient()

    result = run_campaign(
        CampaignRunSpec(
            batch_dir=batch_dir,
            manifest=manifest,
            tenant_id="owned-products",
            subject_type="own",
            worker_count=2,
            poll_seconds=0,
        ),
        client,
        sleep=lambda _seconds: None,
    )

    assert result == 4
    assert client.polls == 3
    assert client.cancelled == 1
    progress = json.loads((batch_dir / "progress.json").read_text(encoding="utf-8"))
    assert progress["status"] == "failed"
    assert progress["latest_error"] == "campaign_poll_failed"
    assert "secret detail" not in json.dumps(progress)


def test_campaign_access_control_member_remains_blocked_in_operator_projection(tmp_path):
    from operator_campaign_runner import CampaignRunSpec, run_campaign

    batch_dir = tmp_path / ("batch-" + "7" * 24)
    batch_dir.mkdir()
    manifest = batch_dir / "manifest.csv"
    write_manifest(manifest, ["B0TEST0001", "B0TEST0002", "B0TEST0003"])

    result = run_campaign(
        CampaignRunSpec(
            batch_dir=batch_dir,
            manifest=manifest,
            tenant_id="owned-products",
            subject_type="own",
            worker_count=2,
            poll_seconds=0,
        ),
        BlockedCampaignClient(),
        sleep=lambda _seconds: None,
    )

    assert result == 0
    progress = json.loads((batch_dir / "progress.json").read_text(encoding="utf-8"))
    assert progress["blocked"] == 1
    assert progress["failed"] == 0
    assert progress["items"][2]["status"] == "blocked"


def test_operator_runner_uses_the_real_loopback_campaign_http_seam(tmp_path):
    import collection_api
    from operator_campaign_runner import CampaignRunSpec, build_collection_client, run_campaign

    repository = HttpCampaignRepository()
    server = collection_api.CollectionServer(
        ("127.0.0.1", 0),
        repository=repository,
        api_key="master",
        campaign_max_asins=5000,
        refresh_notifier=lambda: None,
        refresh_worker_status=lambda: {
            "state": "running",
            "requested_workers": 2,
            "effective_workers": 2,
            "alive_workers": 2,
            "capacity_backpressure": False,
        },
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    batch_dir = tmp_path / ("batch-" + "8" * 24)
    batch_dir.mkdir()
    manifest = batch_dir / "manifest.csv"
    write_manifest(manifest, ["B0TEST0001", "B0TEST0002"])
    try:
        client = build_collection_client(f"http://127.0.0.1:{server.server_port}", "master")
        result = run_campaign(
            CampaignRunSpec(
                batch_dir=batch_dir,
                manifest=manifest,
                tenant_id="owned-products",
                subject_type="own",
                worker_count=2,
                poll_seconds=0,
            ),
            client,
            sleep=lambda _seconds: None,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert result == 0
    progress = json.loads((batch_dir / "progress.json").read_text(encoding="utf-8"))
    assert progress["campaign_id"] == repository.campaign_id
    assert progress["completed"] == 1
    assert progress["variant"] == 1
    assert repository.load_views == [
        {"include_members": False, "member_offset": 0, "member_limit": None},
        {"include_members": False, "member_offset": 0, "member_limit": None},
        {"include_members": False, "member_offset": 0, "member_limit": None},
        {"include_members": True, "member_offset": 0, "member_limit": 2},
    ]


def test_operator_stop_retries_campaign_cancel_before_failing_the_runner(tmp_path):
    from operator_campaign_runner import CampaignRunSpec, run_campaign

    batch_dir = tmp_path / ("batch-" + "a" * 24)
    batch_dir.mkdir()
    manifest = batch_dir / "manifest.csv"
    write_manifest(manifest, ["B0TEST0001", "B0TEST0002", "B0TEST0003"])
    client = RetryingCancelCampaignClient()

    result = run_campaign(
        CampaignRunSpec(
            batch_dir=batch_dir,
            manifest=manifest,
            tenant_id="owned-products",
            subject_type="own",
            worker_count=2,
            poll_seconds=0,
        ),
        client,
        sleep=lambda _seconds: None,
        stop_requested=lambda: True,
    )

    assert result == 130
    assert client.cancelled == 3
    assert json.loads((batch_dir / "progress.json").read_text(encoding="utf-8"))["status"] == "stopped"


def test_operator_rejects_a_collection_service_with_the_wrong_subject_scope(tmp_path):
    from operator_campaign_runner import CampaignRunSpec, run_campaign

    batch_dir = tmp_path / ("batch-" + "b" * 24)
    batch_dir.mkdir()
    manifest = batch_dir / "manifest.csv"
    write_manifest(manifest, ["B0TEST0001", "B0TEST0002", "B0TEST0003"])
    client = SubjectMismatchClient()

    result = run_campaign(
        CampaignRunSpec(
            batch_dir=batch_dir,
            manifest=manifest,
            tenant_id="owned-products",
            subject_type="own",
            worker_count=2,
            poll_seconds=0,
        ),
        client,
        sleep=lambda _seconds: None,
    )

    assert result == 3
    assert client.submissions == []
    progress = json.loads((batch_dir / "progress.json").read_text(encoding="utf-8"))
    assert progress["latest_error"] == "collection_subject_mismatch"


def test_late_stop_does_not_relabel_an_already_completed_campaign(tmp_path):
    from operator_campaign_runner import CampaignRunSpec, run_campaign

    batch_dir = tmp_path / ("batch-" + "d" * 24)
    batch_dir.mkdir()
    manifest = batch_dir / "manifest.csv"
    write_manifest(manifest, ["B0TEST0001", "B0TEST0002", "B0TEST0003"])

    result = run_campaign(
        CampaignRunSpec(
            batch_dir=batch_dir,
            manifest=manifest,
            tenant_id="owned-products",
            subject_type="own",
            worker_count=2,
            poll_seconds=0,
        ),
        CompletedBeforeStopClient(),
        sleep=lambda _seconds: None,
        stop_requested=lambda: True,
    )

    assert result == 0
    progress = json.loads((batch_dir / "progress.json").read_text(encoding="utf-8"))
    assert progress["status"] == "completed"


def test_cancel_response_completed_wins_over_local_stop_intent(tmp_path):
    from operator_campaign_runner import CampaignRunSpec, run_campaign

    batch_dir = tmp_path / ("batch-" + "e" * 24)
    batch_dir.mkdir()
    manifest = batch_dir / "manifest.csv"
    write_manifest(manifest, ["B0TEST0001", "B0TEST0002", "B0TEST0003"])
    client = CompletedDuringCancelClient()

    result = run_campaign(
        CampaignRunSpec(
            batch_dir=batch_dir,
            manifest=manifest,
            tenant_id="owned-products",
            subject_type="own",
            worker_count=2,
            poll_seconds=0,
        ),
        client,
        sleep=lambda _seconds: None,
        stop_requested=lambda: True,
    )

    assert result == 0
    assert client.cancelled == 1
    progress = json.loads((batch_dir / "progress.json").read_text(encoding="utf-8"))
    assert progress["campaign_status"] == "completed"
    assert progress["status"] == "completed"
