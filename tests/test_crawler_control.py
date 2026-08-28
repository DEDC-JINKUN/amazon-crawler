from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "crawler.ps1"
HOST = ROOT / "scripts" / "crawler_process_host.py"


def test_control_script_exposes_small_safe_command_surface():
    text = SCRIPT.read_text(encoding="utf-8")
    assert "ValidateSet('probe', 'run', 'status', 'console', 'stop', 'help')" in text
    assert "Read-Host" in text and "-AsSecureString" in text
    assert "--product-only" in text
    assert "--run-id" in text
    assert "--probe-egress" not in text
    assert "include-blocked" not in text.lower()


def test_control_script_has_locks_logs_receipts_and_safe_stop():
    text = SCRIPT.read_text(encoding="utf-8")
    for expected in (
        ".worker.lock.json",
        ".console.lock.json",
        "worker.stdout.log",
        "worker.stderr.log",
        "receipt.json",
        "StartTime",
        "RedirectStandardOutput",
        "RedirectStandardError",
        "stop -All",
        "System.Threading.Mutex",
        "crawler_process_host.py",
        "Stop-Process",
        "/readyz",
        "preflight.log",
        "Guid]::NewGuid",
    ):
        assert expected in text
    assert "PGPASSWORD" in text
    assert "Remove-Item Env:PGPASSWORD" in text
    assert "123456" not in text


def test_control_script_defaults_to_current_isolated_tenant():
    text = SCRIPT.read_text(encoding="utf-8")
    assert "real_batch_20260828_500_04" in text
    assert "data\\postgres_real_batch_20260828_500_04\\manifest_500.csv" in text
    assert "data\\postgres_real_batch_20260828_500_04\\batch500.toml" in text


def test_worker_host_waits_for_registration_gate_and_uses_kill_on_close_job():
    text = HOST.read_text(encoding="utf-8")
    assert "gate_path" in text
    assert "cancel_path" in text
    assert "JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE" in text
    assert "AssignProcessToJobObject" in text
    assert "return process.wait()" in text
