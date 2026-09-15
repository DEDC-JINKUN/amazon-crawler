#!/usr/bin/env python3
"""Loopback Collection API with a bounded refresh-only PostgreSQL worker."""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import subprocess
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
from collection_console import ConsoleServer, PostgresConsoleRepository  # noqa: E402
from collection_storage import PostgresCollectionRepository  # noqa: E402
from campaign_worker_status import prepare_status_snapshot_launch, read_latest_status_snapshot  # noqa: E402
from postgres_worker_storage import PostgresWorkerStorage  # noqa: E402
from proxy_canary import reservation_slots_for  # noqa: E402
from proxy_capacity_gate import evaluate_capacity_fact  # noqa: E402
from product_watch.delivery_repository import WatchDeliveryRepository  # noqa: E402

MAX_AGENT_REFRESH_BATCH = 5

_CHILD_RUNTIME_ENV = ("SystemRoot", "WINDIR", "PATH", "PATHEXT", "TEMP", "TMP", "COMSPEC")


def _is_retryable_database_error(exc: Exception) -> bool:
    try:
        import psycopg
        from psycopg.errors import ConnectionTimeout
    except ImportError:
        return False
    if isinstance(exc, ConnectionTimeout):
        return True
    return isinstance(exc, psycopg.OperationalError) and str(getattr(exc, "sqlstate", "") or "").startswith("08")


def build_worker_environment(dsn_env: str, config: dict[str, Any]) -> dict[str, str]:
    """Return only the runtime, database, and proxy settings a queue worker needs."""
    names = set(_CHILD_RUNTIME_ENV)
    names.add(dsn_env)
    for key in ("proxy_username_env", "proxy_password_env"):
        value = config.get(key)
        if isinstance(value, str) and value.strip():
            names.add(value.strip())
    names.add("AMAZON_PROXY_CREDENTIAL_GENERATION")
    return {name: os.environ[name] for name in names if name in os.environ}


def worker_process_command(args: argparse.Namespace, worker_script: Path, output_dir: Path) -> list[str]:
    return [sys.executable, str(worker_script), '--config', str(args.config.resolve()), '--output-dir',
            str(output_dir), '--tenant-id', args.tenant_id, '--subject-type', args.subject_type,
            '--dsn-env', args.dsn_env, '--poll-seconds', str(args.poll_seconds),
            '--lease-seconds', str(args.lease_seconds)]

