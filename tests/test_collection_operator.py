from __future__ import annotations

import csv
import io
import json
import shutil
import socket
import subprocess
import sys
import threading
import time
import types
import urllib.error
import urllib.request
import zipfile
from http.cookiejar import CookieJar
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from collection_operator import OperatorSettings, create_server
import operator_controller_host as controller_host
import operator_export
import operator_service_start as service_start
from operator_export import excel_safe, safe_error
from operator_core import BatchConflict, BatchManager, OperatorError
from operator_xlsx import XlsxLimits, XlsxRejected, inspect_xlsx


def make_xlsx(
    rows, *, sheet_name="Products", extra_entries=None, external=False,
    row_numbers=None, worksheet_target="worksheets/sheet1.xml",
):
    shared = []
    index = {}
    def shared_index(value):
        value = str(value)
        if value not in index:
            index[value] = len(shared)
            shared.append(value)
        return index[value]
    xml_rows = []
    for row_index, values in enumerate(rows, 1):
        row_number = row_numbers[row_index - 1] if row_numbers else row_index
        cells = []
        for column_number, value in enumerate(values, 1):
            if value is None:
                continue
            column = chr(64 + column_number)
            cells.append(f'<c r="{column}{row_number}" t="s"><v>{shared_index(value)}</v></c>')
        xml_rows.append(f'<row r="{row_number}">{"".join(cells)}</row>')
    strings = "".join(f"<si><t>{value}</t></si>" for value in shared)
    workbook_rels = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        f'<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="{worksheet_target}"/>'
        + ('<Relationship Id="rId9" Type="externalLink" Target="https://example.test/data" TargetMode="External"/>' if external else '')
        + '</Relationships>'
    )
    entries = {
        "[Content_Types].xml": '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="xml" ContentType="application/xml"/></Types>',
        "xl/workbook.xml": f'<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="{sheet_name}" sheetId="1" r:id="rId1"/></sheets></workbook>',
        "xl/_rels/workbook.xml.rels": workbook_rels,
        "xl/sharedStrings.xml": f'<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">{strings}</sst>',
        "xl/worksheets/sheet1.xml": f'<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>{"".join(xml_rows)}</sheetData></worksheet>',
    }
    entries.update(extra_entries or {})
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def settings(tmp_path):
    config = tmp_path / "fixed.toml"
    config.write_text('[worker]\nmax_actions_per_run=5\n[paths]\nmanifest="old.csv"\noutput_dir="old"\nraw_html_dir="old/raw"\n', encoding="utf-8")
    return OperatorSettings(
        release_root=ROOT,
        data_root=tmp_path / "operator-data",
        config_path=config,
        vault_path=tmp_path / "vault.dpapi",
        fake_runner=True,
        fake_delay_seconds=0.01,
    )


def test_xlsx_preview_reports_valid_duplicates_and_invalid_rows():
    result = inspect_xlsx(make_xlsx([
        ["ASIN", "Name"],
        ["B000000001", "one"],
        ["b000000001", "duplicate"],
        ["not-an-asin", "bad"],
        ["B000000002", "two"],
    ]), XlsxLimits())
    assert result["selected_sheet"] == "Products"
    assert result["selected_column"] == "A"
    assert [row["asin"] for row in result["valid_rows"]] == ["B000000001", "B000000002"]
    assert result["duplicate_rows"] == [{"row": 3, "first_row": 2, "asin": "B000000001"}]
    assert result["invalid_rows"] == [{"row": 4, "reason": "invalid_asin"}]
    assert result["can_start"] is False


def test_xlsx_preview_preserves_sparse_excel_row_numbers():
    result = inspect_xlsx(
        make_xlsx([["ASIN"], ["bad"], ["B000000001"]], row_numbers=[1, 42, 100]),
        XlsxLimits(),
    )
    assert result["invalid_rows"] == [{"row": 42, "reason": "invalid_asin"}]
    assert result["valid_rows"] == [{"row": 100, "asin": "B000000001"}]


def test_xlsx_accepts_package_absolute_worksheet_relationship_target():
    result = inspect_xlsx(
        make_xlsx([["ASIN"], ["B000000001"], ["B000000002"]], worksheet_target="/xl/worksheets/sheet1.xml"),
        XlsxLimits(),
    )
    assert result["unique_count"] == 2 and result["can_start"] is True


@pytest.mark.parametrize("target", [
    "//xl/worksheets/sheet1.xml",
    "\\xl\\worksheets\\sheet1.xml",
    "/xl/worksheets/../../docProps/core.xml",
    "/docProps/core.xml",
])
def test_xlsx_rejects_unsafe_or_nonworksheet_relationship_targets(target):
    with pytest.raises(XlsxRejected, match="malformed_xlsx"):
        inspect_xlsx(make_xlsx([["ASIN"], ["B000000001"]], worksheet_target=target), XlsxLimits())


def test_xlsx_explicit_sheet_column_selection_and_limits():
    workbook = make_xlsx([["Product code"], ["B000000001"], ["B000000002"]])
    automatic = inspect_xlsx(workbook, XlsxLimits())
    assert automatic["selection_required"] is True
    selected = inspect_xlsx(workbook, XlsxLimits(), selected_sheet="Products", selected_column="A")
    assert selected["can_start"] is True and selected["unique_count"] == 2
    with pytest.raises(XlsxRejected, match="row_limit"):
        inspect_xlsx(workbook, XlsxLimits(max_rows=2), selected_sheet="Products", selected_column="A")
    with pytest.raises(XlsxRejected, match="unique_asin_limit"):
        inspect_xlsx(
            make_xlsx([["ASIN"], ["B000000001"], ["B000000002"], ["B000000003"]]),
            XlsxLimits(max_unique_asins=2),
        )


def test_xlsx_rejects_size_and_suspicious_compression():
    workbook = make_xlsx([["ASIN"], ["B000000001"]])
    with pytest.raises(XlsxRejected, match="file_too_large"):
        inspect_xlsx(workbook, XlsxLimits(max_file_bytes=100))
    bomb = make_xlsx([["ASIN"], ["B000000001"]], extra_entries={"xl/media/padding.bin": b"0" * (2 * 1024 * 1024)})
    with pytest.raises(XlsxRejected, match="suspicious_compression_ratio"):
        inspect_xlsx(bomb, XlsxLimits(max_compression_ratio=10))


def test_xlsx_rejects_encryption_marker_and_xml_doctype():
    encrypted = bytearray(make_xlsx([["ASIN"], ["B000000001"]]))
    offset = 0
    while True:
        offset = encrypted.find(b"PK\x03\x04", offset)
        if offset < 0:
            break
        encrypted[offset + 6] |= 1
        offset += 4
    offset = 0
    while True:
        offset = encrypted.find(b"PK\x01\x02", offset)
        if offset < 0:
            break
        encrypted[offset + 8] |= 1
        offset += 4
    with pytest.raises(XlsxRejected, match="encrypted_xlsx_not_allowed"):
        inspect_xlsx(bytes(encrypted), XlsxLimits())
    unsafe = make_xlsx(
        [["ASIN"], ["B000000001"]],
        extra_entries={"xl/sharedStrings.xml": '<!DOCTYPE x [<!ENTITY boom "x">]><sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><si><t>&boom;</t></si></sst>'},
    )
    with pytest.raises(XlsxRejected, match="unsafe_xml"):
        inspect_xlsx(unsafe, XlsxLimits())


