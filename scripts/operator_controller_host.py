"""Run the fixed ordinary crawler controller with structured argv and projection checks."""
from __future__ import annotations

import argparse
import json
import os
import re
import stat
import subprocess
import sys
import time
import tomllib
from pathlib import Path

from proxy_capacity_gate import capacity_batch_actions


BATCH_RE = re.compile(r"batch-[0-9a-f]{24}")
TENANT_RE = re.compile(r"[A-Za-z0-9_.-]{1,120}")
CANARY_REASON_RE = re.compile(r"gate=(?:allowed|denied):([a-z0-9_]{1,100})")
RUN_CAPACITY_REASON_RE = re.compile(r"Capacity gate denied: ((?:capacity|replacement)_[a-z0-9_]{1,100})")
CANARY_TIMEOUT_SECONDS = 180


def _has_reparse_ancestor(path: Path) -> bool:
    current = Path(os.path.abspath(path))
    while True:
        try:
            file_attributes = getattr(os.lstat(current), "st_file_attributes", 0)
        except OSError:
            file_attributes = 0
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        if current.is_symlink() or bool(file_attributes & reparse_flag):
            return True
        if current.parent == current:
            return False
        current = current.parent


def build_controller_command(
    powershell: str,
    controller: Path,
    *,
    tenant_id: str,
    subject_type: str,
    manifest: Path,
    config: Path,
    batch_dir: Path,
    console_port: int,
    recovery_seconds: int,
    worker_count: int,
    limit: int,
) -> list[str]:
    command = [
        powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(controller), "run",
        "-TenantId", tenant_id, "-SubjectType", subject_type, "-ManifestPath", str(manifest), "-ConfigPath", str(config),
        "-OutputDir", str(batch_dir), "-Port", str(console_port),
        "-RecoveryMaxSeconds", str(recovery_seconds), "-WorkerCount", str(worker_count), "-Limit", str(limit),
    ]
    if limit > 100:
        command.append("-ConfirmLargeBatch")
    return command


def build_canary_command(
    powershell: str,
    controller: Path,
    *,
    tenant_id: str,
    config: Path,
    requested_actions: int,
) -> list[str]:
    worker_config = dict(tomllib.loads(config.read_text(encoding="utf-8")).get("worker") or {})
    canary_limit = capacity_batch_actions(worker_config, requested_actions)
    return [
        powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(controller), "canary",
        "-TenantId", tenant_id, "-ConfigPath", str(config), "-Limit", str(canary_limit),
    ]


def projection_command(
    mode: str,
    exporter: Path,
    *,
    tenant_id: str,
    subject_type: str,
    batch_id: str,
    batch_dir: Path,
    manifest: Path,
) -> list[str]:
    return [
        sys.executable, "-B", str(exporter), mode,
        "--tenant-id", tenant_id, "--subject-type", subject_type, "--batch-id", batch_id,
        "--batch-dir", str(batch_dir), "--manifest", str(manifest),
    ]


def _run_projection(command: list[str]) -> int:
    try:
        return subprocess.run(
            command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=15, shell=False,
        ).returncode
    except (OSError, subprocess.SubprocessError):
        return 4


def _stop_child(process: subprocess.Popen) -> None:
    try:
        process.terminate()
        process.wait(timeout=5)
    except (OSError, subprocess.SubprocessError):
        try:
            process.kill()
            process.wait(timeout=5)
        except (OSError, subprocess.SubprocessError):
            pass


def _wait_canary(process: subprocess.Popen, batch_dir: Path) -> tuple[int, str | None]:
    deadline = time.monotonic() + CANARY_TIMEOUT_SECONDS
    stop_path = batch_dir / "operator.stop.json"
    while process.poll() is None:
        if stop_path.is_file():
            _stop_child(process)
            return 130, "canary_cancelled"
        if time.monotonic() >= deadline:
            _stop_child(process)
            return 4, "canary_timeout"
        time.sleep(0.2)
    return int(process.returncode or 0), None


def _wait_controller_process(process: subprocess.Popen, batch_dir: Path, on_poll=None) -> tuple[int, bool]:
    stop_path = batch_dir / "operator.stop.json"
    while process.poll() is None:
        if stop_path.is_file():
            _stop_child(process)
            return 130, True
        time.sleep(2)
        if on_poll is not None:
            on_poll()
    return int(process.returncode or 0), False


