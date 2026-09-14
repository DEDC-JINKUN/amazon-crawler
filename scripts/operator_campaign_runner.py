#!/usr/bin/env python3
"""Project one Operator run from the shared loopback Collection campaign."""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


ASIN_RE = re.compile(r"[A-Z0-9]{10}")
BATCH_RE = re.compile(r"batch-[0-9a-f]{24}")


@dataclass(frozen=True)
class CampaignRunSpec:
    batch_dir: Path
    manifest: Path
    tenant_id: str
    subject_type: str
    worker_count: int
    deadline_seconds: int = 86400
    poll_seconds: float = 1.1
    poll_failure_limit: int = 3


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _read_manifest(spec: CampaignRunSpec) -> list[str]:
    batch_dir = Path(spec.batch_dir).resolve()
    manifest = Path(spec.manifest).resolve()
    if not BATCH_RE.fullmatch(batch_dir.name) or manifest != batch_dir / "manifest.csv":
        raise ValueError("invalid operator campaign paths")
    with manifest.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    asins = list(dict.fromkeys(str(row.get("asin") or "").strip().upper() for row in rows))
    if not asins or any(not ASIN_RE.fullmatch(asin) for asin in asins):
        raise ValueError("invalid operator campaign manifest")
    return asins


def _worker_pool(readiness: dict[str, Any]) -> dict[str, Any]:
    worker = readiness.get("refresh_worker")
    if not isinstance(worker, dict):
        return {"state": "failed", "requested_workers": 0, "effective_workers": 0, "alive_workers": 0}
    state = str(worker.get("state") or "failed")
    effective = int(worker.get("effective_workers") or (1 if state == "running" else 0))
    alive = int(worker.get("alive_workers") or (1 if state == "running" and effective == 1 else 0))
    return {
        "state": state,
        "requested_workers": int(worker.get("requested_workers") or effective),
        "effective_workers": effective,
        "alive_workers": alive,
        "capacity_backpressure": bool(worker.get("capacity_backpressure")),
    }


def _project_campaign(campaign: dict[str, Any], *, stopping: bool = False) -> dict[str, Any]:
    projected_items: list[dict[str, Any]] = []
    mapping = {
        "pending": "pending",
        "deferred": "pending",
        "running": "claimed",
        "succeeded": "completed",
        "variant": "variant",
        "failed": "failed",
        "cancelled": "stopped" if stopping else "failed",
    }
    for raw in campaign.get("members") or []:
        item = dict(raw)
        source_status = str(raw.get("status") or "pending")
        recovery_outcome = str(raw.get("recovery_outcome") or "")
        if source_status == "failed" and (
            str(raw.get("item_status") or "") == "blocked"
            or recovery_outcome in {"blocked", "access_control"}
        ):
            item["status"] = "blocked"
        else:
            item["status"] = mapping.get(source_status, "failed")
        projected_items.append(item)
    counts = {
        name: sum(item["status"] == name for item in projected_items)
        for name in ("pending", "claimed", "completed", "variant", "failed", "blocked", "stopped")
    }
    terminal = counts["completed"] + counts["variant"] + counts["failed"] + counts["blocked"] + counts["stopped"]
    total = len(projected_items)
    campaign_status = str(campaign.get("status") or "")
    campaign_terminal = campaign_status in {"completed", "cancelled", "expired"} and terminal == total
    if not campaign_terminal:
        operator_status = "running"
    elif campaign_status == "completed":
        operator_status = "completed"
    elif stopping:
        operator_status = "stopped"
    else:
        operator_status = "failed"
    return {
        "status": operator_status,
        "requested": total,
        **counts,
        "latest_error": None,
        "campaign_id": campaign.get("campaign_id"),
        "campaign_status": campaign_status,
        "items": projected_items,
    }