@pytest.mark.parametrize("coordinate", ["AAAA1", "XFE1", "A99999999"])
def test_xlsx_rejects_invalid_excel_coordinates(coordinate):
    sheet = f'<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row r="1"><c r="{coordinate}" t="inlineStr"><is><t>ASIN</t></is></c></row></sheetData></worksheet>'
    workbook = make_xlsx([["ASIN"]], extra_entries={"xl/worksheets/sheet1.xml": sheet})
    with pytest.raises(XlsxRejected, match="invalid_cell_coordinate"):
        inspect_xlsx(workbook, XlsxLimits())


def test_xlsx_enforces_total_cell_budget():
    with pytest.raises(XlsxRejected, match="cell_limit"):
        inspect_xlsx(make_xlsx([["ASIN", "Name"], ["B000000001", "one"]]), XlsxLimits(max_cells=3))


@pytest.mark.parametrize("value", ["=1+1", "+SUM(A1:A2)", "-2+3", "@cmd", "  =HYPERLINK(\"x\")"])
def test_csv_formula_values_are_neutralized_but_json_values_can_remain_raw(value):
    assert excel_safe(value) == "'" + value
    payload = {"title": value}
    assert payload["title"] == value
    assert safe_error(value, "collection_failed") == "collection_failed"


def test_csv_ordinary_values_are_unchanged():
    for value in ("Product title", "In Stock", "19.99", ""):
        assert excel_safe(value) == value


@pytest.mark.parametrize(
    ("filename", "extra_entries", "external", "code"),
    [
        ("input.xlsm", {}, False, "unsupported_file_type"),
        ("input.xlsx", {"../escape.xml": "x"}, False, "unsafe_archive_path"),
        ("input.xlsx", {"xl/vbaProject.bin": "macro"}, False, "macro_not_allowed"),
        ("input.xlsx", {}, True, "external_link_not_allowed"),
    ],
)
def test_upload_rejects_unsafe_workbooks(tmp_path, filename, extra_entries, external, code):
    manager = BatchManager(settings(tmp_path))
    with pytest.raises(OperatorError) as caught:
        manager.upload_preview(
            filename,
            make_xlsx([["ASIN"], ["B000000001"]], extra_entries=extra_entries, external=external),
        )
    assert caught.value.code == code


def test_invalid_rows_block_start_and_repeat_start_is_idempotent(tmp_path):
    manager = BatchManager(settings(tmp_path))
    invalid = manager.upload_preview("input.xlsx", make_xlsx([["ASIN"], ["bad"]]))
    with pytest.raises(OperatorError, match="invalid"):
        manager.start_batch(invalid["import_id"], 1)

    preview = manager.upload_preview("input.xlsx", make_xlsx([["ASIN"], ["B000000001"], ["B000000002"]]))
    first = manager.start_batch(preview["import_id"], 1)
    second = manager.start_batch(preview["import_id"], 1)
    assert second["batch_id"] == first["batch_id"]
    with pytest.raises(BatchConflict, match="idempotency_conflict"):
        manager.start_batch(preview["import_id"], 2)
    deadline = time.monotonic() + 5
    while manager.get_batch(first["batch_id"])["status"] not in {"completed", "failed"} and time.monotonic() < deadline:
        time.sleep(0.02)
    assert manager.get_batch(first["batch_id"])["status"] == "completed"


def test_upload_is_preview_only_and_two_worker_is_explicitly_experimental(tmp_path):
    configured = settings(tmp_path)
    configured.fake_delay_seconds = 0.2
    manager = BatchManager(configured)
    preview = manager.upload_preview("input.xlsx", make_xlsx([["ASIN"], ["B000000001"], ["B000000002"]]))
    assert list((configured.data_root / "batches").iterdir()) == []
    batch = manager.start_batch(preview["import_id"], 2)
    assert batch["worker_count"] == 2 and batch["worker_count_experimental"] is True
    manager.stop_batch(batch["batch_id"])


def test_batches_use_fixed_business_tenant_and_publish_dashboard_url(tmp_path):
    configured = settings(tmp_path)
    configured.business_tenant_id = "amazon_us_main"
    manager = BatchManager(configured)
    preview = manager.upload_preview("input.xlsx", make_xlsx([["ASIN"], ["B000000001"]]))
    batch = manager.start_batch(preview["import_id"], 1, "competitor")
    assert batch["tenant_id"] == "amazon_us_main"
    assert batch["subject_type"] == "competitor"
    assert batch["dashboard_url"] == "http://127.0.0.1:8770/?tenant=amazon_us_main"
    manager.stop_batch(batch["batch_id"])


def test_invalid_subject_type_is_rejected_before_start(tmp_path):
    manager = BatchManager(settings(tmp_path))
    preview = manager.upload_preview("input.xlsx", make_xlsx([["ASIN"], ["B000000001"]]))
    with pytest.raises(OperatorError, match="invalid_subject_type"):
        manager.start_batch(preview["import_id"], 1, "other")


def test_business_tenant_must_be_safe():
    with pytest.raises(ValueError, match="business tenant"):
        OperatorSettings(
            release_root=ROOT,
            data_root=ROOT / "data" / "operator-test-invalid-tenant",
            config_path=ROOT / "config" / "amazon_us.windows.toml",
            vault_path=ROOT / "data" / "secure" / "unused.dpapi",
            business_tenant_id="bad tenant?",
        )


def test_service_starter_uses_stable_base_process_and_keeps_worker_runtime(tmp_path, monkeypatch, capsys):
    root = tmp_path / "release"
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    service = scripts / "collection_operator.py"
    service.write_text("", encoding="utf-8")
    worker_python = tmp_path / "venv-python.exe"
    worker_python.write_bytes(b"worker")
    base_python = tmp_path / "base-python.exe"
    base_python.write_bytes(b"base")
    config = tmp_path / "config.toml"
    config.write_text("", encoding="utf-8")
    vault = tmp_path / "vault.dpapi"
    vault.write_bytes(b"vault")
    captured = {}

    class Process:
        pid = 12345

    def popen(command, **kwargs):
        captured.update(command=command, kwargs=kwargs)
        return Process()

    monkeypatch.setattr(service_start.sys, "_base_executable", str(base_python))
    monkeypatch.setattr(service_start.subprocess, "Popen", popen)
    assert service_start.main([
        "--python", str(worker_python), "--service", str(service),
        "--working-directory", str(root), "--data-root", str(tmp_path / "data"),
        "--config", str(config), "--vault", str(vault),
        "--stdout", str(tmp_path / "stdout.log"), "--stderr", str(tmp_path / "stderr.log"),
        "--port", "8772", "--agent-port", "8765", "--internal-console-port", "8773", "--dashboard-port", "8770",
        "--business-tenant-id", "amazon_us_main", "--campaign-deadline-seconds", "86400",
    ]) == 0
    assert captured["command"][0] == str(base_python.resolve())
    assert captured["command"][captured["command"].index("--worker-python") + 1] == str(worker_python.resolve())
    assert captured["command"][captured["command"].index("--dashboard-port") + 1] == "8770"
    assert captured["command"][captured["command"].index("--agent-port") + 1] == "8765"
    assert captured["command"][captured["command"].index("--campaign-deadline-seconds") + 1] == "86400"
    assert json.loads(capsys.readouterr().out)["pid"] == 12345