def effective_worker_count(requested: int, capacity_fact: dict[str, Any] | None, config: dict[str, Any] | None = None, *, process_limit: int = 8) -> int:
    requested=max(1,int(requested)); process_limit=max(1,min(int(process_limit),64))
    upper = min(requested, process_limit)
    if config:
        for count in range(upper, 0, -1):
            decision = evaluate_capacity_fact(
                capacity_fact, config,
                requested_actions=MAX_AGENT_REFRESH_BATCH * count,
            )
            if decision.get("status") == "allowed":
                return count
        return 1
    unique=(capacity_fact or {}).get('unique_egress_count')
    try: slots_per_worker=reservation_slots_for(config or {},MAX_AGENT_REFRESH_BATCH) if config else 2
    except (TypeError,ValueError): slots_per_worker=2
    capacity_limit=max(1,int(unique)//max(1,slots_per_worker)) if type(unique) is int and unique>0 else 1
    return min(requested,process_limit,capacity_limit)


def pool_capacity_decision(fact: dict[str, Any] | None, config: dict[str, Any], *, effective_workers: int) -> dict[str, Any]:
    return evaluate_capacity_fact(
        fact,
        config,
        requested_actions=MAX_AGENT_REFRESH_BATCH * max(1, int(effective_workers)),
    )


def worker_pool_status(
    worker: Any,
    processes: list[Any],
    *,
    requested: int,
    effective: int,
    capacity_decision: dict[str, Any] | None = None,
    pool_snapshot: dict[str, Any] | None = None,
    worker_heartbeat_timeout: float | None = None,
) -> dict[str, Any]:
    if worker is not None:
        status = dict(worker.status())
        alive = 1 if status.get("state") in {"starting", "running", "recovering", "blocked"} else 0
        if worker_heartbeat_timeout is not None and status.get("state") == "running":
            try:
                heartbeat = datetime.fromisoformat(str(status.get("worker_heartbeat_at") or "").replace("Z", "+00:00"))
                heartbeat_fresh = (
                    datetime.now(timezone.utc) - heartbeat.astimezone(timezone.utc)
                ).total_seconds() <= max(0.1, float(worker_heartbeat_timeout))
            except (TypeError, ValueError):
                heartbeat_fresh = False
            if not heartbeat_fresh:
                status["state"] = "blocked"
                status["last_error"] = "worker_heartbeat_stale"
    elif pool_snapshot is not None:
        status = dict(pool_snapshot)
        alive = int(status.get("alive_workers") or 0)
    else:
        alive = sum(process.poll() is None for process in processes)
        status = {"state": "running" if alive == effective else "failed"}
    status.update({
        "requested_workers": int(requested),
        "effective_workers": int(effective),
        "alive_workers": int(alive),
        "capacity_backpressure": int(effective) < int(requested),
    })
    if int(effective) < int(requested) and status.get("state") not in {"failed", "stopped"}:
        status["state"] = "blocked"
        status["last_error"] = "worker_capacity_downshifted"
    if capacity_decision is not None:
        status["capacity_decision"] = dict(capacity_decision)
        if capacity_decision.get("status") != "allowed" and status.get("state") not in {"failed", "stopped"}:
            status["state"] = "blocked"
            status["last_error"] = str(capacity_decision.get("reason") or "capacity_denied")
    return status


class CampaignWorkerSupervisor:
    """Supervise isolated queue consumers and aggregate fresh child heartbeats."""

    def __init__(
        self,
        command: list[str],
        *,
        count: int,
        cwd: Path,
        environment: dict[str, str],
        status_dir: Path,
        spawn: Callable[..., Any] = subprocess.Popen,
        monitor_interval: float = 1.0,
        heartbeat_timeout: float = 5.0,
        worker_heartbeat_timeout: float = 600.0,
        restart_limit: int = 2,
        graceful_stop_timeout: float = 0.25,
        exit_cleanup: Callable[[int, dict[str, Any]], Any] | None = None,
    ):
        self.command = list(command)
        self.count = max(1, int(count))
        self.cwd = Path(cwd).resolve()
        self.environment = dict(environment)
        self.status_dir = Path(status_dir).resolve()
        self.spawn = spawn
        self.monitor_interval = max(0.01, float(monitor_interval))
        self.heartbeat_timeout = max(0.1, float(heartbeat_timeout))
        self.worker_heartbeat_timeout = max(0.1, float(worker_heartbeat_timeout))
        self.restart_limit = max(0, int(restart_limit))
        self.graceful_stop_timeout = max(0.0, float(graceful_stop_timeout))
        self.exit_cleanup = exit_cleanup
        self._children: dict[int, Any] = {}
        self._states = {index: "created" for index in range(1, self.count + 1)}
        self._restart_counts = {index: 0 for index in range(1, self.count + 1)}
        self._launch_tokens = {index: "" for index in range(1, self.count + 1)}
        self._documents: dict[int, dict[str, Any]] = {index: {} for index in range(1, self.count + 1)}
        self._cleaned_launch_tokens: set[str] = set()
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _status_path(self, index: int) -> Path:
        return self.status_dir / f"worker-{index}.json"

    def _stop_path(self, index: int) -> Path:
        return self.status_dir / f"worker-{index}.stop"

    def _spawn_locked(self, index: int) -> None:
        path = self._status_path(index)
        prepare_status_snapshot_launch(path)
        self._stop_path(index).unlink(missing_ok=True)
        launch_token = uuid.uuid4().hex
        self._launch_tokens[index] = launch_token
        self._documents[index] = {}
        command = [
            *self.command,
            "--status-file", str(path),
            "--launch-token", launch_token,
            "--stop-file", str(self._stop_path(index)),
        ]
        self._children[index] = self.spawn(
            command,
            cwd=str(self.cwd),
            env=self.environment,
        )
        self._states[index] = "starting"

    def _cleanup_child_locked(self, index: int) -> bool:
        token = self._launch_tokens[index]
        if not token or token in self._cleaned_launch_tokens or self.exit_cleanup is None:
            return True
        try:
            self.exit_cleanup(index, dict(self._documents[index]))
        except Exception:
            self._states[index] = "failed"
            return False
        self._cleaned_launch_tokens.add(token)
        return True

    def start(self) -> None:
        self.status_dir.mkdir(parents=True, exist_ok=True)
        with self._lock:
            if self._thread is not None:
                raise RuntimeError("campaign worker supervisor is already started")
            try:
                for index in range(1, self.count + 1):
                    self._spawn_locked(index)
                self._thread = threading.Thread(
                    target=self._monitor,
                    name="campaign-worker-supervisor",
                    daemon=True,
                )
                self._thread.start()
            except Exception:
                processes = list(self._children.values())
                self._children.clear()
                self._thread = None
                for process in processes:
                    if process.poll() is None:
                        process.terminate()
                for process in processes:
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
                raise

    def _monitor(self) -> None:
        while not self._stop.wait(self.monitor_interval):
            with self._lock:
                for index, process in list(self._children.items()):
                    return_code = process.poll()
                    if return_code is None:
                        observed = read_latest_status_snapshot(
                            self._status_path(index), launch_token=self._launch_tokens[index],
                        )
                        if observed is not None:
                            self._documents[index] = observed
                        document = self._documents[index]
                        now = datetime.now(timezone.utc)
                        heartbeat_fresh = self._timestamp_fresh(document.get("heartbeat_at"), now, self.heartbeat_timeout)
                        worker_heartbeat_fresh = self._timestamp_fresh(
                            document.get("worker_heartbeat_at"), now, self.worker_heartbeat_timeout,
                        )
                        document_matches = (
                            str(document.get("launch_token") or "") == self._launch_tokens[index]
                        )
                        if (
                            document_matches and heartbeat_fresh
                            and str(document.get("state") or "") == "running"
                            and not worker_heartbeat_fresh
                        ):
                            self._states[index] = "stalled"
                            try:
                                process.terminate()
                            except OSError:
                                pass
                        continue
                    if not self._cleanup_child_locked(index):
                        continue
                    if self._states[index] in {"blocked", "failed"}:
                        continue
                    if int(return_code) == 3:
                        self._states[index] = "blocked"
                    elif self._restart_counts[index] < self.restart_limit:
                        self._restart_counts[index] += 1
                        self._states[index] = "restarting"
                        try:
                            self._spawn_locked(index)
                        except OSError:
                            self._states[index] = "failed"
                    else:
                        self._states[index] = "failed"

    @staticmethod
    def _timestamp_fresh(value: Any, now: datetime, timeout: float) -> bool:
        try:
            observed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
            return (now - observed.astimezone(timezone.utc)).total_seconds() <= timeout
        except (TypeError, ValueError):
            return False

    def status(self) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        alive = 0
        healthy = 0
        child_states: list[dict[str, Any]] = []
        with self._lock:
            for index in range(1, self.count + 1):
                process = self._children[index]
                process_alive = process.poll() is None
                alive += int(process_alive)
                document = self._documents[index]
                heartbeat_fresh = self._timestamp_fresh(document.get("heartbeat_at"), now, self.heartbeat_timeout)
                worker_heartbeat_fresh = self._timestamp_fresh(
                    document.get("worker_heartbeat_at"), now, self.worker_heartbeat_timeout,
                )
                document_matches = (
                    str(document.get("launch_token") or "") == self._launch_tokens[index]
                )
                reported_state = (
                    str(document.get("state") or self._states[index])
                    if document_matches
                    else self._states[index]
                )
                if document_matches and reported_state in {"blocked", "failed"}:
                    self._states[index] = reported_state
                is_healthy = (
                    process_alive and document_matches and heartbeat_fresh
                    and worker_heartbeat_fresh and reported_state == "running"
                )
                snapshot_backlog_files = document.get("snapshot_backlog_files")
                if type(snapshot_backlog_files) is not int or snapshot_backlog_files < 0:
                    snapshot_backlog_files = None
                snapshot_cleanup_failures = document.get("snapshot_cleanup_failures")
                if type(snapshot_cleanup_failures) is not int or snapshot_cleanup_failures < 0:
                    snapshot_cleanup_failures = None
                snapshot_backlog_bytes = document.get("snapshot_backlog_bytes")
                if type(snapshot_backlog_bytes) is not int or snapshot_backlog_bytes < 0:
                    snapshot_backlog_bytes = None
                healthy += int(is_healthy)
                child_states.append({
                    "index": index,
                    "pid": int(process.pid),
                    "state": (
                        self._states[index]
                        if self._states[index] in {"blocked", "failed", "stalled"}
                        else reported_state
                    ),
                    "heartbeat_fresh": heartbeat_fresh,
                    "worker_heartbeat_fresh": worker_heartbeat_fresh,
                    "restart_count": self._restart_counts[index],
                    "status_snapshot_backlog_files": snapshot_backlog_files,
                    "status_snapshot_backlog_bytes": snapshot_backlog_bytes,
                    "status_snapshot_cleanup_failures": snapshot_cleanup_failures,
                    "last_error": (
                        "worker_heartbeat_stale"
                        if self._states[index] == "stalled"
                        else str(document.get("last_error") or "")[:120] or None
                    ),
                })
            states = {child["state"] for child in child_states}
            if "failed" in states:
                state = "failed"
            elif "blocked" in states:
                state = "blocked"
            elif healthy == self.count:
                state = "running"
            else:
                state = "starting"
            last_error = next((child["last_error"] for child in child_states if child["last_error"]), None)
            return {
                "state": state,
                "alive_workers": alive,
                "healthy_workers": healthy,
                "restart_count": sum(self._restart_counts.values()),
                "last_error": last_error,
                "children": child_states,
            }

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(1.0, self.monitor_interval * 2))
        with self._lock:
            processes = list(self._children.values())
            indexed_processes = list(self._children.items())
        self.status_dir.mkdir(parents=True, exist_ok=True)
        for index, process in indexed_processes:
            if process.poll() is None:
                self._stop_path(index).write_text("stop", encoding="ascii")
        deadline = time.monotonic() + self.graceful_stop_timeout
        while any(process.poll() is None for process in processes) and time.monotonic() < deadline:
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
        for process in processes:
            if process.poll() is None:
                process.terminate()
        for process in processes:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        with self._lock:
            for index in self._children:
                self._cleanup_child_locked(index)


