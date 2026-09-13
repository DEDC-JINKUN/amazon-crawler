"""Persistent, batch-scoped core for the ordinary crawler Operator."""
from __future__ import annotations

import csv
import ctypes
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from operator_xlsx import XlsxLimits, XlsxRejected, inspect_xlsx
from operator_lifecycle import capacity_cleanup_confirmed


ID_RE = re.compile(r"(?:import|batch)-[0-9a-f]{24}")
TENANT_RE = re.compile(r"[A-Za-z0-9_.-]{1,120}")
TERMINAL = {"completed", "failed", "stopped"}
ARTIFACTS = {
    "manifest": ("manifest.csv", "text/csv; charset=utf-8"),
    "results_csv": ("results.csv", "text/csv; charset=utf-8"),
    "results_json": ("results.json", "application/json"),
    "receipt": ("receipt.json", "application/json"),
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class OperatorError(ValueError):
    def __init__(self, code: str, status: int = 400):
        super().__init__(code)
        self.code = code
        self.status = status


class BatchConflict(OperatorError):
    def __init__(self, code: str = "another_batch_is_active"):
        super().__init__(code, 409)


@dataclass
class OperatorSettings:
    release_root: Path
    data_root: Path
    config_path: Path
    vault_path: Path
    python_executable: Path | None = None
    powershell_executable: str = "powershell.exe"
    port: int = 8772
    agent_port: int = 8765
    internal_console_port: int = 8773
    dashboard_port: int = 8770
    business_tenant_id: str = "amazon_us_main"
    recovery_max_seconds: int = 5400
    campaign_deadline_seconds: int = 86400
    session_seconds: int = 1200
    fake_runner: bool = False
    fake_delay_seconds: float = 0.05
    xlsx_limits: XlsxLimits = field(default_factory=XlsxLimits)

    def __post_init__(self):
        self.release_root = Path(self.release_root).resolve()
        self.data_root = Path(self.data_root).resolve()
        self.config_path = Path(self.config_path).resolve()
        self.vault_path = Path(self.vault_path).resolve()
        self.python_executable = Path(self.python_executable or sys.executable).resolve()
        if not all(1 <= int(value) <= 65535 for value in (self.port, self.agent_port, self.internal_console_port, self.dashboard_port)):
            raise ValueError("invalid port")
        if len({int(self.port), int(self.agent_port), int(self.internal_console_port), int(self.dashboard_port)}) != 4:
            raise ValueError("Operator, Collection, dashboard, and internal Console ports must differ")
        self.business_tenant_id = str(self.business_tenant_id).strip()
        if not TENANT_RE.fullmatch(self.business_tenant_id):
            raise ValueError("invalid business tenant id")
        if not 60 <= int(self.campaign_deadline_seconds) <= 604800:
            raise ValueError("invalid campaign deadline")


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    for attempt in range(6):
        try:
            os.replace(temporary, path)
            return
        except PermissionError:
            if attempt == 5:
                raise
            time.sleep(0.02 * (attempt + 1))


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _process_start_identity(pid: int) -> str | None:
    if os.name != "nt":
        try:
            return str(Path(f"/proc/{pid}/stat").read_text().split()[21])
        except (OSError, IndexError):
            return None
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return None
    class FILETIME(ctypes.Structure):
        _fields_ = [("low", ctypes.c_uint32), ("high", ctypes.c_uint32)]
    kernel32.GetProcessTimes.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(FILETIME), ctypes.POINTER(FILETIME),
        ctypes.POINTER(FILETIME), ctypes.POINTER(FILETIME),
    ]
    kernel32.GetProcessTimes.restype = ctypes.c_int
    created, exited, kernel, user = FILETIME(), FILETIME(), FILETIME(), FILETIME()
    try:
        if not kernel32.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(kernel), ctypes.byref(user)):
            return None
        return str((created.high << 32) | created.low)
    finally:
        kernel32.CloseHandle(handle)


def _process_matches(pid: int | None, identity: str | None) -> bool:
    return bool(pid and identity and _process_start_identity(int(pid)) == str(identity))


