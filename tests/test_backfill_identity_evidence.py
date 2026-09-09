from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import tempfile


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "backfill_identity_evidence.py"


def load_module():
    spec = importlib.util.spec_from_file_location("backfill_identity_evidence_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class Cursor:
    def __init__(self, rows):
        self.rows = rows
        self.executed = []

    def __enter__(self): return self
    def __exit__(self, exc_type, exc, tb): return False
    def execute(self, sql, params=()): self.executed.append((sql, tuple(params)))
    def fetchall(self): return self.rows


class Connection:
    def __init__(self, rows):
        self.cursor_instance = Cursor(rows)
        self.commits = 0

    def __enter__(self): return self
    def __exit__(self, exc_type, exc, tb): return False
    def cursor(self): return self.cursor_instance
    def commit(self): self.commits += 1


def test_backfill_adds_identity_only_to_legacy_mismatch_evidence():
    module = load_module()
    with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
        raw = Path(temporary) / "mismatch.html"
        raw.write_text("legacy raw", encoding="utf-8")
        connection = Connection([{
            "id": 7,
            "asin": "B0B9ZFDZNJ",
            "url": "https://www.amazon.com/dp/B0B9ZFDZNJ",
            "raw_html_path": str(raw),
        }])

        result = module.backfill_identity_evidence(
            lambda: connection,
            parser=lambda _html, _url: {
                "asin": "B0B9ZFZZZZ",
                "canonical_url": "https://www.amazon.com/dp/B0B9ZFZZZZ",
                "parent_asin": "B0PARENT01",
                "identity_child_asins": ["B0B9ZFDZNJ", "B0B9ZFZZZZ"],
            },
            canonical_asin=lambda url: url.rsplit("/", 1)[-1],
        )

    assert result == {"scanned": 1, "updated": 1, "missing_raw": 0, "parse_failed": 0}
    update_sql, params = connection.cursor_instance.executed[-1]
    assert "jsonb_set" in update_sql and "NOT (context_json ? 'identity')" in update_sql
    identity = json.loads(params[0])
    assert identity["requested_asin"] == "B0B9ZFDZNJ"
    assert identity["observed_asin"] == "B0B9ZFZZZZ"
    assert connection.commits == 1
