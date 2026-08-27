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
