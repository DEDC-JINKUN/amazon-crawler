#!/usr/bin/env python3
"""Amazon US collection worker with PostgreSQL production storage.

HTTP fetches run first and a Firefox browser is created lazily only when
required fields are missing. SQLite remains available only for legacy replay
and offline regression tests; production actions use PostgreSQL leases.
"""
from __future__ import annotations

import argparse
import csv
import email.utils
import gzip
import hashlib
import html as html_module
import http.client
import inspect
import json
import math
import os
import random
import re
import sqlite3
import sys
import tempfile
import threading
import time
import tomllib
import urllib.error
import urllib.request
from collections import Counter, defaultdict, deque
from datetime import datetime, timedelta, timezone
from enum import Enum
from html.parser import HTMLParser
from http.cookiejar import Cookie, CookieJar
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import urljoin, urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parents[1]

try:
    from context_guard import validate_context
except ModuleNotFoundError:
    sys.path.insert(0, str(ROOT / "scripts"))
    from context_guard import validate_context

try:
    from proxy_tunnel_auth import ProxyTunnelAuthHTTPSHandler
except ModuleNotFoundError:
    sys.path.insert(0, str(ROOT / "scripts"))
    from proxy_tunnel_auth import ProxyTunnelAuthHTTPSHandler

try:
    from proxy_connect_relay import ProxyConnectRelay
except ModuleNotFoundError:
    sys.path.insert(0, str(ROOT / "scripts"))
    from proxy_connect_relay import ProxyConnectRelay

try:
    from proxy_capacity_gate import ProxyCapacityGateDenied, acquire_capacity_reservation, capacity_config_hash, reservation_slots_for
except ModuleNotFoundError:
    sys.path.insert(0, str(ROOT / "scripts"))
    from proxy_capacity_gate import ProxyCapacityGateDenied, acquire_capacity_reservation, capacity_config_hash, reservation_slots_for

DEFAULT_CONFIG = ROOT / "config" / "amazon_us.example.toml"
DEFAULT_MANIFEST = ROOT / "amazon_us_asin_manifest.csv"
DEFAULT_DB = ROOT / "state" / "amazon_us.sqlite3"
DEFAULT_OUTPUT_DIR = ROOT / "data" / "amazon_us"
PARSER_VERSION = "amazon-us-v3"
STATUSES = {"pending", "running", "product_done", "reviews_pending", "succeeded", "blocked", "failed"}
ALLOWED_TRANSITIONS: dict[str | None, set[str]] = {
    None: {"pending"},
    "pending": {"pending", "running", "failed"},
    "running": {"pending", "running", "product_done", "reviews_pending", "succeeded", "blocked", "failed"},
    "product_done": {"product_done", "reviews_pending", "succeeded", "failed", "pending"},
    "reviews_pending": {"reviews_pending", "running", "succeeded", "blocked", "failed", "pending"},
    "succeeded": {"succeeded", "running", "pending"},
    "blocked": {"blocked", "pending"},
    "failed": {"failed", "running", "pending"},
}
STOP_STATUSES = {403, 429}
STOP_PHRASES = (
    "robot check",
    "enter the characters",
    "captcha",
    "sorry we just need to make sure you're not a robot",
    "automated access",
    "access denied",
    "too many requests",
)
LOGIN_WALL_DOM_MARKERS = (
    "id=\"ap_email\"",
    "id='ap_email'",
    "id=\"ap_password\"",
    "id='ap_password'",
    "id=\"signinsubmit\"",
    "id='signinsubmit'",
    "authportal-main-section",
)
DEFAULTS: dict[str, Any] = {
    "request_timeout_seconds": 30,
    "http_max_attempts": 2,
    "http_retry_backoff_seconds": 0.5,
    "http_accept_encoding": "gzip",
    "rate_limit_cooldown_seconds": 3600,
    "headless": True,
    "max_actions_per_run": 10,
    "max_attempts": 3,
    "review_page_limit": 0,
    "agent_name": "amazon-us-worker",
    "user_agent": "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0 Agent/amazon-us-worker",
    "stop_on_block": True,
    "geckodriver_path": "/snap/bin/geckodriver",
    "firefox_binary": "",
    "marketplace": "US",
    "egress_profile": "proxy_sessions",
    "proxy_url": "",
    "proxy_username_env": "",
    "proxy_password_env": "",
    "proxy_session_ports": [],
    "proxy_session_mode": "sticky",
    "proxy_session_max_asins": 3,
    "proxy_session_retry_per_asin": 1,
    "proxy_session_consecutive_block_limit": 2,
    "proxy_session_window_size": 20,
    "proxy_session_window_block_limit": 3,
    "firefox_proxy_auth_mode": "disabled",
    "proxy_product_session_scope": "per_asin",
    "proxy_request_jitter_seconds": 0.0,
    "proxy_firefox_verify_on_access_block": True,
    "proxy_canary_url": "https://api.ipify.org?format=json",
    "proxy_canary_timeout_seconds": 15,
    "proxy_canary_max_age_seconds": 3600,
    "global_requests_per_second": 0.0,
    "egress_requests_per_second": 0.0,
    "rate_burst": 1,
    "egress_id": "direct",
    "context": {},
}
PRODUCT_HEADERS = [
    "asin", "marketplace", "canonical_url", "availability", "title", "brand", "rating",
    "reported_rating_count", "reported_review_count", "review_count", "review_count_source",
    "price", "bullets_json", "product_description", "specs_json", "buy_box_json",
    "top_reviews_json", "review_link", "review_section_anchor", "aplus_present", "collected_at", "status",
]
MEDIA_HEADERS = [
    "asin", "marketplace", "placement", "entry_type", "thumbnail_url", "display_url", "asset_url",
    "poster_url", "ordinal", "is_primary", "width", "height", "alt_text", "variant_asin",
    "load_status", "failure_reason", "unique_key",
]
CONTENT_HEADERS = ["asin", "marketplace", "module_type", "position", "order_index", "text", "image_url", "link_url", "status", "unique_key"]
REVIEW_SUMMARY_HEADERS = [
    "asin", "marketplace", "reported_rating_count", "reported_review_count", "reported_count_source",
    "fetched_count", "pages_fetched", "next_page", "status", "updated_at",
]
REVIEW_HEADERS = [
    "asin", "marketplace", "review_id", "rating", "title", "body", "review_url", "review_date",
    "locale", "verified", "body_truncated", "review_images_json", "page", "unique_key",
]
EVIDENCE_HEADERS = [
    "run_id", "asin", "marketplace", "url", "http_status", "transfer_bytes", "retrieved_at", "source_type",
    "content_hash", "raw_html_path", "block_reason", "parser_version", "error_code", "context_json",
]
OUTPUTS = {
    "product_snapshot": ("product_snapshot.csv", PRODUCT_HEADERS),
    "media_asset": ("media_asset.csv", MEDIA_HEADERS),
    "content_module": ("content_module.csv", CONTENT_HEADERS),
    "review_summary": ("review_summary.csv", REVIEW_SUMMARY_HEADERS),
    "review_record": ("review_record.csv", REVIEW_HEADERS),
    "collection_evidence": ("collection_evidence.csv", EVIDENCE_HEADERS),
}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _clean(value: str | None) -> str:
    return re.sub(r"\s+", " ", html_module.unescape(value or "")).strip()


def _number(value: str) -> str:
    match = re.search(r"[\d,.]+", value or "")
    return match.group(0).replace(",", "") if match else ""


def _firefox_proxy_settings(proxy_url: str) -> dict[str, str] | None:
    """Translate an explicit unauthenticated HTTP(S) proxy for Firefox."""
    value = proxy_url.strip()
    if not value:
        return None
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
        raise ValueError("Firefox proxy must be an explicit HTTP(S) URL without embedded credentials")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    hostport = f"{parts.hostname}:{port}"
    return {"proxyType": "manual", "httpProxy": hostport, "sslProxy": hostport}


def classify_block(status: int | None = None, text: str = "", title: str = "") -> str | None:
    if status in STOP_STATUSES:
        return f"http_{status}"
    raw_text = text.lower()
    if any(marker in raw_text for marker in ("awswafcookiedomainlist", "awswafintegration", "token.awswaf.com")):
        return "waf_challenge"
    title_match = re.search(r"<title[^>]*>(.*?)</title>", text, flags=re.IGNORECASE | re.DOTALL)
    page_title = _clean(f"{title} {title_match.group(1) if title_match else ''}").lower()
    auth_title = "amazon sign-in" in page_title or "amazon sign in" in page_title
    auth_form = re.search(
        r"<form\b(?=[^>]*\baction\s*=\s*['\"][^'\"]*/ap/signin(?:[?'\"]|$))[^>]*>",
        raw_text,
    )
    auth_dom = bool(auth_form) or any(marker in raw_text for marker in LOGIN_WALL_DOM_MARKERS)
    canonical_tag = re.search(
        r"<link\b(?=[^>]*\brel\s*=\s*['\"]canonical['\"])[^>]*>", raw_text
    )
    canonical_href = re.search(r"\bhref\s*=\s*['\"]([^'\"]+)['\"]", canonical_tag.group(0)) if canonical_tag else None
    canonical_url = html_module.unescape(canonical_href.group(1)) if canonical_href else ""
    canonical_parts = urlsplit(canonical_url)
    canonical_asin = re.search(r"/(?:dp|clp)/([a-z0-9]{10})(?:/|$)", canonical_parts.path)
    input_asin = re.search(
        r"<input\b(?=[^>]*(?:\bid|\bname)\s*=\s*['\"]asin['\"])(?=[^>]*\bvalue\s*=\s*['\"]([a-z0-9]{10})['\"])[^>]*>",
        raw_text,
    )
    product_title = re.search(
        r"<([a-z0-9]+)\b(?=[^>]*\bid\s*=\s*['\"]producttitle['\"])[^>]*>(.*?)</\1\s*>",
        raw_text,
        flags=re.DOTALL,
    )
    product_identity = bool(
        product_title
        and _clean(re.sub(r"<[^>]+>", " ", product_title.group(2)))
        and canonical_asin
        and canonical_parts.scheme == "https"
        and (canonical_parts.hostname or "").removeprefix("www.") == "amazon.com"
        and input_asin
    )
    if auth_title or (auth_dom and not product_identity):
        return "login_wall"
    if any(tag in text.lower() for tag in ("<script", "<style", "<noscript")):
        text = visible_html_text(text)
    haystack = f"{title}\n{text}".lower()
    for phrase in STOP_PHRASES:
        if phrase in haystack:
            if phrase == "robot check":
                return "robot"
            return phrase.replace(" ", "_").replace("'", "")
    return None


class AdapterFetchError(RuntimeError):
    """A browser fetch failure that can be checkpointed as failed."""

    def __init__(self, message: str, *, stage_code: str | None = None) -> None:
        super().__init__(message)
        self.stage_code = stage_code


class FallbackReason(str, Enum):
    HTTP_TRANSPORT_ERROR = "http_transport_error"
    ACCESS_CONTROL_VERIFICATION = "access_control_verification"
    ACCESS_CONTROL_RETRY = "access_control_retry"
    MISSING_ASIN = "missing_asin"
    MISSING_CANONICAL_URL = "missing_canonical_url"
    MISSING_TITLE = "missing_title"
    CONTEXT_MISMATCH = "context_mismatch"
    REVIEW_EMPTY = "review_empty"


class BrowserFallbackLedger:
    """Run-local admission ledger for auditable browser fallbacks."""

    def __init__(self) -> None:
        self._claims: set[tuple[str, str, FallbackReason]] = set()

    def claim(self, run_id: str, asin: str, reason: FallbackReason) -> bool:
        if not isinstance(reason, FallbackReason):
            raise TypeError("fallback reason must be a FallbackReason")
        key = (str(run_id), str(asin).upper(), reason)
        if key in self._claims:
            return False
        self._claims.add(key)
        return True


