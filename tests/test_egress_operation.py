from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "egress_operation.py"


def load_module():
    spec = importlib.util.spec_from_file_location("egress_operation_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_egress_result_is_safe_and_independent_of_asin_collection(tmp_path, monkeypatch):
    module = load_module()
    config = tmp_path / "worker.toml"
    config.write_text(
        "[worker]\nproxy_url='http://proxy.example:823'\n"
        "proxy_username_env='PROXY_USER'\nproxy_password_env='PROXY_PASS'\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("PROXY_USER", "secret-user")
    monkeypatch.setenv("PROXY_PASS", "secret-pass")

    result = module.run_egress_check(
        config,
        probe=lambda *_args, **_kwargs: {
            "ok": True,
            "status": 200,
            "block_reason": None,
            "elapsed_ms": 2534.1,
            "response_bytes": 42,
        },
    )

    assert result == {
        "ok": True,
        "status": 200,
        "error_class": None,
        "elapsed_ms": 2534.1,
        "response_bytes": 42,
    }
    encoded = repr(result)
    assert "proxy.example" not in encoded
    assert "secret-user" not in encoded
    assert "secret-pass" not in encoded
    assert "requested_actions" not in encoded and "recorded_actions" not in encoded
