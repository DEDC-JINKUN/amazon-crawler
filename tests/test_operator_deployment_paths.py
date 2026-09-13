from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
CONTROLLER = ROOT / "crawler.ps1"
HOST = ROOT / "scripts" / "operator_controller_host.py"


def load_host():
    spec = importlib.util.spec_from_file_location("operator_controller_host_path_test", HOST)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def powershell():
    value = shutil.which("powershell.exe") or shutil.which("powershell")
    if value is None:
        pytest.skip("Windows PowerShell unavailable")
    return value


@pytest.fixture
def external_control_root():
    root = ROOT.parent / f".operator-controlled-root-{uuid.uuid4().hex}"
    root.mkdir()
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_controller_status_uses_explicit_physical_data_root(external_control_root):
    allowed = external_control_root / "batch"
    allowed.mkdir()
    environment = os.environ.copy()
    environment["AMAZON_CRAWLER_ALLOWED_DATA_ROOT"] = str(allowed)
    result = subprocess.run(
        [
            powershell(), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(CONTROLLER),
            "status", "-OutputDir", str(allowed),
        ],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        timeout=15,
    )
    assert result.returncode == 1, result.stdout.decode("utf-8", errors="replace")
    assert b"Worker: stopped" in result.stdout
    assert (allowed / "control" / "console").is_dir()
    assert (allowed / "control").is_dir()


def test_controller_rejects_output_outside_explicit_data_root(external_control_root):
    allowed = external_control_root / "allowed"
    outside = external_control_root / "outside"
    allowed.mkdir()
    outside.mkdir()
    environment = os.environ.copy()
    environment["AMAZON_CRAWLER_ALLOWED_DATA_ROOT"] = str(allowed)
    result = subprocess.run(
        [
            powershell(), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(CONTROLLER),
            "status", "-OutputDir", str(outside),
        ],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        timeout=15,
    )
    assert result.returncode == 2
    assert not (outside / "control").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows junction contract")
def test_controller_rejects_allowed_root_with_junction_ancestor(external_control_root):
    real = external_control_root / "real"
    link = external_control_root / "linked"
    real.mkdir()
    subprocess.run(
        [powershell(), "-NoProfile", "-Command", f"New-Item -ItemType Junction -Path '{link}' -Target '{real}'|Out-Null"],
        check=True,
    )
    allowed = link / "batch"
    allowed.mkdir()
    environment = os.environ.copy()
    environment["AMAZON_CRAWLER_ALLOWED_DATA_ROOT"] = str(allowed)
    try:
        result = subprocess.run(
            [powershell(), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(CONTROLLER), "status", "-OutputDir", str(allowed)],
            cwd=ROOT, env=environment, capture_output=True, timeout=15,
        )
        assert result.returncode != 0
        assert not (allowed / "control").exists()
    finally:
        subprocess.run([powershell(), "-NoProfile", "-Command", f"Remove-Item -LiteralPath '{link}' -Force"], check=False)


@pytest.mark.skipif(os.name != "nt", reason="Windows junction contract")
def test_controller_rejects_control_junction_inside_allowed_root(external_control_root):
    allowed = external_control_root / "batch"
    outside = external_control_root / "outside"
    allowed.mkdir()
    outside.mkdir()
    control = allowed / "control"
    subprocess.run(
        [powershell(), "-NoProfile", "-Command", f"New-Item -ItemType Junction -Path '{control}' -Target '{outside}'|Out-Null"],
        check=True,
    )
    environment = os.environ.copy()
    environment["AMAZON_CRAWLER_ALLOWED_DATA_ROOT"] = str(allowed)
    try:
        result = subprocess.run(
            [powershell(), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(CONTROLLER), "status", "-OutputDir", str(allowed)],
            cwd=ROOT, env=environment, capture_output=True, timeout=15,
        )
        assert result.returncode != 0
        assert not (outside / "console").exists()
    finally:
        subprocess.run([powershell(), "-NoProfile", "-Command", f"Remove-Item -LiteralPath '{control}' -Force"], check=False)


@pytest.mark.skipif(os.name != "nt", reason="Windows junction contract")
def test_reparse_detection_does_not_depend_on_pathlib_is_junction(external_control_root, monkeypatch):
    host = load_host()
    real = external_control_root / "real-python311"
    link = external_control_root / "linked-python311"
    real.mkdir()
    subprocess.run(
        [powershell(), "-NoProfile", "-Command", f"New-Item -ItemType Junction -Path '{link}' -Target '{real}'|Out-Null"],
        check=True,
    )
    try:
        monkeypatch.setattr(host.Path, "is_junction", lambda _path: False, raising=False)
        assert host._has_reparse_ancestor(link / "batch") is True
    finally:
        subprocess.run([powershell(), "-NoProfile", "-Command", f"Remove-Item -LiteralPath '{link}' -Force"], check=False)


