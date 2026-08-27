"""Pluggable raw HTML storage boundary.

The MVP uses the local implementation. A future S3-compatible implementation
can provide the same ``put`` contract without changing worker or evidence
logic.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Protocol


class RawHtmlStore(Protocol):
    def put(self, run_id: str, asin: str, body: str) -> str: ...


class LocalRawHtmlStore:
    def __init__(self, root: Path):
        self.root = root

    def put(self, run_id: str, asin: str, body: str) -> str:
        digest = hashlib.sha256(body.encode()).hexdigest()
        safe_run_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", run_id)[:80] or "run"
        safe_asin = re.sub(r"[^A-Za-z0-9_.-]+", "_", asin)[:20] or "asin"
        relative = Path("US") / safe_asin / f"{safe_run_id}-{digest[:16]}.html"
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.tmp")
        temporary.write_text(body, encoding="utf-8")
        temporary.replace(path)
        return relative.as_posix()