def cleanup_worker_ownership(storage: Any, worker_id: str) -> dict[str, int]:
    """Release only database state owned by one exact exiting Worker."""
    reason = "agent_refresh_process_exit"
    return {
        "requeued": int(storage.requeue_claimed_refreshes(worker_id, reason)),
        "released_leases": int(storage.release_claimed_capacity_leases(worker_id, reason)),
        "released_reservations": int(storage.release_proxy_capacity_by_owner(worker_id)),
    }


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
        worker_id: str | None = None,
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
        self.worker_id = str(worker_id or f"agent-refresh-{os.getpid()}")
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
        self._worker_heartbeat_at: str | None = None

    def _heartbeat(self) -> None:
        with self._lock:
            self._worker_heartbeat_at = datetime.now(timezone.utc).isoformat()

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("refresh worker is already started")
        with self._lock:
            self._state = "starting"
            self._worker_heartbeat_at = datetime.now(timezone.utc).isoformat()
        self._thread = threading.Thread(target=self._run, name="agent-refresh-worker", daemon=True)
        self._thread.start()

    def notify(self) -> None:
        self._event.set()

    def stop(self, timeout_seconds: float = 30.0) -> bool:
        self._stop.set()
        self._event.set()
        if self._thread is not None:
            self._thread.join(timeout=max(0.1, timeout_seconds))
        stopped = self._thread is None or not self._thread.is_alive()
        with self._lock:
            self._state = "stopped" if stopped else "failed"
            if not stopped:
                self._last_error = "worker_stop_timeout"
        return stopped

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
                "worker_heartbeat_at": self._worker_heartbeat_at,
            }

    def _run(self) -> None:
        worker_id = self.worker_id
        database_failures = 0
        while not self._stop.is_set():
            self._heartbeat()
            try:
                self._run_until_stopped(worker_id)
                return
            except Exception as exc:
                if _is_retryable_database_error(exc) and not self._stop.is_set():
                    database_failures += 1
                    with self._lock:
                        self._state = "recovering"
                        self._last_error = type(exc).__name__
                        self._last_capacity_decision = None
                    delay = min(30.0, max(self.poll_seconds, 0.25 * (2 ** min(database_failures - 1, 7))))
                    self._event.wait(delay)
                    self._event.clear()
                    self._heartbeat()
                    continue
                terminalizer = getattr(self.storage, "fail_claimed_refreshes", None)
                if callable(terminalizer):
                    try:
                        terminalizer(worker_id, "agent_refresh_worker_failed")
                    except Exception:
                        pass
                with self._lock:
                    self._state = "failed"
                    self._last_error = type(exc).__name__
                return

    def _run_until_stopped(self, worker_id: str) -> None:
        adapter = None
        try:
            self._heartbeat()
            self.storage.reclaim_expired_leases()
            requeue = getattr(self.storage, "requeue_claimed_refreshes", None)
            if callable(requeue):
                requeue(worker_id, "agent_refresh_database_recovered")
            adapter = self.adapter_factory()
            with self._lock:
                self._state = "running"
                self._last_error = None
                self._last_capacity_decision = None
            while not self._stop.is_set():
                self._heartbeat()
                with self._lock:
                    capacity_blocked = self._state == "blocked" and self._last_error != "access_blocked"
                if capacity_blocked:
                    self._event.wait(max(self.poll_seconds, 30.0))
                    self._event.clear()
                    self._heartbeat()
                    self.storage.reclaim_expired_leases()
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
                    self._heartbeat()
                    continue
                batch_limit = max(1, min(MAX_AGENT_REFRESH_BATCH, pending_count))
                run_id = f"agent-refresh-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}-{uuid.uuid4().hex[:8]}"
                self._refresh_storage.begin_batch_metrics()
                try:
                    self._heartbeat()
                    result = run_postgres_actions(
                        self._refresh_storage,
                        adapter,
                        self.config,
                        limit=batch_limit,
                        run_id=run_id,
                        worker_id=worker_id,
                        lease_seconds=self.lease_seconds,
                    )
                    self._heartbeat()
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
                        self._heartbeat()
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
                self._heartbeat()
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
    parser.add_argument("--campaign-max-asins", type=int, default=5000)
    parser.add_argument("--worker-count", type=int, default=1)
    parser.add_argument("--worker-process-limit", type=int, default=8)
    parser.add_argument("--console-port", type=int, default=0)
    parser.add_argument("--console-tenant-id", default="amazon_us_main")
    parser.add_argument("--operator-base-url")
    parser.add_argument("--shutdown-file", type=Path)
    return parser


