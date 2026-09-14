#!/usr/bin/env python3
"""One supervised campaign queue consumer process; no HTTP server or API key."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import signal
import threading
import time
import uuid

from agent_collection_service import AgentRefreshWorker
from amazon_us_worker import _build_http_adapter, load_config
from postgres_worker_storage import PostgresWorkerStorage

STATUS_REPLACE_ATTEMPTS = 20
STATUS_REPLACE_DELAY_SECONDS = 0.01


def _safe_error(value) -> str | None:
    if value is None:
        return None
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", str(value)).strip()
    return text[:120] or None


def _write_status(path: Path | None, status: dict) -> None:
    if path is None:
        return
    payload = {
        "schema": "amazon-campaign-worker-status-v1",
        "pid": os.getpid(),
        "state": str(status.get("state") or "unknown"),
        "last_error": _safe_error(status.get("last_error")),
        "heartbeat_at": datetime.now(timezone.utc).isoformat(),
        "worker_heartbeat_at": status.get("worker_heartbeat_at"),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    try:
        for attempt in range(STATUS_REPLACE_ATTEMPTS):
            try:
                os.replace(temporary, path)
                return
            except PermissionError:
                if attempt + 1 >= STATUS_REPLACE_ATTEMPTS:
                    raise
                time.sleep(STATUS_REPLACE_DELAY_SECONDS)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--subject-type", default="own")
    parser.add_argument("--dsn-env", default="AMAZON_US_POSTGRES_DSN")
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    parser.add_argument("--lease-seconds", type=int, default=600)
    parser.add_argument("--status-file", type=Path)
    args = parser.parse_args()
    dsn = os.environ.get(args.dsn_env, "").strip()
    if not dsn:
        raise RuntimeError("worker database credential unavailable")
    output_dir = args.output_dir.resolve()
    status_file = args.status_file.resolve() if args.status_file else None
    if status_file is not None:
        expected_parent = output_dir / "control" / "agent-workers"
        if status_file.parent != expected_parent or status_file.suffix != ".json":
            raise RuntimeError("worker status path is outside the controlled output directory")
    config = load_config(args.config.resolve())
    config["output_dir"] = output_dir
    config["raw_html_dir"] = output_dir / "raw_html"
    storage = PostgresWorkerStorage(
        dsn, args.tenant_id, args.subject_type,
        default_lease_seconds=args.lease_seconds,
    )
    worker = AgentRefreshWorker(
        storage=storage,
        adapter_factory=lambda: _build_http_adapter(config),
        config=config,
        poll_seconds=args.poll_seconds,
        lease_seconds=args.lease_seconds,
    )
    stopped = threading.Event()
    stop_requested = False

    def stop(*_args):
        nonlocal stop_requested
        stop_requested = True
        stopped.set()

    signal.signal(signal.SIGINT, stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, stop)
    worker.start()
    exit_code = 0
    last_state = "starting"
    try:
        while not stopped.wait(1.0):
            status = worker.status()
            last_state = str(status.get("state") or "unknown")
            _write_status(status_file, status)
            if last_state == "failed":
                exit_code = 2
                break
            if last_state == "blocked":
                exit_code = 3
                break
    finally:
        worker.stop()
        if stop_requested and last_state not in {"failed", "blocked"}:
            _write_status(status_file, {"state": "stopped", "last_error": None})
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
