from pathlib import Path
import importlib.util
import tempfile
import hashlib
import gzip


ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location("raw_html_store", ROOT / "scripts" / "raw_html_store.py")
store = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(store)


def test_local_store_writes_gzip_content_addressed_relative_key_atomically():
    with tempfile.TemporaryDirectory() as directory:
        raw = "<html>one</html>"
        result = store.LocalRawHtmlStore(Path(directory)).put("run/1", "B00RCPDCQU", raw)
        assert result.startswith("US/B00RCPDCQU/")
        assert result.endswith(".html.gz")
        assert result == store.LocalRawHtmlStore(Path(directory)).put("run/2", "B00RCPDCQU", raw)
        assert gzip.decompress((Path(directory) / result).read_bytes()).decode("utf-8") == raw
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        assert result == f"US/B00RCPDCQU/{digest}.html.gz"


def test_local_store_preserves_existing_legacy_html_files():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        legacy = root / "US" / "B00RCPDCQU" / "legacy.html"
        legacy.parent.mkdir(parents=True)
        legacy.write_text("legacy", encoding="utf-8")
        store.LocalRawHtmlStore(root).put("run", "B00RCPDCQU", "new")
        assert legacy.read_text(encoding="utf-8") == "legacy"


def test_read_raw_html_supports_new_gzip_and_legacy_plain_html():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        relative = store.LocalRawHtmlStore(root).put("run", "B00RCPDCQU", "new")
        legacy = root / "legacy.html"
        legacy.write_text("legacy", encoding="utf-8")
        assert store.read_raw_html(root / relative) == "new"
        assert store.read_raw_html(legacy) == "legacy"
