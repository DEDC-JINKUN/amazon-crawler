#!/usr/bin/env python3
from __future__ import annotations

import csv
import importlib.util
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"

def load(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class ManifestTests(unittest.TestCase):
    def test_existing_manifest_is_valid(self):
        validate = load("validate_us_manifest")
        self.assertEqual(validate.validate_manifest(ROOT / "amazon_us_asin_manifest.csv", 1892), [])

    def test_build_requires_force_for_existing_output(self):
        build = load("build_us_manifest")
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "manifest.csv"
            output.write_text("existing", encoding="utf-8")
            with self.assertRaises(FileExistsError):
                build.build_manifest(ROOT / "lingxing_asin_links.xlsx", output, False)


if __name__ == "__main__":
    unittest.main()
