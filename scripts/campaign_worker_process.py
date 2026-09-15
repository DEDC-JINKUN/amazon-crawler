#!/usr/bin/env python3
"""One supervised campaign queue consumer process; no HTTP server or API key."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import signal
import threading

from agent_collection_service import AgentRefreshWorker, stop_and_cleanup_worker
from amazon_us_worker import _build_http_adapter, load_config
from campaign_worker_status import publish_status_snapshot
from postgres_worker_storage import PostgresWorkerStorage


def _safe_error(value) -> str | None:
    if value is None:
        return None
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", str(value)).strip()
    return text[:120] or None


def _write_status(path: Path | None, status: dict, launch_token: str, sequence: int) -> None:
    if path is None:
        return
    payload = {
        "pid": os.getpid(),
        "state": str(status.get("state") or "unknown"),
        "last_error": _safe_error(status.get("last_error")),
        "heartbeat_at": datetime.now(timezone.utc).isoformat(),
        "worker_heartbeat_at": status.get("worker_heartbeat_at"),
    }
    publish_status_snapshot(path, payload, launch_token=launch_token, sequence=sequence)


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
    parser.add_argument("--launch-token", required=True)
    parser.add_argument("--stop-file", type=Path)
    parser.add_argument("--worker-id")
    args = parser.parse_args()
    dsn = os.environ.get(args.dsn_env, "").strip()
    if not dsn:
        raise RuntimeError("worker database credential unavailable")
    output_dir = args.output_dir.resolve()
    status_file = args.status_file.resolve() if args.status_file else None
    if not re.fullmatch(r"[a-f0-9]{32}", args.launch_token):
        raise RuntimeError("worker launch token is invalid")
    if status_file is not None:
        expected_parent = output_dir / "control" / "agent-workers"
        if status_file.parent != expected_parent or status_file.suffix != ".json":
            raise RuntimeError("worker status path is outside the controlled output directory")
    stop_file = (args.stop_file or (status_file.with_suffix(".stop") if status_file else None))
    if stop_file is None:
        raise RuntimeError("worker stop path is required")
    stop_file = stop_file.resolve()
    if (
        stop_file.parent != output_dir / "control" / "agent-workers"
        or not re.fullmatch(r"worker-[1-8]\.stop", stop_file.name)
    ):
        raise RuntimeError("worker stop path is outside the controlled output directory")
    config = load_config(args.config.resolve())
    config["output_dir"] = output_dir
    config["raw_html_dir"] = output_dir / "raw_html"
    storage = PostgresWorkerStorage(
        dsn, args.tenant_id, args.subject_type,
        default_lease_seconds=args.lease_seconds,
    )
    worker_id = str(args.worker_id or f"agent-refresh-{os.getpid()}")
    if not re.fullmatch(r"agent-refresh-(?:[a-f0-9]{32}|[0-9]+)", worker_id):
        raise RuntimeError("worker owner identity is invalid")
    worker = AgentRefreshWorker(
        storage=storage,
        adapter_factory=lambda: _build_http_adapter(config),
        config=config,
        poll_seconds=args.poll_seconds,
        lease_seconds=args.lease_seconds,
        worker_id=worker_id,
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
    status_sequence = 0
    try:
        while not stopped.wait(1.0):
            if stop_file.exists():
                stop_requested = True
                break
            status = worker.status()
            last_state = str(status.get("state") or "unknown")
            status_sequence += 1
            _write_status(status_file, status, args.launch_token, status_sequence)
            if last_state == "failed":
                exit_code = 2
                break
            if last_state == "blocked":
                exit_code = 3
                break
    finally:
        try:
            stop_and_cleanup_worker(worker, storage, worker_id)
        except Exception:
            exit_code = 2
        if stop_requested and last_state not in {"failed", "blocked"}:
            status_sequence += 1
            _write_status(
                status_file,
                {"state": "stopped", "last_error": None},
                args.launch_token,
                status_sequence,
            )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
