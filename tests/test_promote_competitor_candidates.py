from pathlib import Path
import csv
import importlib.util
import tempfile


ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location("promote_competitor_candidates", ROOT / "scripts" / "promote_competitor_candidates.py")
promote = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(promote)


def test_promote_requires_explicit_approval_and_writes_manifest():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        candidates = root / "candidates.csv"
        candidates.write_text("asin,marketplace,status\nB00RCPDCQU,US,candidate\nB00RCPDI50,US,candidate\n", encoding="utf-8")
        approved = root / "approved.txt"
        approved.write_text("B00RCPDI50\n", encoding="utf-8")
        output = root / "manifest.csv"
        assert promote.promote(candidates, approved, output) == 1
        rows = list(csv.DictReader(output.open(encoding="utf-8-sig")))
        assert rows[0]["asin"] == "B00RCPDI50"
        assert rows[0]["source_site_label"] == "competitor_approved"


def test_discovery_to_approval_to_manifest_pipeline():
    discover_spec = importlib.util.spec_from_file_location("discover", ROOT / "scripts" / "discover_competitor_candidates.py")
    discover = importlib.util.module_from_spec(discover_spec)
    assert discover_spec.loader is not None
    discover_spec.loader.exec_module(discover)
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        html = root / "search.html"
        candidates = root / "candidates.csv"
        approved = root / "approved.txt"
        manifest = root / "manifest.csv"
        html.write_text('<div data-asin="B00RCPDCQU"></div><div data-asin="B00RCPDI50"></div>', encoding="utf-8")
        discover.discover([html], source_type="keyword_search", source_query="sensor", source_url="https://www.amazon.com/s?k=sensor", output=candidates)
        approved.write_text("B00RCPDI50\n", encoding="utf-8")
        assert promote.promote(candidates, approved, manifest) == 1
        row = next(csv.DictReader(manifest.open(encoding="utf-8-sig")))
        assert row["asin"] == "B00RCPDI50"