def test_concurrent_start_creates_only_one_batch(tmp_path):
    configured = settings(tmp_path)
    configured.fake_delay_seconds = 0.2
    manager = BatchManager(configured)
    first = manager.upload_preview("one.xlsx", make_xlsx([["ASIN"], ["B000000001"], ["B000000002"]]))
    second = manager.upload_preview("two.xlsx", make_xlsx([["ASIN"], ["B000000003"]]))
    results = []
    barrier = threading.Barrier(3)
    def start(import_id):
        barrier.wait()
        try:
            results.append(("started", manager.start_batch(import_id, 1)))
        except BatchConflict as exc:
            results.append(("blocked", exc.code))
    threads = [threading.Thread(target=start, args=(value["import_id"],)) for value in (first, second)]
    for thread in threads: thread.start()
    barrier.wait()
    for thread in threads: thread.join(timeout=3)
    assert sorted(value[0] for value in results) == ["blocked", "started"]
    started = next(value[1] for value in results if value[0] == "started")
    manager.stop_batch(started["batch_id"])


def test_production_runner_argv_is_fixed_and_contains_no_secret_or_client_command(tmp_path):
    configured = settings(tmp_path)
    configured.fake_runner = False
    configured.agent_port = 8865
    configured.campaign_deadline_seconds = 172800
    manager = BatchManager(configured)
    # Build with server-owned values only; this does not launch the command.
    batch_dir = configured.data_root / "batches" / ("batch-" + "0" * 24)
    record = {"batch_id": batch_dir.name, "tenant_id": "operator_20260906_fixture", "subject_type": "competitor", "worker_count": 1, "requested": 3}
    command = manager._build_runner_command(record, batch_dir, batch_dir / "manifest.csv", batch_dir / "batch.toml")
    assert isinstance(command, list) and "operator_secure_runner.ps1" in " ".join(command)
    assert "-WorkerCount" in command and "-Limit" in command and "-TenantId" in command
    assert command[command.index("-SubjectType") + 1] == "competitor"
    assert command[command.index("-AgentPort") + 1] == "8865"
    assert command[command.index("-CampaignDeadlineSeconds") + 1] == "172800"
    assert command[command.index("-OperatorDataRoot") + 1] == str(configured.data_root)
    assert not any("POSTGRES" in value or "PROXY" in value or "API_KEY" in value for value in command)
    assert not any(value in {"cmd.exe", "/c", "-Command"} for value in command)
    script = (ROOT / "scripts" / "operator_secure_runner.ps1").read_text(encoding="utf-8")
    assert "Start-Process" not in script and "$joined" not in script


def test_operator_status_exposes_the_shared_campaign_and_effective_worker_pool(tmp_path):
    configured = settings(tmp_path)
    manager = BatchManager(configured)
    batch_id = "batch-" + "9" * 24
    batch_dir = configured.data_root / "batches" / batch_id
    batch_dir.mkdir(parents=True)
    (batch_dir / "batch.json").write_text(json.dumps({
        "batch_id": batch_id,
        "tenant_id": "owned-products",
        "status": "running",
        "worker_count": 2,
        "started_at": "2026-09-13T00:00:00+00:00",
    }), encoding="utf-8")
    (batch_dir / "progress.json").write_text(json.dumps({
        "status": "running",
        "campaign_id": "campaign-" + "9" * 32,
        "campaign_status": "active",
        "requested_workers": 2,
        "effective_workers": 2,
        "alive_workers": 2,
        "requested": 3,
        "pending": 3,
    }), encoding="utf-8")

    status = manager.get_batch(batch_id)

    assert status["campaign_id"] == "campaign-" + "9" * 32
    assert status["campaign_status"] == "active"
    assert status["requested_workers"] == 2
    assert status["effective_workers"] == 2
    assert status["alive_workers"] == 2


def test_stop_relaunches_secure_campaign_runner_when_original_runner_is_gone(tmp_path, monkeypatch):
    import operator_core

    configured = settings(tmp_path)
    configured.fake_runner = False
    manager = BatchManager(configured)
    batch_id = "batch-" + "c" * 24
    batch_dir = configured.data_root / "batches" / batch_id
    batch_dir.mkdir(parents=True)
    (batch_dir / "manifest.csv").write_text("asin\nB000000001\n", encoding="utf-8")
    (batch_dir / "batch.toml").write_text("[worker]\n", encoding="utf-8")
    (batch_dir / "campaign.json").write_text(json.dumps({
        "campaign_id": "campaign-" + "c" * 32,
    }), encoding="utf-8")
    (batch_dir / "batch.json").write_text(json.dumps({
        "schema_version": "amazon-operator-batch-v1",
        "batch_id": batch_id,
        "tenant_id": "owned-products",
        "subject_type": "own",
        "status": "running",
        "worker_count": 2,
        "requested": 1,
        "started_at": "2026-09-14T00:00:00+00:00",
        "stop_requested": False,
    }), encoding="utf-8")
    (batch_dir / "control.json").write_text(json.dumps({
        "pid": 999999,
        "process_start_identity": "gone",
    }), encoding="utf-8")
    captured = {}

    class Process:
        pid = 43210
        def poll(self): return None
        def wait(self): return 130

    class Thread:
        def __init__(self, *args, **kwargs): captured["thread"] = (args, kwargs)
        def start(self): captured["thread_started"] = True

    def popen(command, **kwargs):
        captured.update(command=command, kwargs=kwargs)
        return Process()

    monkeypatch.setattr(operator_core, "_process_matches", lambda *_args: False)
    monkeypatch.setattr(operator_core, "_process_start_identity", lambda _pid: "new-identity")
    monkeypatch.setattr(operator_core.subprocess, "Popen", popen)
    monkeypatch.setattr(operator_core.threading, "Thread", Thread)

    stopped = manager.stop_batch(batch_id)

    assert stopped["status"] == "stopping"
    assert stopped["stop_requested"] is True
    assert captured["thread_started"] is True
    assert "crawler_process_host.py" in " ".join(str(value) for value in captured["command"])
    request = json.loads((batch_dir / "cancel-runner.request.json").read_text(encoding="utf-8"))
    assert "operator_secure_runner.ps1" in " ".join(request["arguments"])
    assert request["gate_path"].endswith("cancel-runner.start.gate")
    assert (batch_dir / "operator.stop.json").is_file()