def test_operator_batch_precreates_raw_html_directory(tmp_path):
    module_path = ROOT / "tests" / "test_collection_operator.py"
    spec = importlib.util.spec_from_file_location("operator_existing_test_helpers", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    configured = module.settings(tmp_path)
    manager = module.BatchManager(configured)
    preview = manager.upload_preview("input.xlsx", module.make_xlsx([["ASIN"], ["B000000001"]]))
    batch = manager.start_batch(preview["import_id"], 1)
    batch_dir = configured.data_root / "batches" / batch["batch_id"]
    assert (batch_dir / "raw_html").is_dir()
    assert (batch_dir / "control" / "console").is_dir()
    assert (batch_dir / "control" / "runs").is_dir()
    manager.stop_batch(batch["batch_id"])


def test_operator_host_injects_only_its_validated_batch_as_allowed_root(tmp_path, monkeypatch):
    host = load_host()
    release = tmp_path / "release"
    scripts = release / "scripts"
    scripts.mkdir(parents=True)
    (release / "crawler.ps1").write_text("", encoding="utf-8")
    (scripts / "operator_export.py").write_text("", encoding="utf-8")
    shell = tmp_path / "powershell.exe"
    shell.write_bytes(b"fixture")
    data_root = tmp_path / "operator-data"
    batch = data_root / "batches" / ("batch-" + "0" * 24)
    batch.mkdir(parents=True)
    (batch / "control" / "console").mkdir(parents=True)
    (batch / "control" / "runs").mkdir()
    (batch / "raw_html").mkdir()
    manifest = batch / "manifest.csv"
    config = batch / "batch.toml"
    manifest.write_text("asin\nB000000001\n", encoding="utf-8")
    config.write_text("[worker]\n", encoding="utf-8")
    calls = []

    class Process:
        returncode = 0

        def poll(self):
            return 0

    def popen(command, **kwargs):
        calls.append((command, kwargs))
        return Process()

    monkeypatch.setattr(host.subprocess, "Popen", popen)
    monkeypatch.setattr(host, "_run_projection", lambda _command: 0)
    result = host.main([
        "--release-root", str(release), "--operator-data-root", str(data_root), "--batch-dir", str(batch),
        "--manifest", str(manifest), "--config", str(config),
        "--tenant-id", "operator_fixture", "--batch-id", batch.name,
        "--worker-count", "1", "--limit", "1", "--console-port", "8773",
        "--recovery-seconds", "60", "--powershell", str(shell),
    ])
    assert result == 0
    assert calls[0][1]["shell"] is False
    assert calls[0][1]["env"]["AMAZON_CRAWLER_ALLOWED_DATA_ROOT"] == str(batch.resolve())


def test_operator_host_rejects_batch_outside_fixed_data_root(tmp_path, monkeypatch):
    host = load_host()
    release = tmp_path / "release"
    scripts = release / "scripts"
    scripts.mkdir(parents=True)
    (release / "crawler.ps1").write_text("", encoding="utf-8")
    (scripts / "operator_export.py").write_text("", encoding="utf-8")
    shell = tmp_path / "powershell.exe"
    shell.write_bytes(b"fixture")
    data_root = tmp_path / "operator-data"
    data_root.mkdir()
    batch = tmp_path / ("batch-" + "0" * 24)
    batch.mkdir()
    (batch / "control" / "console").mkdir(parents=True)
    (batch / "control" / "runs").mkdir()
    (batch / "raw_html").mkdir()
    manifest = batch / "manifest.csv"
    config = batch / "batch.toml"
    manifest.write_text("asin\nB000000001\n", encoding="utf-8")
    config.write_text("[worker]\n", encoding="utf-8")
    monkeypatch.setattr(host.subprocess, "Popen", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not spawn")))
    result = host.main([
        "--release-root", str(release), "--operator-data-root", str(data_root), "--batch-dir", str(batch),
        "--manifest", str(manifest), "--config", str(config),
        "--tenant-id", "operator_fixture", "--batch-id", batch.name,
        "--worker-count", "1", "--limit", "1", "--console-port", "8773",
        "--recovery-seconds", "60", "--powershell", str(shell),
    ])
    assert result == 2
