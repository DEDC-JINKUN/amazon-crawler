#!/usr/bin/env python3
"""Fail-closed proxy capacity decisions backed by the latest PostgreSQL canary fact."""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import tomllib
import uuid
from pathlib import Path
from typing import Any

try:
    from proxy_canary import capacity_config_hash, capacity_resource_slot_ids, effective_slot_budget, reservation_slots_for
except ModuleNotFoundError:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from proxy_canary import capacity_config_hash, capacity_resource_slot_ids, effective_slot_budget, reservation_slots_for


class ProxyCapacityGateDenied(RuntimeError):
    """Stable, non-sensitive denial raised before any collection lease or request."""

    def __init__(self, reason: str, decision: dict[str, Any] | None = None):
        self.reason = str(reason)
        self.decision = dict(decision or {"status": "denied", "reason": self.reason})
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
    try:
        budget = effective_slot_budget(config)
    except (TypeError, ValueError):
        budget = 0
    required = math.ceil(requested_actions / budget) if budget > 0 else requested_actions
    if not list(config.get("proxy_session_ports") or []):
        return _decision("denied", "proxy_sessions_not_configured", requested_actions, required, None, None)
    try:
        expected_hash = capacity_config_hash(config)
        reservation_slots = reservation_slots_for(config, requested_actions)
    except (TypeError, ValueError):
        return _decision("denied", "capacity_configuration_invalid", requested_actions, required, None, None)
    return evaluate_capacity_snapshot(
        fact,
        expected_config_hash=expected_hash,
        slot_budget=budget,
        requested_actions=requested_actions,
        reservation_slots=reservation_slots,
    )


def evaluate_capacity_snapshot(
    fact: dict[str, Any] | None,
    *,
    expected_config_hash: str,
    slot_budget: int,
    requested_actions: int,
    reservation_slots: int,
) -> dict[str, Any]:
    budget = int(slot_budget)
    required = math.ceil(requested_actions / budget) if budget > 0 else requested_actions
    if fact is None:
        return _decision("denied", "capacity_evidence_missing", requested_actions, required, None, None)
    unique = fact.get("unique_egress_count")
    slot_capacity = fact.get("slot_capacity")
    if not bool(fact.get("is_fresh")):
        return _decision("denied", "capacity_evidence_stale", requested_actions, required, unique, slot_capacity)
    if str(fact.get("capacity_config_hash") or "") != expected_config_hash:
        return _decision("denied", "capacity_config_mismatch", requested_actions, required, unique, slot_capacity)
    canary_status = str(fact.get("canary_status") or "")
    fact_gate_status = str(fact.get("capacity_gate_status") or "")
    if canary_status not in {"succeeded", "partial"} or fact_gate_status != "allowed":
        fact_reason = str(fact.get("capacity_gate_reason") or "")
        reason = (
            "replacement_capacity_insufficient"
            if canary_status in {"succeeded", "partial"} and fact_reason == "replacement_capacity_insufficient"
            else
            fact_reason
            if unique is None and slot_capacity is None and fact_reason in {"credentials_missing", "database_credentials_missing"}
            else "canary_fact_denied"
        )
        return _decision("denied", reason, requested_actions, required, unique, slot_capacity)
    count_names = (
        "planned_slots", "tested_slots", "available_slots", "unique_egress_count",
        "duplicate_egress_count", "requested_capacity", "required_slots", "slot_budget", "slot_capacity",
    )
    counts: dict[str, int] = {}
    try:
        for name in count_names:
            raw = fact.get(name)
            if isinstance(raw, bool) or raw is None:
                raise ValueError
            counts[name] = int(raw)
    except (TypeError, ValueError):
        return _decision("denied", "capacity_fact_inconsistent", requested_actions, required, None, None)
    planned = counts["planned_slots"]
    tested = counts["tested_slots"]
    available = counts["available_slots"]
    unique = counts["unique_egress_count"]
    duplicates = counts["duplicate_egress_count"]
    fact_requested = counts["requested_capacity"]
    fact_required = counts["required_slots"]
    fact_budget = counts["slot_budget"]
    slot_capacity = counts["slot_capacity"]
    all_unique = tested == planned == available == unique and duplicates == 0
    consistent = bool(
        planned >= 1
        and tested == planned
        and 0 <= unique <= available <= tested
        and duplicates == available - unique
        and fact_requested >= 1
        and fact_budget == budget
        and fact_required == math.ceil(fact_requested / budget)
        and slot_capacity == unique * budget
        and ((canary_status == "succeeded" and all_unique) or (canary_status == "partial" and unique > 0 and not all_unique))
        and slot_capacity >= fact_requested
        and unique >= fact_required
        and str(fact.get("capacity_gate_reason") or "") == "capacity_sufficient"
    )
    if not consistent:
        return _decision("denied", "capacity_fact_inconsistent", requested_actions, required, unique, slot_capacity)
    if fact_requested < requested_actions:
        return _decision("denied", "canary_scope_insufficient", requested_actions, required, unique, slot_capacity)
    if unique < required or slot_capacity < requested_actions:
        reason = str(fact.get("capacity_gate_reason") or "unique_capacity_insufficient")
        if reason == "capacity_sufficient":
            reason = "unique_capacity_insufficient"
        return _decision("denied", reason, requested_actions, required, unique, slot_capacity)
    if unique < int(reservation_slots):
        return _decision("denied", "replacement_capacity_insufficient", requested_actions, required, unique, slot_capacity)
    return _decision("allowed", "capacity_sufficient", requested_actions, required, unique, slot_capacity)