def _write_results(batch_dir: Path, progress: dict[str, Any]) -> None:
    result_items = [dict(item) for item in progress["items"]]
    _atomic_json(batch_dir / "results.json", {
        "schema_version": "amazon-operator-campaign-results-v1",
        "campaign_id": progress.get("campaign_id"),
        "items": result_items,
    })
    fields = ["asin", "status", "refresh_job_id"]
    with (batch_dir / "results.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(result_items)


def _cancel_campaign(client: Any, campaign_id: str, *, attempts: int, sleep: Callable[[float], None], delay: float) -> dict[str, Any]:
    last_error: Exception | None = None
    for attempt in range(max(1, int(attempts))):
        try:
            return dict((client.cancel_campaign(campaign_id) or {}).get("campaign") or {})
        except (OSError, RuntimeError, ValueError) as exc:
            last_error = exc
            if attempt + 1 < max(1, int(attempts)):
                sleep(max(0.0, float(delay)))
    raise OSError("campaign cancel failed") from last_error


def run_campaign(
    spec: CampaignRunSpec,
    client: Any,
    *,
    sleep: Callable[[float], None] = time.sleep,
    stop_requested: Callable[[], bool] | None = None,
) -> int:
    """Run one Operator batch through the shared campaign interface."""
    asins = _read_manifest(spec)
    if stop_requested is None:
        stop_marker = Path(spec.batch_dir) / "operator.stop.json"
        stop_requested = stop_marker.is_file
    readiness = client.get_readiness()
    pool = _worker_pool(readiness)
    if str(readiness.get("tenant_id") or "") != spec.tenant_id:
        error = "collection_tenant_mismatch"
    elif str(readiness.get("subject_type") or "") != spec.subject_type:
        error = "collection_subject_mismatch"
    elif pool["state"] != "running" or pool["effective_workers"] < spec.worker_count or pool["alive_workers"] < spec.worker_count:
        error = "worker_capacity_unavailable"
    else:
        submitted = client.submit_campaign(
            asins,
            f"operator:{Path(spec.batch_dir).name}",
            subject_type=spec.subject_type,
            deadline_seconds=spec.deadline_seconds,
        )
        campaign = dict(submitted.get("campaign") or {})
        campaign_id = str(campaign.get("campaign_id") or "")
        if not campaign_id:
            raise RuntimeError("campaign submission returned no campaign_id")
        _atomic_json(Path(spec.batch_dir) / "campaign.json", {
            "schema_version": "amazon-operator-campaign-link-v1",
            "campaign_id": campaign_id,
            "tenant_id": spec.tenant_id,
            "subject_type": spec.subject_type,
            "requested_workers": spec.worker_count,
        })
        stopping = False
        poll_failures = 0
        while True:
            try:
                campaign = dict((client.get_campaign(campaign_id) or {}).get("campaign") or {})
                poll_failures = 0
            except (OSError, RuntimeError, ValueError):
                poll_failures += 1
                if poll_failures < max(1, int(spec.poll_failure_limit)):
                    sleep(max(0.0, float(spec.poll_seconds)))
                    continue
                try:
                    campaign = _cancel_campaign(
                        client, campaign_id, attempts=spec.poll_failure_limit,
                        sleep=sleep, delay=spec.poll_seconds,
                    ) or campaign
                except (OSError, RuntimeError, ValueError):
                    pass
                progress = _project_campaign(campaign)
                progress.update({
                    "status": "failed",
                    "latest_error": "campaign_poll_failed",
                    "requested_workers": int(spec.worker_count),
                    "effective_workers": pool["effective_workers"],
                    "alive_workers": pool["alive_workers"],
                })
                _atomic_json(Path(spec.batch_dir) / "progress.json", progress)
                if int(progress.get("pending") or 0) == 0 and int(progress.get("claimed") or 0) == 0:
                    _write_results(Path(spec.batch_dir), progress)
                _atomic_json(Path(spec.batch_dir) / "receipt.json", {
                    "schema_version": "amazon-operator-campaign-receipt-v1",
                    "status": "failed",
                    "campaign_id": campaign_id,
                    "requested": progress["requested"],
                    "completed": progress["completed"],
                    "variant": progress["variant"],
                    "failed": progress["failed"],
                    "blocked": progress["blocked"],
                    "latest_error": "campaign_poll_failed",
                    "requested_workers": int(spec.worker_count),
                    "effective_workers": pool["effective_workers"],
                    "finished_at": datetime.now(timezone.utc).isoformat(),
                })
                return 4
            current_projection = _project_campaign(campaign)
            if not stopping and current_projection["status"] == "running" and stop_requested():
                stopping = True
                campaign = _cancel_campaign(
                    client, campaign_id, attempts=spec.poll_failure_limit,
                    sleep=sleep, delay=spec.poll_seconds,
                ) or campaign
            progress = _project_campaign(campaign, stopping=stopping)
            progress.update({
                "requested_workers": int(spec.worker_count),
                "effective_workers": pool["effective_workers"],
                "alive_workers": pool["alive_workers"],
            })
            _atomic_json(Path(spec.batch_dir) / "progress.json", progress)
            if progress["status"] in {"completed", "failed", "stopped"}:
                _write_results(Path(spec.batch_dir), progress)
                now = datetime.now(timezone.utc).isoformat()
                _atomic_json(Path(spec.batch_dir) / "receipt.json", {
                    "schema_version": "amazon-operator-campaign-receipt-v1",
                    "status": progress["status"],
                    "campaign_id": campaign_id,
                    "requested": progress["requested"],
                    "completed": progress["completed"],
                    "variant": progress["variant"],
                    "failed": progress["failed"],
                    "blocked": progress["blocked"],
                    "requested_workers": int(spec.worker_count),
                    "effective_workers": pool["effective_workers"],
                    "finished_at": now,
                })
                if progress["status"] == "stopped":
                    return 130
                return 0 if progress["status"] == "completed" else 4
            sleep(max(0.0, float(spec.poll_seconds)))
    _atomic_json(Path(spec.batch_dir) / "progress.json", {
        "status": "failed",
        "requested": len(asins),
        "claimed": 0,
        "completed": 0,
        "variant": 0,
        "failed": 0,
        "blocked": 0,
        "pending": len(asins),
        "latest_error": error,
        "requested_workers": int(spec.worker_count),
        "effective_workers": pool["effective_workers"],
        "alive_workers": pool["alive_workers"],
        "items": [{"asin": asin, "status": "pending"} for asin in asins],
    })
    return 3


def build_collection_client(base_url: str, master_key: str) -> Any:
    from amazon_collection_client import AmazonCollectionClient
    from collection_api import derive_agent_key

    agent_id = "refresh-agent"
    return AmazonCollectionClient(base_url, agent_id, derive_agent_key(master_key, agent_id))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-dir", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--subject-type", choices=("own", "competitor", "candidate"), default="own")
    parser.add_argument("--worker-count", type=int, choices=(1, 2), required=True)
    parser.add_argument("--expected-count", type=int, choices=range(1, 5001), required=True)
    parser.add_argument("--deadline-seconds", type=int, choices=range(60, 604801), default=86400)
    parser.add_argument("--poll-seconds", type=float, default=1.1)
    parser.add_argument("--base-url", default="http://127.0.0.1:8765")
    args = parser.parse_args(argv)
    master_key = os.environ.get("AMAZON_COLLECTION_API_KEY", "").strip()
    if not master_key:
        print("operator campaign credential unavailable", file=sys.stderr)
        return 2
    spec = CampaignRunSpec(
        batch_dir=args.batch_dir,
        manifest=args.manifest,
        tenant_id=args.tenant_id,
        subject_type=args.subject_type,
        worker_count=args.worker_count,
        deadline_seconds=args.deadline_seconds,
        poll_seconds=max(1.1, float(args.poll_seconds)),
    )
    try:
        if len(_read_manifest(spec)) != args.expected_count:
            raise ValueError("operator campaign manifest count mismatch")
        client = build_collection_client(args.base_url, master_key)
        return run_campaign(spec, client)
    except (KeyError, OSError, RuntimeError, ValueError) as exc:
        print(f"operator campaign failed: {type(exc).__name__}", file=sys.stderr)
        return 4


__all__ = ["CampaignRunSpec", "run_campaign"]


if __name__ == "__main__":
    raise SystemExit(main())