def test_controller_host_uses_structured_argv_and_propagates_projection_failure(tmp_path, monkeypatch):
    release = tmp_path / "release"
    scripts = release / "scripts"
    scripts.mkdir(parents=True)
    (release / "crawler.ps1").write_text("", encoding="utf-8")
    (scripts / "operator_export.py").write_text("", encoding="utf-8")
    powershell = tmp_path / "powershell.exe"
    powershell.write_bytes(b"fixture")
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
    commands = []
    projections = []
    class Process:
        returncode = 0
        def poll(self): return 0
    def popen(command, **kwargs):
        commands.append((command, kwargs))
        return Process()
    monkeypatch.setattr(controller_host.subprocess, "Popen", popen)
    def project(command):
        projections.append(command)
        return 2
    monkeypatch.setattr(controller_host, "_run_projection", project)
    result = controller_host.main([
        "--release-root", str(release), "--operator-data-root", str(data_root), "--batch-dir", str(batch), "--manifest", str(manifest),
        "--config", str(config), "--tenant-id", "operator_fixture", "--subject-type", "competitor", "--batch-id", batch.name,
        "--worker-count", "1", "--limit", "1", "--console-port", "8773",
        "--recovery-seconds", "60", "--powershell", str(powershell),
    ])
    assert result == 4
    assert len(commands) == 2
    assert [entry[0][entry[0].index(str((release / "crawler.ps1").resolve())) + 1] for entry in commands] == ["canary", "run"]
    assert isinstance(commands[0][0], list) and commands[0][1]["shell"] is False
    assert commands[0][0][0] == str(powershell.resolve()) and "-Command" not in commands[0][0]
    assert commands[1][0][commands[1][0].index("-SubjectType") + 1] == "competitor"
    assert commands[0][0][commands[0][0].index("-TenantId") + 1] == "operator_fixture"
    assert commands[0][0][commands[0][0].index("-ConfigPath") + 1] == str(config.resolve())
    assert projections and all(command[command.index("--subject-type") + 1] == "competitor" for command in projections)


def test_controller_host_stops_before_run_when_tenant_canary_fails(tmp_path, monkeypatch):
    release = tmp_path / "release"
    scripts = release / "scripts"
    scripts.mkdir(parents=True)
    controller = release / "crawler.ps1"
    controller.write_text("", encoding="utf-8")
    (scripts / "operator_export.py").write_text("", encoding="utf-8")
    powershell = tmp_path / "powershell.exe"
    powershell.write_bytes(b"fixture")
    data_root = tmp_path / "operator-data"
    batch = data_root / "batches" / ("batch-" + "1" * 24)
    batch.mkdir(parents=True)
    (batch / "control" / "console").mkdir(parents=True)
    (batch / "control" / "runs").mkdir()
    (batch / "raw_html").mkdir()
    manifest = batch / "manifest.csv"
    config = batch / "batch.toml"
    manifest.write_text("asin\nB000000001\n", encoding="utf-8")
    config.write_text("[worker]\nproxy_session_ports=[10000,10001]\nproxy_session_retry_per_asin=1\n", encoding="utf-8")
    commands = []

    class FailedCanary:
        returncode = 3
        def poll(self): return 3

    def popen(command, **kwargs):
        commands.append(command)
        kwargs["stdout"].write(b"Proxy canary: gate=denied:unique_capacity_insufficient\n")
        kwargs["stdout"].flush()
        return FailedCanary()

    monkeypatch.setattr(controller_host.subprocess, "Popen", popen)
    monkeypatch.setattr(controller_host, "_run_projection", lambda _command: 0)
    result = controller_host.main([
        "--release-root", str(release), "--operator-data-root", str(data_root), "--batch-dir", str(batch),
        "--manifest", str(manifest), "--config", str(config), "--tenant-id", "amazon_us_main",
        "--subject-type", "own", "--batch-id", batch.name, "--worker-count", "1", "--limit", "10",
        "--console-port", "8773", "--recovery-seconds", "60", "--powershell", str(powershell),
    ])

    assert result == 3
    assert len(commands) == 1
    assert commands[0][commands[0].index(str(controller.resolve())) + 1] == "canary"
    assert commands[0][commands[0].index("-Limit") + 1] == "1"
    progress = json.loads((batch / "progress.json").read_text(encoding="utf-8"))
    assert progress["status"] == "failed"
    assert progress["latest_error"] == "canary_unique_capacity_insufficient"


def test_controller_host_projects_safe_run_capacity_denial(tmp_path, monkeypatch):
    release = tmp_path / "release"
    scripts = release / "scripts"
    scripts.mkdir(parents=True)
    controller = release / "crawler.ps1"
    controller.write_text("", encoding="utf-8")
    (scripts / "operator_export.py").write_text("", encoding="utf-8")
    powershell = tmp_path / "powershell.exe"
    powershell.write_bytes(b"fixture")
    data_root = tmp_path / "operator-data"
    batch = data_root / "batches" / ("batch-" + "4" * 24)
    batch.mkdir(parents=True)
    (batch / "control" / "console").mkdir(parents=True)
    runs = batch / "control" / "runs"
    runs.mkdir()
    (batch / "raw_html").mkdir()
    manifest = batch / "manifest.csv"
    config = batch / "batch.toml"
    manifest.write_text("asin\nB000000001\n", encoding="utf-8")
    config.write_text("[worker]\nproxy_session_ports=[10000,10001]\nproxy_session_retry_per_asin=1\n", encoding="utf-8")
    calls = []

    class Process:
        def __init__(self, returncode): self.returncode = returncode
        def poll(self): return self.returncode

    def popen(command, **_kwargs):
        calls.append(command)
        if len(calls) == 1:
            return Process(0)
        run_dir = runs / "run-control-capacity"
        run_dir.mkdir()
        (run_dir / "receipt.json").write_text(
            json.dumps({"status": "failed", "error": "Capacity gate denied: capacity_reserved_elsewhere"}),
            encoding="utf-8",
        )
        return Process(2)

    monkeypatch.setattr(controller_host.subprocess, "Popen", popen)
    monkeypatch.setattr(controller_host, "_run_projection", lambda _command: 0)
    result = controller_host.main([
        "--release-root", str(release), "--operator-data-root", str(data_root), "--batch-dir", str(batch),
        "--manifest", str(manifest), "--config", str(config), "--tenant-id", "amazon_us_main",
        "--subject-type", "own", "--batch-id", batch.name, "--worker-count", "1", "--limit", "10",
        "--console-port", "8773", "--recovery-seconds", "60", "--powershell", str(powershell),
    ])

    assert result == 2
    assert len(calls) == 2
    progress = json.loads((batch / "progress.json").read_text(encoding="utf-8"))
    assert progress["latest_error"] == "capacity_reserved_elsewhere"


def test_controller_host_converts_operator_stop_marker_to_graceful_child_stop(tmp_path):
    batch = tmp_path / ("batch-" + "5" * 24)
    batch.mkdir()
    (batch / "operator.stop.json").write_text("{}", encoding="utf-8")
    events = []

    class Process:
        returncode = None
        def poll(self): return self.returncode
        def terminate(self): events.append("terminate"); self.returncode = 130
        def wait(self, timeout=None): events.append(("wait", timeout)); return self.returncode

    exit_code, stopped = controller_host._wait_controller_process(Process(), batch)

    assert exit_code == 130
    assert stopped is True
    assert events == ["terminate", ("wait", 5)]