def _canary_failure_reason(log_path: Path) -> str:
    try:
        text = log_path.read_bytes()[-32768:].decode("utf-8", errors="replace")
    except OSError:
        return "canary_failed"
    matches = CANARY_REASON_RE.findall(text)
    return f"canary_{matches[-1]}" if matches else "canary_failed"


def _run_failure_reason(batch_dir: Path) -> str | None:
    receipts = sorted((batch_dir / "control" / "runs").glob("run-*/receipt.json"), key=lambda path: path.stat().st_mtime)
    for receipt in reversed(receipts):
        try:
            payload = json.loads(receipt.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        match = RUN_CAPACITY_REASON_RE.fullmatch(str(payload.get("error") or ""))
        if match:
            return match.group(1)
    return None


def _release_stopped_capacity(release_root: Path, batch_dir: Path, config: Path, tenant_id: str) -> bool:
    gate = release_root / "scripts" / "proxy_capacity_gate.py"
    authorizations = sorted((batch_dir / "control" / "runs").glob("run-*/capacity.authorization.json"))
    released = []
    seen = set()
    for authorization in authorizations:
        try:
            payload = json.loads(authorization.read_text(encoding="utf-8-sig"))
            reservation_id = str(payload["reservation_id"])
            owner_id = str(payload["owner_id"])
            requested = int(payload.get("requested_capacity") or 1)
        except (OSError, ValueError, KeyError, TypeError):
            return False
        if (
            not re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", reservation_id)
            or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", owner_id)
            or requested < 1 or requested > 5000
        ):
            return False
        if reservation_id in seen:
            continue
        seen.add(reservation_id)
        try:
            completed = subprocess.run(
                [
                    sys.executable, "-B", str(gate), "--config", str(config), "--tenant-id", tenant_id,
                    "--requested-actions", str(requested), "--release-reservation-id", reservation_id,
                    "--reservation-owner", owner_id,
                ],
                cwd=release_root, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=30, shell=False,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        if completed.returncode != 0:
            return False
        released.append(reservation_id)
    marker = batch_dir / "control" / "operator-capacity-cleanup.json"
    temporary = marker.with_name(f".{marker.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps({
        "schema": "amazon-operator-capacity-cleanup-v1", "status": "released", "reservations": released,
    }, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    os.replace(temporary, marker)
    return True


def _publish_controller_error(batch_dir: Path, error: str) -> None:
    if not re.fullmatch(r"(?:canary|capacity|replacement)_[a-z0-9_]{1,100}", error):
        error = "canary_failed"
    path = batch_dir / "progress.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            payload = {}
    except (OSError, ValueError):
        payload = {}
    payload["status"] = "failed"
    payload["latest_error"] = error
    temporary = batch_dir / ".progress.controller.tmp"
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--release-root", type=Path, required=True)
    parser.add_argument("--operator-data-root", type=Path, required=True)
    parser.add_argument("--batch-dir", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--subject-type", choices=("own", "competitor", "candidate"), default="own")
    parser.add_argument("--batch-id", required=True)
    parser.add_argument("--worker-count", type=int, choices=(1, 2), required=True)
    parser.add_argument("--limit", type=int, choices=range(1, 5001), required=True)
    parser.add_argument("--console-port", type=int, choices=range(1, 65536), required=True)
    parser.add_argument("--recovery-seconds", type=int, choices=range(1, 5401), required=True)
    parser.add_argument("--powershell", required=True)
    args = parser.parse_args(argv)

    release_root = args.release_root.resolve()
    operator_data_root_lexical = Path(os.path.abspath(args.operator_data_root))
    batch_dir_lexical = Path(os.path.abspath(args.batch_dir))
    if _has_reparse_ancestor(operator_data_root_lexical) or _has_reparse_ancestor(batch_dir_lexical):
        return 2
    operator_data_root = operator_data_root_lexical.resolve()
    batch_dir = args.batch_dir.resolve()
    manifest = args.manifest.resolve()
    config = args.config.resolve()
    if not BATCH_RE.fullmatch(args.batch_id) or not TENANT_RE.fullmatch(args.tenant_id):
        return 2
    if (
        batch_dir.name != args.batch_id
        or batch_dir.parent != operator_data_root / "batches"
        or manifest != batch_dir / "manifest.csv"
        or config != batch_dir / "batch.toml"
    ):
        return 2
    for controlled_path in (
        batch_dir / "control",
        batch_dir / "control" / "console",
        batch_dir / "control" / "runs",
        batch_dir / "raw_html",
        manifest,
        config,
    ):
        if not controlled_path.exists() or _has_reparse_ancestor(Path(os.path.abspath(controlled_path))):
            return 2
    controller = (release_root / "crawler.ps1").resolve()
    exporter = (release_root / "scripts" / "operator_export.py").resolve()
    try:
        powershell = Path(args.powershell).resolve()
    except OSError:
        return 2
    if not all(path.is_file() for path in (manifest, config, controller, exporter, powershell)):
        return 2

    remaining = args.limit
    chunk_index = 0
    controller_exit = 0
    controller_error = None
    while remaining > 0:
        chunk_index += 1
        chunk_limit = min(500, remaining)
        chunk_workers = 1 if chunk_limit < 2 else args.worker_count
        command = build_controller_command(
            str(powershell), controller,
            tenant_id=args.tenant_id, subject_type=args.subject_type, manifest=manifest, config=config, batch_dir=batch_dir,
            console_port=args.console_port, recovery_seconds=args.recovery_seconds,
            worker_count=chunk_workers, limit=chunk_limit,
        )
        controller_environment = os.environ.copy()
        controller_environment["AMAZON_CRAWLER_ALLOWED_DATA_ROOT"] = str(batch_dir)
        stage = "canary_setup"
        try:
            canary_command = build_canary_command(
                str(powershell), controller, tenant_id=args.tenant_id, config=config,
                requested_actions=chunk_limit,
            )
            canary_stdout_path = batch_dir / f"canary-{chunk_index}.stdout.log"
            canary_stderr_path = batch_dir / f"canary-{chunk_index}.stderr.log"
            with canary_stdout_path.open("ab") as stdout, canary_stderr_path.open("ab") as stderr:
                canary = subprocess.Popen(
                    canary_command, cwd=release_root, stdout=stdout, stderr=stderr,
                    shell=False, env=controller_environment,
                )
                controller_exit, controller_error = _wait_canary(canary, batch_dir)
            if controller_exit != 0:
                controller_error = controller_error or _canary_failure_reason(canary_stdout_path)
                break
            stage = "run"
            with (batch_dir / f"crawler-{chunk_index}.stdout.log").open("ab") as stdout, (batch_dir / f"crawler-{chunk_index}.stderr.log").open("ab") as stderr:
                process = subprocess.Popen(
                    command, cwd=release_root, stdout=stdout, stderr=stderr,
                    shell=False, env=controller_environment,
                )
                controller_exit, _operator_stopped = _wait_controller_process(
                    process, batch_dir, on_poll=lambda: _run_projection(projection_command(
                        "status", exporter, tenant_id=args.tenant_id, subject_type=args.subject_type, batch_id=args.batch_id,
                        batch_dir=batch_dir, manifest=manifest,
                    )),
                )
                if _operator_stopped and not _release_stopped_capacity(release_root, batch_dir, config, args.tenant_id):
                    controller_error = "capacity_release_failed"
        except (OSError, ValueError, tomllib.TOMLDecodeError):
            controller_exit = 4
            if stage == "canary_setup":
                controller_error = "canary_setup_failed"
        status_exit = _run_projection(projection_command(
            "status", exporter, tenant_id=args.tenant_id, subject_type=args.subject_type, batch_id=args.batch_id,
            batch_dir=batch_dir, manifest=manifest,
        ))
        if controller_exit != 0 or status_exit != 0:
            if controller_exit != 0:
                controller_error = controller_error or _run_failure_reason(batch_dir)
            if controller_exit == 0:
                controller_exit = 4
            break
        remaining -= chunk_limit

    finalize_exit = _run_projection(projection_command(
        "finalize", exporter, tenant_id=args.tenant_id, subject_type=args.subject_type, batch_id=args.batch_id,
        batch_dir=batch_dir, manifest=manifest,
    ))
    if finalize_exit != 0 and controller_exit == 0:
        controller_exit = 4
    if controller_exit != 0 and controller_error:
        _publish_controller_error(batch_dir, controller_error)
    return controller_exit


if __name__ == "__main__":
    raise SystemExit(main())
