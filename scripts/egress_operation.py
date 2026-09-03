#!/usr/bin/env python3
"""Run the approved egress health check without emitting credentials or proxy endpoints."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CANARY_URL = "https://api.ipify.org?format=json"


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def run_egress_check(
    config_path: Path,
    *,
    target_url: str = DEFAULT_CANARY_URL,
    probe: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    worker = _load_module("egress_operation_worker", ROOT / "scripts" / "amazon_us_worker.py")
    config = worker.load_config(config_path)
    proxy_url = str(config.get("proxy_url") or "").strip()
    username_env = str(config.get("proxy_username_env") or "").strip()
    password_env = str(config.get("proxy_password_env") or "").strip()
    if not proxy_url:
        raise ValueError("approved proxy is not configured")
    if bool(username_env) != bool(password_env):
        raise ValueError("proxy credential environment names must be paired")
    username = os.environ.get(username_env) if username_env else None
    password = os.environ.get(password_env) if password_env else None
    if username_env and (not username or not password):
        raise ValueError("configured proxy credential environment values are missing")
    if probe is None:
        probe = _load_module("egress_operation_probe", ROOT / "scripts" / "check_egress.py").probe
    result = probe(
        proxy_url,
        target_url,
        timeout_seconds=int(config.get("request_timeout_seconds") or 30),
        username=username,
        password=password,
    )
    return {
        "ok": bool(result.get("ok")),
        "status": result.get("status"),
        "error_class": result.get("block_reason"),
        "elapsed_ms": result.get("elapsed_ms"),
        "response_bytes": int(result["response_bytes"]) if result.get("response_bytes") is not None else None,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--target-url", default=DEFAULT_CANARY_URL)
    args = parser.parse_args(argv)
    try:
        result = run_egress_check(args.config, target_url=args.target_url)
    except (OSError, RuntimeError, ValueError, KeyError) as exc:
        result = {
            "ok": False,
            "status": None,
            "error_class": "configuration_error",
            "elapsed_ms": None,
            "response_bytes": None,
        }
        print(json.dumps(result, ensure_ascii=False))
        print(f"egress check failed ({type(exc).__name__})", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
