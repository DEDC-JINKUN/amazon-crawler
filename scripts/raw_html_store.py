"""Pluggable raw HTML storage boundary.

The MVP uses the local implementation. A future S3-compatible implementation
can provide the same ``put`` contract without changing worker or evidence
logic.
"""
from __future__ import annotations

import gzip
import hashlib
import re
import uuid
from pathlib import Path
from typing import Protocol


class RawHtmlStore(Protocol):
    def put(self, run_id: str, asin: str, body: str) -> str: ...


def read_raw_html(path: Path) -> str:
    """Read both new gzip evidence and pre-existing plain HTML evidence."""
    if path.name.endswith(".gz"):
        with gzip.open(path, "rt", encoding="utf-8", errors="replace") as handle:
            return handle.read()
    return path.read_text(encoding="utf-8", errors="replace")


class LocalRawHtmlStore:
    def __init__(self, root: Path):
        self.root = root

    def put(self, run_id: str, asin: str, body: str) -> str:
        """Atomically keep UTF-8 source as content-addressed gzip evidence.

        ``run_id`` remains part of the database evidence metadata.  It is not
        part of the filename: identical HTML for the same ASIN is retained once
        and any legacy ``.html`` evidence remains readable in place.
        """
        del run_id
        encoded = body.encode("utf-8")
        digest = hashlib.sha256(encoded).hexdigest()
        safe_asin = re.sub(r"[^A-Za-z0-9_.-]+", "_", asin)[:20] or "asin"
        relative = Path("US") / safe_asin / f"{digest}.html.gz"
        path = self.root / relative
        if path.exists():
            try:
                if hashlib.sha256(read_raw_html(path).encode("utf-8")).hexdigest() == digest:
                    return relative.as_posix()
            except OSError:
                pass
            raise OSError("existing raw evidence does not match its content address")
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        with gzip.open(temporary, "wb", compresslevel=6) as handle:
            handle.write(encoded)
        temporary.replace(path)
        return relative.as_posix()
