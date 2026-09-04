#!/usr/bin/env python3
"""Loopback Collection API with a bounded refresh-only PostgreSQL worker."""
from __future__ import annotations

import argparse
import os
import sys
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from amazon_us_worker import (  # noqa: E402
    DEFAULT_CONFIG,
    ProxyCapacityGateDenied,
    _build_http_adapter,
    load_config,
    run_postgres_actions,
)
from collection_api import CollectionServer, LOOPBACK_HOSTS  # noqa: E402
from collection_storage import PostgresCollectionRepository  # noqa: E402
from postgres_worker_storage import PostgresWorkerStorage  # noqa: E402

MAX_AGENT_REFRESH_BATCH = 5


class RefreshOnlyStorageView:
    """Expose refresh claims while making the ordinary task queue unreachable."""

    def __init__(self, storage: Any):
        self._storage = storage
        self._batch_counts = {"processed": 0, "succeeded": 0, "variant_redirect": 0, "failed": 0, "blocked": 0}
        self._pending_outcome: str | None = None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._storage, name)

    def claim_task(self, *args, **kwargs) -> None:
        return None

    def begin_batch_metrics(self) -> None:
        self._batch_counts = {"processed": 0, "succeeded": 0, "variant_redirect": 0, "failed": 0, "blocked": 0}
        self._pending_outcome = None

    def batch_metrics(self) -> dict[str, int]:
        return dict(self._batch_counts)

    def save_product_result(self, **payload: Any) -> Any:
        result = self._storage.save_product_result(**payload)
        if result:
            self._pending_outcome = "succeeded"
        return result

    def save_failure(self, **payload: Any) -> Any:
        result = self._storage.save_failure(**payload)
        if result:
            state_fields = dict(payload.get("state_fields") or {})
            self._pending_outcome = (
                "variant_redirect"
                if payload.get("reason") == "variant_redirect"
                else "blocked"
                if payload.get("next_status") == "blocked" or state_fields.get("block_reason")
                else "failed"
            )
        return result

    def finish_refresh_request(self, job_id: str, status: str) -> None:
        self._storage.finish_refresh_request(job_id, status)
        if status not in {"completed", "failed", "queued", "cancelled"}:
            return
        outcome = self._pending_outcome
        if outcome not in {"succeeded", "variant_redirect", "failed", "blocked"}:
            outcome = "succeeded" if status == "completed" else "blocked" if status == "queued" else "failed"
        self._batch_counts["processed"] += 1
        self._batch_counts[outcome] += 1
        self._pending_outcome = None