def test_controller_host_releases_exact_stopped_batch_capacity_before_marker(tmp_path, monkeypatch):
    release = tmp_path / "release"
    scripts = release / "scripts"
    scripts.mkdir(parents=True)
    gate = scripts / "proxy_capacity_gate.py"
    gate.write_text("", encoding="utf-8")
    batch = tmp_path / ("batch-" + "7" * 24)
    run_dir = batch / "control" / "runs" / "run-control-fixture"
    run_dir.mkdir(parents=True)
    config = batch / "batch.toml"
    config.write_text("[worker]\n", encoding="utf-8")
    (run_dir / "capacity.authorization.json").write_text(json.dumps({
        "reservation_id": "capacity-run-fixture", "owner_id": "amazon-us-run-fixture",
        "requested_capacity": 3,
    }), encoding="utf-8")
    calls = []
    monkeypatch.setattr(controller_host.subprocess, "run", lambda command, **kwargs: calls.append((command, kwargs)) or type("Result", (), {"returncode": 0})())

    assert controller_host._release_stopped_capacity(release, batch, config, "amazon_us_main") is True

    command = calls[0][0]
    assert command[command.index("--tenant-id") + 1] == "amazon_us_main"
    assert command[command.index("--release-reservation-id") + 1] == "capacity-run-fixture"
    assert command[command.index("--reservation-owner") + 1] == "amazon-us-run-fixture"
    marker = json.loads((batch / "control" / "operator-capacity-cleanup.json").read_text(encoding="utf-8"))
    assert marker == {"schema": "amazon-operator-capacity-cleanup-v1", "status": "released", "reservations": ["capacity-run-fixture"]}


@pytest.mark.parametrize("subject_type", ["competitor", "candidate"])
def test_operator_export_projects_the_requested_subject_type(tmp_path, monkeypatch, subject_type):
    batch = tmp_path / ("batch-" + "0" * 24)
    batch.mkdir()
    manifest = batch / "manifest.csv"
    manifest.write_text("asin\nB000000001\n", encoding="utf-8")
    captured = {}

    class Result:
        def fetchall(self):
            return []

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, sql, params):
            captured.update(sql=sql, params=params)
            return Result()

    psycopg = types.ModuleType("psycopg")
    psycopg.connect = lambda *_args, **_kwargs: Connection()
    psycopg_rows = types.ModuleType("psycopg.rows")
    psycopg_rows.dict_row = object()
    monkeypatch.setitem(sys.modules, "psycopg", psycopg)
    monkeypatch.setitem(sys.modules, "psycopg.rows", psycopg_rows)
    monkeypatch.setenv("TEST_OPERATOR_DSN", "postgresql://fixture")

    assert operator_export.main([
        "status", "--tenant-id", "amazon_us_main", "--subject-type", subject_type,
        "--batch-id", batch.name, "--batch-dir", str(batch), "--manifest", str(manifest),
        "--dsn-env", "TEST_OPERATOR_DSN",
    ]) == 0
    assert captured["params"] == ("amazon_us_main", subject_type, ["B000000001"])
    assert "s.subject_type=%s" in captured["sql"]


def test_runner_zero_without_complete_projection_fails_closed(tmp_path):
    manager = BatchManager(settings(tmp_path))
    batch_id = "batch-" + "1" * 24
    directory = manager.settings.data_root / "batches" / batch_id
    directory.mkdir()
    record = {
        "schema_version": "amazon-operator-batch-v1", "batch_id": batch_id, "tenant_id": "operator_fixture",
        "status": "running", "worker_count": 1, "requested": 2, "claimed": 0, "completed": 0,
        "variant": 0, "failed": 0, "blocked": 0, "pending": 2, "latest_error": None,
        "items": [], "started_at": "2026-09-06T00:00:00+00:00", "finished_at": None,
    }
    (directory / "batch.json").write_text(json.dumps(record), encoding="utf-8")
    manager._finish_record(batch_id, 0)
    result = manager.get_batch(batch_id)
    assert result["status"] == "failed" and result["latest_error"] == "status_projection_unavailable"


def test_null_progress_error_does_not_erase_terminal_runner_failure(tmp_path):
    configured = settings(tmp_path)
    manager = BatchManager(configured)
    batch_id = "batch-" + "a" * 24
    batch_dir = configured.data_root / "batches" / batch_id
    batch_dir.mkdir(parents=True)
    record = {
        "schema_version": "amazon-operator-batch-v1", "batch_id": batch_id,
        "tenant_id": configured.business_tenant_id, "subject_type": "own",
        "status": "running", "worker_count": 1, "requested": 1,
        "claimed": 0, "completed": 0, "variant": 0, "failed": 0,
        "blocked": 0, "pending": 1, "latest_error": None,
        "started_at": "2026-09-07T00:00:00+00:00", "finished_at": None,
    }
    (batch_dir / "batch.json").write_text(json.dumps(record), encoding="utf-8")
    (batch_dir / "progress.json").write_text(json.dumps({**record, "status": "failed"}), encoding="utf-8")

    manager._finish_record(batch_id, 1)

    assert manager.get_batch(batch_id)["latest_error"] == "runner_failed"
    receipt = json.loads((batch_dir / "receipt.json").read_text(encoding="utf-8"))
    assert receipt["latest_error"] == "runner_failed"


def test_cancel_failure_stays_cancel_pending_until_backend_terminal_is_confirmed(tmp_path):
    manager = BatchManager(settings(tmp_path))
    batch_id = "batch-" + "e" * 24
    batch_dir = manager.settings.data_root / "batches" / batch_id
    batch_dir.mkdir(parents=True)
    record = {
        "schema_version": "amazon-operator-batch-v1", "batch_id": batch_id,
        "tenant_id": "owned-products", "subject_type": "own", "status": "stopping",
        "worker_count": 2, "requested": 1, "claimed": 0, "completed": 0,
        "variant": 0, "failed": 0, "blocked": 0, "pending": 1,
        "latest_error": None, "stop_requested": True,
        "started_at": "2026-09-14T00:00:00+00:00", "finished_at": None,
    }
    (batch_dir / "batch.json").write_text(json.dumps(record), encoding="utf-8")
    (batch_dir / "progress.json").write_text(json.dumps({
        **record,
        "status": "failed",
        "campaign_status": "active",
        "latest_error": "campaign_poll_failed",
    }), encoding="utf-8")

    manager._finish_record(batch_id, 4)

    result = manager.get_batch(batch_id)
    assert result["status"] == "cancel_pending"
    assert result["latest_error"] == "campaign_cancel_unconfirmed"
    assert result["finished_at"] is None
    assert manager._active_batch()["batch_id"] == batch_id


def test_cancel_runner_failure_before_first_status_stays_cancel_pending(tmp_path):
    manager = BatchManager(settings(tmp_path))
    batch_id = "batch-" + "1" * 24
    batch_dir = manager.settings.data_root / "batches" / batch_id
    batch_dir.mkdir(parents=True)
    record = {
        "schema_version": "amazon-operator-batch-v1", "batch_id": batch_id,
        "tenant_id": "owned-products", "subject_type": "own", "status": "stopping",
        "worker_count": 2, "requested": 1, "claimed": 0, "completed": 0,
        "variant": 0, "failed": 0, "blocked": 0, "pending": 1,
        "latest_error": None, "stop_requested": True,
        "started_at": "2026-09-14T00:00:00+00:00", "finished_at": None,
    }
    (batch_dir / "batch.json").write_text(json.dumps(record), encoding="utf-8")
    (batch_dir / "campaign.json").write_text(json.dumps({
        "campaign_id": "campaign-" + "1" * 32,
    }), encoding="utf-8")

    manager._finish_record(batch_id, 4)

    result = manager.get_batch(batch_id)
    assert result["status"] == "cancel_pending"
    assert result["latest_error"] == "campaign_cancel_unconfirmed"


