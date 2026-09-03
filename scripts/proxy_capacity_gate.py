#!/usr/bin/env python3
"""Fail-closed proxy capacity decisions backed by the latest PostgreSQL canary fact."""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import tomllib
from pathlib import Path
from typing import Any

try:
    from proxy_canary import capacity_config_hash
except ModuleNotFoundError:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from proxy_canary import capacity_config_hash


class ProxyCapacityGateDenied(RuntimeError):
    """Stable, non-sensitive denial raised before any collection lease or request."""

    def __init__(self, reason: str):
        self.reason = str(reason)
        super().__init__(f"proxy_capacity_gate_denied:{self.reason}")


def _decision(
    status: str,
    reason: str,
    requested_capacity: int,
    required_slots: int,
    available_unique_slots: int | None,
    slot_capacity: int | None,
) -> dict[str, Any]:
    return {
        "status": status,
        "reason": reason,
        "requested_capacity": requested_capacity,
        "required_slots": required_slots,
        "available_unique_slots": available_unique_slots,
        "slot_capacity": slot_capacity,
    }


def evaluate_capacity_fact(
    fact: dict[str, Any] | None,
    config: dict[str, Any],
    *,
    requested_actions: int,
) -> dict[str, Any]:
    if requested_actions < 1:
        raise ValueError("requested_actions must be positive")
    budget = int(config.get("proxy_session_max_asins") or 3)
    required = math.ceil(requested_actions / budget) if budget > 0 else requested_actions
    if not list(config.get("proxy_session_ports") or []):
        return _decision("denied", "proxy_sessions_not_configured", requested_actions, required, None, None)
    try:
        expected_hash = capacity_config_hash(config)
    except (TypeError, ValueError):
        return _decision("denied", "capacity_configuration_invalid", requested_actions, required, None, None)
    if fact is None:
        return _decision("denied", "capacity_evidence_missing", requested_actions, required, None, None)
    unique = fact.get("unique_egress_count")
    slot_capacity = fact.get("slot_capacity")
    if not bool(fact.get("is_fresh")):
        return _decision("denied", "capacity_evidence_stale", requested_actions, required, unique, slot_capacity)
    if str(fact.get("capacity_config_hash") or "") != expected_hash:
        return _decision("denied", "capacity_config_mismatch", requested_actions, required, unique, slot_capacity)
    if int(fact.get("requested_capacity") or 0) < requested_actions:
        return _decision("denied", "canary_scope_insufficient", requested_actions, required, unique, slot_capacity)
    if unique is None or slot_capacity is None:
        fact_reason = str(fact.get("capacity_gate_reason") or "")
        reason = fact_reason if fact_reason in {"credentials_missing", "database_credentials_missing"} else "capacity_unknown"
        return _decision("denied", reason, requested_actions, required, None, None)
    unique = int(unique)
    slot_capacity = int(slot_capacity)
    if unique < required or slot_capacity < requested_actions:
        reason = str(fact.get("capacity_gate_reason") or "unique_capacity_insufficient")
        if reason == "capacity_sufficient":
            reason = "unique_capacity_insufficient"
        return _decision("denied", reason, requested_actions, required, unique, slot_capacity)
    return _decision("allowed", "capacity_sufficient", requested_actions, required, unique, slot_capacity)


def enforce_capacity_gate(storage: Any, config: dict[str, Any], *, requested_actions: int) -> dict[str, Any]:
    reader = getattr(storage, "load_latest_proxy_capacity", None)
    try:
        max_age_seconds = int(config.get("proxy_canary_max_age_seconds") or 3600)
        if max_age_seconds < 1 or max_age_seconds > 86400:
            raise ValueError
    except (TypeError, ValueError):
        raise ProxyCapacityGateDenied("capacity_configuration_invalid") from None
    try:
        fact = reader(max_age_seconds=max_age_seconds) if callable(reader) else None
    except Exception:
        raise ProxyCapacityGateDenied("capacity_fact_unavailable") from None
    decision = evaluate_capacity_fact(fact, config, requested_actions=requested_actions)
    if decision["status"] != "allowed":
        raise ProxyCapacityGateDenied(decision["reason"])
    return decision


def check_capacity(
    config_path: Path,
    *,
    storage: Any,
    requested_actions: int,
) -> dict[str, Any]:
    document = tomllib.loads(config_path.read_text(encoding="utf-8"))
    config = dict(document.get("worker") or {})
    fact = storage.load_latest_proxy_capacity(max_age_seconds=int(config.get("proxy_canary_max_age_seconds") or 3600))
    return evaluate_capacity_fact(fact, config, requested_actions=requested_actions)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--requested-actions", type=int, required=True)
    parser.add_argument("--dsn-env", default="AMAZON_US_POSTGRES_DSN")
    args = parser.parse_args(argv)
    dsn = os.environ.get(args.dsn_env, "").strip()
    if not dsn:
        result = _decision("denied", "database_credentials_missing", args.requested_actions, 0, None, None)
    else:
        try:
            from postgres_worker_storage import PostgresWorkerStorage

            storage = PostgresWorkerStorage(dsn, tenant_id=args.tenant_id)
            result = check_capacity(args.config, storage=storage, requested_actions=args.requested_actions)
        except (OSError, RuntimeError, ValueError, KeyError, tomllib.TOMLDecodeError):
            result = _decision("denied", "capacity_gate_error", args.requested_actions, 0, None, None)
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0 if result["status"] == "allowed" else 3


__all__ = ["ProxyCapacityGateDenied", "capacity_config_hash", "enforce_capacity_gate", "evaluate_capacity_fact"]


if __name__ == "__main__":
    raise SystemExit(main())
