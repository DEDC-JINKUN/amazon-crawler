from pathlib import Path
import importlib.util
import tempfile
import hashlib


ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location("raw_html_store", ROOT / "scripts" / "raw_html_store.py")
store = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(store)


def test_local_store_writes_deterministic_relative_key_atomically():
    with tempfile.TemporaryDirectory() as directory:
        result = store.LocalRawHtmlStore(Path(directory)).put("run/1", "B00RCPDCQU", "<html>one</html>")
        assert result.startswith("US/B00RCPDCQU/run_1-")
        assert result.endswith(".html")
        assert (Path(directory) / result).read_text(encoding="utf-8") == "<html>one</html>"
        digest = hashlib.sha256((Path(directory) / result).read_bytes()).hexdigest()
        assert result.rsplit("-", 1)[1][:-5] == digest[:16]
