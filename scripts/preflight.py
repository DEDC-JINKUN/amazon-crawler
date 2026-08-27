#!/usr/bin/env python3
"""Check local prerequisites before running the crawler."""
from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def _check(name: str, ok: bool, detail: str) -> dict[str, Any]:
    return {"name": name, "ok": bool(ok), "detail": detail}


def _load_validator():
    path = ROOT / "scripts" / "validate_us_manifest.py"
    spec = importlib.util.spec_from_file_location("preflight_manifest_validator", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def run_preflight(manifest: Path, config: Path, db: Path, *, require_live: bool = False) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    checks.append(_check("python", sys.version_info >= (3, 11), f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"))
    checks.append(_check("manifest", manifest.exists(), str(manifest)))
    if manifest.exists():
        try:
            errors = _load_validator().validate_manifest(manifest)
            checks.append(_check("manifest_schema", not errors, "; ".join(errors[:3]) if errors else "valid"))
        except (OSError, ValueError) as exc:
            checks.append(_check("manifest_schema", False, str(exc)))
    checks.append(_check("config", config.exists(), str(config)))
    if config.exists():
        try:
            worker_spec = importlib.util.spec_from_file_location("preflight_worker", ROOT / "scripts" / "amazon_us_worker.py")
            worker = importlib.util.module_from_spec(worker_spec)
            assert worker_spec.loader is not None
            worker_spec.loader.exec_module(worker)
            loaded = worker.load_config(config)
            checks.append(_check("user_agent", f"Agent/{loaded['agent_name']}" in loaded["user_agent"], "transparent"))
            context = loaded.get("context", {})
            postal_code = str(context.get("postal_code") or "").strip()
            checks.append(_check("us_postal_code", bool(postal_code) or not (require_live and str(context.get("expected_country") or "").upper() == "US"), "configured" if postal_code else "required for live US collection"))
        except (OSError, ValueError, KeyError) as exc:
            checks.append(_check("config_parse", False, str(exc)))
    selenium_available = importlib.util.find_spec("selenium") is not None
    configured_firefox = ""
    try:
        configured_firefox = str(loaded.get("firefox_binary") or "") if config.exists() else ""
    except UnboundLocalError:
        configured_firefox = ""
    firefox_candidates = [configured_firefox, "C:/Program Files/Mozilla Firefox/firefox.exe", "C:/Program Files (x86)/Mozilla Firefox/firefox.exe"]
    firefox_path = next((candidate for candidate in firefox_candidates if candidate and Path(candidate).exists()), None) or shutil.which("firefox") or shutil.which("firefox.exe")
    checks.append(_check("selenium", selenium_available or not require_live, "available" if selenium_available else "not installed (optional until live mode)"))
    configured_driver = ""
    try:
        configured_driver = str(loaded.get("geckodriver_path") or "") if config.exists() else ""
    except UnboundLocalError:
        configured_driver = ""
    driver_path = Path(configured_driver) if configured_driver else ROOT / "tools" / "geckodriver-v0.37.1" / "geckodriver.exe"
    checks.append(_check("firefox", bool(firefox_path) or not require_live, firefox_path or "not found (optional until live mode)"))
    checks.append(_check("geckodriver", driver_path.exists() or not require_live, str(driver_path) if driver_path.exists() else "not found (run install_geckodriver.ps1)"))
    parent_ready = db.parent.exists() or db.parent.parent.exists()
    checks.append(_check("database_parent", parent_ready, f"{db.parent} (will be created if missing)" if parent_ready and not db.parent.exists() else str(db.parent)))
    result = {"schema_version": "amazon-us-preflight-v1", "require_live": require_live, "ok": all(item["ok"] for item in checks), "checks": checks}
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=ROOT / "amazon_us_asin_manifest.csv")
    parser.add_argument("--config", type=Path, default=ROOT / "config" / "amazon_us.windows.toml")
    parser.add_argument("--db", type=Path, default=ROOT / "state" / "amazon_us.sqlite3")
    parser.add_argument("--require-live", action="store_true")
    args = parser.parse_args(argv)
    result = run_preflight(args.manifest, args.config, args.db, require_live=args.require_live)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
