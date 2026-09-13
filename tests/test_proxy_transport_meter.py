from __future__ import annotations

import importlib.util
from pathlib import Path
import threading

import pytest


ROOT = Path(__file__).resolve().parents[1]


def load_module():
    spec = importlib.util.spec_from_file_location(
        "proxy_transport_meter_test", ROOT / "scripts" / "proxy_transport_meter.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_snapshot_separates_exact_response_bodies_from_forwarded_tls_and_supplier_billing():
    module = load_module()
    meter = module.ProxyTransportMeter()
    meter.record_http_response_body(100)
    meter.record_tunnel_client_to_upstream(20)
    meter.record_tunnel_upstream_to_client(180)

    assert meter.snapshot() == {
        "schema_version": "amazon-proxy-transport-v1",
        "http_compressed_response_body": {
            "bytes": 100,
            "measurement": "exact_body_bytes",
        },
        "proxy_tunnel_forwarded_tls": {
            "client_to_upstream_bytes": 20,
            "upstream_to_client_bytes": 180,
            "total_bytes": 200,
            "measurement": "exact_forwarded_payload_bytes",
            "observed": True,
        },
        "direct_http_tunnel_total_bytes": None,
        "supplier_billed_bytes": None,
    }


def test_meter_is_thread_safe_and_reset_is_action_scoped():
    module = load_module()
    meter = module.ProxyTransportMeter()

    threads = [
        threading.Thread(target=lambda: [meter.record_tunnel_upstream_to_client(1) for _ in range(500)])
        for _ in range(4)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert meter.snapshot()["proxy_tunnel_forwarded_tls"]["total_bytes"] == 2000
    old_generation = meter.generation
    meter.reset()
    assert meter.snapshot()["proxy_tunnel_forwarded_tls"]["total_bytes"] == 0
    meter.record_tunnel_upstream_to_client(99, generation=old_generation)
    assert meter.snapshot()["proxy_tunnel_forwarded_tls"]["total_bytes"] == 0
    meter.record_tunnel_upstream_to_client(7, generation=meter.generation)
    assert meter.snapshot()["proxy_tunnel_forwarded_tls"]["total_bytes"] == 7


def test_negative_byte_counts_are_rejected():
    module = load_module()
    meter = module.ProxyTransportMeter()

    with pytest.raises(ValueError, match="non-negative"):
        meter.record_http_response_body(-1)