class DeploymentBatchMutex:
    """One deployment-wide ordinary batch; Windows uses a named kernel mutex."""
    def __init__(self, data_root: Path):
        digest = hashlib.sha256(str(data_root).lower().encode()).hexdigest()[:24]
        self.name = f"Local\\AmazonCrawlerOperator_{digest}"
        self.handle = None
        self.fd = None
        self.path = data_root / ".active-batch.lock"
        self._owner_thread = None
        self._owner_release = None

    def acquire(self) -> bool:
        if self.handle is not None or self.fd is not None:
            return True
        if os.name == "nt":
            acquired = threading.Event()
            released = threading.Event()
            outcome = []
            def own_mutex():
                kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
                kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
                kernel32.CreateMutexW.restype = ctypes.c_void_p
                kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
                kernel32.WaitForSingleObject.restype = ctypes.c_uint32
                kernel32.ReleaseMutex.argtypes = [ctypes.c_void_p]
                kernel32.ReleaseMutex.restype = ctypes.c_int
                kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
                handle = kernel32.CreateMutexW(None, False, self.name)
                ok = bool(handle) and kernel32.WaitForSingleObject(handle, 0) == 0
                outcome.append(ok)
                if ok:
                    self.handle = (kernel32, handle)
                elif handle:
                    kernel32.CloseHandle(handle)
                acquired.set()
                if ok:
                    released.wait()
                    kernel32.ReleaseMutex(handle)
                    kernel32.CloseHandle(handle)
                    self.handle = None
            owner = threading.Thread(target=own_mutex, daemon=True)
            owner.start()
            acquired.wait(2)
            if not outcome or not outcome[0]:
                return False
            self._owner_thread = owner
            self._owner_release = released
            return True
        try:
            self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.write(self.fd, str(os.getpid()).encode())
            return True
        except FileExistsError:
            return False

    def release(self) -> None:
        if self.handle is not None:
            self._owner_release.set()
            self._owner_thread.join(timeout=2)
            self._owner_release = None
            self._owner_thread = None
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass


