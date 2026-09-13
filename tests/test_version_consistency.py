from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_project_version_is_consistent_across_release_files():
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    readme_version = re.search(r"当前版本：`([^`]+)`", readme)
    changelog_version = re.search(r"^## (\d+\.\d+\.\d+) - ", changelog, flags=re.MULTILINE)
    assert readme_version and readme_version.group(1) == version
    assert changelog_version and changelog_version.group(1) == version