class RunScopedAmazonCookieSession:
    """In-memory anonymous Amazon cookies bound to one run/tenant/worker."""

    def __init__(
        self,
        run_id: str,
        tenant_id: str,
        worker_id: str,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.scope = (str(run_id), str(tenant_id), str(worker_id))
        self.jar = CookieJar()
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._accepted = 0
        self._rejected = 0
        self._closed = False

    @staticmethod
    def _valid_domain(raw_domain: Any) -> tuple[str, bool] | None:
        domain = str(raw_domain or "").strip().lower().rstrip(".")
        if not domain or any(ord(char) < 33 for char in domain):
            return None
        bare = domain.lstrip(".")
        if bare != "amazon.com" and not bare.endswith(".amazon.com"):
            return None
        return domain, domain.startswith(".")

    def sync_from_firefox(
        self,
        cookies: Iterable[dict[str, Any]],
        *,
        run_id: str,
        tenant_id: str,
        worker_id: str,
        context_confirmed: bool,
    ) -> int:
        if self._closed:
            raise RuntimeError("cookie session is closed")
        if (str(run_id), str(tenant_id), str(worker_id)) != self.scope:
            raise ValueError("cookie session scope mismatch")
        if not context_confirmed:
            return 0
        accepted = 0
        now_timestamp = int(self._now().timestamp())
        for item in cookies:
            domain_result = self._valid_domain(item.get("domain"))
            path = str(item.get("path") or "/")
            name = str(item.get("name") or "")
            value = str(item.get("value") or "")
            expiry_value = item.get("expiry")
            expiry: int | None = None
            try:
                if expiry_value is not None:
                    expiry = int(expiry_value)
            except (TypeError, ValueError, OverflowError):
                self._rejected += 1
                continue
            if (
                domain_result is None
                or not name
                or any(char in name for char in "\r\n\t;=,")
                or not path.startswith("/")
                or any(ord(char) < 32 for char in path)
                or (expiry is not None and (expiry <= now_timestamp or expiry > 253402300799))
            ):
                self._rejected += 1
                continue
            domain, initial_dot = domain_result
            cookie = Cookie(
                version=0,
                name=name,
                value=value,
                port=None,
                port_specified=False,
                domain=domain,
                domain_specified=initial_dot,
                domain_initial_dot=initial_dot,
                path=path,
                path_specified=True,
                secure=bool(item.get("secure", False)),
                expires=expiry,
                discard=expiry is None,
                comment=None,
                comment_url=None,
                rest={"HttpOnly": bool(item.get("httpOnly", False))},
                rfc2109=False,
            )
            self.jar.set_cookie(cookie)
            self._accepted += 1
            accepted += 1
        return accepted

    def audit_summary(self) -> dict[str, int | bool]:
        return {
            "cookie_count": sum(1 for _ in self.jar),
            "accepted_count": self._accepted,
            "rejected_count": self._rejected,
            "closed": self._closed,
        }

    def close(self) -> None:
        self.jar.clear()
        self._closed = True


class BrowserNetworkLedger:
    """BiDi request policy and nullable browser transfer accounting."""

    BLOCKED_RESOURCE_TYPES = frozenset({"image", "font", "media"})
    BLOCKED_HOST_SUFFIXES = (
        "amazon-adsystem.com",
        "doubleclick.net",
        "googlesyndication.com",
    )
    TELEMETRY_HOST_PATHS = {
        "fls-na.amazon.com": ("/1/batch/",),
        "unagi-na.amazon.com": ("/1/events/", "/1/batch/"),
        "www.amazon.com": ("/uedata/", "/gp/uedata"),
    }

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.reset()

    def reset(self, top_context_id: str | None = None) -> None:
        with getattr(self, "_lock", threading.Lock()):
            self._top_context_id = top_context_id
            self._saw_main_document = False
            self._request_buckets: dict[str, deque[str]] = defaultdict(deque)
            self._known = {"main": 0, "subresource": 0}
            self._known_count = {"main": 0, "subresource": 0}
            self._unknown_count = {"main": 0, "subresource": 0}
            self._blocked = Counter()
            self._blocked_request_race_count = 0
            self._continued_request_race_count = 0

    @classmethod
    def _is_explicit_ad_or_telemetry(cls, url: str) -> bool:
        parts = urlsplit(str(url or ""))
        host = (parts.hostname or "").lower().rstrip(".")
        path = parts.path.lower()
        if any(host == suffix or host.endswith("." + suffix) for suffix in cls.BLOCKED_HOST_SUFFIXES):
            return True
        return any(marker in path for marker in cls.TELEMETRY_HOST_PATHS.get(host, ()))

    def should_block_request(self, request: Any) -> bool:
        resource_type = str(getattr(request, "resource_type", "") or "unknown").lower()
        url = str(getattr(request, "url", "") or "")
        blocked = resource_type in self.BLOCKED_RESOURCE_TYPES or self._is_explicit_ad_or_telemetry(url)
        with self._lock:
            if blocked:
                self._blocked[resource_type] += 1
            else:
                params = getattr(request, "_params", {}) or {}
                context_id = params.get("context") if isinstance(params, dict) else None
                if resource_type == "document":
                    if self._top_context_id is not None:
                        bucket = "main" if context_id == self._top_context_id else "subresource"
                    else:
                        bucket = "main" if not self._saw_main_document else "subresource"
                    if bucket == "main":
                        self._saw_main_document = True
                else:
                    bucket = "subresource"
                self._request_buckets[url].append(bucket)
        return blocked

    def handle_request(self, request: Any) -> bool:
        blocked = self.should_block_request(request)
        if blocked:
            request.fail()
        return blocked

    def record_blocked_request_race(self) -> None:
        with self._lock:
            self._blocked_request_race_count += 1
            self._unknown_count["subresource"] += 1

    def record_continued_request_race(self, request: Any) -> None:
        url = str(getattr(request, "url", "") or "")
        with self._lock:
            self._continued_request_race_count += 1
            queue = self._request_buckets.get(url)
            if queue:
                bucket = queue.pop()
                if not queue:
                    self._request_buckets.pop(url, None)
                self._unknown_count[bucket] += 1
            else:
                self._unknown_count["main"] += 1
                self._unknown_count["subresource"] += 1

    @staticmethod
    def _response_payload(event: Any) -> dict[str, Any]:
        if isinstance(event, dict):
            value = event.get("response") or {}
        else:
            value = getattr(event, "response", {}) or {}
        return value if isinstance(value, dict) else getattr(value, "__dict__", {})

    def handle_response_completed(self, event: Any) -> None:
        response = self._response_payload(event)
        url = str(response.get("url") or "")
        bytes_value = response.get("bytesReceived", response.get("bytes_received"))
        with self._lock:
            queue = self._request_buckets.get(url)
            if not queue:
                self._unknown_count["main"] += 1
                self._unknown_count["subresource"] += 1
                return
            bucket = queue.popleft()
            if queue is not None and not queue:
                self._request_buckets.pop(url, None)
            try:
                byte_count = int(bytes_value) if bytes_value is not None else None
            except (TypeError, ValueError, OverflowError):
                byte_count = None
            if byte_count is None or byte_count < 0:
                self._unknown_count[bucket] += 1
            else:
                self._known[bucket] += byte_count
                self._known_count[bucket] += 1

    def handle_fetch_error(self, event: Any) -> None:
        if isinstance(event, dict):
            request = event.get("request") or {}
            url = str(request.get("url") or event.get("url") or "") if isinstance(request, dict) else ""
        else:
            url = ""
        with self._lock:
            queue = self._request_buckets.get(url) if url else None
            if queue:
                bucket = queue.popleft()
                if not queue:
                    self._request_buckets.pop(url, None)
                self._unknown_count[bucket] += 1
            else:
                # Selenium 4.47's high-level FetchErrorParameters currently
                # omits request identity. Conservatively mark every pending
                # request unknown; if none is pending, mark both scopes rather
                # than guessing which byte bucket failed.
                pending = [bucket for values in self._request_buckets.values() for bucket in values]
                if pending:
                    for bucket in pending:
                        self._unknown_count[bucket] += 1
                    self._request_buckets.clear()
                else:
                    self._unknown_count["main"] += 1
                    self._unknown_count["subresource"] += 1

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            main_unknown = self._unknown_count["main"]
            sub_unknown = self._unknown_count["subresource"]
            for queue in self._request_buckets.values():
                main_unknown += sum(1 for bucket in queue if bucket == "main")
                sub_unknown += sum(1 for bucket in queue if bucket == "subresource")
            main_known = self._known_count["main"]
            sub_known = self._known_count["subresource"]
            if main_known == 0 and main_unknown == 0:
                main_unknown = 1
            if sub_known == 0 and sub_unknown == 0:
                sub_unknown = 1
            return {
                "main_document_bytes": None if main_unknown else self._known["main"],
                "subresource_bytes": None if sub_unknown else self._known["subresource"],
                "main_document_known_count": main_known,
                "subresource_known_count": sub_known,
                "main_document_unknown_count": main_unknown,
                "subresource_unknown_count": sub_unknown,
                "blocked_resource_counts": dict(sorted(self._blocked.items())),
                "blocked_request_race_count": self._blocked_request_race_count,
                "continued_request_race_count": self._continued_request_race_count,
            }


class _DOMParser(HTMLParser):
    VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.nodes: list[dict[str, Any]] = []
        self.stack: list[int] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        node = {"tag": tag, "attrs": dict(attrs), "text": "", "children": [], "parent": self.stack[-1] if self.stack else None}
        index = len(self.nodes)
        self.nodes.append(node)
        if self.stack:
            self.nodes[self.stack[-1]]["children"].append(index)
        if tag not in self.VOID_TAGS:
            self.stack.append(index)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if self.stack and self.nodes[self.stack[-1]]["tag"] == tag.lower():
            self.stack.pop()

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        for position in range(len(self.stack) - 1, -1, -1):
            if self.nodes[self.stack[position]]["tag"] == tag:
                del self.stack[position:]
                break

    def handle_data(self, data: str) -> None:
        if self.stack:
            self.nodes[self.stack[-1]]["text"] += data


def _node_text(parser: _DOMParser, index: int) -> str:
    node = parser.nodes[index]
    if node["tag"] in {"script", "style", "noscript"}:
        return ""
    return _clean(node["text"] + " " + " ".join(_node_text(parser, child) for child in node["children"]))


def _visible_text(parser: _DOMParser, index: int) -> str:
    node = parser.nodes[index]
    if node["tag"] in {"script", "style", "noscript"}:
        return ""
    return _clean(node["text"] + " " + " ".join(_visible_text(parser, child) for child in node["children"]))


def _descendants(parser: _DOMParser, index: int) -> list[int]:
    result: list[int] = []
    for child in parser.nodes[index]["children"]:
        result.append(child)
        result.extend(_descendants(parser, child))
    return result


def _has_class(node: dict[str, Any], name: str) -> bool:
    return name in (node["attrs"].get("class") or "").split()


def _matches(node: dict[str, Any], query: dict[str, Any]) -> bool:
    for key, value in query.items():
        if key == "tag" and node["tag"] != value:
            return False
        if key == "id_value" and node["attrs"].get("id") != value:
            return False
        if key == "class_name" and not _has_class(node, value):
            return False
        if key == "attr" and node["attrs"].get(value[0]) != value[1]:
            return False
    return True


def _find(parser: _DOMParser, **query: Any) -> list[int]:
    return [index for index, node in enumerate(parser.nodes) if _matches(node, query)]


def _first_text(parser: _DOMParser, queries: list[dict[str, Any]]) -> str:
    for query in queries:
        found = _find(parser, **query)
        if found:
            text = _node_text(parser, found[0])
            if text:
                return text
    return ""


def _raw_container_text(source_html: str, ids: tuple[str, ...]) -> str:
    """Extract text from a named HTML container without trusting malformed DOM nesting."""
    for identifier in ids:
        match = re.search(
            rf"<(?:div|section|span)[^>]+id=[\"']{re.escape(identifier)}[\"'][^>]*>",
            source_html,
            flags=re.IGNORECASE,
        )
        if not match:
            continue
        closing = re.search(r"</(?:div|section|span)\s*>", source_html[match.end() :], flags=re.IGNORECASE)
        fragment = source_html[match.end() : match.end() + (closing.start() if closing else 12000)]
        fragment = re.sub(r"<!--.*?-->|<script\b[^>]*>.*?</script>|<style\b[^>]*>.*?</style>", " ", fragment, flags=re.IGNORECASE | re.DOTALL)
        text = re.sub(r"<[^>]+>", " ", fragment)
        text = _clean(text)
        if text:
            return text
        return ""
    return ""


def _first_attr(parser: _DOMParser, queries: list[dict[str, Any]], attribute: str) -> str:
    for query in queries:
        for index in _find(parser, **query):
            value = parser.nodes[index]["attrs"].get(attribute)
            if value:
                return _clean(value)
    return ""


def _first_text_under(parser: _DOMParser, index: int, queries: list[dict[str, Any]]) -> str:
    for candidate in [index, *_descendants(parser, index)]:
        if any(_matches(parser.nodes[candidate], query) for query in queries):
            value = _node_text(parser, candidate)
            if value:
                return value
    return ""


def _first_attr_under(parser: _DOMParser, index: int, queries: list[dict[str, Any]], attribute: str) -> str:
    for candidate in [index, *_descendants(parser, index)]:
        if any(_matches(parser.nodes[candidate], query) for query in queries):
            value = parser.nodes[candidate]["attrs"].get(attribute)
            if value:
                return _clean(value)
    return ""


def _meta(parser: _DOMParser, name: str) -> str:
    for index in _find(parser, tag="meta"):
        attrs = parser.nodes[index]["attrs"]
        if attrs.get("name", "").lower() == name.lower() or attrs.get("property", "").lower() == name.lower():
            return _clean(attrs.get("content"))
    return ""


def _ancestor_label(parser: _DOMParser, index: int) -> str | None:
    labels: list[str] = []
    current: int | None = index
    while current is not None:
        node = parser.nodes[current]
        labels.append(
            f"{node['attrs'].get('id', '')} {node['attrs'].get('class', '')}".lower()
        )
        current = node["parent"]
    joined = " ".join(labels)
    if any(term in joined for term in ("recommend", "sponsored", "advert", "tracking", "nav", "footer")):
        return "excluded"
    if "aplus" in joined or "a-plus" in joined:
        return "aplus"
    if any(term in joined for term in ("fromthebrand", "from-the-brand", "from_brand", "from-brand", "frombrand", "brand-store", "brandstore")):
        return "from_brand"
    if "review" in joined:
        return "review"
    if any(term in joined for term in ("product-video", "product-videos", "productvideo", "video-player")):
        return "product_videos"
    if any(term in joined for term in ("imageblock", "image-block", "image-gallery", "imagegallery", "altimages", "ivlargeimage", "imgtagwrapper")):
        return "gallery"
    return None


def _spec_root_indices(parser: _DOMParser) -> list[int]:
    roots: list[int] = []
    known_ids = {
        "productdetails_techspec_section_1",
        "productdetails_detailbullets_sections1",
        "productdetails",
        "product-information",
        "product_information",
        "technical-details",
        "technical_details",
        "detailbullets",
        "proddetails",
    }
    for index, node in enumerate(parser.nodes):
        identifier = " ".join(
            str(node["attrs"].get(name) or "")
            for name in ("id", "class", "data-section", "data-feature-name")
        ).lower()
        if node["attrs"].get("id", "").lower() in known_ids or any(
            token in identifier
            for token in (
                "techspec",
                "productdetails",
                "product-details",
                "product-information",
                "technical-details",
                "technical_details",
                "detailbullets",
                "proddetails",
            )
        ):
            roots.append(index)
    return list(dict.fromkeys(roots))


def _spec_value(parser: _DOMParser, index: int, label: str) -> str:
    text = _node_text(parser, index)
    if text == label:
        return ""
    if text.lower().startswith(label.lower()):
        text = text[len(label):].lstrip(" :\u00a0")
    return _clean(text)


def _add_spec(specs: dict[str, str], label: str, value: str) -> None:
    label, value = _clean(label), _clean(value)
    label_lower = label.lower()
    blocked_prefixes = (
        "would you like", "date of the price", "features & specs ",
        "additional details ", "feedback", "price availability",
    )
    blocked_exact = {
        "details", "item details", "product information", "technical details",
        "features & specs", "additional details",
    }
    if (
        not label
        or not value
        or len(label) > 120
        or len(value) > 500
        or label_lower in blocked_exact
        or label_lower.startswith(blocked_prefixes)
    ):
        return
    specs[label] = value


def _parse_specs(parser: _DOMParser) -> dict[str, str]:
    specs: dict[str, str] = {}
    for root in _spec_root_indices(parser):
        for index in [root, *_descendants(parser, root)]:
            node = parser.nodes[index]
            if node["tag"] == "tr":
                cells = [
                    _node_text(parser, child)
                    for child in node["children"]
                    if parser.nodes[child]["tag"] in {"th", "td"}
                ]
                if len(cells) >= 2:
                    _add_spec(specs, cells[0], cells[1])
                continue
            if node["tag"] not in {"li", "div"} or index == root:
                continue
            attrs = node["attrs"]
            label = attrs.get("data-label") or attrs.get("aria-label")
            if label:
                _add_spec(specs, label, _spec_value(parser, index, label))
            children = node["children"]
            if len(children) == 2:
                first, second = parser.nodes[children[0]], parser.nodes[children[1]]
                if first["tag"] in {"span", "div", "dt", "strong", "label"} and second["tag"] in {"span", "div", "dd", "p"}:
                    _add_spec(specs, _node_text(parser, children[0]), _node_text(parser, children[1]))
            text = _node_text(parser, index)
            if ":" in text and text.count(":") == 1 and not label:
                key, value = text.split(":", 1)
                if not node["children"] or all(not _node_text(parser, child) for child in node["children"]):
                    _add_spec(specs, key, value)
    return specs


def _parse_bullets(parser: _DOMParser) -> list[str]:
    bullets: list[str] = []
    for root in _find(parser, id_value="feature-bullets") + _find(parser, class_name="product-bullets"):
        for index in [root, *_descendants(parser, root)]:
            if parser.nodes[index]["tag"] == "li":
                value = _node_text(parser, index)
                if value:
                    bullets.append(value)
    return list(dict.fromkeys(bullets))


def _parse_media(parser: _DOMParser, page_url: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    ordinal = 0
    blocked_markers = ("grey-pixel", "transparent-pixel", "pixel.gif", "1x1")
    for index, node in enumerate(parser.nodes):
        if node["tag"] not in {"img", "video", "source"}:
            continue
        placement = _ancestor_label(parser, index)
        if placement in {None, "excluded"}:
            continue
        asset = (
            node["attrs"].get("src")
            or node["attrs"].get("data-src")
            or node["attrs"].get("data-old-hires")
        )
        if not asset:
            continue
        asset_url = urljoin(page_url, asset)
        asset_parts = urlsplit(asset_url)
        asset_haystack = f"{asset_parts.path}?{asset_parts.query}".lower()
        if any(marker in asset_haystack for marker in blocked_markers):
            continue
        if asset_url in seen:
            continue
        seen.add(asset_url)
        parent = node["parent"]
        entry_url = (
            parser.nodes[parent]["attrs"].get("href", "")
            if parent is not None and parser.nodes[parent]["tag"] == "a"
            else ""
        )
        entry_url = urljoin(page_url, entry_url) if entry_url else asset_url
        thumbnail = node["attrs"].get("data-thumb") or node["attrs"].get("data-thumbnail") or ""
        display = node["attrs"].get("data-display-url") or entry_url
        is_video = node["tag"] in {"video", "source"} or "video" in (node["attrs"].get("class") or "").lower()
        result.append({
            "placement": placement,
            "entry_type": "video" if is_video else "image",
            "media_type": "video" if is_video else "image",
            "url": asset_url,
            "thumbnail_url": urljoin(page_url, thumbnail) if thumbnail else ("" if is_video else asset_url),
            "display_url": urljoin(page_url, display),
            "asset_url": asset_url,
            "poster_url": urljoin(page_url, node["attrs"].get("poster", "")) if node["attrs"].get("poster") else "",
            "ordinal": ordinal,
            "is_primary": ordinal == 0 and placement == "gallery",
            "width": node["attrs"].get("width", ""),
            "height": node["attrs"].get("height", ""),
            "alt_text": _clean(node["attrs"].get("alt")),
            "variant_asin": "",
            "load_status": "available",
            "failure_reason": "",
        })
        ordinal += 1
    return result


def _is_review_url(url: str) -> bool:
    path = urlsplit(url).path.lower()
    return "/product-reviews/" in path or "/portal/customer-reviews/" in path


def _alternate_review_url(url: str, asin: str) -> str | None:
    """Return the stable review endpoint when Amazon gave a portal URL."""
    if "/portal/customer-reviews/" not in urlsplit(url).path.lower():
        return None
    parts = urlsplit(url)
    return urlunsplit((parts.scheme or "https", parts.netloc or "www.amazon.com", f"/product-reviews/{asin}", "", ""))


def _without_fragment(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))


def _review_url_asin(url: str) -> str:
    match = re.search(r"/(?:product-reviews|portal/customer-reviews)/([A-Za-z0-9]{10})(?:/|$)", urlsplit(url).path, flags=re.IGNORECASE)
    return match.group(1).upper() if match else ""


def _review_links(parser: _DOMParser, page_url: str, expected_asin: str = "") -> tuple[str, str]:
    candidates: list[tuple[int, str]] = []
    section_anchor = ""
    for index in _find(parser, tag="a"):
        node = parser.nodes[index]
        href = _clean(node["attrs"].get("href"))
        if not href:
            continue
        absolute = urljoin(page_url, href)
        text = _node_text(parser, index).lower()
        data_hook = node["attrs"].get("data-hook", "").lower()
        haystack = " ".join(
            (
                href,
                node["attrs"].get("id", ""),
                data_hook,
                node["attrs"].get("aria-label", ""),
                text,
            )
        ).lower()
        if _is_review_url(absolute):
            review_asin = _review_url_asin(absolute)
            query = urlsplit(absolute).query.lower()
            if expected_asin and review_asin and review_asin != expected_asin.upper():
                continue
            if "filterbystar=" in query:
                continue
            score = 0
            if "see-all-reviews-link-foot" in data_hook:
                score += 100
            if "see more reviews" in text or "all reviews" in text:
                score += 80
            if "/product-reviews/" in urlsplit(absolute).path.lower():
                score += 50
            if "reviewertype=all_reviews" in query:
                score += 20
            candidates.append((score, _without_fragment(absolute)))
            continue
        fragment = urlsplit(absolute).fragment
        if fragment and not section_anchor and any(term in haystack for term in ("review", "rating", "acr")):
            section_anchor = f"#{fragment}"
    candidates.sort(key=lambda item: (-item[0], item[1]))
    return (candidates[0][1] if candidates else "", section_anchor)


def _count_from_text(text: str) -> str:
    text = re.sub(r"\b\d+(?:\.\d+)?\s+out\s+of\s+5\s+stars?\b", "", text or "", flags=re.IGNORECASE)
    candidates = re.findall(r"(?<![\d.,])\d[\d,]*(?![\d.,])", text)
    return candidates[0].replace(",", "") if candidates else ""


def _count_kind(text: str, source: str = "") -> str:
    text_lower = (text or "").lower()
    source_lower = (source or "").lower()
    if "review" in text_lower:
        return "review"
    if "rating" in text_lower:
        return "rating"
    if "rating-count" in source_lower or "ratingcount" in source_lower:
        return "rating"
    if any(token in source_lower for token in ("review", "see-all-reviews", "acrcustomerreviewlink")):
        return "review"
    return ""


def _review_counts(parser: _DOMParser, summary_text: str) -> tuple[str, str, str]:
    rating_count = ""
    review_count = ""

    for index in _find(parser, id_value="acrCustomerReviewLink") + _find(parser, attr=("data-hook", "total-review-count")):
        text = _node_text(parser, index)
        count = _count_from_text(text)
        if count:
            review_count = count
            break

    for index in _find(parser, attr=("data-hook", "rating-count")) + _find(parser, id_value="ratingCount"):
        text = parser.nodes[index]["attrs"].get("aria-label", "") or _node_text(parser, index)
        count = _count_from_text(text)
        if count:
            rating_count = count
            break

    if not review_count and summary_text:
        review_count = _count_from_text(summary_text)

    if rating_count and review_count:
        source = "separate_fields"
    elif rating_count:
        source = "rating_count_only"
    elif review_count:
        source = "review_count_only"
    else:
        source = ""
    return rating_count, review_count, source


def visible_html_text(source_html: str) -> str:
    parser = _DOMParser()
    parser.feed(source_html)
    return " ".join(_visible_text(parser, index) for index, node in enumerate(parser.nodes) if node["parent"] is None)


def _price_text(parser: _DOMParser) -> str:
    """Prefer Amazon's accessible off-screen price over duplicated visual spans."""
    roots = _find(parser, id_value="corePrice_feature_div") + _find(parser, id_value="priceblock_ourprice")
    for root in roots:
        value = _first_text_under(parser, root, [{"class_name": "a-offscreen"}])
        if value:
            return _normalize_price(value)
    return _normalize_price(_first_text(parser, [{"id_value": "corePrice_feature_div"}, {"id_value": "priceblock_ourprice"}, {"class_name": "a-price"}]))


def _normalize_price(value: str) -> str:
    """Collapse duplicated visual price spans while preserving displayed currency."""
    text = _clean(value)
    if not text:
        return ""
    currency = re.search(r"(?:\$|USD|HKD|CAD|AUD|GBP|EUR|JPY|CNY)", text, flags=re.IGNORECASE)
    amount = re.search(r"\d[\d,]*(?:\s*\.\s*\d{1,2})?", text)
    if not currency or not amount:
        return text
    symbol = currency.group(0).upper() if currency.group(0).isalpha() else currency.group(0)
    number = re.sub(r"\s+", "", amount.group(0))
    return f"{symbol}{number}"


def _normalize_brand(value: str) -> str:
    """Remove Amazon's store CTA wrapper while preserving the brand text."""
    text = _clean(value)
    match = re.fullmatch(r"visit\s+the\s+(.+?)\s+store", text, flags=re.IGNORECASE)
    return _clean(match.group(1)) if match else text


def _retry_after_seconds(value: str | None, now: datetime | None = None) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.isdigit():
        seconds = int(text)
    else:
        try:
            retry_at = email.utils.parsedate_to_datetime(text)
        except (TypeError, ValueError, OverflowError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        seconds = math.ceil((retry_at - (now or datetime.now(timezone.utc))).total_seconds())
    if seconds <= 0:
        return None
    return min(seconds, 86400)


def _buy_box_facts(parser: _DOMParser) -> dict[str, str]:
    text = _first_text(parser, [{"id_value": "desktop_buybox"}, {"id_value": "buybox"}])
    if not text:
        fragments = []
        for node in parser.nodes:
            identifier = f"{node['attrs'].get('id', '')} {node['attrs'].get('class', '')}".lower()
            if any(token in identifier for token in ("buybox", "coupon", "promotion", "deliveryblock", "merchantinfo")):
                value = _node_text(parser, parser.nodes.index(node))
                if value:
                    fragments.append(value)
        text = _clean(" ".join(dict.fromkeys(fragments)))
    facts = {"text": text}
    seller = _first_text(parser, [{"id_value": "sellerProfileTriggerId"}, {"id_value": "sellerName"}])
    if not seller:
        seller_match = re.search(r"(?:sold\s+by|ships\s+from)\s+(.{1,100}?)(?=\s+(?:and|fulfilled|get\s+it|free\s+delivery|save|coupon|apply)|$)", text, flags=re.IGNORECASE)
        seller = _clean(seller_match.group(1)) if seller_match else ""
    if seller:
        facts["seller"] = seller
    coupon = re.search(r"(?:save|coupon|off)[^$%\d]{0,20}(?:\$\s*\d+(?:\.\d{1,2})?|\d+\s*%)", text, flags=re.IGNORECASE)
    if coupon:
        facts["coupon"] = _clean(coupon.group(0))
    delivery = re.search(r"(?:free delivery|get it by|arrives|delivering to).{0,120}?(?=\s+(?:add to list|added to|unable to|sold by|ships from)|$)", text, flags=re.IGNORECASE)
    if delivery:
        facts["delivery"] = _clean(delivery.group(0))
    return facts


def _named_asin_values(source_html: str, name: str) -> set[str]:
    pattern = re.compile(
        rf"(?:['\"]{re.escape(name)}['\"]|\b{re.escape(name)}\b)\s*(?::|=)\s*['\"]?([A-Z0-9]{{10}})",
        flags=re.IGNORECASE,
    )
    return {match.group(1).upper() for match in pattern.finditer(source_html)}


def _named_object_payloads(source_html: str, name: str) -> list[str]:
    marker = re.compile(
        rf"(?:['\"]{re.escape(name)}['\"]|\b{re.escape(name)}\b)\s*:\s*\{{",
        flags=re.IGNORECASE,
    )
    payloads: list[str] = []
    for match in marker.finditer(source_html):
        start = match.end() - 1
        depth = 0
        quote = ""
        escaped = False
        for index in range(start, min(len(source_html), start + 500_000)):
            char = source_html[index]
            if quote:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == quote:
                    quote = ""
                continue
            if char in {"'", '"'}:
                quote = char
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    payloads.append(source_html[start : index + 1])
                    break
    return payloads


def _product_identity_metadata(source_html: str) -> tuple[str, list[str]]:
    parents = _named_asin_values(source_html, "parentAsin")
    parent_asin = next(iter(parents)) if len(parents) == 1 else ""
    children = set()
    for name in ("landingAsin", "current_asin", "currentAsin"):
        children.update(_named_asin_values(source_html, name))
    for name in ("dimensionValuesDisplayData", "colorToAsin"):
        for payload in _named_object_payloads(source_html, name):
            children.update(re.findall(r"['\"]([A-Z0-9]{10})['\"]", payload, flags=re.IGNORECASE))
    return parent_asin, sorted(value.upper() for value in children)


def parse_product_html(source_html: str, page_url: str = "") -> dict[str, Any]:
    parser = _DOMParser()
    parser.feed(source_html)
    asin = _first_attr(parser, [{"tag": "input", "attr": ("id", "ASIN")}, {"tag": "input", "attr": ("name", "ASIN")}], "value")
    canonical = _first_attr(parser, [{"tag": "link", "attr": ("rel", "canonical")}], "href") or _meta(parser, "og:url") or page_url
    parent_asin, identity_child_asins = _product_identity_metadata(source_html)
    title = _first_text(parser, [{"id_value": "productTitle"}, {"tag": "h1", "class_name": "product-title"}])
    delivery_context = _first_text(parser, [{"id_value": "glow-ingress-line1"}, {"id_value": "glow-ingress-line2"}])
    review_summary_text = _first_text(parser, [{"id_value": "acrCustomerReviewLink"}, {"attr": ("data-hook", "total-review-count")}])
    reported_rating_text = _first_text(parser, [{"id_value": "acrPopover"}, {"attr": ("data-hook", "rating-out-of-five")}])
    reported_rating_count_text = _first_text(parser, [{"attr": ("data-hook", "rating-count")}, {"id_value": "ratingCount"}])
    reported_rating_count, reported_review_count, review_count_source = _review_counts(parser, review_summary_text)
    reported_rating_count = reported_rating_count or _count_from_text(reported_rating_count_text)
    if reported_rating_count and reported_review_count:
        review_count_source = "separate_fields"
    elif reported_rating_count and not reported_review_count:
        review_count_source = "rating_count_only"
    elif reported_review_count:
        review_count_source = "review_count_only"
    review_link, review_section_anchor = _review_links(parser, page_url, asin)
    content_modules: list[dict[str, Any]] = []
    position = 0
    for bullet in _parse_bullets(parser):
        content_modules.append({"module_type": "bullet", "position": position, "order_index": position, "text": bullet, "image_url": "", "link_url": "", "status": "available"})
        position += 1
    description = _raw_container_text(source_html, ("productDescription", "bookDescription_feature_div"))
    if description:
        content_modules.append({"module_type": "product_description", "position": position, "order_index": position, "text": description, "image_url": "", "link_url": "", "status": "available"})
        position += 1
    specs = _parse_specs(parser)
    if specs:
        content_modules.append({"module_type": "product_information", "position": position, "order_index": position, "text": _json(specs), "image_url": "", "link_url": "", "status": "available"})
        position += 1
    for index, node in enumerate(parser.nodes):
        identifier = f"{node['attrs'].get('id', '')} {node['attrs'].get('class', '')}".lower()
        if "aplus" in identifier or "a-plus" in identifier:
            images = [urljoin(page_url, parser.nodes[c]["attrs"]["src"]) for c in [index, *_descendants(parser, index)] if parser.nodes[c]["tag"] == "img" and parser.nodes[c]["attrs"].get("src")]
            content_modules.append({"module_type": "aplus", "position": position, "order_index": position, "text": _node_text(parser, index), "image_url": images[0] if images else "", "link_url": "", "status": "available"})
            position += 1
        if "from-brand" in identifier or "brand-store" in identifier:
            content_modules.append({"module_type": "from_brand", "position": position, "order_index": position, "text": _node_text(parser, index), "image_url": "", "link_url": "", "status": "available"})
            position += 1
    body_text = " ".join(_visible_text(parser, index) for index, node in enumerate(parser.nodes) if node["parent"] is None)
    return {
        "asin": asin.upper(), "marketplace": "US", "canonical_url": canonical,
        "parent_asin": parent_asin, "identity_child_asins": identity_child_asins,
        "delivery_context": delivery_context,
        "availability": _first_text(parser, [{"id_value": "availability"}, {"id_value": "outOfStock"}]),
        "title": title, "brand": _normalize_brand(_first_text(parser, [{"id_value": "bylineInfo"}, {"id_value": "brand"}])),
        "rating": reported_rating_text, "reported_ratings": _count_from_text(review_summary_text), "reported_rating_count": reported_rating_count,
        "reported_review_count": reported_review_count, "review_count": review_summary_text,
        "review_count_source": review_count_source, "price": _price_text(parser),
        "bullets": _parse_bullets(parser), "product_description": description, "specs": specs,
        "buy_box": _buy_box_facts(parser),
        "top_reviews": [_node_text(parser, index) for index in _find(parser, attr=("data-hook", "review"))],
        "review_link": review_link, "review_section_anchor": review_section_anchor,
        "aplus_present": any(x["module_type"] == "aplus" for x in content_modules),
        "media": _parse_media(parser, page_url), "content_modules": content_modules,
        "block_reason": classify_block(text=source_html, title=_first_text(parser, [{"tag": "title"}])),
    }


def parse_reviews_html(source_html: str, page: int = 1, page_url: str = "") -> tuple[list[dict[str, Any]], str | None]:
    parser = _DOMParser()
    parser.feed(source_html)
    records: list[dict[str, Any]] = []
    for index in _find(parser, attr=("data-hook", "review")):
        text = _node_text(parser, index)
        title = _first_text_under(parser, index, [{"attr": ("data-hook", "review-title")}])
        body = _first_text_under(parser, index, [{"attr": ("data-hook", "review-body")}])
        date = _first_text_under(parser, index, [{"attr": ("data-hook", "review-date")}, {"class_name": "review-date"}])
        verified = _first_text_under(parser, index, [{"attr": ("data-hook", "avp-badge")}, {"class_name": "verified"}])
        rating = _first_text_under(parser, index, [{"attr": ("data-hook", "review-star-rating")}, {"attr": ("data-hook", "cmps-review-star-rating")}])
        review_images = [urljoin(page_url, parser.nodes[c]["attrs"].get("src") or parser.nodes[c]["attrs"].get("data-src") or parser.nodes[c]["attrs"].get("data-old-hires", "")) for c in [index, *_descendants(parser, index)] if parser.nodes[c]["tag"] == "img" and (parser.nodes[c]["attrs"].get("src") or parser.nodes[c]["attrs"].get("data-src") or parser.nodes[c]["attrs"].get("data-old-hires"))]
        review_id = parser.nodes[index]["attrs"].get("id") or hashlib.sha256(text.encode()).hexdigest()[:24]
        locale = parser.nodes[index]["attrs"].get("lang", "")
        link = _first_attr_under(parser, index, [{"attr": ("data-hook", "review-title")}], "href")
        records.append({"review_id": review_id, "rating": rating, "title": title, "body": body, "review_url": urljoin(page_url, link) if link else "", "review_date": date, "locale": locale, "verified": bool(verified), "body_truncated": False, "review_images_json": _json(review_images), "page": page})
    next_link = _first_attr(parser, [{"id_value": "cm_cr-pagination_bar"}, {"attr": ("data-hook", "pagination-bar")}], "href")
    if not next_link:
        for index in _find(parser, tag="a"):
            candidate = parser.nodes[index]["attrs"].get("href", "")
            if "next" in _node_text(parser, index).lower() and candidate and _is_review_url(urljoin(page_url, candidate)):
                next_link = candidate
                break
    return records, (urljoin(page_url, next_link) if next_link else None)


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _schema_needs_rebuild(conn: sqlite3.Connection) -> bool:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(item_state)")}
    if not columns:
        return False
    required = {"max_attempts", "resume_status", "task_stage", "next_review_page"}
    status_sql = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='item_state'").fetchone()
    return not required.issubset(columns) or (status_sql and "product_done" not in (status_sql[0] or ""))


def init_db(path: Path = DEFAULT_DB, max_attempts: int = 3) -> sqlite3.Connection:
    conn = _connect(path)
    if _schema_needs_rebuild(conn):
        conn.executescript("""
            DROP TABLE IF EXISTS review_page_state;
            DROP TABLE IF EXISTS review_record;
            DROP TABLE IF EXISTS product_snapshot;
            DROP TABLE IF EXISTS media_asset;
            DROP TABLE IF EXISTS content_module;
            DROP TABLE IF EXISTS review_summary;
            DROP TABLE IF EXISTS collection_evidence;
            DROP TABLE IF EXISTS state_history;
            DROP TABLE IF EXISTS item_state;
        """)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS item_state (
          marketplace TEXT NOT NULL, asin TEXT NOT NULL, url TEXT NOT NULL,
          status TEXT NOT NULL CHECK(status IN ('pending','running','product_done','reviews_pending','succeeded','blocked','failed')),
          attempts INTEGER NOT NULL DEFAULT 0, max_attempts INTEGER NOT NULL DEFAULT 3,
          resume_status TEXT, task_stage TEXT NOT NULL DEFAULT 'product', next_review_url TEXT,
          next_review_page INTEGER, review_page_limit INTEGER NOT NULL DEFAULT 0,
          next_retry_at TEXT,
          reported_rating_count INTEGER, reported_review_count INTEGER,
          reported_count_source TEXT, fetched_review_count INTEGER NOT NULL DEFAULT 0,
          review_pages_fetched INTEGER NOT NULL DEFAULT 0, block_reason TEXT, last_error TEXT,
          updated_at TEXT NOT NULL, PRIMARY KEY(marketplace, asin)
        );
        CREATE TABLE IF NOT EXISTS state_history (
          id INTEGER PRIMARY KEY AUTOINCREMENT, marketplace TEXT NOT NULL, asin TEXT NOT NULL,
          from_status TEXT, to_status TEXT NOT NULL, reason TEXT, changed_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS refresh_request (
          job_id TEXT PRIMARY KEY, marketplace TEXT NOT NULL, asin TEXT NOT NULL,
          requested_by TEXT NOT NULL, reason TEXT NOT NULL, status TEXT NOT NULL,
          requested_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS review_page_state (
          marketplace TEXT NOT NULL, asin TEXT NOT NULL, page INTEGER NOT NULL, url TEXT NOT NULL,
          status TEXT NOT NULL, next_url TEXT, fetched_at TEXT, PRIMARY KEY(marketplace, asin, page)
        );
        CREATE TABLE IF NOT EXISTS product_snapshot (
          marketplace TEXT NOT NULL, asin TEXT NOT NULL, canonical_url TEXT, availability TEXT, title TEXT,
          brand TEXT, rating TEXT, reported_rating_count INTEGER, reported_review_count INTEGER,
          review_count TEXT, review_count_source TEXT, price TEXT, bullets_json TEXT,
          product_description TEXT, specs_json TEXT, buy_box_json TEXT, top_reviews_json TEXT,
          review_link TEXT, review_section_anchor TEXT, aplus_present INTEGER, collected_at TEXT, status TEXT,
          PRIMARY KEY(marketplace, asin)
        );
        CREATE TABLE IF NOT EXISTS product_snapshot_history (
          id INTEGER PRIMARY KEY AUTOINCREMENT, marketplace TEXT NOT NULL, asin TEXT NOT NULL,
          captured_at TEXT NOT NULL, source_type TEXT, raw_html_path TEXT, value_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS media_asset (
          marketplace TEXT NOT NULL, asin TEXT NOT NULL, placement TEXT NOT NULL, entry_type TEXT,
          thumbnail_url TEXT, display_url TEXT, asset_url TEXT, poster_url TEXT, ordinal INTEGER,
          is_primary INTEGER, width TEXT, height TEXT, alt_text TEXT, variant_asin TEXT,
          load_status TEXT, failure_reason TEXT, unique_key TEXT NOT NULL UNIQUE,
          PRIMARY KEY(marketplace, asin, unique_key)
        );
        CREATE TABLE IF NOT EXISTS content_module (
          marketplace TEXT NOT NULL, asin TEXT NOT NULL, module_type TEXT NOT NULL, position INTEGER,
          order_index INTEGER, text TEXT, image_url TEXT, link_url TEXT, status TEXT, unique_key TEXT NOT NULL UNIQUE,
          PRIMARY KEY(marketplace, asin, unique_key)
        );
        CREATE TABLE IF NOT EXISTS review_summary (
          marketplace TEXT NOT NULL, asin TEXT NOT NULL, reported_rating_count INTEGER,
          reported_review_count INTEGER, reported_count_source TEXT, fetched_count INTEGER,
          pages_fetched INTEGER, next_page TEXT, status TEXT, updated_at TEXT,
          PRIMARY KEY(marketplace, asin)
        );
        CREATE TABLE IF NOT EXISTS review_record (
          marketplace TEXT NOT NULL, asin TEXT NOT NULL, review_id TEXT NOT NULL, rating TEXT,
          title TEXT, body TEXT, review_url TEXT, review_date TEXT, locale TEXT, verified INTEGER,
          body_truncated INTEGER, review_images_json TEXT, page INTEGER, unique_key TEXT NOT NULL UNIQUE,
          PRIMARY KEY(marketplace, asin, review_id)
        );
        CREATE TABLE IF NOT EXISTS collection_evidence (
          id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, marketplace TEXT NOT NULL,
          asin TEXT NOT NULL, url TEXT NOT NULL, http_status INTEGER, transfer_bytes INTEGER, retrieved_at TEXT NOT NULL,
          source_type TEXT, content_hash TEXT, raw_html_path TEXT, block_reason TEXT, parser_version TEXT, error_code TEXT
        );
        """
    )
    product_columns = {row[1] for row in conn.execute("PRAGMA table_info(product_snapshot)")}
    if "review_section_anchor" not in product_columns:
        conn.execute("ALTER TABLE product_snapshot ADD COLUMN review_section_anchor TEXT")
    evidence_columns = {row[1] for row in conn.execute("PRAGMA table_info(collection_evidence)")}
    if "raw_html_path" not in evidence_columns:
        conn.execute("ALTER TABLE collection_evidence ADD COLUMN raw_html_path TEXT")
    if "context_json" not in evidence_columns:
        conn.execute("ALTER TABLE collection_evidence ADD COLUMN context_json TEXT")
    if "transfer_bytes" not in evidence_columns:
        conn.execute("ALTER TABLE collection_evidence ADD COLUMN transfer_bytes INTEGER")
    state_columns = {row[1] for row in conn.execute("PRAGMA table_info(item_state)")}
    if "next_retry_at" not in state_columns:
        conn.execute("ALTER TABLE item_state ADD COLUMN next_retry_at TEXT")
    recover_running(conn)
    conn.commit()
    return conn


def resolve_path(value: str | Path, base: Path = ROOT) -> Path:
    path = Path(value)
    return path if path.is_absolute() else base / path


def load_config(path: Path = DEFAULT_CONFIG) -> dict[str, Any]:
    config = dict(DEFAULTS)
    with path.open("rb") as handle:
        raw = tomllib.load(handle)
    worker = raw.get("worker", {})
    collection = raw.get("collection", {})
    config.update({key: value for key, value in worker.items() if key in config})
    config.update({key: value for key, value in collection.items() if key in config})
    config["paths"] = raw.get("paths", {})
    config["context"] = raw.get("context", {})
    agent_name = str(config["agent_name"]).strip()
    user_agent = str(config.get("user_agent") or "").strip()
    if user_agent and f"Agent/{agent_name}" not in user_agent:
        raise ValueError("user_agent 必须包含透明标识 Agent/<agent_name>")
    config["agent_name"] = agent_name
    encoding = str(config.get("http_accept_encoding") or "identity").strip().lower()
    if encoding not in {"gzip", "identity"}:
        raise ValueError("http_accept_encoding must be gzip or identity")
    config["http_accept_encoding"] = encoding
    config["max_attempts"] = max(1, int(config["max_attempts"]))
    config["max_actions_per_run"] = max(1, int(config["max_actions_per_run"]))
    config["review_page_limit"] = max(0, int(config["review_page_limit"]))
    if "firefox_proxy_auth_mode" not in worker and config.get("proxy_username_env") and config.get("proxy_password_env"):
        config["firefox_proxy_auth_mode"] = "loopback_connect_relay"
    if "proxy_request_jitter_seconds" not in worker and config.get("proxy_url"):
        config["proxy_request_jitter_seconds"] = 1.0
    if str(config.get("firefox_proxy_auth_mode") or "") not in {"disabled", "loopback_connect_relay"}:
        raise ValueError("firefox_proxy_auth_mode must be disabled or loopback_connect_relay")
    if str(config.get("proxy_product_session_scope") or "") not in {"per_asin", "bounded"}:
        raise ValueError("proxy_product_session_scope must be per_asin or bounded")
    jitter = float(config.get("proxy_request_jitter_seconds") or 0.0)
    if jitter < 0 or jitter > 5:
        raise ValueError("proxy_request_jitter_seconds must be between 0 and 5")
    config["proxy_request_jitter_seconds"] = jitter
    if config.get("proxy_url"):
        for rate_name in ("global_requests_per_second", "egress_requests_per_second"):
            rate = float(config.get(rate_name) or 0.0)
            if rate <= 0:
                rate = 0.2
            if rate > 0.2:
                raise ValueError(f"{rate_name} must be between 0 and 0.2 for paid proxy production")
            config[rate_name] = rate
        if int(config.get("rate_burst") or 1) != 1:
            raise ValueError("rate_burst must be 1 for paid proxy production")
    config["proxy_credential_generation"] = os.environ.get("AMAZON_PROXY_CREDENTIAL_GENERATION", "").strip()
    return config


def _set_status(conn: sqlite3.Connection, marketplace: str, asin: str, status: str, *, reason: str = "", **fields: Any) -> None:
    if status not in STATUSES:
        raise ValueError(f"未知状态: {status}")
    row = conn.execute("SELECT status FROM item_state WHERE marketplace=? AND asin=?", (marketplace, asin)).fetchone()
    if row is None:
        raise KeyError(f"未知商品: {marketplace}+{asin}")
    current = row["status"]
    if status not in ALLOWED_TRANSITIONS.get(current, set()):
        raise ValueError(f"非法状态转移: {current}->{status}")
    allowed = {"attempts", "max_attempts", "resume_status", "task_stage", "next_review_url", "next_review_page", "next_retry_at", "review_page_limit", "reported_rating_count", "reported_review_count", "reported_count_source", "fetched_review_count", "review_pages_fetched", "block_reason", "last_error"}
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError(f"不允许更新状态字段: {sorted(unknown)}")
    assignments = ["status=?", "updated_at=?"]
    values: list[Any] = [status, utc_now()]
    for name, value in fields.items():
        assignments.append(f"{name}=?")
        values.append(value)
    values.extend([marketplace, asin])
    with conn:
        conn.execute(f"UPDATE item_state SET {', '.join(assignments)} WHERE marketplace=? AND asin=?", values)
        conn.execute("INSERT INTO state_history(marketplace,asin,from_status,to_status,reason,changed_at) VALUES(?,?,?,?,?,?)", (marketplace, asin, current, status, reason, utc_now()))


def _record_failure(
    conn: sqlite3.Connection,
    marketplace: str,
    asin: str,
    reason: str,
    error: str,
    *,
    terminal: bool = False,
) -> None:
    row = conn.execute("SELECT attempts,max_attempts FROM item_state WHERE marketplace=? AND asin=?", (marketplace, asin)).fetchone()
    if row is None:
        raise KeyError(f"未知商品: {marketplace}+{asin}")
    attempts = int(row["max_attempts"]) if terminal else int(row["attempts"]) + 1
    _set_status(conn, marketplace, asin, "failed", reason=reason, attempts=attempts, last_error=error)


def _set_rate_limited(conn: sqlite3.Connection, marketplace: str, asin: str, stage: str, cooldown_seconds: int | float | None = None) -> None:
    target = "reviews_pending" if stage == "reviews" else "pending"
    cooldown = max(0, int(DEFAULTS.get("rate_limit_cooldown_seconds", 3600) if cooldown_seconds is None else cooldown_seconds))
    retry_at = (datetime.now(timezone.utc) + timedelta(seconds=cooldown)).replace(microsecond=0).isoformat()
    _set_status(
        conn,
        marketplace,
        asin,
        target,
        reason="rate_limited_deferred",
        block_reason="http_429",
        last_error="http_429",
        resume_status=target,
        task_stage=stage,
        next_retry_at=retry_at,
    )


def initialize_manifest(conn: sqlite3.Connection, manifest: Path, config: dict[str, Any] | None = None) -> int:
    config = config or DEFAULTS
    with manifest.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    now = utc_now()
    with conn:
        for row in rows:
            marketplace, asin, url = row["marketplace"], row["asin"], row["url"]
            exists = conn.execute("SELECT 1 FROM item_state WHERE marketplace=? AND asin=?", (marketplace, asin)).fetchone()
            conn.execute(
                """INSERT INTO item_state(marketplace,asin,url,status,max_attempts,review_page_limit,updated_at)
                   VALUES(?,?,?,?,?,?,?) ON CONFLICT(marketplace,asin) DO UPDATE SET url=excluded.url, max_attempts=excluded.max_attempts, review_page_limit=excluded.review_page_limit""",
                (marketplace, asin, url, "pending", int(config.get("max_attempts", 3)), int(config.get("review_page_limit", 0)), now),
            )
            if exists is None:
                conn.execute("INSERT INTO state_history(marketplace,asin,from_status,to_status,reason,changed_at) VALUES(?,?,?,?,?,?)", (marketplace, asin, None, "pending", "manifest_init", now))
    return len(rows)


def recover_running(conn: sqlite3.Connection) -> int:
    rows = list(conn.execute("SELECT marketplace,asin,resume_status FROM item_state WHERE status='running'"))
    for row in rows:
        target = "reviews_pending" if row["resume_status"] == "reviews_pending" else "pending"
        _set_status(conn, row["marketplace"], row["asin"], target, reason="crash_recovery", resume_status=None)
    return len(rows)


def _persist_raw_html(raw_html_dir: Path | None, run_id: str, asin: str, body: str) -> str | None:
    if raw_html_dir is None:
        return None
    try:
        from raw_html_store import LocalRawHtmlStore
    except ModuleNotFoundError:
        sys.path.insert(0, str(ROOT / "scripts"))
        from raw_html_store import LocalRawHtmlStore
    return LocalRawHtmlStore(raw_html_dir).put(run_id, asin, body)


def _insert_evidence(conn: sqlite3.Connection, run_id: str, asin: str, url: str, status: int | None, body: str | None, block_reason: str | None, error_code: str | None = None, source_type: str = "selenium_dom", raw_html_dir: Path | None = None, raw_html_path: str | None = None, context: dict[str, Any] | None = None, transfer_bytes: int | None = None) -> str | None:
    if raw_html_path is None and body is not None:
        raw_html_path = _persist_raw_html(raw_html_dir, run_id, asin, body)
    context_json = _json(context or {})
    content_hash = hashlib.sha256(body.encode()).hexdigest() if body is not None else None
    conn.execute("INSERT INTO collection_evidence(run_id,marketplace,asin,url,http_status,transfer_bytes,retrieved_at,source_type,content_hash,raw_html_path,block_reason,parser_version,error_code,context_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (run_id, "US", asin, url, status, transfer_bytes, utc_now(), source_type, content_hash, raw_html_path, block_reason, PARSER_VERSION, error_code, context_json))
    return raw_html_path


def _write_fetch_failure_action(
    conn: sqlite3.Connection,
    run_id: str,
    task: sqlite3.Row,
    error: str,
    adapter: Any,
    context: dict[str, Any] | None,
) -> None:
    with conn:
        _insert_evidence(
            conn,
            run_id,
            task["asin"],
            task["url"],
            None,
            None,
            None,
            "fetch_error",
            getattr(adapter, "source_type", "http_html"),
            context=_evidence_context(context, adapter),
            transfer_bytes=getattr(adapter, "last_transfer_bytes", None),
        )
        _record_failure(conn, "US", task["asin"], "fetch_error", error)


def _write_product_action(conn: sqlite3.Connection, run_id: str, task: sqlite3.Row, data: dict[str, Any], body: str, status: int | None, block_reason: str | None, error_code: str | None = None, source_type: str = "selenium_dom", raw_html_dir: Path | None = None, context: dict[str, Any] | None = None, cooldown_seconds: int | float | None = None, transfer_bytes: int | None = None) -> None:
    asin = task["asin"]
    missing_core = [key for key in ("asin", "canonical_url", "title") if not str(data.get(key) or "").strip()]
    missing_error = "missing_core_fields:" + ",".join(missing_core) if missing_core else None
    explicit_identity_mismatch = _has_explicit_asin_mismatch(data, asin)
    variant_redirect = explicit_identity_mismatch and _is_sibling_variant_redirect(data, asin)
    evidence_error = error_code or (
        "asin_mismatch" if explicit_identity_mismatch and not block_reason
        else missing_error if not block_reason else None
    )
    with conn:
        raw_html_path = _insert_evidence(conn, run_id, asin, task["url"], status, body, block_reason, evidence_error, source_type, raw_html_dir, context=context, transfer_bytes=transfer_bytes)
        if block_reason:
            if status == 429 or block_reason == "too_many_requests":
                _set_rate_limited(conn, "US", asin, "product", cooldown_seconds)
            else:
                _set_status(conn, "US", asin, "blocked", reason=block_reason, block_reason=block_reason, last_error=block_reason)
            return
        if error_code and error_code.startswith("context_mismatch"):
            _record_failure(conn, "US", asin, "context_mismatch", error_code)
            return
        if explicit_identity_mismatch:
            if variant_redirect:
                _set_status(
                    conn, "US", asin, "succeeded", reason="variant_redirect",
                    attempts=0, task_stage="complete", resume_status=None,
                    block_reason=None, last_error=None,
                )
            else:
                _record_failure(conn, "US", asin, "asin_mismatch", "asin_mismatch", terminal=True)
            return
        if missing_core:
            _record_failure(
                conn,
                "US",
                asin,
                "missing_core_fields",
                str(missing_error),
                terminal=_is_terminal_missing_core_failure(status, missing_core),
            )
            return
        canonical = data.get("canonical_url") or ""
        if not _valid_asin_identity(data, asin):
            _insert_evidence(conn, run_id, asin, task["url"], status, body, None, "asin_mismatch", source_type, raw_html_dir, raw_html_path, context, transfer_bytes)
            _record_failure(conn, "US", asin, "asin_mismatch", "asin_mismatch", terminal=True)
            return
        now = utc_now()
        product = {"asin": asin, "marketplace": "US", "canonical_url": canonical, "availability": data.get("availability", ""), "title": data.get("title", ""), "brand": data.get("brand", ""), "rating": data.get("rating", ""), "reported_rating_count": data.get("reported_rating_count") or None, "reported_review_count": data.get("reported_review_count") or None, "review_count": data.get("review_count", ""), "review_count_source": data.get("review_count_source", ""), "price": data.get("price", ""), "bullets_json": _json(data.get("bullets", [])), "product_description": data.get("product_description", ""), "specs_json": _json(data.get("specs", {})), "buy_box_json": _json(data.get("buy_box", {})), "top_reviews_json": _json(data.get("top_reviews", [])), "review_link": data.get("review_link", ""), "review_section_anchor": data.get("review_section_anchor", ""), "aplus_present": int(bool(data.get("aplus_present"))), "collected_at": now, "status": "product_done"}
        conn.execute(
            "INSERT INTO product_snapshot_history(marketplace,asin,captured_at,source_type,raw_html_path,value_json) VALUES(?,?,?,?,?,?)",
            ("US", asin, now, source_type, raw_html_path, _json(data)),
        )
        columns = PRODUCT_HEADERS
        conn.execute(f"INSERT INTO product_snapshot({','.join(columns)}) VALUES({','.join('?' for _ in columns)}) ON CONFLICT(marketplace,asin) DO UPDATE SET " + ",".join(f"{c}=excluded.{c}" for c in columns if c not in {"asin", "marketplace"}), [product.get(c, "") for c in columns])
        conn.execute("DELETE FROM media_asset WHERE marketplace='US' AND asin=?", (asin,))
        for media in data.get("media", []):
            media = dict(media)
            media["asin"], media["marketplace"] = asin, "US"
            media["unique_key"] = f"US|{asin}|{media.get('placement','')}|{media.get('entry_type','')}|{media.get('asset_url','')}"
            cols = MEDIA_HEADERS
            conn.execute(f"INSERT OR REPLACE INTO media_asset({','.join(cols)}) VALUES({','.join('?' for _ in cols)})", [media.get(c, "") for c in cols])
        conn.execute("DELETE FROM content_module WHERE marketplace='US' AND asin=?", (asin,))
        for module in data.get("content_modules", []):
            module = dict(module)
            module["asin"], module["marketplace"] = asin, "US"
            module["unique_key"] = f"US|{asin}|{module.get('module_type','')}|{module.get('position',0)}"
            cols = CONTENT_HEADERS
            conn.execute(f"INSERT OR REPLACE INTO content_module({','.join(cols)}) VALUES({','.join('?' for _ in cols)})", [module.get(c, "") for c in cols])
        review_url = data.get("review_link") or None
        if review_url:
            conn.execute("INSERT OR REPLACE INTO review_summary VALUES(?,?,?,?,?,?,?,?,?,?)", ("US", asin, data.get("reported_rating_count") or None, data.get("reported_review_count") or None, data.get("review_count_source", ""), 0, 0, review_url, "in_progress", now))
            _set_status(conn, "US", asin, "product_done", reason="product_parsed", attempts=0, task_stage="reviews", next_review_url=review_url, next_review_page=1, reported_rating_count=data.get("reported_rating_count") or None, reported_review_count=data.get("reported_review_count") or None, reported_count_source=data.get("review_count_source", ""))
            _set_status(conn, "US", asin, "reviews_pending", reason="reviews_required", resume_status="reviews_pending")
        else:
            summary_status = "section_only" if data.get("review_section_anchor") else "not_available"
            conn.execute(
                "INSERT OR REPLACE INTO review_summary VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    "US", asin, data.get("reported_rating_count") or None,
                    data.get("reported_review_count") or None,
                    data.get("review_count_source", ""), 0, 0, None,
                    summary_status, now,
                ),
            )
            _set_status(conn, "US", asin, "product_done", reason="product_parsed", attempts=0, task_stage="complete", reported_rating_count=data.get("reported_rating_count") or None, reported_review_count=data.get("reported_review_count") or None, reported_count_source=data.get("review_count_source", ""))
            _set_status(conn, "US", asin, "succeeded", reason="no_paginated_review_link", resume_status=None)


def _write_review_action(conn: sqlite3.Connection, run_id: str, task: sqlite3.Row, page: int, url: str, records: list[dict[str, Any]], next_url: str | None, body: str, status: int | None, block_reason: str | None, page_limit: int, source_type: str = "selenium_dom", raw_html_dir: Path | None = None, context: dict[str, Any] | None = None, cooldown_seconds: int | float | None = None, transfer_bytes: int | None = None) -> None:
    asin = task["asin"]
    with conn:
        raw_html_path = _insert_evidence(conn, run_id, asin, url, status, body, block_reason, source_type=source_type, raw_html_dir=raw_html_dir, context=context, transfer_bytes=transfer_bytes)
        if block_reason:
            if status == 429 or block_reason == "too_many_requests":
                conn.execute("INSERT OR REPLACE INTO review_page_state VALUES(?,?,?,?,?,?,?)", ("US", asin, page, url, "deferred", url, utc_now()))
                conn.execute("INSERT OR REPLACE INTO review_summary VALUES(?,?,?,?,?,?,?,?,?,?)", ("US", asin, task["reported_rating_count"], task["reported_review_count"], task["reported_count_source"], task["fetched_review_count"], task["review_pages_fetched"], url, "rate_limited", utc_now()))
                _set_rate_limited(conn, "US", asin, "reviews", cooldown_seconds)
            else:
                conn.execute("INSERT OR REPLACE INTO review_page_state VALUES(?,?,?,?,?,?,?)", ("US", asin, page, url, "blocked", None, utc_now()))
                _set_status(conn, "US", asin, "blocked", reason=block_reason, block_reason=block_reason, last_error=block_reason)
            return
        for record in records:
            record = dict(record)
            review_id = record["review_id"]
            values = {"asin": asin, "marketplace": "US", **record, "unique_key": f"US|{asin}|{review_id}"}
            cols = REVIEW_HEADERS
            conn.execute(f"INSERT OR REPLACE INTO review_record({','.join(cols)}) VALUES({','.join('?' for _ in cols)})", [values.get(c, "") for c in cols])
        if not records and int(task["reported_review_count"] or 0) > 0:
            _insert_evidence(conn, run_id, asin, url, status, body, None, "empty_review_page", source_type, raw_html_dir, raw_html_path, context, transfer_bytes)
            conn.execute(
                "INSERT OR REPLACE INTO review_page_state VALUES(?,?,?,?,?,?,?)",
                ("US", asin, page, url, "failed", url, utc_now()),
            )
            conn.execute(
                "INSERT OR REPLACE INTO review_summary VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    "US", asin, task["reported_rating_count"], task["reported_review_count"],
                    task["reported_count_source"], task["fetched_review_count"],
                    task["review_pages_fetched"], url, "failed", utc_now(),
                ),
            )
            _record_failure(conn, "US", asin, "empty_review_page", "empty_review_page")
            return
        conn.execute("INSERT OR REPLACE INTO review_page_state VALUES(?,?,?,?,?,?,?)", ("US", asin, page, url, "fetched", next_url, utc_now()))
        fetched_count = conn.execute("SELECT COUNT(*) FROM review_record WHERE marketplace='US' AND asin=?", (asin,)).fetchone()[0]
        next_page = page + 1 if next_url else None
        conn.execute("UPDATE item_state SET fetched_review_count=?,review_pages_fetched=?,next_review_url=?,next_review_page=?,task_stage=?,updated_at=? WHERE marketplace='US' AND asin=?", (fetched_count, page, next_url, next_page, "reviews" if next_url else "complete", utc_now(), asin))
        summary_status = "page_limit" if next_url and page_limit and page >= page_limit else ("in_progress" if next_url else "exhausted")
        conn.execute("INSERT OR REPLACE INTO review_summary VALUES(?,?,?,?,?,?,?,?,?,?)", ("US", asin, task["reported_rating_count"], task["reported_review_count"], task["reported_count_source"], fetched_count, page, next_url, summary_status, utc_now()))
        if not next_url:
            _set_status(conn, "US", asin, "succeeded", reason="reviews_exhausted", attempts=0, resume_status=None)
        elif page_limit and page >= page_limit:
            _set_status(conn, "US", asin, "reviews_pending", reason="review_page_limit", attempts=0, resume_status="reviews_pending")
        else:
            _set_status(conn, "US", asin, "reviews_pending", reason="review_page_fetched", attempts=0, resume_status="reviews_pending")


class ReviewPaginator:
    def __init__(self, conn: sqlite3.Connection, asin: str, initial_url: str) -> None:
        self.conn, self.asin, self.next_url = conn, asin, initial_url

    def resume(self) -> tuple[int, str | None]:
        row = self.conn.execute("SELECT next_review_page,next_review_url FROM item_state WHERE marketplace='US' AND asin=?", (self.asin,)).fetchone()
        if row and row["next_review_url"]:
            return int(row["next_review_page"] or 1), row["next_review_url"]
        return 1, self.next_url

    def record_page(self, page: int, url: str, next_url: str | None, records: list[dict[str, Any]], blocked: str | None = None) -> None:
        with self.conn:
            for record in records:
                values = {"asin": self.asin, "marketplace": "US", **record, "unique_key": f"US|{self.asin}|{record['review_id']}"}
                self.conn.execute(f"INSERT OR REPLACE INTO review_record({','.join(REVIEW_HEADERS)}) VALUES({','.join('?' for _ in REVIEW_HEADERS)})", [values.get(c, "") for c in REVIEW_HEADERS])
            self.conn.execute("INSERT OR REPLACE INTO review_page_state VALUES(?,?,?,?,?,?,?)", ("US", self.asin, page, url, "blocked" if blocked else "fetched", next_url, utc_now()))
            count = self.conn.execute("SELECT COUNT(*) FROM review_record WHERE marketplace='US' AND asin=?", (self.asin,)).fetchone()[0]
            self.conn.execute("UPDATE item_state SET fetched_review_count=?,review_pages_fetched=?,next_review_url=?,next_review_page=?,updated_at=? WHERE marketplace='US' AND asin=?", (count, page, next_url, page + 1 if next_url else None, utc_now(), self.asin))
        self.next_url = next_url


def extract_response_status(driver: Any) -> int | None:
    try:
        entries = driver.execute_script("return performance.getEntriesByType('navigation');") or []
        if not entries:
            return None
        value = entries[-1].get("responseStatus") if isinstance(entries[-1], dict) else None
        return int(value) if value not in (None, "", 0) else None
    except (AttributeError, TypeError, ValueError):
        return None


def delivery_context_confirmed(driver: Any, postal_code: str) -> bool:
    try:
        header = " ".join(
            (driver.find_element("id", element_id).text or "")
            for element_id in ("glow-ingress-line1", "glow-ingress-line2")
        )
        currency = driver.find_element("id", "currencyOfPreference").get_attribute("value") or ""
    except Exception:
        return False
    return postal_code in header and currency.strip().upper() == "USD"


def _is_stale_bidi_fail_request(error: Exception) -> bool:
    message = str(getattr(error, "msg", "") or "").strip().lower()
    return re.fullmatch(r"no such request:\s*blocked request with id \S+ not found", message) is not None


class SeleniumFirefoxAdapter:
    def __init__(self, config: dict[str, Any] | None = None, headless: bool | None = None, timeout: int | None = None) -> None:
        config = config or DEFAULTS
        self.config = config
        try:
            from selenium import webdriver
            from selenium.webdriver.common.proxy import Proxy
            from selenium.webdriver.firefox.options import Options
            from selenium.webdriver.firefox.service import Service
        except ImportError as exc:
            raise RuntimeError("live 模式需要 selenium；请在获得批准后准备运行依赖。") from exc
        self._temp_profile = tempfile.TemporaryDirectory(prefix="amazon-us-firefox-")
        options = Options()
        options.profile = self._temp_profile.name
        options.page_load_strategy = "eager"
        options.enable_bidi = True
        if headless if headless is not None else bool(config.get("headless", True)):
            options.add_argument("-headless")
        user_agent = str(config.get("user_agent") or "")
        if user_agent:
            options.set_preference("general.useragent.override", user_agent)
        proxy_settings = _firefox_proxy_settings(str(config.get("proxy_url") or ""))
        if proxy_settings:
            options.proxy = Proxy(proxy_settings)
        geckodriver_path = str(config.get("geckodriver_path") or "").strip()
        if geckodriver_path and not Path(geckodriver_path).is_absolute():
            geckodriver_path = str(ROOT / geckodriver_path)
        service = Service(executable_path=geckodriver_path) if geckodriver_path else Service()
        firefox_binary = str(config.get("firefox_binary") or "").strip()
        if firefox_binary and not Path(firefox_binary).is_absolute():
            firefox_binary = str(ROOT / firefox_binary)
        if firefox_binary:
            options.binary_location = firefox_binary
        self.driver = webdriver.Firefox(options=options, service=service)
        self.driver.set_page_load_timeout(timeout or int(config.get("request_timeout_seconds", 30)))
        self._context_initialized = False
        self._network_ledger = BrowserNetworkLedger()
        self.last_traffic = self._network_ledger.snapshot()
        self._request_handler_id: Any | None = None
        self._response_handler_id: Any | None = None
        self._fetch_error_handler_id: Any | None = None
        self._bidi_available = False
        self._closed = False
        self.last_context_error_stage: str | None = None
        self.last_context_error_code: str | None = None
        # Selenium 4.47 exposes the stable high-level driver.network surface.
        # Initialization is fail-closed so Firefox never silently runs without
        # the requested interception and measurement controls.
        try:
            network = self.driver.network
            self._request_handler_id = network.add_request_handler("before_request", self._handle_bidi_request)
            self._response_handler_id = network.add_event_handler(
                "response_completed", self._network_ledger.handle_response_completed
            )
            self._fetch_error_handler_id = network.add_event_handler(
                "fetch_error", self._network_ledger.handle_fetch_error
            )
            self._bidi_available = True
        except Exception as exc:
            try:
                self.driver.quit()
            finally:
                self._temp_profile.cleanup()
                self._closed = True
            raise RuntimeError("Firefox WebDriver BiDi network controls are unavailable") from exc

    def _handle_bidi_request(self, request: Any) -> None:
        from selenium.common.exceptions import WebDriverException

        blocked = self._network_ledger.should_block_request(request) or bool(getattr(self,"_recovery_denied",False))
        authorize = self.config.get("_recovery_browser_request")
        if not blocked and callable(authorize):
            try:
                authorize()
            except Exception:
                self._recovery_denied = True
                blocked = True
        try:
            if blocked:
                request.fail()
            else:
                request.continue_request()
        except WebDriverException as exc:
            if _is_stale_bidi_fail_request(exc):
                if blocked:
                    self._network_ledger.record_blocked_request_race()
                else:
                    self._network_ledger.record_continued_request_race(request)
                return
            raise

    def _ensure_delivery_context(self) -> bool:
        """Set the configured ZIP in this isolated browser session once."""
        postal_code = str((self.config.get("context") or {}).get("postal_code") or "").strip()
        if not postal_code:
            return False
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.support.ui import WebDriverWait

        try:
            current = " ".join(
                (self.driver.find_element(By.ID, element_id).text or "")
                for element_id in ("glow-ingress-line1", "glow-ingress-line2")
            )
        except Exception:
            current = ""
        if postal_code in current and delivery_context_confirmed(self.driver, postal_code):
            self._context_initialized = True
            return False
        self.driver.find_element(By.ID, "nav-global-location-popover-link").click()
        field = WebDriverWait(self.driver, 10).until(EC.presence_of_element_located((By.ID, "GLUXZipUpdateInput")))
        field.clear()
        field.send_keys(postal_code)
        apply_button = WebDriverWait(self.driver, 10).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "#GLUXZipUpdate input[type='submit']"))
        )
        # Native Firefox clicks can be acknowledged without firing Amazon's
        # location handlers. Use the same DOM click for Apply and confirmation.
        self.driver.execute_script("arguments[0].click()", apply_button)

        def commit_state(driver: Any) -> tuple[str, Any | None] | bool:
            if delivery_context_confirmed(driver, postal_code):
                return ("confirmed", None)
            candidates = [
                *driver.find_elements(By.CSS_SELECTOR, "button[name='glowDoneButton']"),
            ]
            try:
                candidates.append(
                    driver.find_element(By.XPATH, "//button[normalize-space()='Done' or normalize-space()='完成']")
                )
            except Exception:
                pass
            try:
                candidates.append(driver.find_element(By.ID, "GLUXConfirmClose"))
            except Exception:
                pass
            visible = [candidate for candidate in candidates if candidate.is_displayed()]
            return ("control", visible[-1]) if visible else False

        state, control = WebDriverWait(self.driver, 10).until(commit_state)
        if state == "control":
            self.driver.execute_script("arguments[0].click()", control)
            WebDriverWait(self.driver, 15).until(lambda driver: delivery_context_confirmed(driver, postal_code))
        # The modal updates the header before product modules are repainted.
        # Reload once after the location is committed, then confirm the ZIP
        # survived the navigation before taking page_source.
        self.driver.refresh()
        WebDriverWait(self.driver, 15).until(lambda driver: delivery_context_confirmed(driver, postal_code))
        time.sleep(0.8)
        self._context_initialized = True
        return True

    def fetch(self, url: str) -> tuple[str, int | None]:
        if not hasattr(self, "_network_ledger"):
            self._network_ledger = BrowserNetworkLedger()
        self._network_ledger.reset(top_context_id=None)
        self.last_context_error_stage = None
        self.last_context_error_code = None
        self._recovery_denied = False
        try:
            top_context_id = self.driver.current_window_handle
            self._network_ledger.reset(top_context_id=top_context_id)
            self.driver.get(url)
        except Exception:
            self.last_traffic = self._network_ledger.snapshot()
            self.close()
            if self._recovery_denied:
                from recovery_scheduler import RecoveryDenied
                raise RecoveryDenied("recovery_browser_budget_denied") from None
            raise AdapterFetchError(
                "Firefox browser session is unavailable", stage_code="browser_navigation"
            ) from None
        if self._recovery_denied:
            from recovery_scheduler import RecoveryDenied
            self.close()
            raise RecoveryDenied("recovery_browser_budget_denied")
        try:
            initial_body = self.driver.page_source
            initial_status = extract_response_status(self.driver)
        except Exception:
            self.last_traffic = self._network_ledger.snapshot()
            self.close()
            raise AdapterFetchError(
                "Firefox browser session is unavailable", stage_code="browser_capture"
            ) from None
        if classify_block(initial_status, initial_body):
            self.last_traffic = self._network_ledger.snapshot()
            return initial_body, initial_status
        try:
            self._ensure_delivery_context()
        except Exception as exc:
            self._context_initialized = False
            if self._recovery_denied:
                from recovery_scheduler import RecoveryDenied
                self.close()
                raise RecoveryDenied("recovery_browser_budget_denied") from None
            self.last_context_error_stage = "browser_delivery_context"
            self.last_context_error_code = (
                "delivery_context_timeout"
                if exc.__class__.__name__ in {"TimeoutException", "TimeoutError"}
                else "delivery_context_failed"
            )
            self.last_traffic = self._network_ledger.snapshot()
            self.close()
            return initial_body, initial_status
        if self._recovery_denied:
            from recovery_scheduler import RecoveryDenied
            self.close()
            raise RecoveryDenied("recovery_browser_budget_denied")
        try:
            body = self.driver.page_source
            status = extract_response_status(self.driver)
        except Exception:
            self.last_traffic = self._network_ledger.snapshot()
            self.close()
            raise AdapterFetchError(
                "Firefox browser session is unavailable", stage_code="browser_capture"
            ) from None
        self.last_traffic = self._network_ledger.snapshot()
        return body, status

    def export_anonymous_amazon_cookies(self) -> list[dict[str, Any]]:
        if not self._context_initialized:
            return []
        try:
            cookies = self.driver.get_cookies()
        except Exception as exc:
            raise AdapterFetchError("could not read isolated Firefox cookies") from exc
        return [dict(item) for item in cookies if isinstance(item, dict)]

    def close(self) -> None:
        if getattr(self, "_closed", False):
            return
        self._closed = True
        try:
            try:
                if getattr(self, "_bidi_available", False):
                    try:
                        network = self.driver.network
                        if self._request_handler_id is not None:
                            network.remove_request_handler("before_request", self._request_handler_id)
                        if self._response_handler_id is not None:
                            network.remove_event_handler("response_completed", self._response_handler_id)
                        if self._fetch_error_handler_id is not None:
                            network.remove_event_handler("fetch_error", self._fetch_error_handler_id)
                    except Exception:
                        pass
            finally:
                try:
                    self.driver.quit()
                except Exception:
                    pass
        finally:
            try:
                temp_profile = getattr(self, "_temp_profile", None)
                if temp_profile is not None:
                    temp_profile.cleanup()
            except Exception:
                pass


class HttpFirstAdapter:
    """Fetch public HTML with HTTP first and lazily fall back to Firefox."""

    source_type = "http_html"

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        try:
            from amazon_us_throttle import EgressLimiter
        except ModuleNotFoundError:
            sys.path.insert(0, str(ROOT / "scripts"))
            from amazon_us_throttle import EgressLimiter
        self.config = config or DEFAULTS
        self.timeout = int(self.config.get("request_timeout_seconds", 30))
        self.user_agent = str(self.config.get("user_agent") or "")
        self._opener_handlers: list[Any] = []
        self._deadline_guard = None
        observer = self._observe_deadline_connection if callable(self.config.get("_recovery_before_request")) else None
        self._recovery_deadline_enabled = observer is not None
        if observer is not None:
            from http_deadline import DeadlineHTTPHandler
            self._opener_handlers.append(DeadlineHTTPHandler(observer))
        self._proxy_auth_configured = False
        self._proxy_upstream_url = ""
        self._proxy_username_env = ""
        self._proxy_password_env = ""
        self._proxy_relay: ProxyConnectRelay | None = None
        self._proxy_request_jitter_seconds = max(0.0, min(float(self.config.get("proxy_request_jitter_seconds") or 0.0), 5.0))
        proxy_url = str(self.config.get("proxy_url") or "").strip()
        if proxy_url:
            proxy_parts = urlsplit(proxy_url)
            if proxy_parts.scheme not in {"http", "https"} or not proxy_parts.hostname:
                raise ValueError("proxy_url must be an explicit http(s) URL")
            if proxy_parts.username or proxy_parts.password:
                raise ValueError("proxy credentials must not be embedded in proxy_url")
            proxy_handler = urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
            self._opener_handlers.append(proxy_handler)
            self._proxy_upstream_url = proxy_url
            username_env = str(self.config.get("proxy_username_env") or "").strip()
            password_env = str(self.config.get("proxy_password_env") or "").strip()
            if bool(username_env) != bool(password_env):
                raise ValueError("proxy_username_env and proxy_password_env must be configured together")
            if username_env:
                username = os.environ.get(username_env, "")
                password = os.environ.get(password_env, "")
                if not username or not password:
                    raise ValueError("proxy credential environment variables are not both populated")
                self._opener_handlers.append(ProxyTunnelAuthHTTPSHandler(username, password, connection_observer=observer))
                self._proxy_auth_configured = True
                self._proxy_username_env = username_env
                self._proxy_password_env = password_env
        if observer is not None and not self._proxy_auth_configured:
            from http_deadline import DeadlineHTTPSHandler
            self._opener_handlers.append(DeadlineHTTPSHandler(observer))
        self.cookie_session = RunScopedAmazonCookieSession(
            "adapter-instance", str(self.config.get("tenant_id") or "local"), str(self.config.get("worker_id") or "worker")
        )
        self._rebuild_opener()
        self.egress_id = str(self.config.get("egress_id") or "direct")
        self.limiter = EgressLimiter(
            float(self.config.get("global_requests_per_second", 0.0)),
            float(self.config.get("egress_requests_per_second", 0.0)),
            int(self.config.get("rate_burst", 1)),
        )
        self.browser: SeleniumFirefoxAdapter | None = None
        self.last_retry_after_seconds: int | None = None
        self.last_transfer_bytes: int | None = None
        self.action_http_transfer_bytes = 0
        self.last_browser_traffic: dict[str, Any] | None = None
        self.last_browser_context_confirmed = False
        self.last_fallback_reason: str | None = None
        self.action_fallback_reasons: list[str] = []
        self.browser_attempted = False
        self.last_cookie_bridge_status: str | None = None
        self.last_cookie_bridge_error_code: str | None = None
        self.last_context_error_stage: str | None = None
        self.last_context_error_code: str | None = None

    def _observe_deadline_connection(self, connection) -> None:
        if self._deadline_guard is not None:
            self._deadline_guard.bind_connection(connection)

    def enable_recovery_deadline(self) -> None:
        """Support late hook binding without losing cookies or CONNECT auth."""
        if self._recovery_deadline_enabled or not callable(self.config.get("_recovery_before_request")):
            return
        from http_deadline import DeadlineHTTPHandler, DeadlineHTTPSHandler
        observer=self._observe_deadline_connection
        self._opener_handlers.append(DeadlineHTTPHandler(observer))
        if self._proxy_auth_configured:
            for handler in self._opener_handlers:
                if isinstance(handler,ProxyTunnelAuthHTTPSHandler):
                    handler._connection_observer=observer
        else:
            self._opener_handlers.append(DeadlineHTTPSHandler(observer))
        self._rebuild_opener()
        self._recovery_deadline_enabled=True

    def _rebuild_opener(self) -> None:
        cookie_handler = urllib.request.HTTPCookieProcessor(self.cookie_session.jar)
        self.opener = urllib.request.build_opener(*self._opener_handlers, cookie_handler)

    def begin_run(self, run_id: str, tenant_id: str, worker_id: str) -> None:
        scope = (str(run_id), str(tenant_id), str(worker_id))
        if self.cookie_session.scope == scope:
            return
        try:
            if self.browser is not None:
                self.browser.close()
        finally:
            self.browser = None
            self.cookie_session.close()
            self.cookie_session = RunScopedAmazonCookieSession(*scope)
            self._rebuild_opener()

    def begin_action(self) -> None:
        if callable(self.config.get("_recovery_before_request")):
            # Never rebind an old browser's background requests to a new lease.
            if self.browser is not None:
                self.browser.close()
                self.browser=None
            if self._proxy_relay is not None:
                self._proxy_relay.close()
                self._proxy_relay=None
        self.action_http_transfer_bytes = 0
        self.last_browser_traffic = None
        self.last_browser_context_confirmed = False
        self.last_fallback_reason = None
        self.action_fallback_reasons = []
        self.browser_attempted = False
        self.last_cookie_bridge_status = None
        self.last_cookie_bridge_error_code = None
        self.last_context_error_stage = None
        self.last_context_error_code = None

    @staticmethod
    def _decode(response: Any, body: bytes) -> str:
        charset = "utf-8"
        try:
            content_type = response.headers.get_content_charset()
            if content_type:
                charset = content_type
        except (AttributeError, LookupError):
            pass
        return body.decode(charset, errors="replace")

    def fetch(self, url: str) -> tuple[str, int | None]:
        self.last_retry_after_seconds = None
        self.enable_recovery_deadline()
        self.last_transfer_bytes = 0
        self.last_browser_traffic = None
        self.last_fallback_reason = None
        self.last_browser_context_confirmed = False
        self.source_type = "http_html"
        max_attempts = max(1, min(int(self.config.get("http_max_attempts", 2)), 3))
        backoff = max(0.0, min(float(self.config.get("http_retry_backoff_seconds", 0.5)), 5.0))
        last_error: Exception | None = None
        for attempt in range(1, max_attempts + 1):
            self.limiter.acquire(self.egress_id)
            if self._proxy_upstream_url and self._proxy_request_jitter_seconds:
                delay = random.uniform(0.0, self._proxy_request_jitter_seconds)
                if delay > 0:
                    time.sleep(delay)
            authorize = self.config.get("_recovery_before_request")
            request_timeout = min(self.timeout, authorize()) if callable(authorize) else self.timeout
            request = urllib.request.Request(
                url,
                headers={
                    "Accept": "text/html,application/xhtml+xml",
                    "Accept-Encoding": str(self.config.get("http_accept_encoding") or "identity"),
                    "Connection": "close",
                    "User-Agent": self.user_agent,
                },
                method="GET",
            )
            if callable(authorize):
                from http_deadline import HttpDeadline
                self._deadline_guard = HttpDeadline(request_timeout)
            try:
                with self.opener.open(request, timeout=request_timeout) as response:
                    encoded_body = self._read_budgeted_body(response)
                    self.last_transfer_bytes += len(encoded_body)
                    self.action_http_transfer_bytes += len(encoded_body)
                    body = self._decode_content(response, encoded_body)
                    self.source_type = "http_html"
                    return self._decode(response, body), int(response.getcode() or 200)
            except urllib.error.HTTPError as exc:
                if int(exc.code) == 429:
                    self.last_retry_after_seconds = _retry_after_seconds(exc.headers.get("Retry-After") if exc.headers else None)
                try:
                    encoded_body = self._read_budgeted_body(exc)
                except http.client.IncompleteRead as partial:
                    encoded_body = partial.partial or b""
                self.last_transfer_bytes += len(encoded_body)
                self.action_http_transfer_bytes += len(encoded_body)
                body = self._decode_content(exc, encoded_body)
                self.source_type = "http_html"
                return self._decode(exc, body), int(exc.code)
            except http.client.IncompleteRead as exc:
                self.last_transfer_bytes += len(exc.partial or b"")
                self.action_http_transfer_bytes += len(exc.partial or b"")
                last_error = exc
                if attempt < max_attempts and backoff:
                    time.sleep(backoff * attempt)
            except (urllib.error.URLError, ConnectionResetError, TimeoutError, OSError) as exc:
                if self._deadline_guard is not None:
                    self._deadline_guard.check()
                last_error = exc
                if attempt < max_attempts and backoff:
                    time.sleep(backoff * attempt)
            finally:
                if self._deadline_guard is not None:
                    self._deadline_guard.close()
                    self._deadline_guard = None
        # Observed partial bytes remain in action_http_transfer_bytes; a failed
        # transport has no known complete transfer total (including TLS EOF).
        self.last_transfer_bytes = None
        raise AdapterFetchError(f"{last_error} after {max_attempts} HTTP attempts") from last_error

    def _read_budgeted_body(self, response: Any) -> bytes:
        if not callable(self.config.get("_recovery_before_request")):
            return response.read()
        limit = 4*1024*1024
        if self._deadline_guard is not None:
            self._deadline_guard.bind_response(response)
        body = response.read(limit+1)
        if self._deadline_guard is not None:
            self._deadline_guard.check()
        if len(body) > limit:
            self.last_transfer_bytes += len(body)
            self.action_http_transfer_bytes += len(body)
            from recovery_scheduler import RecoveryDenied
            raise RecoveryDenied("recovery_http_body_budget_denied")
        return body

    @staticmethod
    def _decode_content(response: Any, body: bytes) -> bytes:
        try:
            encoding = str(response.headers.get("Content-Encoding", "")).lower()
        except AttributeError:
            encoding = ""
        if "gzip" in encoding:
            try:
                return gzip.decompress(body)
            except (OSError, EOFError) as exc:
                raise AdapterFetchError("invalid gzip HTTP response") from exc
        return body

    def needs_browser_fallback(self, data: dict[str, Any]) -> bool:
        # A page without these anchors cannot be safely accepted as a product page.
        return not all(data.get(key) for key in ("asin", "canonical_url", "title"))

    def fetch_browser(
        self,
        url: str,
        *,
        fallback_reason: FallbackReason,
        run_id: str,
        asin: str,
    ) -> tuple[str, int | None]:
        if not isinstance(fallback_reason, FallbackReason):
            raise TypeError("fallback reason must be a FallbackReason")
        self.last_fallback_reason = fallback_reason.value
        browser_config = getattr(self, "config", DEFAULTS)
        authorize = browser_config.get("_recovery_before_request")
        if callable(authorize):
            remaining = authorize()
            browser_config = {**browser_config, "request_timeout_seconds": min(self.timeout, remaining)}
        if getattr(self, "_proxy_auth_configured", False):
            if str(self.config.get("firefox_proxy_auth_mode") or "disabled") != "loopback_connect_relay":
                raise AdapterFetchError("Firefox proxy authentication is unavailable")
            if self._proxy_relay is None:
                username = os.environ.get(self._proxy_username_env, "")
                password = os.environ.get(self._proxy_password_env, "")
                if not username or not password:
                    raise AdapterFetchError("Firefox proxy authentication relay is unavailable")
                try:
                    self._proxy_relay = ProxyConnectRelay(
                        self._proxy_upstream_url, username, password,
                        connect_timeout_seconds=self.timeout,
                        transfer_budget=self.config.get("_recovery_relay_bytes"),
                    )
                    self._proxy_relay.start()
                except (OSError, RuntimeError, ValueError):
                    self._proxy_relay = None
                    raise AdapterFetchError(
                        "Firefox proxy authentication relay is unavailable",
                        stage_code="browser_proxy_setup",
                    ) from None
            relay_host, relay_port = self._proxy_relay.address
            browser_config = {
                **browser_config,
                "proxy_url": f"http://{relay_host}:{relay_port}",
                "proxy_username_env": "",
                "proxy_password_env": "",
            }
        if self.browser is None:
            try:
                self.browser = SeleniumFirefoxAdapter(browser_config)
            except (RuntimeError, OSError):
                raise AdapterFetchError(
                    "Firefox browser initialization failed", stage_code="browser_driver_init"
                ) from None
        browser = self.browser
        try:
            body, status = browser.fetch(url)
        except AdapterFetchError:
            self.last_browser_traffic = dict(getattr(browser, "last_traffic", None) or {})
            self.last_browser_context_confirmed = False
            try:
                close_browser = getattr(browser, "close", None)
                if close_browser is not None:
                    close_browser()
            except Exception:
                pass
            finally:
                self.browser = None
            raise
        self.source_type = "selenium_dom"
        self.last_transfer_bytes = None
        self.last_browser_traffic = dict(browser.last_traffic)
        self.last_browser_context_confirmed = bool(browser._context_initialized)
        self.last_context_error_stage = getattr(browser, "last_context_error_stage", None)
        self.last_context_error_code = getattr(browser, "last_context_error_code", None)
        if bool(getattr(browser, "_closed", False)):
            self.browser = None
        return body, status

    def proxy_relay_summary(self) -> dict[str, Any] | None:
        return self._proxy_relay.audit_summary() if self._proxy_relay is not None else None

    def commit_browser_context(self, run_id: str, *, context_confirmed: bool) -> int:
        if self.browser is None or not context_confirmed or not self.browser._context_initialized:
            return 0
        cookies = self.browser.export_anonymous_amazon_cookies()
        if cookies:
            scope_run, scope_tenant, scope_worker = self.cookie_session.scope
            if str(run_id) != scope_run:
                raise AdapterFetchError("browser cookie scope does not match current run")
            return self.cookie_session.sync_from_firefox(
                cookies,
                run_id=scope_run,
                tenant_id=scope_tenant,
                worker_id=scope_worker,
                context_confirmed=self.browser._context_initialized,
            )
        return 0

    def close(self) -> None:
        try:
            if self.browser is not None:
                self.browser.close()
        finally:
            try:
                if self._proxy_relay is not None:
                    self._proxy_relay.close()
            finally:
                self._proxy_relay = None
                self.cookie_session.close()


def _canonical_asin(value: Any) -> str:
    parts = urlsplit(str(value or ""))
    match = re.search(r"/(?:dp|clp)/([A-Za-z0-9]{10})(?:/|$)", parts.path)
    return match.group(1).upper() if match else ""


def _valid_amazon_canonical(value: Any) -> bool:
    parts = urlsplit(str(value or ""))
    return bool(
        parts.scheme == "https"
        and (parts.hostname or "").lower().removeprefix("www.") == "amazon.com"
        and _canonical_asin(value)
    )


def _is_canonical_parent_child(data: dict[str, Any], expected_asin: str) -> bool:
    expected = expected_asin.upper()
    parsed = str(data.get("asin") or "").upper()
    parent = str(data.get("parent_asin") or "").upper()
    canonical = _canonical_asin(data.get("canonical_url"))
    children = {str(value or "").upper() for value in data.get("identity_child_asins") or []}
    return bool(
        parsed == expected
        and parent
        and parent != expected
        and canonical == parent
        and expected in children
        and _valid_amazon_canonical(data.get("canonical_url"))
    )


def _valid_asin_identity(data: dict[str, Any], expected_asin: str) -> bool:
    expected = expected_asin.upper()
    parsed = str(data.get("asin") or "").upper()
    canonical = _canonical_asin(data.get("canonical_url"))
    return bool(
        parsed == expected
        and _valid_amazon_canonical(data.get("canonical_url"))
        and (canonical == expected or _is_canonical_parent_child(data, expected))
    )


def _has_explicit_asin_mismatch(data: dict[str, Any], expected_asin: str) -> bool:
    expected = expected_asin.upper()
    parsed = str(data.get("asin") or "").upper()
    canonical_url = data.get("canonical_url")
    canonical = _canonical_asin(canonical_url)
    return bool(
        (parsed and parsed != expected)
        or (canonical_url and not _valid_amazon_canonical(canonical_url))
        or (canonical and canonical != expected and not _is_canonical_parent_child(data, expected))
    )


def _valid_product_identity(data: dict[str, Any], expected_asin: str) -> bool:
    return bool(
        _valid_asin_identity(data, expected_asin)
        and str(data.get("title") or "").strip()
    )


def _is_terminal_missing_core_failure(status: int | None, missing_core: list[str]) -> bool:
    missing = set(missing_core)
    return status == 404 and {"asin", "title"}.issubset(missing)


def _core_fallback_reason(data: dict[str, Any], expected_asin: str) -> FallbackReason | None:
    if _has_explicit_asin_mismatch(data, expected_asin):
        return None
    if not str(data.get("asin") or "").strip():
        return FallbackReason.MISSING_ASIN
    if not str(data.get("canonical_url") or "").strip():
        return FallbackReason.MISSING_CANONICAL_URL
    if not str(data.get("title") or "").strip():
        return FallbackReason.MISSING_TITLE
    return None


def _fetch_browser_once(
    adapter: Any,
    url: str,
    *,
    fallback_reason: FallbackReason,
    run_id: str,
    asin: str,
    ledger: BrowserFallbackLedger,
    max_attempts: int = 1,
    raw_html_dir: Path | None = None,
) -> tuple[str, int | None] | None:
    attempt_count = int(
        getattr(
            adapter,
            "browser_attempt_count",
            1 if bool(getattr(adapter, "browser_attempted", False)) else 0,
        )
    )
    if attempt_count >= int(max_attempts):
        return None
    if not hasattr(adapter, "fetch_browser") or not ledger.claim(run_id, asin, fallback_reason):
        return None
    setattr(adapter, "last_fallback_reason", fallback_reason.value)
    setattr(adapter, "browser_attempted", True)
    setattr(adapter, "browser_attempt_count", attempt_count + 1)
    fallback_reasons = getattr(adapter, "action_fallback_reasons", None)
    if fallback_reasons is None:
        fallback_reasons = []
        setattr(adapter, "action_fallback_reasons", fallback_reasons)
    if fallback_reason.value not in fallback_reasons:
        fallback_reasons.append(fallback_reason.value)
    method = adapter.fetch_browser
    parameters = inspect.signature(method).parameters
    accepts_keywords = "fallback_reason" in parameters or any(
        value.kind == inspect.Parameter.VAR_KEYWORD for value in parameters.values()
    )
    try:
        if accepts_keywords:
            return method(url, fallback_reason=fallback_reason, run_id=run_id, asin=asin)
        return method(url)
    except AdapterFetchError as exc:
        _preserve_browser_failure_attempt(adapter, url, raw_html_dir, run_id, asin, exc)
        raise


def _evidence_context(base_context: dict[str, Any] | None, adapter: Any) -> dict[str, Any]:
    context = dict(base_context or {})
    recovery_snapshot = getattr(adapter, "config", {}).get("_recovery_snapshot")
    if callable(recovery_snapshot):
        context["recovery"] = recovery_snapshot()
    traffic: dict[str, Any] = {}
    action_http_transfer = getattr(adapter, "action_http_transfer_bytes", None)
    action_http_unknown = int(getattr(adapter, "action_http_transfer_unknown_count", 0) or 0)
    if action_http_transfer is not None or action_http_unknown:
        traffic["http_compressed_response_bytes"] = action_http_transfer
        if action_http_unknown:
            traffic["http_compressed_response_unknown_count"] = action_http_unknown
    elif getattr(adapter, "source_type", "http_html") == "http_html":
        transfer = getattr(adapter, "last_transfer_bytes", None)
        traffic["http_compressed_response_bytes"] = transfer
    if getattr(adapter, "source_type", "http_html") != "http_html" or bool(getattr(adapter, "browser_attempted", False)):
        browser = dict(getattr(adapter, "last_browser_traffic", None) or {})
        traffic.update(
            {
                "firefox_main_document_bytes": browser.get("main_document_bytes"),
                "firefox_subresource_bytes": browser.get("subresource_bytes"),
                "firefox_main_document_known_count": int(browser.get("main_document_known_count") or 0),
                "firefox_subresource_known_count": int(browser.get("subresource_known_count") or 0),
                "firefox_main_document_unknown_count": int(browser.get("main_document_unknown_count") or 1),
                "firefox_subresource_unknown_count": int(browser.get("subresource_unknown_count") or 1),
                "blocked_resource_counts": dict(browser.get("blocked_resource_counts") or {}),
                "blocked_request_race_count": int(browser.get("blocked_request_race_count") or 0),
                "continued_request_race_count": int(browser.get("continued_request_race_count") or 0),
            }
        )
    context["traffic"] = traffic
    fallback_reason = getattr(adapter, "last_fallback_reason", None)
    if fallback_reason:
        context["fallback_reason"] = str(fallback_reason)
    fallback_reasons = list(getattr(adapter, "action_fallback_reasons", None) or [])
    if fallback_reasons:
        context["fallback_reasons"] = fallback_reasons
    bridge_status = getattr(adapter, "last_cookie_bridge_status", None)
    if bridge_status:
        bridge = {"status": str(bridge_status)}
        bridge_error = getattr(adapter, "last_cookie_bridge_error_code", None)
        if bridge_error:
            bridge["error_code"] = str(bridge_error)
        context["cookie_bridge"] = bridge
    context_error_stage = getattr(adapter, "last_context_error_stage", None)
    context_error_code = getattr(adapter, "last_context_error_code", None)
    if context_error_stage or context_error_code:
        context["browser_context"] = {
            "status": "partial",
            "error_stage": str(context_error_stage or "browser_delivery_context"),
            "error_code": str(context_error_code or "delivery_context_failed"),
        }
    pool_context = getattr(adapter, "evidence_context", None)
    if callable(pool_context):
        context["proxy_session_pool"] = pool_context()
    relay_summary = getattr(adapter, "proxy_relay_summary", None)
    if callable(relay_summary):
        relay = relay_summary()
        if relay:
            context["proxy_connect_relay"] = relay
    if config_profile := str(getattr(adapter, "config", {}).get("egress_profile") or "proxy_sessions"):
        context["egress_profile"] = config_profile
    return context


def _capture_proxy_attempt_evidence(
    adapter: Any,
    raw_html_dir: Path | None,
    run_id: str,
    asin: str,
) -> None:
    drain = getattr(adapter, "drain_intermediate_attempts", None)
    accept = getattr(adapter, "add_attempt_evidence", None)
    if not callable(drain) or not callable(accept):
        return
    persisted = []
    for attempt in drain():
        value = dict(attempt)
        body = str(value.pop("body", ""))
        value.pop("url", None)
        value["content_hash"] = hashlib.sha256(body.encode()).hexdigest() if body else None
        value["raw_html_path"] = _persist_raw_html(raw_html_dir, run_id, asin, body) if body else None
        persisted.append(value)
    accept(persisted)


def _preserve_browser_failure_attempt(
    adapter: Any,
    url: str,
    raw_html_dir: Path | None,
    run_id: str,
    asin: str,
    error: AdapterFetchError,
) -> None:
    allowed_stages = {
        "browser_proxy_setup", "browser_driver_init", "browser_navigation",
        "browser_delivery_context", "browser_capture",
    }
    stage_code = str(getattr(error, "stage_code", "") or "")
    if stage_code not in allowed_stages:
        stage_code = "browser_capture"
    preserve = getattr(adapter, "preserve_browser_attempt", None)
    if callable(preserve):
        preserve(
            url, "", None, None,
            error_code="browser_fetch_error", stage_code=stage_code,
        )
        _capture_proxy_attempt_evidence(adapter, raw_html_dir, run_id, asin)


def _proxy_circuit_reason(adapter: Any) -> str | None:
    return str(getattr(adapter, "circuit_open_reason", "") or "") or None


def _proxy_capacity_available(adapter: Any) -> bool:
    method = getattr(adapter, "can_claim_new_asin", None)
    return bool(method()) if callable(method) else True


def _browser_fallback_available(adapter: Any) -> bool:
    method = getattr(adapter, "browser_fallback_available", None)
    if callable(method):
        return bool(method())
    return callable(getattr(adapter, "fetch_browser", None))


def _record_proxy_outcome(adapter: Any, outcome: str, asin: str) -> None:
    method = getattr(adapter, "record_outcome", None)
    if callable(method):
        method(outcome, asin)


def _note_proxy_unrequested(adapter: Any, count: int) -> None:
    method = getattr(adapter, "note_unrequested", None)
    if callable(method):
        method(max(0, count))


def _should_stop_after_block(adapter: Any, config: dict[str, Any], status: int | None, reason: str | None) -> bool:
    if callable(getattr(adapter, "evidence_context", None)):
        return _proxy_circuit_reason(adapter) is not None
    return bool(config.get("stop_on_block", True)) or status == 429 or reason == "too_many_requests"


def _build_http_adapter(config: dict[str, Any]) -> Any:
    if not config.get("proxy_session_ports"):
        return HttpFirstAdapter(config)
    try:
        from proxy_session_pool import ProxySessionPool
    except ModuleNotFoundError:
        sys.path.insert(0, str(ROOT / "scripts"))
        from proxy_session_pool import ProxySessionPool
    return ProxySessionPool(config, HttpFirstAdapter, classify_block)


def _product_evidence_context(
    base_context: dict[str, Any] | None,
    adapter: Any,
    data: dict[str, Any],
    expected_asin: str,
) -> dict[str, Any]:
    context = _evidence_context(base_context, adapter)
    _, quality = _assess_product_context(data, base_context, adapter)
    context.update(quality)
    if _is_canonical_parent_child(data, expected_asin):
        context["parent_asin"] = str(data.get("parent_asin") or "").upper()
        context["identity_relation"] = "child_of_canonical_parent"
    return context


def _identity_mismatch_evidence_context(
    base_context: dict[str, Any] | None,
    adapter: Any,
    data: dict[str, Any],
    expected_asin: str,
) -> dict[str, Any]:
    """Persist identity evidence only; never project sibling product fields onto the requested ASIN."""
    context = _evidence_context(base_context, adapter)
    context["identity"] = {
        "requested_asin": expected_asin.upper(),
        "observed_asin": str(data.get("asin") or "").upper(),
        "canonical_asin": str(_canonical_asin(data.get("canonical_url")) or "").upper(),
        "canonical_valid_amazon": _valid_amazon_canonical(data.get("canonical_url")),
        "parent_asin": str(data.get("parent_asin") or "").upper(),
        "child_asins": sorted({str(value).upper() for value in data.get("identity_child_asins") or [] if value}),
    }
    return context


def _is_sibling_variant_redirect(data: dict[str, Any], expected_asin: str) -> bool:
    expected = expected_asin.upper()
    observed = str(data.get("asin") or "").upper()
    canonical = str(_canonical_asin(data.get("canonical_url")) or "").upper()
    parent = str(data.get("parent_asin") or "").upper()
    children = {str(value).upper() for value in data.get("identity_child_asins") or []}
    return bool(
        parent
        and expected != observed
        and canonical == observed
        and expected in children
        and observed in children
        and _valid_amazon_canonical(data.get("canonical_url"))
    )


def _assess_product_context(
    data: dict[str, Any],
    expected_context: dict[str, Any] | None,
    adapter: Any,
) -> tuple[list[str], dict[str, Any]]:
    expected = dict(expected_context or {})
    expected_country = str(expected.get("expected_country") or "").strip().upper()
    expected_currency = str(expected.get("expected_currency") or "").strip().upper()
    expected_postal = str(expected.get("postal_code") or "").strip()[:5]
    browser_confirmed = bool(getattr(adapter, "last_browser_context_confirmed", False))
    browser_context_failed = (
        str(getattr(adapter, "last_context_error_stage", None) or "") == "browser_delivery_context"
    )
    errors = validate_context(data, expected)
    hard_errors = [error for error in errors if error != "delivery_postal_mismatch"]

    price = str(data.get("price") or "").strip().upper()
    if expected_currency == "USD" and not browser_confirmed and not ("$" in price or "USD" in price):
        if "currency_mismatch" not in hard_errors:
            hard_errors.append("currency_not_observed")
    page_text = " ".join(
        str(data.get(key) or "")
        for key in ("availability", "buy_box", "product_description", "delivery_context")
    )
    observed_postals = sorted(set(re.findall(r"(?<!\d)(\d{5})(?:-\d{4})?(?!\d)", page_text)))
    explicit_us = bool(
        re.search(r"(?:\bunited states\b|\busa\b|\bu\.s\.(?:\s|$))", page_text, flags=re.IGNORECASE)
        or re.search(r"deliver(?:ing)?\s+to.{0,80}(?<!\d)\d{5}(?:-\d{4})?(?!\d)", page_text, flags=re.IGNORECASE)
    )
    if expected_country == "US" and not browser_confirmed and not explicit_us:
        if "delivery_country_mismatch" not in hard_errors:
            hard_errors.append("delivery_country_not_observed")
    postal_confirmed = bool(
        not browser_context_failed
        and (not expected_postal or browser_confirmed or expected_postal in observed_postals)
    )
    observed_postal = expected_postal if expected_postal in observed_postals else (observed_postals[0] if observed_postals else None)
    context_quality = (
        "invalid" if hard_errors
        else "partial" if browser_context_failed or not postal_confirmed
        else "full"
    )
    quality: dict[str, Any] = {
        "context_quality": context_quality,
        "postal_confirmed": postal_confirmed,
        "expected_postal": expected_postal or None,
        "observed_postal": observed_postal,
    }
    if context_quality == "partial":
        quality["location_sensitive_fields_unverified"] = ["price", "availability", "buy_box", "delivery"]
    return list(dict.fromkeys(hard_errors)), quality


def _commit_browser_context_safely(adapter: Any, run_id: str) -> None:
    try:
        accepted = adapter.commit_browser_context(run_id, context_confirmed=True)
    except AdapterFetchError:
        setattr(adapter, "last_cookie_bridge_status", "failed")
        setattr(adapter, "last_cookie_bridge_error_code", "cookie_bridge_error")
    else:
        setattr(adapter, "last_cookie_bridge_status", "committed" if accepted else "confirmed_no_cookie")
        setattr(adapter, "last_cookie_bridge_error_code", None)


def _atomic_csv(path: Path, headers: list[str], rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.tmp")
    with temp.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=headers, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    temp.replace(path)


def materialize_csvs(conn: sqlite3.Connection, output_dir: Path = DEFAULT_OUTPUT_DIR) -> None:
    queries = {
        "product_snapshot": "SELECT * FROM product_snapshot ORDER BY marketplace,asin",
        "media_asset": "SELECT * FROM media_asset ORDER BY marketplace,asin,unique_key",
        "content_module": "SELECT * FROM content_module ORDER BY marketplace,asin,position,unique_key",
        "review_summary": "SELECT * FROM review_summary ORDER BY marketplace,asin",
        "review_record": "SELECT * FROM review_record ORDER BY marketplace,asin,page,review_id",
        "collection_evidence": "SELECT run_id,asin,marketplace,url,http_status,transfer_bytes,retrieved_at,source_type,content_hash,raw_html_path,block_reason,parser_version,error_code,context_json FROM collection_evidence ORDER BY id",
    }
    for table, query in queries.items():
        filename, headers = OUTPUTS[table]
        rows = [dict(row) for row in conn.execute(query)]
        _atomic_csv(output_dir / filename, headers, rows)


def _claim_action(conn: sqlite3.Connection, row: sqlite3.Row) -> None:
    if row["status"] in {"pending", "failed"}:
        _set_status(conn, "US", row["asin"], "running", reason="action_claim", max_attempts=row["max_attempts"], resume_status="reviews_pending" if row["status"] == "reviews_pending" or row["task_stage"] == "reviews" else "pending", task_stage=row["task_stage"])
    elif row["status"] == "reviews_pending":
        _set_status(conn, "US", row["asin"], "running", reason="review_action_claim", resume_status="reviews_pending", task_stage="reviews")
    else:
        raise ValueError(f"不可领取状态: {row['status']}")


def _claim_refresh_request(conn: sqlite3.Connection) -> sqlite3.Row | None:
    request = conn.execute("SELECT * FROM refresh_request WHERE status='queued' ORDER BY requested_at,job_id LIMIT 1").fetchone()
    if request is None:
        return None
    state = conn.execute("SELECT * FROM item_state WHERE marketplace=? AND asin=?", (request["marketplace"], request["asin"])).fetchone()
    if state is None or state["status"] == "running":
        return None
    _set_status(
        conn, request["marketplace"], request["asin"], "pending", reason="refresh_request_claimed",
        attempts=0, task_stage="product", resume_status=None, next_review_url=None, next_review_page=None, next_retry_at=None,
        block_reason=None, last_error=None,
    )
    with conn:
        conn.execute("UPDATE refresh_request SET status='claimed' WHERE job_id=?", (request["job_id"],))
    return request


def _finish_refresh_request(conn: sqlite3.Connection, job_id: str, status: str) -> None:
    if status not in {"queued", "completed", "failed"}:
        raise ValueError(f"unknown refresh request status: {status}")
    with conn:
        conn.execute("UPDATE refresh_request SET status=? WHERE job_id=?", (status, job_id))


def _select_actions(conn: sqlite3.Connection, max_actions: int, exclude_asins: set[str] | None = None) -> list[sqlite3.Row]:
    now = utc_now()
    rows = list(conn.execute("SELECT * FROM item_state WHERE ((status IN ('pending','reviews_pending') AND (next_retry_at IS NULL OR next_retry_at <= ?)) OR (status='failed' AND attempts < max_attempts AND (next_retry_at IS NULL OR next_retry_at <= ?))) ORDER BY asin LIMIT ?", (now, now, max_actions + len(exclude_asins or set()))))
    return [row for row in rows if row["asin"] not in (exclude_asins or set())][:max_actions]


def run_actions(conn: sqlite3.Connection, adapter: Any, config: dict[str, Any], *, limit: int | None = None, run_id: str | None = None) -> int:
    run_id = run_id or f"run-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}"
    fallback_ledger = BrowserFallbackLedger()
    if hasattr(adapter, "begin_run"):
        adapter.begin_run(run_id, "sqlite-local", str(config.get("agent_name") or "amazon-us-worker"))
    max_actions = min(limit, int(config["max_actions_per_run"])) if limit else int(config["max_actions_per_run"])
    raw_html_dir_value = config.get("raw_html_dir")
    raw_html_dir = Path(raw_html_dir_value) if raw_html_dir_value else None
    actions = 0
    blocked = False
    refresh_request = _claim_refresh_request(conn)
    refresh_job_id = refresh_request["job_id"] if refresh_request is not None else None
    refresh_asin = refresh_request["asin"] if refresh_request is not None else None
    selected = []
    if refresh_request is not None:
        selected.append(conn.execute("SELECT * FROM item_state WHERE marketplace=? AND asin=?", (refresh_request["marketplace"], refresh_request["asin"])).fetchone())
    selected.extend(_select_actions(conn, max(0, max_actions - len(selected)), {refresh_asin} if refresh_asin else set()))
    for initial in selected:
        if _proxy_circuit_reason(adapter) or not _proxy_capacity_available(adapter):
            if not _proxy_circuit_reason(adapter):
                setattr(adapter, "circuit_open_reason", "session_pool_exhausted")
            _note_proxy_unrequested(adapter, len(selected) - actions)
            break
        row = conn.execute("SELECT * FROM item_state WHERE marketplace='US' AND asin=?", (initial["asin"],)).fetchone()
        if row["status"] == "failed" and row["attempts"] >= row["max_attempts"]:
            continue
        _claim_action(conn, row)
        if hasattr(adapter, "begin_action"):
            adapter.begin_action()
        row = conn.execute("SELECT * FROM item_state WHERE marketplace='US' AND asin=?", (row["asin"],)).fetchone()
        if row["task_stage"] == "reviews" and row["next_review_url"]:
            page, url = int(row["next_review_page"] or 1), row["next_review_url"]
            try:
                body, response_status = adapter.fetch(url)
                _capture_proxy_attempt_evidence(adapter, raw_html_dir, run_id, row["asin"])
            except AdapterFetchError:
                _capture_proxy_attempt_evidence(adapter, raw_html_dir, run_id, row["asin"])
                _record_failure(conn, "US", row["asin"], "review_fetch_error", "review_fetch_error")
                if refresh_job_id and row["asin"] == refresh_asin:
                    _finish_refresh_request(conn, refresh_job_id, "failed")
                actions += 1
                continue
            reason = classify_block(response_status, body)
            records, next_url = parse_reviews_html(body, page, url) if not reason else ([], None)
            alternate_url = _alternate_review_url(url, row["asin"])
            if not reason and not records and int(row["reported_review_count"] or 0) > 0 and alternate_url:
                try:
                    alternate_body, alternate_status = adapter.fetch(alternate_url)
                    _capture_proxy_attempt_evidence(adapter, raw_html_dir, run_id, row["asin"])
                except AdapterFetchError:
                    _capture_proxy_attempt_evidence(adapter, raw_html_dir, run_id, row["asin"])
                    pass
                else:
                    alternate_reason = classify_block(alternate_status, alternate_body)
                    alternate_records, alternate_next_url = parse_reviews_html(alternate_body, page, alternate_url) if not alternate_reason else ([], None)
                    if alternate_reason:
                        body, response_status, url, records, next_url, reason = alternate_body, alternate_status, alternate_url, [], None, alternate_reason
                    elif alternate_records or alternate_next_url:
                        body, response_status, url, records, next_url = alternate_body, alternate_status, alternate_url, alternate_records, alternate_next_url
                    else:
                        # Keep the empty fallback page as evidence before the
                        # primary portal result is recorded below.
                        _insert_evidence(conn, run_id, row["asin"], alternate_url, alternate_status, alternate_body, None, "empty_review_page", getattr(adapter, "source_type", "http_html"), raw_html_dir, context=_evidence_context(config.get("context"), adapter), transfer_bytes=getattr(adapter, "last_transfer_bytes", None))
            if (
                not reason
                and not records
                and int(row["reported_review_count"] or 0) > 0
            ):
                try:
                    browser_result = _fetch_browser_once(
                        adapter, url, fallback_reason=FallbackReason.REVIEW_EMPTY,
                        run_id=run_id, asin=row["asin"], ledger=fallback_ledger,
                    )
                except AdapterFetchError:
                    pass
                else:
                    if browser_result is None:
                        browser_body = browser_status = None
                    else:
                        browser_body, browser_status = browser_result
                    if browser_result is None:
                        browser_reason = None
                        browser_records, browser_next_url = [], None
                    else:
                        browser_reason = classify_block(browser_status, browser_body)
                        browser_records, browser_next_url = parse_reviews_html(browser_body, page, url) if not browser_reason else ([], None)
                    if browser_reason:
                        body, response_status, records, next_url, reason = browser_body, browser_status, [], None, browser_reason
                    elif browser_records or browser_next_url:
                        body, response_status, records, next_url = browser_body, browser_status, browser_records, browser_next_url
                        reason = browser_reason
            source_type = getattr(adapter, "source_type", "selenium_dom")
            retry_after = getattr(adapter, "last_retry_after_seconds", None)
            transfer_bytes = getattr(adapter, "last_transfer_bytes", None)
            cooldown = retry_after if retry_after is not None else config.get("rate_limit_cooldown_seconds")
            if not reason and source_type == "selenium_dom" and hasattr(adapter, "commit_browser_context"):
                _commit_browser_context_safely(adapter, run_id)
            _write_review_action(conn, run_id, row, page, url, records, next_url, body, response_status, reason, int(config["review_page_limit"]), source_type, raw_html_dir, _evidence_context(config.get("context"), adapter), cooldown, transfer_bytes)
            if refresh_job_id and row["asin"] == refresh_asin:
                _finish_refresh_request(conn, refresh_job_id, "queued" if reason == "http_429" else "failed" if reason else "completed")
            blocked = blocked or bool(reason)
            actions += 1
            if blocked and _should_stop_after_block(adapter, config, response_status, reason):
                _note_proxy_unrequested(adapter, len(selected) - actions)
                break
            continue
        try:
            body, response_status = adapter.fetch(row["url"])
            _capture_proxy_attempt_evidence(adapter, raw_html_dir, run_id, row["asin"])
        except AdapterFetchError as exc:
            _capture_proxy_attempt_evidence(adapter, raw_html_dir, run_id, row["asin"])
            # A truncated/timeout HTTP response can still be recoverable by
            # the browser layer when a delivery context is configured. Keep
            # the normal HTTP-first route, but do not discard the fallback.
            postal_code = str((config.get("context") or {}).get("postal_code") or "").strip()
            if not postal_code or not hasattr(adapter, "fetch_browser"):
                _write_fetch_failure_action(conn, run_id, row, str(exc), adapter, config.get("context"))
                if refresh_job_id and row["asin"] == refresh_asin:
                    _finish_refresh_request(conn, refresh_job_id, "failed")
                actions += 1
                continue
            try:
                browser_result = _fetch_browser_once(
                    adapter, row["url"], fallback_reason=FallbackReason.HTTP_TRANSPORT_ERROR,
                    run_id=run_id, asin=row["asin"], ledger=fallback_ledger,
                )
                if browser_result is None:
                    raise AdapterFetchError("duplicate browser fallback suppressed")
                body, response_status = browser_result
            except AdapterFetchError:
                _write_fetch_failure_action(conn, run_id, row, str(exc), adapter, config.get("context"))
                if refresh_job_id and row["asin"] == refresh_asin:
                    _finish_refresh_request(conn, refresh_job_id, "failed")
                actions += 1
                continue
        reason = classify_block(response_status, body)
        data = parse_product_html(body, row["url"]) if not reason else {"asin": "", "canonical_url": ""}
        core_reason = _core_fallback_reason(data, row["asin"]) if not reason else None
        if core_reason is not None:
            try:
                browser_result = _fetch_browser_once(
                    adapter, row["url"], fallback_reason=core_reason,
                    run_id=run_id, asin=row["asin"], ledger=fallback_ledger,
                )
            except AdapterFetchError:
                pass
            else:
                browser_body, browser_status = browser_result if browser_result is not None else (None, None)
                browser_reason = classify_block(browser_status, browser_body or "") if browser_result is not None else None
                if browser_reason:
                    body, response_status, data, reason = browser_body, browser_status, {"asin": "", "canonical_url": ""}, browser_reason
                elif browser_result is not None:
                    body, response_status, data, reason = browser_body, browser_status, parse_product_html(browser_body, row["url"]), browser_reason
        if not reason and _has_explicit_asin_mismatch(data, row["asin"]):
            source_type = getattr(adapter, "source_type", "http_html")
            transfer_bytes = getattr(adapter, "last_transfer_bytes", None)
            _write_product_action(
                conn, run_id, row, data, body, response_status, None,
                source_type=source_type, raw_html_dir=raw_html_dir,
                context=_identity_mismatch_evidence_context(
                    config.get("context"), adapter, data, row["asin"]
                ),
                transfer_bytes=transfer_bytes,
            )
            if refresh_job_id and row["asin"] == refresh_asin:
                _finish_refresh_request(
                    conn, refresh_job_id,
                    "completed" if _is_sibling_variant_redirect(data, row["asin"]) else "failed",
                )
            actions += 1
            continue
        missing_core = [key for key in ("asin", "canonical_url", "title") if not str(data.get(key) or "").strip()]
        if not reason and _is_terminal_missing_core_failure(response_status, missing_core):
            error = "missing_core_fields:" + ",".join(missing_core)
            source_type = getattr(adapter, "source_type", "http_html")
            _write_product_action(
                conn,
                run_id,
                row,
                data,
                body,
                response_status,
                None,
                error_code=error,
                source_type=source_type,
                raw_html_dir=raw_html_dir,
                context=_product_evidence_context(config.get("context"), adapter, data, row["asin"]),
                transfer_bytes=getattr(adapter, "last_transfer_bytes", None),
            )
            if refresh_job_id and row["asin"] == refresh_asin:
                _finish_refresh_request(conn, refresh_job_id, "failed")
            actions += 1
            continue
        context_errors, context_quality = (
            _assess_product_context(data, config.get("context"), adapter) if not reason else ([], {})
        )
        if not reason and (context_errors or not context_quality.get("postal_confirmed", True)):
            try:
                browser_result = _fetch_browser_once(
                    adapter, row["url"], fallback_reason=FallbackReason.CONTEXT_MISMATCH,
                    run_id=run_id, asin=row["asin"], ledger=fallback_ledger,
                )
            except AdapterFetchError:
                pass
            else:
                browser_body, browser_status = browser_result if browser_result is not None else (None, None)
                browser_reason = classify_block(browser_status, browser_body or "") if browser_result is not None else None
                browser_data = parse_product_html(browser_body, row["url"]) if browser_result is not None and not browser_reason else data
                browser_context_errors, browser_context_quality = (
                    _assess_product_context(browser_data, config.get("context"), adapter)
                    if browser_result is not None and not browser_reason else (context_errors, context_quality)
                )
                if browser_reason:
                    body, response_status, data, reason = browser_body, browser_status, {"asin": "", "canonical_url": ""}, browser_reason
                elif browser_result is not None and not browser_context_errors and browser_context_quality.get("postal_confirmed"):
                    body, response_status, data, reason = browser_body, browser_status, browser_data, browser_reason
        if not reason:
            context_errors, context_quality = _assess_product_context(data, config.get("context"), adapter)
        else:
            context_errors, context_quality = [], {}
        if context_errors:
            error_code = "context_mismatch:" + ",".join(context_errors)
        else:
            error_code = None
        source_type = getattr(adapter, "source_type", "selenium_dom")
        retry_after = getattr(adapter, "last_retry_after_seconds", None)
        transfer_bytes = getattr(adapter, "last_transfer_bytes", None)
        cooldown = retry_after if retry_after is not None else config.get("rate_limit_cooldown_seconds")
        if (
            not reason
            and not context_errors
            and context_quality.get("context_quality") == "full"
            and source_type == "selenium_dom"
            and _valid_product_identity(data, row["asin"])
            and hasattr(adapter, "commit_browser_context")
        ):
            _commit_browser_context_safely(adapter, run_id)
        _write_product_action(conn, run_id, row, data, body, response_status, reason, error_code=error_code, source_type=source_type, raw_html_dir=raw_html_dir, context=_product_evidence_context(config.get("context"), adapter, data, row["asin"]), cooldown_seconds=cooldown, transfer_bytes=transfer_bytes)
        if refresh_job_id and row["asin"] == refresh_asin:
            _finish_refresh_request(conn, refresh_job_id, "queued" if reason == "http_429" else "failed" if reason else "completed")
        blocked = blocked or bool(reason)
        actions += 1
        if blocked and _should_stop_after_block(adapter, config, response_status, reason):
            _note_proxy_unrequested(adapter, len(selected) - actions)
            break
    materialize_csvs(conn, Path(config["output_dir"]))
    return -1 if blocked else actions


def _postgres_evidence(
    run_id: str,
    task: dict[str, Any],
    body: str | None,
    status: int | None,
    source_type: str,
    raw_html_dir: Path | None,
    context: dict[str, Any] | None,
    transfer_bytes: int | None,
    *,
    block_reason: str | None = None,
    error_code: str | None = None,
) -> dict[str, Any]:
    raw_html_path = _persist_raw_html(raw_html_dir, run_id, task["asin"], body) if body is not None else None
    return {
        "run_id": run_id,
        "url": task["url"],
        "http_status": status,
        "transfer_bytes": transfer_bytes,
        "retrieved_at": utc_now(),
        "source_type": source_type,
        "content_hash": hashlib.sha256(body.encode()).hexdigest() if body is not None else None,
        "raw_html_path": raw_html_path,
        "block_reason": block_reason,
        "parser_version": PARSER_VERSION,
        "error_code": error_code,
        "context_json": context or {},
    }


def _run_postgres_actions_impl(
    storage: Any,
    adapter: Any,
    config: dict[str, Any],
    *,
    limit: int | None = None,
    run_id: str | None = None,
    worker_id: str = "amazon-us-worker",
    lease_seconds: int = 600,
    product_only: bool = False,
    reviews_only: bool = False,
    capacity_validator: Callable[[], dict[str, Any]] | None = None,
) -> int:
    """Run a bounded production batch using PostgreSQL task leases."""
    if product_only and reviews_only:
        raise ValueError("product_only and reviews_only are mutually exclusive")
    run_id = run_id or f"run-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}"
    max_actions = min(limit, int(config["max_actions_per_run"])) if limit else int(config["max_actions_per_run"])
    fallback_ledger = BrowserFallbackLedger()
    if hasattr(adapter, "begin_run"):
        adapter.begin_run(run_id, str(getattr(storage, "tenant_id", "postgres-local")), worker_id)
    raw_value = config.get("raw_html_dir")
    raw_html_dir = Path(raw_value) if raw_value else None
    actions = 0
    blocked = False
    while actions < max_actions:
        if capacity_validator is not None:
            capacity_validator()
        if _proxy_circuit_reason(adapter) or not _proxy_capacity_available(adapter):
            if not _proxy_circuit_reason(adapter):
                setattr(adapter, "circuit_open_reason", "session_pool_exhausted")
            _note_proxy_unrequested(adapter, max_actions - actions)
            break
        stage_filter = "product" if product_only else "reviews" if reviews_only else None
        task = storage.claim_refresh_task(worker_id, lease_seconds=lease_seconds) if stage_filter is None and hasattr(storage, "claim_refresh_task") else None
        if task is None:
            task = (
                storage.claim_task(worker_id, lease_seconds=lease_seconds, task_stage=stage_filter)
                if stage_filter is not None
                else storage.claim_task(worker_id, lease_seconds=lease_seconds)
            )
        if task is None:
            reason_reader = getattr(storage,"recovery_denial_reason",None)
            denial = reason_reader() if callable(reason_reader) else None
            if denial in {"recovery_global_pause","recovery_manual_pause","recovery_authorization_expired"}:
                adapter.circuit_open_reason = denial
                _note_proxy_unrequested(adapter,max_actions-actions)
            break
        if hasattr(adapter, "begin_action"):
            adapter.begin_action()
        if task.get("recovery"):
            hooks={
                "_recovery_before_request":lambda bound=task: storage.before_recovery_request(bound),
                "_recovery_browser_request":lambda bound=task: storage.before_recovery_browser_request(bound),
                "_recovery_relay_bytes":lambda count,bound=task: storage.consume_recovery_relay_bytes(bound,count),
                "_recovery_snapshot":lambda bound=task: storage.recovery_status(bound["asin"],bound["recovery"]["stage"]),
            }
            binder=getattr(adapter,"bind_recovery_hooks",None)
            if callable(binder): binder(hooks)
            else: adapter.config.update(hooks)
        if reviews_only and (
            task.get("task_stage") != "reviews" or not str(task.get("next_review_url") or "").strip()
        ):
            evidence = _postgres_evidence(
                run_id, task, None, None, getattr(adapter, "source_type", "http_html"),
                raw_html_dir, _evidence_context(config.get("context"), adapter),
                getattr(adapter, "last_transfer_bytes", None), error_code="invalid_review_task",
            )
            storage.save_failure(
                task=task,
                reason="invalid_review_task",
                error="reviews-only claim requires task_stage=reviews and next_review_url",
                evidence=evidence,
            )
            actions += 1
            continue
        refresh_job_id = task.get("job_id")
        if task.get("task_stage") == "reviews" and task.get("next_review_url"):
            page = int(task.get("next_review_page") or 1)
            url = task["next_review_url"]
            task = dict(task)
            task["url"] = url
            try:
                body, response_status = adapter.fetch(url)
                _capture_proxy_attempt_evidence(adapter, raw_html_dir, run_id, task["asin"])
            except AdapterFetchError:
                _capture_proxy_attempt_evidence(adapter, raw_html_dir, run_id, task["asin"])
                evidence = _postgres_evidence(
                    run_id, task, None, None, getattr(adapter, "source_type", "http_html"),
                    raw_html_dir, _evidence_context(config.get("context"), adapter), getattr(adapter, "last_transfer_bytes", None),
                    error_code="review_fetch_error",
                )
                storage.save_failure(task=task, reason="review_fetch_error", error="review_fetch_error", evidence=evidence)
                if refresh_job_id:
                    storage.finish_refresh_request(refresh_job_id, "failed")
                actions += 1
                continue
            reason = classify_block(response_status, body)
            records, next_url = parse_reviews_html(body, page, url) if not reason else ([], None)
            alternate_url = _alternate_review_url(url, task["asin"])
            if not reason and not records and int(task.get("reported_review_count") or 0) > 0 and alternate_url:
                try:
                    alternate_body, alternate_status = adapter.fetch(alternate_url)
                    _capture_proxy_attempt_evidence(adapter, raw_html_dir, run_id, task["asin"])
                except AdapterFetchError:
                    _capture_proxy_attempt_evidence(adapter, raw_html_dir, run_id, task["asin"])
                    pass
                else:
                    alternate_reason = classify_block(alternate_status, alternate_body)
                    alternate_records, alternate_next_url = parse_reviews_html(alternate_body, page, alternate_url) if not alternate_reason else ([], None)
                    if alternate_reason:
                        body, response_status, url, records, next_url, reason = alternate_body, alternate_status, alternate_url, [], None, alternate_reason
                    elif alternate_records or alternate_next_url:
                        body, response_status, url, records, next_url = alternate_body, alternate_status, alternate_url, alternate_records, alternate_next_url
            if not reason and not records and int(task.get("reported_review_count") or 0) > 0:
                try:
                    browser_result = _fetch_browser_once(
                        adapter, url, fallback_reason=FallbackReason.REVIEW_EMPTY,
                        run_id=run_id, asin=task["asin"], ledger=fallback_ledger,
                    )
                except AdapterFetchError:
                    pass
                else:
                    browser_body, browser_status = browser_result if browser_result is not None else (None, None)
                    browser_reason = classify_block(browser_status, browser_body or "") if browser_result is not None else None
                    browser_records, browser_next_url = parse_reviews_html(browser_body, page, url) if browser_result is not None and not browser_reason else ([], None)
                    if browser_reason:
                        body, response_status, records, next_url, reason = browser_body, browser_status, [], None, browser_reason
                    elif browser_records or browser_next_url:
                        body, response_status, records, next_url = browser_body, browser_status, browser_records, browser_next_url
            task["url"] = url
            source_type = getattr(adapter, "source_type", "http_html")
            if not reason and source_type == "selenium_dom" and hasattr(adapter, "commit_browser_context"):
                _commit_browser_context_safely(adapter, run_id)
            evidence = _postgres_evidence(
                run_id, task, body, response_status, source_type, raw_html_dir, _evidence_context(config.get("context"), adapter),
                getattr(adapter, "last_transfer_bytes", None), block_reason=reason,
                error_code="empty_review_page" if not reason and not records and int(task.get("reported_review_count") or 0) > 0 else None,
            )
            if reason:
                cooldown = getattr(adapter, "last_retry_after_seconds", None) or config.get("rate_limit_cooldown_seconds", 3600)
                deferred = response_status == 429 or reason == "too_many_requests"
                next_retry = (datetime.now(timezone.utc) + timedelta(seconds=int(cooldown))).replace(microsecond=0).isoformat() if deferred else None
                storage.save_failure(
                    task=task, reason=reason, error=reason, evidence=evidence,
                    next_status="reviews_pending" if deferred else "blocked",
                    state_fields={"task_stage": "reviews", "resume_status": "reviews_pending", "next_review_url": url, "next_review_page": page, "next_retry_at": next_retry, "block_reason": reason},
                    increment_attempts=False,
                )
                if refresh_job_id:
                    storage.finish_refresh_request(refresh_job_id, "queued" if deferred else "failed")
                blocked = True
            else:
                empty = not records and int(task.get("reported_review_count") or 0) > 0
                values = []
                for record in records:
                    item = dict(record)
                    item["unique_key"] = f"US|{task['asin']}|{item['review_id']}"
                    item["review_images"] = item.pop("review_images", item.pop("review_images_json", []))
                    values.append(item)
                fetched = int(task.get("fetched_review_count") or 0) + len(values)
                next_page = page + 1 if next_url else None
                page_limit = int(config.get("review_page_limit") or 0)
                next_status = "failed" if empty else "reviews_pending" if next_url else "succeeded"
                summary_status = "failed" if empty else "page_limit" if next_url and page_limit and page >= page_limit else "in_progress" if next_url else "exhausted"
                storage.save_review_result(
                    task=task,
                    evidence=evidence,
                    page={"page": page, "url": url, "status": "failed" if empty else "fetched", "next_url": next_url or (url if empty else None)},
                    records=values,
                    summary={
                        "reported_rating_count": task.get("reported_rating_count"),
                        "reported_review_count": task.get("reported_review_count"),
                        "reported_count_source": task.get("reported_count_source"),
                        "fetched_count": fetched,
                        "pages_fetched": page,
                        "next_page": next_url or (url if empty else None),
                        "status": summary_status,
                    },
                    next_status=next_status,
                    state_fields={
                        "task_stage": "reviews" if next_url or empty else "complete",
                        "resume_status": "reviews_pending" if next_url or empty else None,
                        "next_review_url": next_url or (url if empty else None),
                        "next_review_page": next_page or (page if empty else None),
                        "fetched_review_count": fetched,
                        "review_pages_fetched": page,
                        "last_error": "empty_review_page" if empty else None,
                    },
                    reason="empty_review_page" if empty else "review_page_fetched" if next_url else "reviews_exhausted",
                    increment_attempts=empty,
                )
                if refresh_job_id:
                    storage.finish_refresh_request(refresh_job_id, "failed" if empty else "completed")
            actions += 1
            if blocked and _should_stop_after_block(adapter, config, response_status, reason):
                _note_proxy_unrequested(adapter, max_actions - actions)
                break
            continue
        try:
            body, response_status = adapter.fetch(task["url"])
            _capture_proxy_attempt_evidence(adapter, raw_html_dir, run_id, task["asin"])
        except AdapterFetchError as exc:
            _capture_proxy_attempt_evidence(adapter, raw_html_dir, run_id, task["asin"])
            browser_result = None
            transport_recovered = False
            rotate_transport = getattr(adapter, "rotate_after_transport_failure", None)
            if callable(rotate_transport) and rotate_transport(task["url"]):
                try:
                    body, response_status = adapter.fetch(task["url"])
                    _capture_proxy_attempt_evidence(adapter, raw_html_dir, run_id, task["asin"])
                    transport_recovered = True
                except AdapterFetchError as retry_exc:
                    exc = retry_exc
                    _capture_proxy_attempt_evidence(adapter, raw_html_dir, run_id, task["asin"])
            elif not callable(rotate_transport):
                postal_code = str((config.get("context") or {}).get("postal_code") or "").strip()
                if postal_code:
                    try:
                        browser_result = _fetch_browser_once(
                            adapter, task["url"], fallback_reason=FallbackReason.HTTP_TRANSPORT_ERROR,
                            run_id=run_id, asin=task["asin"], ledger=fallback_ledger,
                        )
                    except AdapterFetchError:
                        browser_result = None
            if not transport_recovered and browser_result is None:
                _record_proxy_outcome(adapter, "failed", task["asin"])
                failure_transfer_bytes = (
                    None
                    if bool(getattr(adapter, "browser_attempted", False))
                    else getattr(adapter, "last_transfer_bytes", None)
                )
                evidence = _postgres_evidence(
                    run_id, task, None, None, getattr(adapter, "source_type", "http_html"),
                    raw_html_dir, _evidence_context(config.get("context"), adapter), failure_transfer_bytes,
                    error_code="fetch_error",
                )
                failure_error = "fetch_error" if callable(rotate_transport) else str(exc)
                storage.save_failure(task=task, reason="fetch_error", error=failure_error, evidence=evidence)
                if refresh_job_id:
                    storage.finish_refresh_request(refresh_job_id, "failed")
                actions += 1
                continue
            if browser_result is not None:
                body, response_status = browser_result
        reason = classify_block(response_status, body)
        if (
            reason
            and response_status != 429 and reason != "too_many_requests"
            and bool(config.get("proxy_firefox_verify_on_access_block", True))
            and callable(getattr(adapter, "evidence_context", None))
            and _browser_fallback_available(adapter)
        ):
            first_browser_failed = False
            try:
                browser_result = _fetch_browser_once(
                    adapter, task["url"], fallback_reason=FallbackReason.ACCESS_CONTROL_VERIFICATION,
                    run_id=run_id, asin=task["asin"], ledger=fallback_ledger,
                )
            except AdapterFetchError:
                browser_verification = getattr(adapter, "record_browser_verification", None)
                if callable(browser_verification):
                    browser_verification(False)
                first_browser_failed = True
                browser_result = None
            if browser_result is not None:
                browser_body, browser_status = browser_result
                browser_reason = classify_block(browser_status, browser_body)
                if browser_reason:
                    preserve = getattr(adapter, "preserve_browser_attempt", None)
                    if callable(preserve):
                        preserve(
                            task["url"], browser_body, browser_status, browser_reason,
                            stage_code="browser_capture",
                        )
                browser_verification = getattr(adapter, "record_browser_verification", None)
                if callable(browser_verification):
                    browser_verification(browser_reason is None)
                body, response_status, reason = browser_body, browser_status, browser_reason
                first_browser_failed = browser_reason is not None
                if first_browser_failed:
                    _capture_proxy_attempt_evidence(adapter, raw_html_dir, run_id, task["asin"])
            rotate = getattr(adapter, "rotate_after_browser_failure", None)
            if reason and response_status != 429 and reason != "too_many_requests" and first_browser_failed and callable(rotate) and rotate(task["url"]):
                try:
                    browser_result = _fetch_browser_once(
                        adapter, task["url"], fallback_reason=FallbackReason.ACCESS_CONTROL_RETRY,
                        run_id=run_id, asin=task["asin"], ledger=fallback_ledger, max_attempts=2,
                    )
                except AdapterFetchError:
                    browser_verification = getattr(adapter, "record_browser_verification", None)
                    if callable(browser_verification):
                        browser_verification(False)
                    browser_result = None
                if browser_result is not None:
                    browser_body, browser_status = browser_result
                    browser_reason = classify_block(browser_status, browser_body)
                    if browser_reason:
                        preserve = getattr(adapter, "preserve_browser_attempt", None)
                        if callable(preserve):
                            preserve(
                                task["url"], browser_body, browser_status, browser_reason,
                                stage_code="browser_capture",
                            )
                    browser_verification = getattr(adapter, "record_browser_verification", None)
                    if callable(browser_verification):
                        browser_verification(browser_reason is None)
                    body, response_status, reason = browser_body, browser_status, browser_reason
                    if browser_reason:
                        _capture_proxy_attempt_evidence(adapter, raw_html_dir, run_id, task["asin"])
        data = parse_product_html(body, task["url"]) if not reason else {"asin": "", "canonical_url": ""}
        core_reason = _core_fallback_reason(data, task["asin"]) if not reason else None
        if core_reason is not None:
            try:
                browser_result = _fetch_browser_once(
                    adapter, task["url"], fallback_reason=core_reason,
                    run_id=run_id, asin=task["asin"], ledger=fallback_ledger,
                )
            except AdapterFetchError:
                pass
            else:
                browser_body, browser_status = browser_result if browser_result is not None else (None, None)
                browser_reason = classify_block(browser_status, browser_body or "") if browser_result is not None else None
                if browser_result is not None:
                    body, response_status, reason = browser_body, browser_status, browser_reason
                    data = parse_product_html(browser_body, task["url"]) if not browser_reason else {"asin": "", "canonical_url": ""}
        if not reason and _has_explicit_asin_mismatch(data, task["asin"]):
            variant_redirect = _is_sibling_variant_redirect(data, task["asin"])
            _record_proxy_outcome(
                adapter,
                "variant_redirect" if variant_redirect else "failed",
                task["asin"],
            )
            source_type = getattr(adapter, "source_type", "http_html")
            transfer_bytes = getattr(adapter, "last_transfer_bytes", None)
            evidence = _postgres_evidence(
                run_id, task, body, response_status, source_type, raw_html_dir,
                _identity_mismatch_evidence_context(config.get("context"), adapter, data, task["asin"]), transfer_bytes,
                error_code="asin_mismatch",
            )
            if variant_redirect:
                storage.save_failure(
                    task=task, reason="variant_redirect", error=None, evidence=evidence,
                    next_status="succeeded",
                    state_fields={
                        "task_stage": "complete", "resume_status": None,
                        "next_review_url": None, "next_review_page": None,
                        "next_retry_at": None, "block_reason": None,
                    },
                    increment_attempts=False,
                )
            else:
                storage.save_failure(
                    task=task, reason="asin_mismatch", error="asin_mismatch", evidence=evidence, terminal=True
                )
            if refresh_job_id:
                storage.finish_refresh_request(refresh_job_id, "completed" if variant_redirect else "failed")
            actions += 1
            continue
        missing_core = [key for key in ("asin", "canonical_url", "title") if not str(data.get(key) or "").strip()]
        if not reason and _is_terminal_missing_core_failure(response_status, missing_core):
            _record_proxy_outcome(adapter, "failed", task["asin"])
            error = "missing_core_fields:" + ",".join(missing_core)
            source_type = getattr(adapter, "source_type", "http_html")
            evidence = _postgres_evidence(
                run_id,
                task,
                body,
                response_status,
                source_type,
                raw_html_dir,
                _product_evidence_context(config.get("context"), adapter, data, task["asin"]),
                getattr(adapter, "last_transfer_bytes", None),
                error_code=error,
            )
            storage.save_failure(
                task=task,
                reason="missing_core_fields",
                error=error,
                evidence=evidence,
                terminal=True,
            )
            if refresh_job_id:
                storage.finish_refresh_request(refresh_job_id, "failed")
            actions += 1
            continue
        context_errors, context_quality = (
            _assess_product_context(data, config.get("context"), adapter) if not reason else ([], {})
        )
        if not reason and (context_errors or not context_quality.get("postal_confirmed", True)):
            try:
                browser_result = _fetch_browser_once(
                    adapter, task["url"], fallback_reason=FallbackReason.CONTEXT_MISMATCH,
                    run_id=run_id, asin=task["asin"], ledger=fallback_ledger,
                )
            except AdapterFetchError:
                pass
            else:
                browser_body, browser_status = browser_result if browser_result is not None else (None, None)
                browser_reason = classify_block(browser_status, browser_body or "") if browser_result is not None else None
                browser_data = parse_product_html(browser_body, task["url"]) if browser_result is not None and not browser_reason else data
                browser_context_errors, browser_context_quality = (
                    _assess_product_context(browser_data, config.get("context"), adapter)
                    if browser_result is not None and not browser_reason else (context_errors, context_quality)
                )
                if browser_reason:
                    body, response_status, data, reason = browser_body, browser_status, {"asin": "", "canonical_url": ""}, browser_reason
                elif browser_result is not None and not browser_context_errors and browser_context_quality.get("postal_confirmed"):
                    body, response_status, data, reason = browser_body, browser_status, browser_data, browser_reason
        if not reason:
            context_errors, context_quality = _assess_product_context(data, config.get("context"), adapter)
        else:
            context_errors, context_quality = [], {}
        source_type = getattr(adapter, "source_type", "http_html")
        transfer_bytes = getattr(adapter, "last_transfer_bytes", None)
        error_code = "context_mismatch:" + ",".join(context_errors) if context_errors else None
        pending_missing = [key for key in ("asin", "canonical_url", "title") if not str(data.get(key) or "").strip()]
        if reason:
            _record_proxy_outcome(adapter, "blocked", task["asin"])
        elif context_errors or pending_missing:
            _record_proxy_outcome(adapter, "failed", task["asin"])
        elif not _valid_asin_identity(data, task["asin"]):
            _record_proxy_outcome(
                adapter,
                "variant_redirect" if _is_sibling_variant_redirect(data, task["asin"]) else "failed",
                task["asin"],
            )
        else:
            _record_proxy_outcome(adapter, "completed", task["asin"])
        if reason and _proxy_circuit_reason(adapter):
            _note_proxy_unrequested(adapter, max_actions - actions - 1)
        evidence = _postgres_evidence(
            run_id, task, body, response_status, source_type, raw_html_dir,
            _product_evidence_context(config.get("context"), adapter, data, task["asin"]), transfer_bytes,
            block_reason=reason, error_code=error_code,
        )
        if reason or context_errors:
            deferred = response_status == 429 or reason == "too_many_requests"
            cooldown = getattr(adapter, "last_retry_after_seconds", None) or config.get("rate_limit_cooldown_seconds", 3600)
            next_retry = (datetime.now(timezone.utc) + timedelta(seconds=int(cooldown))).replace(microsecond=0).isoformat() if deferred else None
            storage.save_failure(
                task=task, reason=reason or "context_mismatch", error=reason or error_code or "context_mismatch", evidence=evidence,
                next_status="pending" if deferred else "blocked" if reason else "failed",
                state_fields={"task_stage": "product", "resume_status": "pending", "next_retry_at": next_retry, "block_reason": reason},
                increment_attempts=not bool(reason),
            )
            if refresh_job_id:
                storage.finish_refresh_request(refresh_job_id, "queued" if deferred else "failed")
            blocked = bool(reason)
            actions += 1
            if blocked and _should_stop_after_block(adapter, config, response_status, reason):
                _note_proxy_unrequested(adapter, max_actions - actions)
                break
            continue
        missing_core = [key for key in ("asin", "canonical_url", "title") if not str(data.get(key) or "").strip()]
        if missing_core:
            error = "missing_core_fields:" + ",".join(missing_core)
            evidence["error_code"] = error
            storage.save_failure(
                task=task,
                reason="missing_core_fields",
                error=error,
                evidence=evidence,
                terminal=_is_terminal_missing_core_failure(response_status, missing_core),
            )
            if refresh_job_id:
                storage.finish_refresh_request(refresh_job_id, "failed")
            actions += 1
            continue
        if not _valid_asin_identity(data, task["asin"]):
            evidence["context_json"] = _identity_mismatch_evidence_context(
                config.get("context"), adapter, data, task["asin"]
            )
            evidence["error_code"] = "asin_mismatch"
            storage.save_failure(
                task=task, reason="asin_mismatch", error="asin_mismatch", evidence=evidence, terminal=True
            )
            if refresh_job_id:
                storage.finish_refresh_request(refresh_job_id, "failed")
            actions += 1
            continue
        if (
            context_quality.get("context_quality") == "full"
            and source_type == "selenium_dom"
            and hasattr(adapter, "commit_browser_context")
        ):
            _commit_browser_context_safely(adapter, run_id)
            evidence["context_json"] = _product_evidence_context(
                config.get("context"), adapter, data, task["asin"]
            )
        review_url = data.get("review_link") or None
        media = []
        for item in data.get("media", []):
            value = dict(item)
            value["unique_key"] = f"US|{task['asin']}|{value.get('placement','')}|{value.get('entry_type','')}|{value.get('asset_url','')}"
            media.append(value)
        content = []
        for item in data.get("content_modules", []):
            value = dict(item)
            value["unique_key"] = f"US|{task['asin']}|{value.get('module_type','')}|{value.get('position',0)}"
            content.append(value)
        storage.save_product_result(
            task=task,
            evidence=evidence,
            product={
                "canonical_url": data.get("canonical_url"), "availability": data.get("availability"),
                "title": data.get("title"), "brand": data.get("brand"), "rating": data.get("rating"),
                "reported_rating_count": data.get("reported_rating_count"), "reported_review_count": data.get("reported_review_count"),
                "review_count": data.get("review_count"), "review_count_source": data.get("review_count_source"),
                "price": data.get("price"), "bullets": data.get("bullets", []),
                "product_description": data.get("product_description"), "specs": data.get("specs", {}),
                "buy_box": data.get("buy_box", {}), "top_reviews": data.get("top_reviews", []),
                "review_link": data.get("review_link"), "review_section_anchor": data.get("review_section_anchor"),
                "aplus_present": bool(data.get("aplus_present")), "collected_at": utc_now(), "status": "product_done",
            },
            media=media,
            content_modules=content,
            review_summary={
                "reported_rating_count": data.get("reported_rating_count"),
                "reported_review_count": data.get("reported_review_count"),
                "reported_count_source": data.get("review_count_source"),
                "fetched_count": 0, "pages_fetched": 0, "next_page": review_url,
                "status": "in_progress" if review_url else "section_only" if data.get("review_section_anchor") else "not_available",
            },
            next_status="reviews_pending" if review_url else "succeeded",
            state_fields={
                "task_stage": "reviews" if review_url else "complete", "resume_status": "reviews_pending" if review_url else None,
                "next_review_url": review_url, "next_review_page": 1 if review_url else None,
                "reported_rating_count": data.get("reported_rating_count"),
                "reported_review_count": data.get("reported_review_count"),
                "reported_count_source": data.get("review_count_source"),
            },
            reason="reviews_required" if review_url else "no_paginated_review_link",
        )
        if refresh_job_id:
            storage.finish_refresh_request(refresh_job_id, "completed")
        actions += 1
    pooled = callable(getattr(adapter, "evidence_context", None))
    return -1 if _proxy_circuit_reason(adapter) or (blocked and not pooled) else actions


def run_postgres_actions(storage: Any, adapter: Any, config: dict[str, Any], *,
                         limit: int | None = None, run_id: str | None = None,
                         worker_id: str = "amazon-us-worker", lease_seconds: int = 600,
                         product_only: bool = False, reviews_only: bool = False,
                         capacity_reservation_id: str | None = None) -> int:
    from proxy_capacity_gate import capacity_batch_actions
    target = min(limit, int(config["max_actions_per_run"])) if limit else int(config["max_actions_per_run"])
    run_id = run_id or f"run-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}"
    begin_recovery = getattr(storage,"begin_recovery_run",None)
    if callable(begin_recovery):
        begin_recovery()
    done = 0
    while done < target:
        fact_reader = getattr(storage,"load_latest_proxy_capacity",None)
        try:
            fact = fact_reader(max_age_seconds=int(config.get('proxy_canary_max_age_seconds') or 3600)) if callable(fact_reader) else None
        except Exception:
            raise ProxyCapacityGateDenied('capacity_evidence_unavailable') from None
        size = capacity_batch_actions(config, target-done, fact)
        result = _run_reserved_postgres_actions(
            storage,adapter,config,limit=size,run_id=run_id,worker_id=worker_id,
            lease_seconds=lease_seconds,product_only=product_only,reviews_only=reviews_only,
            capacity_reservation_id=capacity_reservation_id if done == 0 else None,
        )
        if result < 0:
            return result
        done += result
        if result < size:
            break
    return done


def _run_reserved_postgres_actions(
    storage: Any,
    adapter: Any,
    config: dict[str, Any],
    *,
    limit: int | None = None,
    run_id: str | None = None,
    worker_id: str = "amazon-us-worker",
    lease_seconds: int = 600,
    product_only: bool = False,
    reviews_only: bool = False,
    capacity_reservation_id: str | None = None,
) -> int:
    """Reserve shared proxy capacity, bind it to the run, and release it on every exit path."""
    configure_recovery = getattr(storage, "configure_recovery", None)
    if callable(configure_recovery):
        configure_recovery(config)
    max_actions = min(limit, int(config["max_actions_per_run"])) if limit else int(config["max_actions_per_run"])
    validate = getattr(storage, "validate_proxy_capacity_reservation", None)
    release = getattr(storage, "release_proxy_capacity", None)
    if not callable(validate) or not callable(release):
        raise ProxyCapacityGateDenied("capacity_reservation_unavailable")
    if capacity_reservation_id:
        try:
            reservation = validate(
                capacity_reservation_id,
                worker_id,
                max_age_seconds=int(config.get("proxy_canary_max_age_seconds") or 3600),
                lease_seconds=lease_seconds,
            )
        except Exception:
            raise ProxyCapacityGateDenied("capacity_reservation_unavailable") from None
        if not isinstance(reservation, dict) or reservation.get("status") != "active":
            reason = str(reservation.get("reason") if isinstance(reservation, dict) else "capacity_reservation_denied")
            raise ProxyCapacityGateDenied(reason, reservation if isinstance(reservation, dict) else None)
    else:
        reservation = acquire_capacity_reservation(
            storage,
            config,
            requested_actions=max_actions,
            owner_id=worker_id,
            lease_seconds=lease_seconds,
        )
    reservation_id = str(reservation["reservation_id"])
    bind_recovery = getattr(storage,"bind_recovery_authorization",None)
    if callable(bind_recovery):
        bind_recovery(reservation)
    expected_hash = capacity_config_hash(config)
    expected_generation = str(config.get("proxy_credential_generation") or "")
    expected_reserved_slots = reservation_slots_for(config, max_actions)
    if (
        reservation.get("capacity_config_hash") != expected_hash
        or reservation.get("credential_generation") != expected_generation
        or int(reservation.get("requested_capacity") or 0) < max_actions
        or int(reservation.get("reserved_slots") or 0) < expected_reserved_slots
    ):
        release(reservation_id, worker_id)
        raise ProxyCapacityGateDenied("capacity_reservation_scope_mismatch")

    def validate_reservation() -> dict[str, Any]:
        def release_claimed_leases(reason: str) -> None:
            cleanup = getattr(storage, "release_claimed_capacity_leases", None)
            if callable(cleanup):
                cleanup(worker_id, reason)

        try:
            current = validate(
                reservation_id,
                worker_id,
                max_age_seconds=int(config.get("proxy_canary_max_age_seconds") or 3600),
                lease_seconds=lease_seconds,
            )
        except Exception:
            try:
                release_claimed_leases("capacity_reservation_unavailable")
            except Exception:
                raise ProxyCapacityGateDenied(
                    "capacity_task_release_failed",
                    {
                        "status": "denied",
                        "reason": "capacity_task_release_failed",
                        "lease_cleanup_status": "ttl_fallback",
                    },
                ) from None
            raise ProxyCapacityGateDenied("capacity_reservation_unavailable") from None
        if not isinstance(current, dict) or current.get("status") != "active":
            reason = str(current.get("reason") if isinstance(current, dict) else "capacity_reservation_denied")
            try:
                release_claimed_leases(reason)
            except Exception:
                raise ProxyCapacityGateDenied("capacity_task_release_failed") from None
            raise ProxyCapacityGateDenied(reason, current if isinstance(current, dict) else None)
        return current

    configure_reservation = getattr(adapter, "configure_capacity_reservation", None)
    if not callable(configure_reservation):
        release(reservation_id, worker_id)
        raise ProxyCapacityGateDenied("capacity_adapter_unavailable")
    try:
        configure_reservation(list(reservation.get("slot_ids") or []), validate_reservation)
    except Exception:
        release(reservation_id, worker_id)
        raise ProxyCapacityGateDenied("capacity_adapter_configuration_failed") from None
    scoped_config = dict(config)
    scoped_context = dict(config.get("context") or {})
    scoped_context["capacity_authorization"] = dict(reservation)
    scoped_config["context"] = scoped_context
    try:
        return _run_postgres_actions_impl(
            storage, adapter, scoped_config, limit=limit, run_id=run_id, worker_id=worker_id,
            lease_seconds=lease_seconds, product_only=product_only, reviews_only=reviews_only,
            capacity_validator=validate_reservation,
        )
    except Exception as exc:
        from recovery_scheduler import RecoveryDenied
        if isinstance(exc, RecoveryDenied):
            task = storage.recovery_active_task()
            evidence = None
            if task:
                raw_root = Path(config["raw_html_dir"]) if config.get("raw_html_dir") else None
                _capture_proxy_attempt_evidence(adapter,raw_root,run_id,task['asin'])
                evidence = _postgres_evidence(run_id,task,None,None,getattr(adapter,'source_type','http_html'),
                    raw_root,_evidence_context(scoped_config.get('context'),adapter),getattr(adapter,'last_transfer_bytes',None),
                    error_code='recovery_budget_or_lease_denied')
            storage.abort_recovery(evidence)
            raise ProxyCapacityGateDenied("recovery_budget_or_lease_denied") from None
        raise
    finally:
        release_adapter = getattr(adapter, "release_capacity_reservation", None)
        if callable(release_adapter):
            release_adapter()
        release(reservation_id, worker_id)


def _prepare_paths(args: argparse.Namespace, config: dict[str, Any]) -> tuple[Path, Path, Path]:
    paths = config.get("paths", {})
    manifest = resolve_path(args.manifest or paths.get("manifest", DEFAULT_MANIFEST))
    state = resolve_path(args.state or paths.get("state", DEFAULT_DB))
    output = resolve_path(args.output_dir or paths.get("output_dir", DEFAULT_OUTPUT_DIR))
    config["output_dir"] = output
    raw_html_value = paths.get("raw_html_dir") or (output / "raw_html")
    config["raw_html_dir"] = resolve_path(raw_html_value)
    return manifest, state, output


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--state", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--init", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--materialize-only", action="store_true")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--visible", action="store_true")
    parser.add_argument("--once", action="store_true", help="运行一个批次后退出")
    parser.add_argument("--limit", type=int, help="覆盖本次 action 数上限")
    parser.add_argument("--backend", choices=("postgres", "sqlite"), default="postgres")
    parser.add_argument("--dsn-env", default="AMAZON_US_POSTGRES_DSN")
    parser.add_argument("--tenant-id", default="amazon_us_local")
    parser.add_argument("--subject-type", choices=("own", "competitor", "candidate"), default="own")
    parser.add_argument("--worker-id", default=f"amazon-us-worker-{os.getpid()}")
    parser.add_argument("--lease-seconds", type=int, default=600)
    stage_group = parser.add_mutually_exclusive_group()
    stage_group.add_argument("--product-only", action="store_true", help="claim only product-stage PostgreSQL tasks")
    stage_group.add_argument("--reviews-only", action="store_true", help="claim only review-stage PostgreSQL tasks")
    parser.add_argument("--run-id", help="explicit run identifier for logs and evidence")
    parser.add_argument("--capacity-reservation-id", help="controller-created proxy capacity reservation")
    return parser


def run(args: argparse.Namespace) -> int:
    if getattr(args, "live", False) and getattr(args, "backend", "postgres") != "postgres":
        raise ValueError("SQLite live collection is disabled; production collection requires PostgreSQL capacity gating")
    config = load_config(resolve_path(args.config))
    manifest, state, output = _prepare_paths(args, config)
    config["output_dir"] = output
    if getattr(args, "backend", "sqlite") == "postgres":
        dsn = os.environ.get(args.dsn_env, "").strip()
        if not dsn:
            raise ValueError(f"PostgreSQL DSN environment variable is required: {args.dsn_env}")
        try:
            from postgres_worker_storage import PostgresWorkerStorage
        except ModuleNotFoundError:
            sys.path.insert(0, str(ROOT / "scripts"))
            from postgres_worker_storage import PostgresWorkerStorage
        with manifest.open("r", encoding="utf-8-sig", newline="") as handle:
            manifest_rows = list(csv.DictReader(handle))
        storage = PostgresWorkerStorage(
            dsn, tenant_id=args.tenant_id, subject_type=args.subject_type,
            default_lease_seconds=args.lease_seconds,
        )
        initialized = storage.initialize_manifest(manifest_rows)
        storage.reclaim_expired_leases()
        if args.init or args.dry_run:
            print(f"phase=initialized; manifest={initialized} 条；PostgreSQL；不访问网络。")
            return 0
        if args.materialize_only:
            raise ValueError("PostgreSQL materialization is not implemented; use Collection API or PostgreSQL export")
        if not args.live:
            print("错误: 采集必须显式指定 --live；当前未发出网络请求。", file=sys.stderr)
            return 2
        if args.visible:
            config["headless"] = False
        adapter = _build_http_adapter(config)
        try:
            action_result = run_postgres_actions(
                storage, adapter, config, limit=args.limit, worker_id=args.worker_id,
                lease_seconds=args.lease_seconds, product_only=args.product_only,
                reviews_only=args.reviews_only, run_id=args.run_id,
                capacity_reservation_id=args.capacity_reservation_id,
            )
            return 3 if action_result == -1 else 0
        finally:
            adapter.close()
    conn = init_db(state, int(config["max_attempts"]))
    initialize_manifest(conn, manifest, config)
    recover_running(conn)
    if args.init or args.dry_run or args.materialize_only:
        materialize_csvs(conn, output)
        phase = "initialized" if args.init or args.dry_run else "materialized"
        print(f"phase={phase}; manifest={conn.execute('SELECT COUNT(*) FROM item_state').fetchone()[0]} 条；不访问网络。")
        conn.close()
        return 0
    if not args.live:
        conn.close()
        print("错误: 采集必须显式指定 --live；当前未发出网络请求。", file=sys.stderr)
        return 2
    if args.visible:
        config["headless"] = False
    adapter = _build_http_adapter(config)
    result = 0
    try:
        limit = args.limit
        action_result = run_actions(conn, adapter, config, limit=limit)
        result = 3 if action_result == -1 else 0
    finally:
        adapter.close()
        conn.close()
    return result


def validate_runtime_args(args: argparse.Namespace) -> None:
    if args.live and args.backend != "postgres":
        raise ValueError("SQLite live collection is disabled; production collection requires PostgreSQL capacity gating")
    if args.product_only and args.backend != "postgres":
        raise ValueError("--product-only is supported only with the PostgreSQL backend")
    if args.reviews_only and args.backend != "postgres":
        raise ValueError("--reviews-only is supported only with the PostgreSQL backend")
    if args.run_id and args.backend != "postgres":
        raise ValueError("--run-id is supported only with the PostgreSQL backend")
    if args.run_id and not re.fullmatch(r"[A-Za-z0-9_-]{1,120}", args.run_id):
        raise ValueError("--run-id must contain 1-120 letters, digits, underscores, or hyphens")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.limit is not None and args.limit < 1:
        print("错误: --limit 必须为正整数", file=sys.stderr)
        return 2
    if args.lease_seconds < 1:
        print("错误: --lease-seconds 必须为正整数", file=sys.stderr)
        return 2
    try:
        validate_runtime_args(args)
        return run(args)
    except (OSError, csv.Error, sqlite3.Error, ValueError, RuntimeError, KeyError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