def acquire_capacity_reservation(
    storage: Any,
    config: dict[str, Any],
    *,
    requested_actions: int,
    owner_id: str,
    lease_seconds: int,
    reservation_id: str | None = None,
) -> dict[str, Any]:
    if requested_actions < 1:
        raise ValueError("requested_actions must be positive")
    owner_id = str(owner_id or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", owner_id):
        raise ValueError("invalid capacity reservation owner")
    lease_seconds = int(lease_seconds)
    if lease_seconds < 1 or lease_seconds > 3600:
        raise ValueError("capacity reservation lease_seconds must be between 1 and 3600")
    reservation_id = str(reservation_id or f"capacity-{uuid.uuid4().hex}")
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", reservation_id):
        raise ValueError("invalid capacity reservation id")
    try:
        slot_budget = effective_slot_budget(config)
    except (TypeError, ValueError):
        raise ProxyCapacityGateDenied("capacity_configuration_invalid") from None
    if slot_budget < 1 or slot_budget > 5:
        raise ProxyCapacityGateDenied("capacity_configuration_invalid")
    required_slots = math.ceil(requested_actions / slot_budget) if slot_budget > 0 else requested_actions
    reservation_slots = reservation_slots_for(config, requested_actions)
    if reservation_slots > len(list(config.get("proxy_session_ports") or [])):
        raise ProxyCapacityGateDenied("replacement_capacity_insufficient")
    max_age_seconds = int(config.get("proxy_canary_max_age_seconds") or 3600)
    if max_age_seconds < 1 or max_age_seconds > 86400:
        raise ProxyCapacityGateDenied("capacity_configuration_invalid")
    credential_generation = str(
        config.get("proxy_credential_generation")
        or os.environ.get("AMAZON_PROXY_CREDENTIAL_GENERATION")
        or ""
    ).strip()
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{8,100}", credential_generation):
        raise ProxyCapacityGateDenied("credential_generation_missing")
    reserve = getattr(storage, "reserve_proxy_capacity", None)
    if not callable(reserve):
        raise ProxyCapacityGateDenied("capacity_reservation_unavailable")
    try:
        config_hash = capacity_config_hash(config)
    except (TypeError, ValueError):
        raise ProxyCapacityGateDenied("capacity_configuration_invalid") from None
    try:
        decision = reserve(
            reservation_id=reservation_id,
            owner_id=owner_id,
            capacity_config_hash=config_hash,
            credential_generation=credential_generation,
            resource_slot_ids=capacity_resource_slot_ids(config),
            requested_capacity=requested_actions,
            required_slots=required_slots,
            reservation_slots=reservation_slots,
            slot_budget=slot_budget,
            max_age_seconds=max_age_seconds,
            lease_seconds=lease_seconds,
        )
    except Exception:
        raise ProxyCapacityGateDenied("capacity_reservation_unavailable") from None
    if not isinstance(decision, dict) or decision.get("status") != "active":
        reason = str(decision.get("reason") if isinstance(decision, dict) else "capacity_reservation_denied")
        if not re.fullmatch(r"[a-z0-9_]{1,100}", reason):
            reason = "capacity_reservation_denied"
        raise ProxyCapacityGateDenied(reason, decision if isinstance(decision, dict) else None)
    if (
        int(decision.get("reserved_slots") or 0) < reservation_slots
        or len(list(decision.get("slot_ids") or [])) != int(decision.get("reserved_slots") or 0)
    ):
        raise ProxyCapacityGateDenied("capacity_reservation_scope_mismatch")
    return dict(decision)


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


def capacity_batch_actions(config: dict[str, Any], requested_actions: int, fact: dict[str, Any] | None = None) -> int:
    """A queue is not an instantaneous capacity request; keep reservations small."""
    if requested_actions < 1:
        raise ValueError("requested_actions must be positive")
    slots = len(config.get("proxy_session_ports") or [])
    per_asin = 1 + int(config.get("proxy_session_retry_per_asin", 1))
    size = min(requested_actions, 5, max(1, slots // max(1,per_asin)))
    if fact and type(fact.get('unique_egress_count')) is int and type(fact.get('requested_capacity')) is int:
        size = min(size,max(1,fact['unique_egress_count']//max(1,per_asin)),max(1,fact['requested_capacity']))
    return size


def reserve_capacity(
    config_path: Path,
    *,
    storage: Any,
    requested_actions: int,
    owner_id: str,
    lease_seconds: int,
    reservation_id: str | None = None,
) -> dict[str, Any]:
    document = tomllib.loads(config_path.read_text(encoding="utf-8"))
    config = dict(document.get("worker") or {})
    config["proxy_credential_generation"] = os.environ.get("AMAZON_PROXY_CREDENTIAL_GENERATION", "").strip()
    fact_reader = getattr(storage,"load_latest_proxy_capacity",None)
    fact = fact_reader(max_age_seconds=int(config.get('proxy_canary_max_age_seconds') or 3600)) if callable(fact_reader) else None
    return acquire_capacity_reservation(
        storage,
        config,
        requested_actions=capacity_batch_actions(config, requested_actions, fact),
        owner_id=owner_id,
        lease_seconds=lease_seconds,
        reservation_id=reservation_id,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--requested-actions", type=int, required=True)
    parser.add_argument("--dsn-env", default="AMAZON_US_POSTGRES_DSN")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--reserve", action="store_true")
    mode.add_argument("--release-reservation-id")
    parser.add_argument("--reservation-id")
    parser.add_argument("--reservation-owner")
    parser.add_argument("--lease-seconds", type=int, default=600)
    args = parser.parse_args(argv)
    dsn = os.environ.get(args.dsn_env, "").strip()
    if not dsn:
        result = _decision("denied", "database_credentials_missing", args.requested_actions, 0, None, None)
    else:
        try:
            from postgres_worker_storage import PostgresWorkerStorage

            storage = PostgresWorkerStorage(dsn, tenant_id=args.tenant_id)
            if args.release_reservation_id:
                if not args.reservation_owner:
                    raise ValueError("--reservation-owner is required for release")
                released = storage.release_proxy_capacity(args.release_reservation_id, args.reservation_owner)
                result = {
                    "status": "released", "reason": "capacity_released" if released else "capacity_already_inactive",
                    "reservation_id": args.release_reservation_id,
                }
            elif args.reserve:
                if not args.reservation_owner:
                    raise ValueError("--reservation-owner is required for reserve")
                result = reserve_capacity(
                    args.config,
                    storage=storage,
                    requested_actions=args.requested_actions,
                    owner_id=args.reservation_owner,
                    lease_seconds=args.lease_seconds,
                    reservation_id=args.reservation_id,
                )
            else:
                result = check_capacity(args.config, storage=storage, requested_actions=args.requested_actions)
        except ProxyCapacityGateDenied as exc:
            result = dict(exc.decision)
        except (OSError, RuntimeError, ValueError, KeyError, tomllib.TOMLDecodeError):
            result = _decision("denied", "capacity_gate_error", args.requested_actions, 0, None, None)
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0 if result["status"] in {"allowed", "active", "released"} else 3


__all__ = [
    "ProxyCapacityGateDenied",
    "acquire_capacity_reservation",
    "capacity_config_hash",
    "evaluate_capacity_fact",
    "evaluate_capacity_snapshot",
    "reservation_slots_for",
    "reserve_capacity",
]


if __name__ == "__main__":
    raise SystemExit(main())