def serve_until_shutdown(server: Any, shutdown_file: Path | None, *, poll_seconds: float = 0.2) -> None:
    if shutdown_file is None:
        server.serve_forever()
        return
    finished = threading.Event()

    def watch() -> None:
        while not finished.wait(max(0.01, poll_seconds)):
            if shutdown_file.exists():
                server.shutdown()
                return

    watcher = threading.Thread(target=watch, name="agent-service-shutdown", daemon=True)
    watcher.start()
    try:
        server.serve_forever()
    finally:
        finished.set()
        watcher.join(timeout=1)


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
    if (
        args.port < 1 or args.port > 65535 or args.lease_seconds < 1 or args.poll_seconds <= 0
        or args.console_port < 0 or args.console_port > 65535
        or (args.console_port and args.console_port == args.port)
    ):
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
    repository = PostgresCollectionRepository(dsn, tenant_id=args.tenant_id, subject_type=args.subject_type)
    fact_loader=getattr(storage,'load_latest_proxy_capacity',None)
    fact=fact_loader(max_age_seconds=int(config.get('proxy_canary_max_age_seconds') or 3600)) if callable(fact_loader) else None
    effective=effective_worker_count(args.worker_count,fact,config,process_limit=args.worker_process_limit)
    worker = AgentRefreshWorker(storage=storage,adapter_factory=lambda: _build_http_adapter(config),config=config,poll_seconds=args.poll_seconds,lease_seconds=args.lease_seconds) if effective==1 else None
    supervisor = None
    def pool_status():
        try:
            current_fact = fact_loader(
                max_age_seconds=int(config.get('proxy_canary_max_age_seconds') or 3600),
            ) if callable(fact_loader) else None
            decision = pool_capacity_decision(current_fact, config, effective_workers=effective)
        except Exception:
            decision = {"status": "denied", "reason": "capacity_status_unavailable"}
        return worker_pool_status(
            worker,
            [],
            requested=args.worker_count,
            effective=effective,
            capacity_decision=decision,
            pool_snapshot=supervisor.status() if supervisor is not None else None,
            worker_heartbeat_timeout=args.lease_seconds,
        )
    reporting_repository = WatchDeliveryRepository(dsn)
    server = None
    console_server = None
    console_thread = None
    console_started = False

    def cleanup_exited_child(_index: int, document: dict[str, Any]) -> None:
        pid = document.get("pid")
        if type(pid) is not int or pid <= 0:
            raise RuntimeError("campaign worker cleanup identity is unavailable")
        cleanup_worker_ownership(storage, f"agent-refresh-{pid}")

    try:
        server = CollectionServer(
            (args.host, args.port),
            repository=repository,
            api_key=api_key,
            agent_rate_limit=args.agent_rate_limit,
            refresh_notifier=worker.notify if worker is not None else (lambda:None),
            refresh_worker_status=pool_status,
            reporting_repository=reporting_repository,
            campaign_max_asins=args.campaign_max_asins,
        )
        if args.console_port:
            console_server = ConsoleServer(
                (args.host, args.console_port),
                PostgresConsoleRepository(dsn, args.console_tenant_id),
                api_key=api_key,
                watch_repository=reporting_repository,
                operator_base_url=args.operator_base_url,
            )
            console_thread = threading.Thread(target=console_server.serve_forever, name="primary-console", daemon=True)
            console_thread.start()
            console_started = True
        if effective>1:
            worker_script=Path(__file__).with_name('campaign_worker_process.py')
            worker_environment=build_worker_environment(args.dsn_env,config)
            command=worker_process_command(args,worker_script,output_dir)
            supervisor = CampaignWorkerSupervisor(
                command,
                count=effective,
                cwd=Path(__file__).resolve().parents[1],
                environment=worker_environment,
                status_dir=output_dir / 'control' / 'agent-workers',
                worker_heartbeat_timeout=args.lease_seconds,
                graceful_stop_timeout=35.0,
                exit_cleanup=cleanup_exited_child,
            )
            supervisor.start()
        if worker is not None: worker.start()
        print(f"Agent Collection Service listening on http://{args.host}:{server.server_port}", flush=True)
        serve_until_shutdown(server, args.shutdown_file.resolve() if args.shutdown_file else None)
    except KeyboardInterrupt:
        pass
    finally:
        if server is not None:
            server.server_close()
        if console_server is not None:
            if console_started:
                console_server.shutdown()
            console_server.server_close()
        if console_thread is not None:
            console_thread.join(timeout=5)
        if worker is not None: worker.stop()
        if supervisor is not None: supervisor.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
