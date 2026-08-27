#!/usr/bin/env python3
"""Create a redacted acceptance summary from verification JSON."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = ROOT / "data" / "amazon_us" / "verification" / "latest_verification.json"


def summarize(path: Path) -> tuple[dict, list[str]]:
    errors: list[str] = []
    if not path.exists():
        return {}, [f"verification JSON 不存在: {path}"]
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return {}, [f"verification JSON 无法读取: {exc}"]
    if not isinstance(payload, dict) or not payload.get("verification_version"):
        errors.append("verification JSON 缺少版本字段")
    if not payload.get("ok"):
        errors.extend(str(item) for item in payload.get("errors", []))
    state = payload.get("state", {})
    coverage = payload.get("coverage", {})
    blocked = payload.get("blocked", [])
    phase = payload.get("phase", "unknown")
    collection_phase = payload.get("collection_phase", "unknown")
    summary = {
        "verification_version": payload.get("verification_version", ""),
        "verified_at": payload.get("verified_at", ""),
        "ok": not errors,
        "phase": phase,
        "collection_phase": collection_phase,
        "collection_claim": "not_collected" if phase == "initialized" or collection_phase == "not_collected" else collection_phase,
        "manifest_count": payload.get("manifest", {}).get("count", 0),
        "state_count": state.get("count", 0),
        "status_counts": state.get("status_counts", {}),
        "missing_state_count": coverage.get("missing_state", 0),
        "extra_state_count": coverage.get("extra_state", 0),
        "blocked_count": len(blocked),
        "blocked_reasons": sorted({item.get("reason", "") for item in blocked if item.get("reason")}),
        "exhausted_failed_count": len(payload.get("exhausted_failed", [])),
        "output_counts": payload.get("outputs", {}),
        "error_count": len(errors),
    }
    return summary, errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verification", type=Path, default=DEFAULT_INPUT)
    args = parser.parse_args(argv)
    summary, errors = summarize(args.verification)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if errors:
        print("验收摘要失败:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
