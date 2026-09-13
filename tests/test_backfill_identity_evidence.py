from __future__ import annotations

import importlib.util
import gzip
import hashlib
import json
from pathlib import Path
import subprocess
import sys
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


def test_backfill_uses_tenant_scoped_real_raw_shape_and_hash_verified_root():
    module = load_module()
    with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
        raw_root = Path(temporary) / "raw_html"
        relative = Path("US") / "B0B9ZFDZNJ" / "mismatch.html.gz"
        raw = raw_root / relative
        raw.parent.mkdir(parents=True)
        body = "legacy raw"
        with gzip.open(raw, "wb") as handle:
            handle.write(body.encode("utf-8"))
        connection = Connection([{
            "id": 7,
            "tenant_id": "tenant-a",
            "asin": "B0B9ZFDZNJ",
            "url": "https://www.amazon.com/dp/B0B9ZFDZNJ",
            "raw_html_path": relative.as_posix(),
            "content_hash": hashlib.sha256(body.encode("utf-8")).hexdigest(),
        }])

        result = module.backfill_identity_evidence(
            lambda: connection,
            tenant_id="tenant-a",
            raw_html_dir=raw_root,
            parser=lambda _html, _url: {
                "asin": "B0B9ZFZZZZ",
                "canonical_url": "https://www.amazon.com/dp/B0B9ZFZZZZ",
                "parent_asin": "B0PARENT01",
                "identity_child_asins": ["B0B9ZFDZNJ", "B0B9ZFZZZZ"],
            },
            canonical_asin=lambda url: url.rsplit("/", 1)[-1],
            canonical_valid=lambda _url: True,
        )

    assert result == {"scanned": 1, "updated": 1, "missing_raw": 0, "hash_mismatch": 0, "parse_failed": 0}
    update_sql, params = connection.cursor_instance.executed[-1]
    assert "jsonb_set" in update_sql and "tenant_id=%s" in update_sql
    identity = json.loads(params[0])
    assert identity["requested_asin"] == "B0B9ZFDZNJ"
    assert identity["observed_asin"] == "B0B9ZFZZZZ"
    assert identity["canonical_valid_amazon"] is True
    select_sql, select_params = connection.cursor_instance.executed[0]
    assert "WHERE tenant_id=%s" in select_sql
    assert select_params == ("tenant-a",)
    assert connection.commits == 1


def test_backfill_rejects_path_escape_and_hash_mismatch_without_searching_other_roots():
    module = load_module()
    with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
        root = Path(temporary) / "raw_html"
        root.mkdir()
        outside = Path(temporary) / "outside.html"
        outside.write_text("outside", encoding="utf-8")
        mismatched = root / "US" / "B0B9ZFDZNJ" / "mismatch.html"
        mismatched.parent.mkdir(parents=True)
        mismatched.write_text("actual", encoding="utf-8")
        rows = [
            {"id": 1, "tenant_id": "tenant-a", "asin": "B0B9ZFDZNJ", "url": "https://www.amazon.com/dp/B0B9ZFDZNJ",
             "raw_html_path": "../outside.html", "content_hash": hashlib.sha256(b"outside").hexdigest()},
            {"id": 2, "tenant_id": "tenant-a", "asin": "B0B9ZFDZNJ", "url": "https://www.amazon.com/dp/B0B9ZFDZNJ",
             "raw_html_path": "US/B0B9ZFDZNJ/mismatch.html", "content_hash": "0" * 64},
        ]
        connection = Connection(rows)

        result = module.backfill_identity_evidence(
            lambda: connection, tenant_id="tenant-a", raw_html_dir=root,
            parser=lambda _html, _url: {}, canonical_asin=lambda _url: "",
            canonical_valid=lambda _url: False,
        )

    assert result == {"scanned": 2, "updated": 0, "missing_raw": 1, "hash_mismatch": 1, "parse_failed": 0}
    assert len(connection.cursor_instance.executed) == 1


def test_backfill_cli_requires_tenant_and_raw_root_before_database_access():
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--dsn-env", "MISSING_TEST_DSN"],
        cwd=ROOT, capture_output=True, text=True, timeout=10,
    )

    assert result.returncode == 2
    assert "--tenant-id" in result.stderr
    assert "--raw-html-dir" in result.stderr