def test_completed_campaign_wins_over_late_operator_stop_at_batch_manager_seam(tmp_path):
    manager = BatchManager(settings(tmp_path))
    batch_id = "batch-" + "f" * 24
    batch_dir = manager.settings.data_root / "batches" / batch_id
    batch_dir.mkdir(parents=True)
    record = {
        "schema_version": "amazon-operator-batch-v1", "batch_id": batch_id,
        "tenant_id": "owned-products", "subject_type": "own", "status": "stopping",
        "worker_count": 2, "requested": 1, "claimed": 0, "completed": 0,
        "variant": 0, "failed": 0, "blocked": 0, "pending": 1,
        "latest_error": None, "stop_requested": True,
        "started_at": "2026-09-14T00:00:00+00:00", "finished_at": None,
    }
    (batch_dir / "batch.json").write_text(json.dumps(record), encoding="utf-8")
    (batch_dir / "progress.json").write_text(json.dumps({
        **record,
        "status": "completed",
        "campaign_status": "completed",
        "completed": 1,
        "pending": 0,
        "finished_at": "2026-09-14T00:05:00+00:00",
    }), encoding="utf-8")

    manager._finish_record(batch_id, 0)

    result = manager.get_batch(batch_id)
    assert result["status"] == "completed"
    assert result["completed"] == 1
    assert json.loads((batch_dir / "receipt.json").read_text(encoding="utf-8"))["status"] == "completed"


def test_backend_completed_campaign_is_not_failed_by_a_lagging_member_projection(tmp_path):
    manager = BatchManager(settings(tmp_path))
    batch_id = "batch-" + "9" * 24
    batch_dir = manager.settings.data_root / "batches" / batch_id
    batch_dir.mkdir(parents=True)
    record = {
        "schema_version": "amazon-operator-batch-v1", "batch_id": batch_id,
        "tenant_id": "owned-products", "subject_type": "own", "status": "running",
        "worker_count": 2, "requested": 1, "claimed": 0, "completed": 0,
        "variant": 0, "failed": 0, "blocked": 0, "pending": 1,
        "latest_error": None, "stop_requested": False,
        "started_at": "2026-09-14T00:00:00+00:00", "finished_at": None,
    }
    (batch_dir / "batch.json").write_text(json.dumps(record), encoding="utf-8")
    (batch_dir / "campaign.json").write_text(json.dumps({
        "campaign_id": "campaign-" + "9" * 32,
    }), encoding="utf-8")
    (batch_dir / "progress.json").write_text(json.dumps({
        **record,
        "campaign_status": "completed",
    }), encoding="utf-8")

    manager._finish_record(batch_id, 0)

    result = manager.get_batch(batch_id)
    assert result["campaign_status"] == "completed"
    assert result["status"] == "completed"
    assert result["latest_error"] is None


def test_monitor_releases_batch_mutex_when_finalization_raises(tmp_path, monkeypatch):
    manager = BatchManager(settings(tmp_path))
    released = []
    manager._mutex = type("Mutex", (), {"release": lambda _self: released.append(True)})()
    monkeypatch.setattr(manager, "_finish_record", lambda *_args: (_ for _ in ()).throw(RuntimeError("disk failure")))
    process = type("Process", (), {"wait": lambda _self: 0})()
    with pytest.raises(RuntimeError, match="disk failure"):
        manager._monitor("batch-" + "2" * 24, process)
    assert released == [True]


def test_stopped_batch_holds_mutex_until_worker_host_cleanup_receipt(tmp_path, monkeypatch):
    manager = BatchManager(settings(tmp_path))
    batch_id = "batch-" + "3" * 24
    run_dir = manager._batch_dir(batch_id) / "control" / "runs" / "run-control-fixture"
    run_dir.mkdir(parents=True)
    (manager._batch_dir(batch_id) / "operator.stop.json").write_text("{}", encoding="utf-8")
    (run_dir / "capacity.authorization.json").write_text("{}", encoding="utf-8")
    events = []
    manager._mutex = type("Mutex", (), {"release": lambda _self: events.append("released")})()
    monkeypatch.setattr(manager, "_finish_record", lambda *_args: events.append("finished"))
    process = type("Process", (), {"wait": lambda _self: 130})()

    thread = threading.Thread(target=manager._monitor, args=(batch_id, process))
    thread.start()
    time.sleep(0.1)
    assert events == []

    (manager._batch_dir(batch_id) / "control" / "operator-capacity-cleanup.json").write_text(
        '{"schema":"amazon-operator-capacity-cleanup-v1","status":"released"}', encoding="utf-8",
    )
    thread.join(timeout=2)
    assert events == ["finished", "released"]


def test_http_restart_and_page_refresh_returns_latest_batch_without_empty_id(tmp_path):
    configured = settings(tmp_path)
    manager = BatchManager(configured)
    preview = manager.upload_preview("input.xlsx", make_xlsx([["ASIN"], ["B000000001"]]))
    started = manager.start_batch(preview["import_id"], 1)
    deadline = time.monotonic() + 5
    while manager.get_batch(started["batch_id"])["status"] not in {"completed", "failed"} and time.monotonic() < deadline:
        time.sleep(0.02)
    restarted = BatchManager(settings(tmp_path))
    server = create_server(restarted, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
    try:
        _request(opener, f"http://127.0.0.1:{server.server_port}/")
        _, _, payload = _request(opener, f"http://127.0.0.1:{server.server_port}/api/get_batch")
        assert json.loads(payload)["batch_id"] == started["batch_id"]
        source = (ROOT / "operator" / "app.js").read_text(encoding="utf-8")
        assert "batch_id=${encodeURIComponent(batchId)}" in source and "batchId||\"\"" not in source
        assert "batch.latest_error" in source
        assert 'cancel_pending:"取消待确认"' in source
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=2)


def test_only_one_active_batch_and_stop_is_batch_scoped(tmp_path):
    configured = settings(tmp_path)
    configured.fake_delay_seconds = 0.2
    manager = BatchManager(configured)
    one = manager.upload_preview("one.xlsx", make_xlsx([["ASIN"], ["B000000001"], ["B000000002"]]))
    two = manager.upload_preview("two.xlsx", make_xlsx([["ASIN"], ["B000000003"]]))
    active = manager.start_batch(one["import_id"], 1)
    with pytest.raises(BatchConflict):
        manager.start_batch(two["import_id"], 1)
    stopped = manager.stop_batch(active["batch_id"])
    assert stopped["batch_id"] == active["batch_id"]
    assert stopped["stop_requested"] is True
    assert (configured.data_root / "batches" / active["batch_id"] / "operator.stop.json").exists()
    deadline = time.monotonic() + 3
    while manager.get_batch(active["batch_id"])["status"] != "stopped" and time.monotonic() < deadline:
        time.sleep(0.02)
    assert manager.get_batch(active["batch_id"])["status"] == "stopped"


