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

    def __getattr__(self, name: str) -> Any:
        return getattr(self._storage, name)

    def claim_task(self, *args, **kwargs) -> None:
        return None


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
        self.poll_seconds = max(0.05, float(poll_seconds))
        self.lease_seconds = max(1, int(lease_seconds))
        self._event = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._state = "created"
        self._completed_actions = 0
        self._last_error: str | None = None
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
                "completed_actions": self._completed_actions,
                "last_action_at": self._last_action_at,
                "last_error": self._last_error,
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
                run_id = f"agent-refresh-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}-{uuid.uuid4().hex[:8]}"
                try:
                    result = run_postgres_actions(
                        self._refresh_storage,
                        adapter,
                        self.config,
                        limit=MAX_AGENT_REFRESH_BATCH,
                        run_id=run_id,
                        worker_id=worker_id,
                        lease_seconds=self.lease_seconds,
                        enforce_capacity_gate=True,
                    )
                except ProxyCapacityGateDenied as exc:
                    with self._lock:
                        self._state = "blocked"
                        self._last_error = exc.reason
                    self._event.wait(self.poll_seconds)
                    self._event.clear()
                    continue
                with self._lock:
                    if self._state == "blocked":
                        self._state = "running"
                        self._last_error = None
                if result == -1:
                    with self._lock:
                        self._state = "blocked"
                        self._last_error = "access_blocked"
                    return
                if result > 0:
                    with self._lock:
                        self._completed_actions += result
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
