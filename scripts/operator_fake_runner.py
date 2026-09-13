"""Offline-only Operator runner used by tests and browser acceptance."""
from __future__ import annotations

import argparse
import csv
import json
import os
import signal
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path


stopping = False


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic(path: Path, value) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    for attempt in range(6):
        try:
            os.replace(temporary, path)
            return
        except PermissionError:
            if attempt == 5:
                raise
            time.sleep(0.02 * (attempt + 1))


def request_stop(_signum, _frame):
    global stopping
    stopping = True


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch-dir", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--delay-seconds", type=float, default=0.05)
    args = parser.parse_args(argv)
    signal.signal(signal.SIGINT, request_stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, request_stop)
    batch_dir = args.batch_dir.resolve()
    manifest = args.manifest.resolve()
    if manifest.parent != batch_dir or batch_dir.name.startswith("batch-") is False:
        return 2
    with manifest.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    asins = [str(row.get("asin") or "") for row in rows]
    items = [{"asin": asin, "status": "pending"} for asin in asins]
    started = now()
    for index, asin in enumerate(asins):
        if stopping:
            break
        items[index]["status"] = "claimed"
        atomic(batch_dir / "progress.json", {
            "status": "running", "requested": len(asins), "claimed": 1,
            "completed": index, "variant": 0, "failed": 0, "blocked": 0,
            "pending": len(asins) - index - 1, "latest_error": None, "items": items,
        })
        time.sleep(max(0.0, min(float(args.delay_seconds), 2.0)))
        items[index]["status"] = "completed"
    completed = sum(1 for item in items if item["status"] == "completed")
    status = "stopped" if stopping else "completed"
    finished = now()
    progress = {
        "status": status, "requested": len(asins), "claimed": 0,
        "completed": completed, "variant": 0, "failed": 0, "blocked": 0,
        "pending": len(asins) - completed, "latest_error": None,
        "items": items, "finished_at": finished,
    }
    atomic(batch_dir / "progress.json", progress)
    results = [{"asin": item["asin"], "status": item["status"], "source": "offline_fake"} for item in items]
    atomic(batch_dir / "results.json", {"schema_version": "amazon-operator-results-v1", "items": results})
    with (batch_dir / "results.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["asin", "status", "source"])
        writer.writeheader()
        writer.writerows(results)
    atomic(batch_dir / "receipt.json", {
        "schema_version": "amazon-operator-receipt-v1",
        "status": status,
        "requested": len(asins),
        "completed": completed,
        "started_at": started,
        "finished_at": finished,
        "runner": "offline_fake",
    })
    return 130 if stopping else 0


if __name__ == "__main__":
    raise SystemExit(main())
