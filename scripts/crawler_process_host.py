#!/usr/bin/env python3
"""Run one child inside a Windows kill-on-close Job Object."""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9
SYNCHRONIZE = 0x00100000
PROCESS_QUERY_LIMITED_INFORMATION = 0x00001000
WAIT_OBJECT_0 = 0x00000000
WAIT_TIMEOUT = 0x00000102


class OwnerUnavailableError(RuntimeError):
    pass


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def create_kill_on_close_job():
    if os.name != "nt":
        return None
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.restype = ctypes.c_void_p
    kernel32.SetInformationJobObject.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    kernel32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        raise OSError(ctypes.get_last_error(), "CreateJobObjectW failed")
    information = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    information.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not kernel32.SetInformationJobObject(
        job,
        JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
        ctypes.byref(information),
        ctypes.sizeof(information),
    ):
        error = ctypes.get_last_error()
        kernel32.CloseHandle(job)
        raise OSError(error, "SetInformationJobObject failed")
    return kernel32, job


def wait_for_gate(gate: Path, cancel: Path, timeout_seconds: float = 30.0, owner_monitor=None) -> None:
    deadline = time.monotonic() + timeout_seconds
    while not gate.exists():
        if owner_monitor is not None and not owner_is_alive(owner_monitor):
            raise OwnerUnavailableError("controlled process owner stopped before the start gate")
        if cancel.exists():
            raise SystemExit(2)
        if time.monotonic() >= deadline:
            raise TimeoutError("process start gate timed out")
        time.sleep(0.05)


def open_owner_monitor(
    owner_pid: int | None,
    owner_start_time: str | None,
    heartbeat_path: str | None = None,
    heartbeat_timeout_seconds: float = 15.0,
):
    if owner_pid is None:
        return None
    if owner_pid <= 0 or not str(owner_start_time or "").strip():
        raise RuntimeError("controlled process owner identity is incomplete")
    if os.name != "nt":
        try:
            os.kill(owner_pid, 0)
        except OSError as exc:
            raise RuntimeError("controlled process owner is unavailable") from exc
        process_monitor = owner_pid
        return {
            "process": process_monitor,
            "heartbeat": Path(heartbeat_path) if heartbeat_path else None,
            "heartbeat_timeout_seconds": heartbeat_timeout_seconds,
        }
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel32.WaitForSingleObject.restype = ctypes.c_uint32
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    class FILETIME(ctypes.Structure):
        _fields_ = [("dwLowDateTime", ctypes.c_uint32), ("dwHighDateTime", ctypes.c_uint32)]
    kernel32.GetProcessTimes.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(FILETIME),
        ctypes.POINTER(FILETIME),
        ctypes.POINTER(FILETIME),
        ctypes.POINTER(FILETIME),
    ]
    kernel32.GetProcessTimes.restype = ctypes.c_int
    handle = kernel32.OpenProcess(SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION, False, owner_pid)
    if not handle:
        raise RuntimeError("controlled process owner is unavailable")
    if kernel32.WaitForSingleObject(handle, 0) != WAIT_TIMEOUT:
        kernel32.CloseHandle(handle)
        raise RuntimeError("controlled process owner is not running")
    creation = FILETIME()
    unused_exit = FILETIME()
    unused_kernel = FILETIME()
    unused_user = FILETIME()
    if not kernel32.GetProcessTimes(
        handle, ctypes.byref(creation), ctypes.byref(unused_exit), ctypes.byref(unused_kernel), ctypes.byref(unused_user)
    ):
        kernel32.CloseHandle(handle)
        raise RuntimeError("controlled process owner identity cannot be verified")
    try:
        expected = datetime.fromisoformat(str(owner_start_time).replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError as exc:
        kernel32.CloseHandle(handle)
        raise RuntimeError("controlled process owner start time is invalid") from exc
    actual_ticks = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
    expected_ticks = int((expected.timestamp() + 11644473600) * 10_000_000)
    if abs(actual_ticks - expected_ticks) > 100:
        kernel32.CloseHandle(handle)
        raise RuntimeError("controlled process owner identity does not match")
    return {
        "process": (kernel32, handle),
        "heartbeat": Path(heartbeat_path) if heartbeat_path else None,
        "heartbeat_timeout_seconds": heartbeat_timeout_seconds,
    }


def owner_is_alive(monitor) -> bool:
    if monitor is None:
        return True
    process_monitor = monitor["process"]
    if os.name == "nt":
        kernel32, handle = process_monitor
        process_alive = kernel32.WaitForSingleObject(handle, 0) == WAIT_TIMEOUT
    else:
        try:
            os.kill(int(process_monitor), 0)
        except OSError:
            process_alive = False
        else:
            process_alive = True
    if not process_alive:
        return False
    heartbeat = monitor.get("heartbeat")
    if heartbeat is None:
        return True
    try:
        age_seconds = time.time() - heartbeat.stat().st_mtime
    except OSError:
        return False
    return age_seconds <= float(monitor["heartbeat_timeout_seconds"])


def close_owner_monitor(monitor) -> None:
    if monitor is not None and os.name == "nt":
        kernel32, handle = monitor["process"]
        kernel32.CloseHandle(handle)


def terminate_process(process: subprocess.Popen, timeout_seconds: float = 5.0) -> int | None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=timeout_seconds)
    return process.returncode


