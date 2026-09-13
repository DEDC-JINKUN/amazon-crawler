"""Start the loopback Operator service with structured argv and detached stdio."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--service", type=Path, required=True)
    parser.add_argument("--working-directory", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--vault", type=Path, required=True)
    parser.add_argument("--stdout", type=Path, required=True)
    parser.add_argument("--stderr", type=Path, required=True)
    parser.add_argument("--port", type=int, choices=range(1, 65536), required=True)
    parser.add_argument("--internal-console-port", type=int, choices=range(1, 65536), required=True)
    parser.add_argument("--dashboard-port", type=int, choices=range(1, 65536), required=True)
    parser.add_argument("--business-tenant-id", required=True)
    parser.add_argument("--fake-runner", action="store_true")
    args = parser.parse_args(argv)
    python = args.python.resolve()
    service = args.service.resolve()
    root = args.working_directory.resolve()
    config = args.config.resolve()
    vault = args.vault.resolve()
    if not all(path.is_file() for path in (python, service, config, vault)) or service.parent != root / "scripts":
        return 2
    service_python = Path(getattr(sys, "_base_executable", sys.executable)).resolve()
    if not service_python.is_file():
        return 2
    data_root = args.data_root.resolve()
    data_root.mkdir(parents=True, exist_ok=True)
    command = [
        str(service_python), str(service), "--host", "127.0.0.1", "--port", str(args.port),
        "--data-root", str(data_root), "--config-path", str(config), "--vault-path", str(vault),
        "--internal-console-port", str(args.internal_console_port),
        "--dashboard-port", str(args.dashboard_port),
        "--business-tenant-id", args.business_tenant_id,
        "--worker-python", str(python),
    ]
    if args.fake_runner:
        command.append("--fake-runner")
    creationflags = 0
    if os.name == "nt":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    with args.stdout.open("ab") as stdout, args.stderr.open("ab") as stderr:
        process = subprocess.Popen(
            command,
            cwd=root,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            shell=False,
            close_fds=True,
            creationflags=creationflags,
            start_new_session=os.name != "nt",
        )
    print(json.dumps({"pid": process.pid}, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
