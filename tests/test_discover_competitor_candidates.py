from pathlib import Path
import csv
import importlib.util
import tempfile


ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location("discover_competitor_candidates", ROOT / "scripts" / "discover_competitor_candidates.py")
discover = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(discover)


def test_discover_deduplicates_amazon_candidates_and_excludes_foreign_links():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        source = root / "search.html"
        output = root / "candidates.csv"
        source.write_text('<a href="/dp/B00RCPDCQU">one</a><a href="https://www.amazon.com/gp/product/B00RCPDCQU">duplicate</a><a href="/dp/B00RCPDI50?x=1">two</a><a href="https://example.com/dp/B00RCPDKDA">no</a>', encoding="utf-8")
        assert discover.discover([source], source_type="keyword_search", source_query="pressure sensor", source_url="https://www.amazon.com/s?k=pressure+sensor", output=output) == 2
        rows = list(csv.DictReader(output.open(encoding="utf-8-sig")))
        assert [row["asin"] for row in rows] == ["B00RCPDCQU", "B00RCPDI50"]
        assert all(row["status"] == "candidate" for row in rows)