def finalize_interrupted_run(lifecycle: dict, worker_exit_code: int | None, python_executable: str) -> None:
    script = Path(__file__).with_name("postgres_run_ledger.py")
    command = [
        python_executable, str(script), "interrupt", "--dsn-env", str(lifecycle.get("dsn_env") or "AMAZON_US_POSTGRES_DSN"),
        "--tenant-id", str(lifecycle["tenant_id"]), "--run-id", str(lifecycle["run_id"]),
        "--command", str(lifecycle["command"]), "--requested-actions", str(lifecycle["requested_actions"]),
        "--receipt", str(lifecycle["receipt_path"]),
    ]
    if worker_exit_code is not None:
        command.extend(["--worker-exit-code", str(worker_exit_code)])
    if lifecycle.get("operation_id"):
        command.extend(["--operation-id", str(lifecycle["operation_id"])])
    completed = subprocess.run(command, cwd=str(Path(__file__).resolve().parents[1]), timeout=30)
    if completed.returncode != 0:
        raise RuntimeError(f"interrupted run finalization failed with exit code {completed.returncode}")


def wait_for_process(process: subprocess.Popen, owner_monitor, lifecycle: dict | None, python_executable: str) -> int:
    if owner_monitor is None:
        return process.wait()
    while process.poll() is None:
        if not owner_is_alive(owner_monitor):
            worker_exit_code = terminate_process(process)
            if lifecycle:
                finalize_interrupted_run(lifecycle, worker_exit_code, python_executable)
            return 130
        time.sleep(0.1)
    return int(process.returncode or 0)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", type=Path, required=True)
    args = parser.parse_args()
    request = json.loads(args.request.read_text(encoding="utf-8-sig"))
    gate = Path(request["gate_path"])
    cancel = Path(request["cancel_path"])
    owner_monitor = open_owner_monitor(
        request.get("owner_pid"),
        request.get("owner_start_time"),
        request.get("owner_heartbeat_path"),
        float(request.get("owner_heartbeat_timeout_seconds") or 15.0),
    )
    try:
        wait_for_gate(gate, cancel, owner_monitor=owner_monitor)
    except OwnerUnavailableError:
        try:
            if request.get("lifecycle"):
                finalize_interrupted_run(request["lifecycle"], None, str(request["python"]))
        finally:
            close_owner_monitor(owner_monitor)
        return 130
    except BaseException:
        close_owner_monitor(owner_monitor)
        raise
    command = [str(request["python"]), *[str(value) for value in request["arguments"]]]
    process = subprocess.Popen(command, cwd=request["working_directory"])
    job_info = create_kill_on_close_job()
    if job_info is not None:
        kernel32, job = job_info
        if not kernel32.AssignProcessToJobObject(job, ctypes.c_void_p(int(process._handle))):
            error = ctypes.get_last_error()
            process.kill()
            process.wait()
            kernel32.CloseHandle(job)
            raise OSError(error, "AssignProcessToJobObject failed")
    else:
        kernel32 = job = None

    def terminate_child(_signum, _frame):
        if process.poll() is None:
            process.terminate()

    signal.signal(signal.SIGINT, terminate_child)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, terminate_child)
    try:
        return wait_for_process(process, owner_monitor, request.get("lifecycle"), str(request["python"]))
    finally:
        if job is not None:
            kernel32.CloseHandle(job)
        close_owner_monitor(owner_monitor)


if __name__ == "__main__":
    raise SystemExit(main())
