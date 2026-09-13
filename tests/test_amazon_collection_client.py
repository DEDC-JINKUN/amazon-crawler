from __future__ import annotations

import importlib.util
import io
from pathlib import Path
import subprocess
import sys
import urllib.error


ROOT = Path(__file__).resolve().parents[1]


def load_client():
    spec = importlib.util.spec_from_file_location(
        "amazon_collection_client_test", ROOT / "scripts" / "amazon_collection_client.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_wait_polls_one_job_per_interval_and_stays_below_sixty_requests_per_minute(monkeypatch):
    module = load_client()
    client = module.AmazonCollectionClient("http://127.0.0.1:8765", "agent", "key")
    calls = []
    clock = {"now": 0.0}

    def get_job(job_id):
        calls.append((job_id, clock["now"]))
        cycle = sum(1 for value, _at in calls if value == job_id)
        return {"job": {"job_id": job_id, "status": "completed" if cycle == 2 else "queued"}}

    monkeypatch.setattr(client, "get_job", get_job)
    monkeypatch.setattr(module.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(module.time, "sleep", lambda seconds: clock.__setitem__("now", clock["now"] + seconds))

    result = client.wait_for_jobs([f"job-{index}" for index in range(5)], timeout_seconds=60, poll_seconds=0.01)

    assert len(result["results"]) == 5
    assert len(calls) == 10
    assert all(second[1] - first[1] >= 1.0 for first, second in zip(calls, calls[1:]))


def test_wait_honors_retry_after_without_losing_pending_jobs(monkeypatch):
    module = load_client()
    client = module.AmazonCollectionClient("http://127.0.0.1:8765", "agent", "key")
    clock = {"now": 0.0}
    calls = 0

    def get_job(_job_id):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise module.AgentRateLimitError(3.0)
        return {"job": {"status": "completed"}}

    monkeypatch.setattr(client, "get_job", get_job)
    monkeypatch.setattr(module.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(module.time, "sleep", lambda seconds: clock.__setitem__("now", clock["now"] + seconds))

    result = client.wait_for_jobs(["job-1"], timeout_seconds=10, poll_seconds=0.01)

    assert result["results"][0]["job"]["status"] == "completed"
    assert clock["now"] >= 3.0


def test_request_converts_http_429_retry_after_to_stable_rate_limit_error():
    module = load_client()
    client = module.AmazonCollectionClient("http://127.0.0.1:8765", "agent", "key")

    class Opener:
        def open(self, *_args, **_kwargs):
            raise urllib.error.HTTPError(
                "http://127.0.0.1:8765/v1/jobs/job-1", 429, "limited", {"Retry-After": "4"}, io.BytesIO(b"{}")
            )

    client._opener = Opener()
    try:
        client.get_job("job-1")
    except module.AgentRateLimitError as exc:
        assert exc.retry_after_seconds == 4.0
    else:
        raise AssertionError("expected AgentRateLimitError")


def test_json_output_forces_utf8_even_when_parent_requests_gbk():
    code = (
        "import sys; sys.path.insert(0, r'" + str(ROOT / "scripts") + "'); "
        "import amazon_collection_client as c; c.write_json({'title':'Möbel'})"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, check=False,
        env={**__import__("os").environ, "PYTHONIOENCODING": "gbk"},
    )

    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    assert '"title":"Möbel"' in result.stdout.decode("utf-8")