class BatchManager:
    def __init__(self, settings: OperatorSettings):
        self.settings = settings
        self.settings.data_root.mkdir(parents=True, exist_ok=True)
        (self.settings.data_root / "imports").mkdir(exist_ok=True)
        (self.settings.data_root / "batches").mkdir(exist_ok=True)
        self._lock = threading.RLock()
        self._mutex = DeploymentBatchMutex(self.settings.data_root)
        self._processes: dict[str, subprocess.Popen] = {}
        self._recover()

    def _import_dir(self, import_id: str) -> Path:
        if not ID_RE.fullmatch(import_id) or not import_id.startswith("import-"):
            raise OperatorError("import_not_found", 404)
        return self.settings.data_root / "imports" / import_id

    def _batch_dir(self, batch_id: str) -> Path:
        if not ID_RE.fullmatch(batch_id) or not batch_id.startswith("batch-"):
            raise OperatorError("batch_not_found", 404)
        return self.settings.data_root / "batches" / batch_id

    def dashboard_url(self) -> str:
        tenant = quote(self.settings.business_tenant_id, safe="")
        return f"http://127.0.0.1:{self.settings.dashboard_port}/?tenant={tenant}"

    def _write_manifest(self, path: Path, rows: list[dict[str, Any]]) -> bytes:
        import io
        buffer = io.StringIO(newline="")
        writer = csv.DictWriter(buffer, fieldnames=["asin", "url", "marketplace", "source_site_label", "source_workbook"])
        writer.writeheader()
        for row in rows:
            asin = row["asin"]
            writer.writerow({
                "asin": asin,
                "url": f"https://www.amazon.com/dp/{asin}",
                "marketplace": "US",
                "source_site_label": "operator_upload",
                "source_workbook": "operator_import",
            })
        payload = buffer.getvalue().encode("utf-8-sig")
        path.write_bytes(payload)
        return payload

    def upload_preview(
        self,
        filename: str,
        payload: bytes,
        *,
        selected_sheet: str | None = None,
        selected_column: str | None = None,
        import_id: str | None = None,
    ) -> dict[str, Any]:
        if import_id is None:
            if not filename or "/" in filename or "\\" in filename or Path(filename).suffix.lower() != ".xlsx":
                raise OperatorError("unsupported_file_type")
            try:
                preview = inspect_xlsx(payload, self.settings.xlsx_limits, selected_sheet=selected_sheet, selected_column=selected_column)
            except XlsxRejected as exc:
                raise OperatorError(exc.code) from None
            import_id = f"import-{uuid.uuid4().hex[:24]}"
            directory = self._import_dir(import_id)
            directory.mkdir(parents=False)
            (directory / "source.xlsx").write_bytes(payload)
            source_hash = _sha256(payload)
        else:
            directory = self._import_dir(import_id)
            record = _read_json(directory / "import.json")
            if record is None:
                raise OperatorError("import_not_found", 404)
            try:
                payload = (directory / "source.xlsx").read_bytes()
            except OSError:
                raise OperatorError("import_not_found", 404) from None
            try:
                preview = inspect_xlsx(payload, self.settings.xlsx_limits, selected_sheet=selected_sheet, selected_column=selected_column)
            except XlsxRejected as exc:
                raise OperatorError(exc.code) from None
            source_hash = str(record["source_sha256"])
        record = {
            "import_id": import_id,
            "source_sha256": source_hash,
            "created_at": utc_now(),
            "preview": preview,
            "batch_id": None,
        }
        existing = _read_json(directory / "import.json")
        if existing and existing.get("batch_id"):
            record["batch_id"] = existing["batch_id"]
        _atomic_json(directory / "import.json", record)
        if preview["valid_rows"]:
            self._write_manifest(directory / "manifest.csv", preview["valid_rows"])
        return {"import_id": import_id, "source_sha256": source_hash, **preview}

    def _release_id(self) -> str:
        pointer = self.settings.release_root / "RELEASE_COMMIT"
        try:
            value = pointer.read_text(encoding="ascii").strip()
        except OSError:
            value = ""
        if re.fullmatch(r"[0-9a-f]{7,40}", value):
            return value
        try:
            completed = subprocess.run(
                ["git", "-C", str(self.settings.release_root), "rev-parse", "HEAD"],
                capture_output=True, timeout=5, check=True,
            )
            value = completed.stdout.decode("ascii", errors="ignore").strip()
        except (OSError, subprocess.SubprocessError):
            value = "unknown"
        return value if re.fullmatch(r"[0-9a-f]{7,40}", value) else "unknown"

    def _prepare_config(self, batch_dir: Path, manifest: Path) -> tuple[Path, str, str]:
        try:
            base = self.settings.config_path.read_bytes()
        except OSError:
            if self.settings.fake_runner:
                base = b"[worker]\nmax_actions_per_run=5\n[paths]\n"
            else:
                raise OperatorError("fixed_config_unavailable", 503) from None
        text = base.decode("utf-8-sig")
        paths = {
            "manifest": manifest.as_posix(),
            "output_dir": batch_dir.as_posix(),
            "raw_html_dir": (batch_dir / "raw_html").as_posix(),
        }
        lines = text.splitlines()
        output = []
        in_paths = False
        found_paths = False
        written = set()
        for line in lines:
            section = re.fullmatch(r"\s*\[([^]]+)\]\s*", line)
            if section:
                if in_paths:
                    for key in paths:
                        if key not in written:
                            output.append(f'{key} = {json.dumps(paths[key])}')
                in_paths = section.group(1).strip() == "paths"
                found_paths = found_paths or in_paths
                output.append(line)
                continue
            match = re.match(r"\s*(manifest|output_dir|raw_html_dir)\s*=", line) if in_paths else None
            if match:
                key = match.group(1)
                output.append(f'{key} = {json.dumps(paths[key])}')
                written.add(key)
            else:
                output.append(line)
        if not found_paths:
            output.append("[paths]")
        if in_paths or not found_paths:
            for key in paths:
                if key not in written:
                    output.append(f'{key} = {json.dumps(paths[key])}')
        rendered = ("\n".join(output).rstrip() + "\n").encode("utf-8")
        path = batch_dir / "batch.toml"
        path.write_bytes(rendered)
        return path, _sha256(base), _sha256(rendered)

    def _active_batch(self) -> dict[str, Any] | None:
        records = [_read_json(path) for path in (self.settings.data_root / "batches").glob("batch-*/batch.json")]
        active = [record for record in records if record and record.get("status") not in TERMINAL]
        return max(active, key=lambda item: str(item.get("started_at") or ""), default=None)

    def _build_runner_command(self, record: dict[str, Any], batch_dir: Path, manifest: Path, config: Path) -> list[str]:
        if self.settings.fake_runner:
            return [
                str(self.settings.python_executable),
                str(self.settings.release_root / "scripts" / "operator_fake_runner.py"),
                "--batch-dir", str(batch_dir),
                "--manifest", str(manifest),
                "--delay-seconds", str(self.settings.fake_delay_seconds),
            ]
        return [
            self.settings.powershell_executable,
            "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
            str(self.settings.release_root / "scripts" / "operator_secure_runner.ps1"),
            "-ReleaseRoot", str(self.settings.release_root),
            "-OperatorDataRoot", str(self.settings.data_root),
            "-BatchDir", str(batch_dir),
            "-ManifestPath", str(manifest),
            "-ConfigPath", str(config),
            "-VaultPath", str(self.settings.vault_path),
            "-TenantId", str(record["tenant_id"]),
            "-SubjectType", str(record["subject_type"]),
            "-BatchId", str(record["batch_id"]),
            "-WorkerCount", str(record["worker_count"]),
            "-Limit", str(record["requested"]),
            "-AgentPort", str(self.settings.agent_port),
            "-InternalConsolePort", str(self.settings.internal_console_port),
            "-CampaignDeadlineSeconds", str(self.settings.campaign_deadline_seconds),
            "-PythonPath", str(self.settings.python_executable),
        ]

    def start_batch(self, import_id: str, worker_count: int = 1, subject_type: str = "own") -> dict[str, Any]:
        if int(worker_count) not in {1, 2}:
            raise OperatorError("invalid_worker_count")
        worker_count = int(worker_count)
        subject_type = str(subject_type).strip().lower()
        if subject_type not in {"own", "competitor", "candidate"}:
            raise OperatorError("invalid_subject_type")
        with self._lock:
            import_dir = self._import_dir(import_id)
            imported = _read_json(import_dir / "import.json")
            if imported is None:
                raise OperatorError("import_not_found", 404)
            if imported.get("batch_id"):
                existing = self.get_batch(str(imported["batch_id"]))
                if int(existing["worker_count"]) != worker_count or str(existing.get("subject_type") or "own") != subject_type:
                    raise BatchConflict("idempotency_conflict")
                return existing
            preview = imported.get("preview") or {}
            if not preview.get("can_start"):
                raise OperatorError("invalid_rows_block_start")
            active = self._active_batch()
            if active is not None:
                raise BatchConflict()
            if not self._mutex.acquire():
                raise BatchConflict("deployment_batch_lock_busy")
            process = None
            batch_dir = None
            try:
                batch_id = f"batch-{uuid.uuid4().hex[:24]}"
                tenant_id = self.settings.business_tenant_id
                batch_dir = self._batch_dir(batch_id)
                batch_dir.mkdir(parents=False)
                (batch_dir / "raw_html").mkdir()
                (batch_dir / "control" / "console").mkdir(parents=True)
                (batch_dir / "control" / "runs").mkdir()
                manifest = batch_dir / "manifest.csv"
                manifest_payload = (import_dir / "manifest.csv").read_bytes()
                manifest.write_bytes(manifest_payload)
                config, base_config_hash, config_hash = self._prepare_config(batch_dir, manifest)
                started_at = utc_now()
                requested = int(preview["unique_count"])
                record = {
                    "schema_version": "amazon-operator-batch-v1",
                    "batch_id": batch_id,
                    "import_id": import_id,
                    "tenant_id": tenant_id,
                    "subject_type": subject_type,
                    "status": "starting",
                    "worker_count": worker_count,
                    "worker_count_experimental": worker_count == 2,
                    "requested": requested,
                    "claimed": 0,
                    "completed": 0,
                    "variant": 0,
                    "failed": 0,
                    "blocked": 0,
                    "pending": requested,
                    "latest_error": None,
                    "items": [{"asin": row["asin"], "status": "pending"} for row in preview["valid_rows"]],
                    "source_sha256": imported["source_sha256"],
                    "manifest_sha256": _sha256(manifest_payload),
                    "release_id": self._release_id(),
                    "base_config_sha256": base_config_hash,
                    "config_sha256": config_hash,
                    "started_at": started_at,
                    "finished_at": None,
                    "stop_requested": False,
                }
                _atomic_json(batch_dir / "batch.json", record)
                command = self._build_runner_command(record, batch_dir, manifest, config)
                gate = batch_dir / "runner.start.gate"
                gate.write_bytes(b"")
                request_path = batch_dir / "runner.request.json"
                request = {
                    "python": command[0],
                    "working_directory": str(self.settings.release_root),
                    "arguments": command[1:],
                    "gate_path": str(gate),
                    "cancel_path": str(batch_dir / "runner.cancel"),
                }
                _atomic_json(request_path, request)
                stdout = (batch_dir / "runner.stdout.log").open("ab")
                stderr = (batch_dir / "runner.stderr.log").open("ab")
                if self.settings.fake_runner:
                    process_command = command
                else:
                    process_command = [
                        str(self.settings.python_executable),
                        str(self.settings.release_root / "scripts" / "crawler_process_host.py"),
                        "--request", str(request_path),
                    ]
                process = subprocess.Popen(
                    process_command,
                    cwd=self.settings.release_root,
                    stdout=stdout,
                    stderr=stderr,
                    shell=False,
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" and self.settings.fake_runner else 0,
                    start_new_session=os.name != "nt" and self.settings.fake_runner,
                )
                stdout.close()
                stderr.close()
                identity = None
                for _ in range(20):
                    identity = _process_start_identity(process.pid)
                    if identity:
                        break
                    time.sleep(0.01)
                control = {
                    "pid": process.pid,
                    "process_start_identity": identity,
                    "argv_contract": [Path(command[0]).name, *[value for value in command[1:] if str(value).startswith("-")]],
                }
                _atomic_json(batch_dir / "control.json", control)
                record["status"] = "running"
                _atomic_json(batch_dir / "batch.json", record)
                imported["batch_id"] = batch_id
                _atomic_json(import_dir / "import.json", imported)
                self._processes[batch_id] = process
                threading.Thread(target=self._monitor, args=(batch_id, process), daemon=True).start()
                return self._public(record)
            except Exception:
                if process is not None and process.poll() is None:
                    try:
                        process.terminate()
                        process.wait(timeout=3)
                    except (OSError, subprocess.SubprocessError):
                        try:
                            process.kill()
                        except OSError:
                            pass
                if batch_dir is not None:
                    failed = _read_json(batch_dir / "batch.json")
                    if failed:
                        failed["status"] = "failed"
                        failed["latest_error"] = "runner_start_failed"
                        failed["finished_at"] = utc_now()
                        _atomic_json(batch_dir / "batch.json", failed)
                self._mutex.release()
                raise

    def _merge_progress(self, record: dict[str, Any], batch_dir: Path) -> dict[str, Any]:
        if record.get('status') in TERMINAL or record.get('status') == 'cleanup_pending':
            return record
        progress = _read_json(batch_dir / "progress.json") or {}
        allowed = {
            "status", "requested", "claimed", "completed", "variant", "failed", "blocked", "stopped", "pending",
            "latest_error", "items", "finished_at", "campaign_id", "campaign_status",
            "requested_workers", "effective_workers", "alive_workers",
        }
        for key in allowed:
            if key in progress:
                if key == "latest_error" and not progress[key]:
                    continue
                if key == "status" and record.get("status") in TERMINAL and progress.get("status") not in TERMINAL:
                    continue
                record[key] = progress[key]
        return record

    def _public(self, record: dict[str, Any]) -> dict[str, Any]:
        safe = {key: value for key, value in record.items() if not key.startswith("_")}
        try:
            started = datetime.fromisoformat(str(safe["started_at"]))
            end = datetime.fromisoformat(str(safe["finished_at"])) if safe.get("finished_at") else datetime.now(timezone.utc)
            safe["elapsed_seconds"] = max(0.0, round((end - started).total_seconds(), 3))
        except (KeyError, TypeError, ValueError):
            safe["elapsed_seconds"] = None
        safe["downloads"] = [name for name, (filename, _) in ARTIFACTS.items() if (self._batch_dir(str(record["batch_id"])) / filename).is_file()]
        safe["dashboard_url"] = self.dashboard_url()
        return safe

    def get_batch(self, batch_id: str | None = None) -> dict[str, Any]:
        with self._lock:
            if batch_id is None:
                records = [(_read_json(path), path) for path in (self.settings.data_root / "batches").glob("batch-*/batch.json")]
                records = [(record, path) for record, path in records if record]
                if not records:
                    raise OperatorError("batch_not_found", 404)
                _, batch_path = max(records, key=lambda item: str(item[0].get("started_at") or ""))
            else:
                batch_path = self._batch_dir(batch_id) / "batch.json"
            record = _read_json(batch_path)
            if record is None:
                raise OperatorError("batch_not_found", 404)
            stored_status = record.get("status")
            record = self._merge_progress(record, batch_path.parent)
            if stored_status not in TERMINAL and record.get("status") in TERMINAL:
                record["status"] = stored_status
            return self._public(record)

    def _finish_record(self, batch_id: str, exit_code: int | None) -> None:
        batch_dir = self._batch_dir(batch_id)
        record = _read_json(batch_dir / "batch.json")
        if record is None:
            return
        if record.get('status') in TERMINAL:
            return
        pending = record.get('_pending_finalization')
        if record.get('status') == 'cleanup_pending' and isinstance(pending, dict):
            # Resume the exact outcome captured when the child exited; a new
            # projection or restart must not replace that evidence.
            record = dict(pending['record'])
            exit_code = pending['exit_code']
        else:
            record = self._merge_progress(record, batch_dir)
        if not capacity_cleanup_confirmed(batch_dir):
            record['_pending_finalization'] = {'record': dict(record), 'exit_code': exit_code}
            record['status'] = 'cleanup_pending'
            record['latest_error'] = 'capacity_cleanup_unconfirmed'
            record['finished_at'] = None
            _atomic_json(batch_dir / 'batch.json', record)
            return
        resolved = sum(int(record.get(key) or 0) for key in ("completed", "variant", "failed", "blocked"))
        projection_complete = (
            record.get("status") == "completed"
            and int(record.get("requested") or 0) == resolved
            and int(record.get("pending") or 0) == 0
            and int(record.get("claimed") or 0) == 0
        )
        if record.get("stop_requested"):
            record["status"] = "stopped"
        elif exit_code == 0 and projection_complete:
            record["status"] = "completed"
        else:
            record["status"] = "failed"
            if not record.get("latest_error"):
                record["latest_error"] = "status_projection_unavailable" if exit_code == 0 else "runner_failed"
        record["finished_at"] = record.get("finished_at") or utc_now()
        receipt = _read_json(batch_dir / "receipt.json") or {}
        receipt.update({key: record.get(key) for key in (
            "schema_version", "batch_id", "tenant_id", "subject_type", "status", "worker_count", "requested", "claimed",
            "completed", "variant", "failed", "blocked", "pending", "source_sha256", "manifest_sha256",
            "release_id", "base_config_sha256", "config_sha256", "latest_error", "started_at", "finished_at",
        )})
        receipt["schema_version"] = "amazon-operator-receipt-v1"
        receipt["runner_exit_code"] = exit_code
        _atomic_json(batch_dir / "receipt.json", receipt)
        # batch.json terminal status is the commit marker: publish it only
        # after the final receipt and result files are durable.
        _atomic_json(batch_dir / "batch.json", record)

    def _monitor(self, batch_id: str, process: subprocess.Popen) -> None:
        try:
            exit_code = process.wait()
            self._wait_for_stopped_host_cleanup(batch_id)
            with self._lock:
                self._finish_record(batch_id, exit_code)
        finally:
            with self._lock:
                self._processes.pop(batch_id, None)
                self._mutex.release()

    def _watch_recovered(self, batch_id: str, pid: int, identity: str) -> None:
        try:
            while _process_matches(pid, identity):
                time.sleep(0.2)
            self._wait_for_stopped_host_cleanup(batch_id)
            with self._lock:
                self._finish_record(batch_id, None)
        finally:
            with self._lock:
                self._mutex.release()

    def _wait_for_stopped_host_cleanup(self, batch_id: str, timeout_seconds: float = 15.0) -> None:
        batch_dir = self._batch_dir(batch_id)
        if not (batch_dir / "operator.stop.json").is_file():
            return
        run_dirs = [path.parent for path in (batch_dir / "control" / "runs").glob("run-*/capacity.authorization.json")]
        if not run_dirs:
            return
        deadline = time.monotonic() + timeout_seconds
        marker = batch_dir / "control" / "operator-capacity-cleanup.json"
        while True:
            cleanup = _read_json(marker)
            if cleanup and cleanup.get("schema") == "amazon-operator-capacity-cleanup-v1" and cleanup.get("status") == "released":
                return
            if time.monotonic() >= deadline:
                return
            time.sleep(0.05)

    def _force_stop_after_grace(
        self, batch_id: str, process: subprocess.Popen | None, pid: Any, identity: Any,
        grace_seconds: float = 20.0,
    ) -> None:
        deadline = time.monotonic() + grace_seconds
        while time.monotonic() < deadline:
            if process is not None:
                if process.poll() is not None:
                    return
            elif not _process_matches(pid, identity):
                return
            time.sleep(0.2)
        try:
            if process is not None and process.poll() is None:
                process.terminate()
            elif process is None and _process_matches(pid, identity):
                os.kill(int(pid), signal.SIGTERM)
        except OSError:
            pass

    def _recover(self) -> None:
        with self._lock:
            active = self._active_batch()
            if active is None:
                return
            batch_id = str(active["batch_id"])
            control = _read_json(self._batch_dir(batch_id) / "control.json") or {}
            pid = control.get("pid")
            identity = control.get("process_start_identity")
            if _process_matches(pid, identity):
                if self._mutex.acquire():
                    threading.Thread(target=self._watch_recovered, args=(batch_id, int(pid), str(identity)), daemon=True).start()
                return
            self._finish_record(batch_id, None)

    def stop_batch(self, batch_id: str) -> dict[str, Any]:
        with self._lock:
            batch_dir = self._batch_dir(batch_id)
            record = _read_json(batch_dir / "batch.json")
            if record is None:
                raise OperatorError("batch_not_found", 404)
            if record.get("status") in TERMINAL:
                return self._public(record)
            control = _read_json(batch_dir / "control.json") or {}
            pid = control.get("pid")
            identity = control.get("process_start_identity")
            if not _process_matches(pid, identity):
                record["status"] = "failed"
                record["latest_error"] = "managed_process_unavailable"
                record["finished_at"] = utc_now()
                _atomic_json(batch_dir / "batch.json", record)
                self._mutex.release()
                return self._public(record)
            record["stop_requested"] = True
            _atomic_json(batch_dir / "batch.json", record)
            _atomic_json(batch_dir / "operator.stop.json", {
                "schema_version": "amazon-operator-stop-v1",
                "batch_id": batch_id,
                "requested_at": utc_now(),
                "reason": "operator_requested",
            })
            process = self._processes.get(batch_id)
            if self.settings.fake_runner:
                try:
                    if process is not None:
                        process.terminate()
                    else:
                        os.kill(int(pid), signal.SIGTERM)
                except OSError:
                    pass
            else:
                threading.Thread(
                    target=self._force_stop_after_grace,
                    args=(batch_id, process, pid, identity),
                    name=f"operator-stop-{batch_id}", daemon=True,
                ).start()
            return self._public(record)

    def download_artifact(self, batch_id: str, artifact: str) -> tuple[Path, str, str]:
        if artifact not in ARTIFACTS:
            raise OperatorError("artifact_not_found", 404)
        batch_dir = self._batch_dir(batch_id).resolve()
        filename, content_type = ARTIFACTS[artifact]
        path = (batch_dir / filename).resolve()
        if path.parent != batch_dir or not path.is_file():
            raise OperatorError("artifact_not_found", 404)
        return path, content_type, filename
