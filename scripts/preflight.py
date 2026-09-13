#!/usr/bin/env python3
"""Check local prerequisites before running the crawler."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shutil
import sys
from urllib.parse import urlsplit
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def _check(name: str, ok: bool, detail: str) -> dict[str, Any]:
    return {"name": name, "ok": bool(ok), "detail": detail}


def _validate_proxy_url(value: str) -> tuple[bool, str]:
    """Validate an explicit proxy without exposing credentials in diagnostics."""
    value = value.strip()
    if not value:
        return True, "direct"
    try:
        parts = urlsplit(value)
    except ValueError:
        return False, "invalid proxy URL"
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        return False, "proxy must be an explicit http(s) URL"
    try:
        _ = parts.port
    except ValueError:
        return False, "proxy port is invalid"
    if parts.username or parts.password:
        return False, "embedded proxy credentials are not allowed; use approved credential configuration"
    return True, f"configured {parts.scheme} proxy"


def _load_validator():
    path = ROOT / "scripts" / "validate_us_manifest.py"
    spec = importlib.util.spec_from_file_location("preflight_manifest_validator", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _load_egress_probe():
    path = ROOT / "scripts" / "check_egress.py"
    spec = importlib.util.spec_from_file_location("preflight_egress_probe", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _probe_postgres(dsn: str) -> tuple[bool, str]:
    try:
        from collection_storage import PostgresCollectionRepository
    except ModuleNotFoundError:
        sys.path.insert(0, str(ROOT / "scripts"))
        from collection_storage import PostgresCollectionRepository
    contract = PostgresCollectionRepository(dsn, tenant_id="amazon_us_local").load_schema_contract()
    expected = {
        "item_state": ["lease_expires_at", "lease_owner", "lease_token", "next_retry_at"],
        "collection_evidence": ["context_json", "transfer_bytes"],
    }
    return contract == expected, "schema ready" if contract == expected else "required worker schema columns are missing"


def run_preflight(manifest: Path, config: Path, db: Path, *, require_live: bool = False, probe_egress: bool = False, probe_target_url: str = "https://api.ipify.org?format=json", backend: str = "sqlite", dsn: str = "", postgres_probe=None) -> dict[str, Any]:
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
            proxy_ok, proxy_detail = _validate_proxy_url(str(loaded.get("proxy_url") or ""))
            checks.append(_check("proxy_url", proxy_ok, proxy_detail))
            username_env = str(loaded.get("proxy_username_env") or "").strip()
            password_env = str(loaded.get("proxy_password_env") or "").strip()
            checks.append(_check("proxy_credential_env", bool(username_env) == bool(password_env), "paired" if username_env and password_env else "not configured" if not username_env else "username/password environment names must be paired"))
            should_probe_egress = probe_egress or (require_live and bool(str(loaded.get("proxy_url") or "").strip()))
            if should_probe_egress:
                if not proxy_ok or not str(loaded.get("proxy_url") or "").strip():
                    checks.append(_check("proxy_probe", False, "probe requires a configured approved proxy_url"))
                elif username_env and password_env and (not os.environ.get(username_env) or not os.environ.get(password_env)):
                    checks.append(_check("proxy_probe", False, "configured proxy credential environment values are missing"))
                else:
                    try:
                        probe_result = _load_egress_probe().probe(
                            str(loaded["proxy_url"]),
                            probe_target_url,
                            timeout_seconds=int(loaded.get("request_timeout_seconds", 30)),
                            username=os.environ.get(username_env) if username_env else None,
                            password=os.environ.get(password_env) if password_env else None,
                        )
                        checks.append(_check("proxy_probe", bool(probe_result.get("ok")), json.dumps({key: probe_result.get(key) for key in ("status", "block_reason", "elapsed_ms", "response_bytes")}, ensure_ascii=False)))
                    except (OSError, ValueError) as exc:
                        checks.append(_check("proxy_probe", False, str(exc)))
            context = loaded.get("context", {})
            postal_code = str(context.get("postal_code") or "").strip()
            postal_valid = bool(re.fullmatch(r"\d{5}(?:-\d{4})?", postal_code))
            checks.append(_check("us_postal_code", not postal_code or postal_valid,
                                 "fixed ZIP configured" if postal_valid else "US marketplace; ZIP observation only" if not postal_code
                                 else "optional fixed ZIP must be 5 digits (or ZIP+4)"))
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
    if not driver_path.is_absolute():
        driver_path = ROOT / driver_path
    checks.append(_check("firefox", bool(firefox_path) or not require_live, firefox_path or "not found (optional until live mode)"))
    checks.append(_check("geckodriver", driver_path.exists() or not require_live, str(driver_path) if driver_path.exists() else "not found (run install_geckodriver.ps1)"))
    if backend == "postgres":
        if not dsn.strip():
            checks.append(_check("postgres", False, "PostgreSQL DSN is not configured"))
        else:
            try:
                ok, detail = (postgres_probe or _probe_postgres)(dsn)
                checks.append(_check("postgres", ok, detail))
            except Exception as exc:
                checks.append(_check("postgres", False, f"PostgreSQL check failed ({type(exc).__name__})"))
    else:
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
    parser.add_argument("--probe-egress", action="store_true", help="Probe configured proxy before allowing the run (network access)")
    parser.add_argument("--probe-target-url", default="https://api.ipify.org?format=json")
    parser.add_argument("--backend", choices=("postgres", "sqlite"), default="sqlite")
    parser.add_argument("--dsn-env", default="AMAZON_US_POSTGRES_DSN")
    args = parser.parse_args(argv)
    result = run_preflight(
        args.manifest, args.config, args.db, require_live=args.require_live,
        probe_egress=args.probe_egress, probe_target_url=args.probe_target_url,
        backend=args.backend, dsn=os.environ.get(args.dsn_env, "") if args.backend == "postgres" else "",
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