def test_production_stop_signals_batch_before_any_force_termination(tmp_path, monkeypatch):
    configured = settings(tmp_path)
    configured.fake_runner = False
    manager = BatchManager(configured)
    batch_id = "batch-" + "6" * 24
    batch_dir = manager._batch_dir(batch_id)
    batch_dir.mkdir(parents=True)
    record = {"batch_id": batch_id, "status": "running", "started_at": "2026-09-07T00:00:00+00:00"}
    (batch_dir / "batch.json").write_text(json.dumps(record), encoding="utf-8")
    (batch_dir / "control.json").write_text(json.dumps({"pid": 123, "process_start_identity": "fixture"}), encoding="utf-8")
    events = []

    class Process:
        def poll(self): return None
        def terminate(self): events.append("terminate")

    manager._processes[batch_id] = Process()
    monkeypatch.setattr("operator_core._process_matches", lambda *_args: True)
    stopped = manager.stop_batch(batch_id)

    assert stopped["stop_requested"] is True
    assert (batch_dir / "operator.stop.json").is_file()
    assert events == []


def test_new_manager_recovers_active_batch_projection(tmp_path):
    configured = settings(tmp_path)
    configured.fake_delay_seconds = 0.4
    first = BatchManager(configured)
    preview = first.upload_preview("input.xlsx", make_xlsx([["ASIN"], ["B000000001"], ["B000000002"]]))
    started = first.start_batch(preview["import_id"], 1)
    recovered = BatchManager(settings(tmp_path)).get_batch(started["batch_id"])
    assert recovered["status"] in {"starting", "running", "completed"}
    assert recovered["requested"] == 2
    first.stop_batch(started["batch_id"])


def _request(opener, url, method="GET", data=None, headers=None):
    request = urllib.request.Request(url, method=method, data=data, headers=headers or {})
    with opener.open(request, timeout=3) as response:
        return response.status, dict(response.headers), response.read()


def test_http_fake_runner_end_to_end_auth_csrf_download_and_restart(tmp_path):
    manager = BatchManager(settings(tmp_path))
    server = create_server(manager, host="127.0.0.1", port=0)
    import threading
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    jar = CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    try:
        _, page_headers, page = _request(opener, base + "/")
        cookie = page_headers["Set-Cookie"]
        assert "HttpOnly" in cookie and "SameSite=Strict" in cookie and "Max-Age=" in cookie
        csrf = page.decode("utf-8").split('data-csrf="', 1)[1].split('"', 1)[0]
        boundary = "operator-test-boundary"
        workbook = make_xlsx([["ASIN"], ["B000000001"], ["B000000002"]])
        upload = (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"input.xlsx\"\r\n"
            "Content-Type: application/vnd.openxmlformats-officedocument.spreadsheetml.sheet\r\n\r\n"
        ).encode() + workbook + f"\r\n--{boundary}--\r\n".encode()
        with pytest.raises(urllib.error.HTTPError) as unauthorized:
            _request(urllib.request.build_opener(), base + "/api/upload_preview", "POST", upload, {
                "Content-Type": f"multipart/form-data; boundary={boundary}", "X-CSRF-Token": csrf,
            })
        assert unauthorized.value.code == 401
        _, _, preview_raw = _request(opener, base + "/api/upload_preview", "POST", upload, {
            "Content-Type": f"multipart/form-data; boundary={boundary}", "X-CSRF-Token": csrf,
        })
        preview = json.loads(preview_raw)
        with pytest.raises(urllib.error.HTTPError) as csrf_error:
            _request(opener, base + "/api/start_batch", "POST", json.dumps({"import_id": preview["import_id"], "worker_count": 1}).encode(), {"Content-Type": "application/json"})
        assert csrf_error.value.code == 403
        _, _, started_raw = _request(opener, base + "/api/start_batch", "POST", json.dumps({"import_id": preview["import_id"], "worker_count": 1}).encode(), {"Content-Type": "application/json", "X-CSRF-Token": csrf})
        batch_id = json.loads(started_raw)["batch_id"]
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            _, _, current_raw = _request(opener, base + f"/api/get_batch?batch_id={batch_id}")
            current = json.loads(current_raw)
            if current["status"] == "completed":
                break
            time.sleep(0.02)
        assert current["completed"] == 2 and current["pending"] == 0, current
        for artifact in ("manifest", "results_csv", "results_json", "receipt"):
            status, headers, body = _request(opener, base + f"/api/download_artifact?batch_id={batch_id}&artifact={artifact}")
            assert status == 200 and body and "attachment" in headers["Content-Disposition"]
        with pytest.raises(urllib.error.HTTPError) as unsafe:
            _request(opener, base + f"/api/download_artifact?batch_id={batch_id}&artifact=../../secret")
        assert unsafe.value.code == 404
        with pytest.raises(urllib.error.HTTPError) as no_download_auth:
            _request(urllib.request.build_opener(), base + f"/api/download_artifact?batch_id={batch_id}&artifact=receipt")
        assert no_download_auth.value.code == 401
        bad_host = urllib.request.Request(base + "/healthz", headers={"Host": "evil.test"})
        with pytest.raises(urllib.error.HTTPError) as host_error:
            urllib.request.urlopen(bad_host, timeout=3)
        assert host_error.value.code == 403
        assert str(tmp_path).encode() not in current_raw and b"POSTGRES" not in current_raw and b"PROXY" not in current_raw
        recovered = BatchManager(settings(tmp_path)).get_batch(batch_id)
        assert recovered["status"] == "completed" and recovered["requested"] == 2
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_real_powershell_entry_start_status_open_health_stop(tmp_path):
    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    if powershell is None:
        pytest.skip("Windows PowerShell unavailable")
    def free_port():
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            return listener.getsockname()[1]
    port = free_port()
    internal_port = free_port()
    data_root = tmp_path / "operator-entry"
    vault = tmp_path / "unused.dpapi"
    vault.write_text("offline-fake", encoding="utf-8")
    common = [
        "-Port", str(port), "-InternalConsolePort", str(internal_port),
        "-DataRoot", str(data_root), "-ConfigPath", str(ROOT / "config" / "amazon_us.windows.toml"),
        "-VaultPath", str(vault),
    ]
    script = ROOT / "operator.ps1"
    stop = [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), "stop", *common]
    try:
        start_stdout = tmp_path / "start.stdout.log"
        start_stderr = tmp_path / "start.stderr.log"
        with start_stdout.open("wb") as stdout, start_stderr.open("wb") as stderr:
            started = subprocess.run(
                [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), "start", "-FakeRunner", *common],
                stdout=stdout, stderr=stderr, timeout=20,
            )
        assert started.returncode == 0, start_stderr.read_bytes().decode("utf-8", errors="replace")
        status = subprocess.run(
            [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), "status", *common],
            capture_output=True, timeout=10,
        )
        assert status.returncode == 0
        status_payload = json.loads(status.stdout.decode("utf-8-sig", errors="strict"))
        assert status_payload["running"] is True and status_payload["healthy"] is True
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=3) as response:
            assert json.loads(response.read())["ok"] is True
        open_probe = subprocess.run(
            [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), "open", "-PrintUrl", "-Port", str(port)],
            capture_output=True, timeout=10,
        )
        assert open_probe.returncode == 0 and f"http://127.0.0.1:{port}" in open_probe.stdout.decode("utf-8", errors="replace")
    finally:
        subprocess.run(stop, capture_output=True, timeout=10)
    with pytest.raises(OSError):
        urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1)


