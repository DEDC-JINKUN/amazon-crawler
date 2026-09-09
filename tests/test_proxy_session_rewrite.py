"""代理会话 ID 改写回归：ZooProxy -sid-XXX-t-N 与 DataImpulse _session-XXX 两种格式。"""
from __future__ import annotations

import re

from scripts.amazon_us_worker import _rewrite_proxy_session_id


def _seed(worker_id: str, nonce: int = 0) -> str:
    import hashlib

    return hashlib.sha256(f"zoo-{worker_id}-{nonce}".encode("utf-8")).hexdigest()[:10]


def test_zooproxy_sid_rewritten_with_sticky_minutes():
    out = _rewrite_proxy_session_id(
        "p9smiluq6d-region-US-sid-s291yKxd-t-10", "worker-a", nonce=0, sticky_minutes=120
    )
    assert out == f"p9smiluq6d-region-US-sid-{_seed('worker-a')}-t-120"


def test_zooproxy_nonce_changes_session():
    a = _rewrite_proxy_session_id("u-region-US-sid-x-t-10", "worker-a", nonce=0, sticky_minutes=120)
    b = _rewrite_proxy_session_id("u-region-US-sid-x-t-10", "worker-a", nonce=1, sticky_minutes=120)
    assert a != b


def test_dataimpulse_session_segment_replaced_in_place():
    out = _rewrite_proxy_session_id("myuser_session-old_country-us", "worker-b")
    assert out == f"myuser_session-{_seed('worker-b')}_country-us"


def test_dataimpulse_session_appended_when_missing():
    out = _rewrite_proxy_session_id("myuser_country-us", "worker-b", proxy_url="http://gw.dataimpulse.com:823")
    assert out == f"myuser_country-us_session-{_seed('worker-b')}"


def test_unknown_username_without_session_untouched():
    out = _rewrite_proxy_session_id("plainuser", "worker-c", proxy_url="http://other.example.com:5000")
    assert out == "plainuser"