class AgentRefreshWorker:
    """Own one background consumer that can claim refresh requests only."""

    def __init__(
        self,
        *,
        storage: Any,
        adapter_factory: Callable[[], Any],
        config: dict[str, Any],
        poll_seconds: float = 1.0,
        lease_seconds: int = 600,
    ):
        self.storage = storage
        self._refresh_storage = RefreshOnlyStorageView(storage)
        self.adapter_factory = adapter_factory
        self.config = dict(config)
        configure = getattr(self.storage, "configure_recovery", None)
        if callable(configure):
            configure(self.config)
        self.poll_seconds = max(0.05, float(poll_seconds))
        self.lease_seconds = max(1, int(lease_seconds))
        self._event = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._state = "created"
        self._processed_actions = 0
        self._succeeded_actions = 0
        self._variant_redirect_actions = 0
        self._failed_actions = 0
        self._blocked_actions = 0
        self._last_error: str | None = None
        self._last_capacity_decision: dict[str, Any] | None = None
        self._last_action_at: str | None = None

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("refresh worker is already started")
        with self._lock:
            self._state = "starting"
        self._thread = threading.Thread(target=self._run, name="agent-refresh-worker", daemon=True)
        self._thread.start()

    def notify(self) -> None:
        self._event.set()

    def stop(self, timeout_seconds: float = 10.0) -> None:
        self._stop.set()
        self._event.set()
        if self._thread is not None:
            self._thread.join(timeout=max(0.1, timeout_seconds))
        with self._lock:
            self._state = "stopped"

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "state": self._state,
                "processed_actions": self._processed_actions,
                "succeeded_actions": self._succeeded_actions,
                "variant_redirect_actions": self._variant_redirect_actions,
                "failed_actions": self._failed_actions,
                "blocked_actions": self._blocked_actions,
                # Backward-compatible field: completed now means successful, never merely attempted.
                "completed_actions": self._succeeded_actions,
                "last_action_at": self._last_action_at,
                "last_error": self._last_error,
                "capacity_decision": dict(self._last_capacity_decision) if self._last_capacity_decision else None,
            }

    def _run(self) -> None:
        adapter = None
        worker_id = f"agent-refresh-{os.getpid()}"
        try:
            self.storage.reclaim_expired_leases()
            adapter = self.adapter_factory()
            with self._lock:
                self._state = "running"
            while not self._stop.is_set():
                with self._lock:
                    capacity_blocked = self._state == "blocked" and self._last_error != "access_blocked"
                if capacity_blocked:
                    self._event.wait(max(self.poll_seconds, 30.0))
                    self._event.clear()
                    try:
                        self.storage.reclaim_expired_leases()
                    except Exception:
                        continue
                # A restarted service can begin before the old lease expires.
                # Sweep while idle too, otherwise claimed refreshes never wake.
                self.storage.reclaim_expired_leases()
                counter = getattr(self.storage, "count_pending_refresh_tasks", None)
                if callable(counter):
                    pending_count = int(counter(MAX_AGENT_REFRESH_BATCH))
                else:
                    has_pending = getattr(self.storage, "has_pending_refresh_task", None)
                    pending_count = MAX_AGENT_REFRESH_BATCH if not callable(has_pending) or has_pending() else 0
                if pending_count <= 0:
                    self._event.wait(self.poll_seconds)
                    self._event.clear()
                    continue
                batch_limit = max(1, min(MAX_AGENT_REFRESH_BATCH, pending_count))
                run_id = f"agent-refresh-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}-{uuid.uuid4().hex[:8]}"
                self._refresh_storage.begin_batch_metrics()
                try:
                    result = run_postgres_actions(
                        self._refresh_storage,
                        adapter,
                        self.config,
                        limit=batch_limit,
                        run_id=run_id,
                        worker_id=worker_id,
                        lease_seconds=self.lease_seconds,
                    )
                except ProxyCapacityGateDenied as exc:
                    with self._lock:
                        self._state = "blocked"
                        self._last_error = exc.reason
                        self._last_capacity_decision = dict(exc.decision)
                    continue
                with self._lock:
                    if self._state == "blocked":
                        self._state = "running"
                        self._last_error = None
                        self._last_capacity_decision = None
                if result == -1:
                    metrics = self._refresh_storage.batch_metrics()
                    with self._lock:
                        self._processed_actions += metrics["processed"]
                        self._succeeded_actions += metrics["succeeded"]
                        self._variant_redirect_actions += metrics["variant_redirect"]
                        self._failed_actions += metrics["failed"]
                        self._blocked_actions += metrics["blocked"]
                    with self._lock:
                        self._state = "blocked"
                        self._last_error = "access_blocked"
                    if callable(getattr(self.storage, "configure_recovery", None)):
                        self._event.wait(self.poll_seconds)
                        self._event.clear()
                        continue
                    return
                if result > 0:
                    metrics = self._refresh_storage.batch_metrics()
                    with self._lock:
                        self._processed_actions += metrics["processed"]
                        self._succeeded_actions += metrics["succeeded"]
                        self._variant_redirect_actions += metrics["variant_redirect"]
                        self._failed_actions += metrics["failed"]
                        self._blocked_actions += metrics["blocked"]
                        self._last_action_at = datetime.now(timezone.utc).isoformat()
                    continue
                self._event.wait(self.poll_seconds)
                self._event.clear()
        except Exception as exc:
            terminalizer = getattr(self.storage, "fail_claimed_refreshes", None)
            if callable(terminalizer):
                try:
                    terminalizer(worker_id, "agent_refresh_worker_failed")
                except Exception:
                    pass
            with self._lock:
                self._state = "failed"
                self._last_error = type(exc).__name__
        finally:
            if adapter is not None and hasattr(adapter, "close"):
                adapter.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--subject-type", choices=("own", "competitor", "candidate"), default="own")
    parser.add_argument("--dsn-env", default="AMAZON_US_POSTGRES_DSN")
    parser.add_argument("--api-key-env", default="AMAZON_COLLECTION_API_KEY")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    parser.add_argument("--lease-seconds", type=int, default=600)
    parser.add_argument("--agent-rate-limit", type=int, default=60)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.host not in LOOPBACK_HOSTS:
        print("error: Agent Collection Service only allows loopback", file=sys.stderr)
        return 2
    dsn = os.environ.get(args.dsn_env, "").strip()
    api_key = os.environ.get(args.api_key_env, "").strip()
    if not dsn or not api_key:
        print("error: service credentials are unavailable", file=sys.stderr)
        return 2
    if args.port < 1 or args.port > 65535 or args.lease_seconds < 1 or args.poll_seconds <= 0:
        print("error: invalid service limits", file=sys.stderr)
        return 2

    config = load_config(args.config.resolve())
    output_dir = args.output_dir.resolve()
    config["output_dir"] = output_dir
    config["raw_html_dir"] = output_dir / "raw_html"
    storage = PostgresWorkerStorage(
        dsn, tenant_id=args.tenant_id, subject_type=args.subject_type,
        default_lease_seconds=args.lease_seconds,
    )
    repository = PostgresCollectionRepository(dsn, tenant_id=args.tenant_id)
    worker = AgentRefreshWorker(
        storage=storage,
        adapter_factory=lambda: _build_http_adapter(config),
        config=config,
        poll_seconds=args.poll_seconds,
        lease_seconds=args.lease_seconds,
    )
    server = CollectionServer(
        (args.host, args.port),
        repository=repository,
        api_key=api_key,
        agent_rate_limit=args.agent_rate_limit,
        refresh_notifier=worker.notify,
        refresh_worker_status=worker.status,
    )
    worker.start()
    try:
        print(f"Agent Collection Service listening on http://{args.host}:{server.server_port}", flush=True)
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        worker.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
