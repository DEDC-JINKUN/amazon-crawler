#!/usr/bin/env python3
"""Benchmark the parser against saved HTML without making network requests."""
from __future__ import annotations

import argparse
import importlib.util
import json
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def _load_worker():
    spec = importlib.util.spec_from_file_location("amazon_us_worker_benchmark", ROOT / "scripts" / "amazon_us_worker.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def benchmark(raw_html_dir: Path, repeat: int = 1) -> dict[str, Any]:
    if repeat < 1 or repeat > 10000:
        raise ValueError("repeat must be between 1 and 10000")
    files = sorted(raw_html_dir.glob("**/*.html"))
    if not files:
        raise FileNotFoundError(f"No HTML files found under {raw_html_dir}")
    payloads = [(path, path.read_text(encoding="utf-8", errors="replace")) for path in files]
    worker = _load_worker()
    started = time.perf_counter()
    page_count = 0
    byte_count = 0
    media_count = content_count = 0
    for _ in range(repeat):
        for path, html in payloads:
            data = worker.parse_product_html(html, "https://www.amazon.com/dp/UNKNOWN")
            page_count += 1
            byte_count += len(html.encode("utf-8"))
            media_count += len(data.get("media", []))
            content_count += len(data.get("content_modules", []))
    elapsed = time.perf_counter() - started
    return {
        "schema_version": "amazon-us-parser-benchmark-v1",
        "input_file_count": len(files),
        "repeat": repeat,
        "synthetic_repeat": repeat > 1,
        "pages_parsed": page_count,
        "bytes_parsed": byte_count,
        "media_records": media_count,
        "content_records": content_count,
        "elapsed_seconds": round(elapsed, 4),
        "pages_per_second": round(page_count / elapsed, 2) if elapsed else None,
        "megabytes_per_second": round(byte_count / elapsed / 1024 / 1024, 2) if elapsed else None,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-html-dir", type=Path, required=True)
    parser.add_argument("--repeat", type=int, default=1)
    args = parser.parse_args(argv)
    try:
        print(json.dumps(benchmark(args.raw_html_dir, args.repeat), ensure_ascii=False, indent=2))
    except (OSError, ValueError) as exc:
        print(f"error: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