def test_concurrent_real_powershell_start_has_one_service_and_one_lock(tmp_path):
    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    if powershell is None:
        pytest.skip("Windows PowerShell unavailable")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0)); port = listener.getsockname()[1]
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0)); internal_port = listener.getsockname()[1]
    data_root = tmp_path / "operator-concurrent"
    vault = tmp_path / "unused.dpapi"
    vault.write_text("offline-fake", encoding="utf-8")
    script = ROOT / "operator.ps1"
    common = ["-Port", str(port), "-InternalConsolePort", str(internal_port), "-DataRoot", str(data_root),
              "-ConfigPath", str(ROOT / "config" / "amazon_us.windows.toml"), "-VaultPath", str(vault)]
    command = [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), "start", "-FakeRunner", *common]
    processes = []
    handles = []
    try:
        for index in range(2):
            stdout = (tmp_path / f"start-{index}.stdout.log").open("wb")
            stderr = (tmp_path / f"start-{index}.stderr.log").open("wb")
            handles.extend([stdout, stderr])
            processes.append(subprocess.Popen(command, stdout=stdout, stderr=stderr))
        codes = [process.wait(timeout=20) for process in processes]
        assert codes.count(0) == 1 and sum(code != 0 for code in codes) == 1
        lock = json.loads((data_root / "control" / "operator.lock.json").read_text(encoding="utf-8-sig"))
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=3) as response:
            assert json.loads(response.read())["ok"] is True
        status = subprocess.run(
            [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), "status", *common],
            capture_output=True, timeout=10,
        )
        status_payload = json.loads(status.stdout.decode("utf-8-sig"))
        assert status_payload["running"] is True and status_payload["pid"] == lock["pid"]
    finally:
        for handle in handles: handle.close()
        subprocess.run(
            [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), "stop", *common],
            capture_output=True, timeout=10,
        )


def test_real_powershell_failed_child_start_removes_lock_and_listener(tmp_path):
    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    if powershell is None:
        pytest.skip("Windows PowerShell unavailable")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0)); port = listener.getsockname()[1]
    data_root = tmp_path / "operator-failed-start"
    vault = tmp_path / "unused.dpapi"
    vault.write_text("offline-fake", encoding="utf-8")
    stderr_path = tmp_path / "failed.stderr.log"
    with (tmp_path / "failed.stdout.log").open("wb") as stdout, stderr_path.open("wb") as stderr:
        result = subprocess.run([
            powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ROOT / "operator.ps1"),
            "start", "-FakeRunner", "-Port", str(port), "-InternalConsolePort", str(port),
            "-DataRoot", str(data_root), "-ConfigPath", str(ROOT / "config" / "amazon_us.windows.toml"),
            "-VaultPath", str(vault), "-StartHealthAttempts", "4",
        ], stdout=stdout, stderr=stderr, timeout=20)
    assert result.returncode != 0
    assert not (data_root / "control" / "operator.lock.json").exists()
    with pytest.raises(OSError):
        urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1)


def test_real_powershell_stop_timeout_returns_nonzero_and_preserves_lock(tmp_path):
    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    if powershell is None:
        pytest.skip("Windows PowerShell unavailable")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0)); port = listener.getsockname()[1]
    data_root = tmp_path / "operator-stop-timeout"
    control = data_root / "control"
    control.mkdir(parents=True)
    script = ROOT / "operator.ps1"
    command = (
        f"$p=Get-Process -Id $PID; "
        f"[ordered]@{{schema='amazon-operator-lock-v1';pid=$PID;start_time=$p.StartTime.ToUniversalTime().ToString('o');port={port}}}"
        f"|ConvertTo-Json|Set-Content -LiteralPath '{control / 'operator.lock.json'}' -Encoding UTF8; "
        "function Stop-Process { [CmdletBinding()]param([int]$Id,[switch]$Force) }; "
        f"& '{script}' stop -Port {port} -DataRoot '{data_root}' -StopWaitMilliseconds 20"
    )
    result = subprocess.run(
        [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
        capture_output=True, timeout=10,
    )
    assert result.returncode != 0
    assert (control / "operator.lock.json").exists()
    assert b"lock retained" in result.stderr


def test_concurrent_real_powershell_stop_then_start_keeps_new_service_managed(tmp_path):
    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    if powershell is None:
        pytest.skip("Windows PowerShell unavailable")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0)); port = listener.getsockname()[1]
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0)); internal_port = listener.getsockname()[1]
    data_root = tmp_path / "operator-stop-start"
    vault = tmp_path / "unused.dpapi"
    vault.write_text("offline-fake", encoding="utf-8")
    script = ROOT / "operator.ps1"
    common = ["-Port", str(port), "-InternalConsolePort", str(internal_port), "-DataRoot", str(data_root),
              "-ConfigPath", str(ROOT / "config" / "amazon_us.windows.toml"), "-VaultPath", str(vault)]
    start = [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), "start", "-FakeRunner", *common]
    stop = [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), "stop", *common]
    with (tmp_path / "initial.stdout.log").open("wb") as stdout, (tmp_path / "initial.stderr.log").open("wb") as stderr:
        initial = subprocess.run(start, stdout=stdout, stderr=stderr, timeout=20)
    assert initial.returncode == 0
    stop_entered = tmp_path / "stop-entered.marker"
    delayed_stop = (
        "function Stop-Process { [CmdletBinding()]param([int]$Id,[switch]$Force) "
        f"Set-Content -LiteralPath '{stop_entered}' -Value entered; Start-Sleep -Milliseconds 500; "
        "Microsoft.PowerShell.Management\\Stop-Process @PSBoundParameters }; "
        f"& '{script}' stop -Port {port} -DataRoot '{data_root}'"
    )
    start_stdout = (tmp_path / "restart.stdout.log").open("wb")
    start_stderr = (tmp_path / "restart.stderr.log").open("wb")
    try:
        stopping = subprocess.Popen(
            [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", delayed_stop],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 5
        while not stop_entered.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert stop_entered.exists(), "stop did not enter the shared control-mutex critical section"
        restarting = subprocess.Popen(start, stdout=start_stdout, stderr=start_stderr)
        assert stopping.wait(timeout=15) == 0
        assert restarting.wait(timeout=20) == 0
        lock = json.loads((data_root / "control" / "operator.lock.json").read_text(encoding="utf-8-sig"))
        status = subprocess.run(
            [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), "status", *common],
            capture_output=True, timeout=10,
        )
        status_payload = json.loads(status.stdout.decode("utf-8-sig"))
        assert status.returncode == 0 and status_payload["healthy"] is True
        assert status_payload["pid"] == lock["pid"]
    finally:
        start_stdout.close(); start_stderr.close()
        subprocess.run(stop, capture_output=True, timeout=10)
    with pytest.raises(OSError):
        urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1)
