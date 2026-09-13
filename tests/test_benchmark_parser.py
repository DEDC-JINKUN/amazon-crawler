from pathlib import Path
import importlib.util
import tempfile


ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location("benchmark_parser", ROOT / "scripts" / "benchmark_parser.py")
benchmark_parser = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(benchmark_parser)


def test_benchmark_marks_repeated_input_as_synthetic():
    with tempfile.TemporaryDirectory() as directory:
        raw = Path(directory)
        (raw / "one.html").write_text('<html><input id="ASIN" value="B00RCPDCQU"><link rel="canonical" href="https://www.amazon.com/dp/B00RCPDCQU"><h1 id="productTitle">One</h1></html>', encoding="utf-8")
        result = benchmark_parser.benchmark(raw, repeat=3)
        assert result["pages_parsed"] == 3
        assert result["synthetic_repeat"] is True
        assert result["bytes_parsed"] > 0
