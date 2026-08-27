#!/usr/bin/env python3
"""Offline-first Amazon US collection worker.

The SQLite database is the source of truth. HTTP fetches run first and a
Firefox browser is created lazily only when required fields are missing. All
durable writes for an action are transactional.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import html as html_module
import http.client
import json
import os
import re
import sqlite3
import sys
import tempfile
import tomllib
import urllib.error
import urllib.request
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urljoin, urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parents[1]

try:
    from context_guard import validate_context
except ModuleNotFoundError:
    sys.path.insert(0, str(ROOT / "scripts"))
    from context_guard import validate_context

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
DEFAULTS: dict[str, Any] = {
    "request_timeout_seconds": 30,
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
    "proxy_url": "",
    "proxy_username_env": "",
    "proxy_password_env": "",
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
    "run_id", "asin", "marketplace", "url", "http_status", "retrieved_at", "source_type",
    "content_hash", "raw_html_path", "block_reason", "parser_version", "error_code",
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


def parse_product_html(source_html: str, page_url: str = "") -> dict[str, Any]:
    parser = _DOMParser()
    parser.feed(source_html)
    asin = _first_attr(parser, [{"tag": "input", "attr": ("id", "ASIN")}, {"tag": "input", "attr": ("name", "ASIN")}], "value")
    canonical = _first_attr(parser, [{"tag": "link", "attr": ("rel", "canonical")}], "href") or _meta(parser, "og:url") or page_url
    title = _first_text(parser, [{"id_value": "productTitle"}, {"tag": "h1", "class_name": "product-title"}])
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
    description = _first_text(parser, [{"id_value": "productDescription"}, {"id_value": "bookDescription_feature_div"}])
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
        "availability": _first_text(parser, [{"id_value": "availability"}, {"id_value": "outOfStock"}]),
        "title": title, "brand": _first_text(parser, [{"id_value": "bylineInfo"}, {"id_value": "brand"}]),
        "rating": reported_rating_text, "reported_ratings": _count_from_text(review_summary_text), "reported_rating_count": reported_rating_count,
        "reported_review_count": reported_review_count, "review_count": review_summary_text,
        "review_count_source": review_count_source, "price": _first_text(parser, [{"id_value": "corePrice_feature_div"}, {"id_value": "priceblock_ourprice"}, {"class_name": "a-price"}]),
        "bullets": _parse_bullets(parser), "product_description": description, "specs": specs,
        "buy_box": {"text": _first_text(parser, [{"id_value": "desktop_buybox"}, {"id_value": "buybox"}])},
        "top_reviews": [_node_text(parser, index) for index in _find(parser, attr=("data-hook", "review"))],
        "review_link": review_link, "review_section_anchor": review_section_anchor,
        "aplus_present": any(x["module_type"] == "aplus" for x in content_modules),
        "media": _parse_media(parser, page_url), "content_modules": content_modules,
        "block_reason": classify_block(text=body_text, title=_first_text(parser, [{"tag": "title"}])),
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
          asin TEXT NOT NULL, url TEXT NOT NULL, http_status INTEGER, retrieved_at TEXT NOT NULL,
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
    config["max_attempts"] = max(1, int(config["max_attempts"]))
    config["max_actions_per_run"] = max(1, int(config["max_actions_per_run"]))
    config["review_page_limit"] = max(0, int(config["review_page_limit"]))
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
    allowed = {"attempts", "max_attempts", "resume_status", "task_stage", "next_review_url", "next_review_page", "review_page_limit", "reported_rating_count", "reported_review_count", "reported_count_source", "fetched_review_count", "review_pages_fetched", "block_reason", "last_error"}
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


def _record_failure(conn: sqlite3.Connection, marketplace: str, asin: str, reason: str, error: str) -> None:
    row = conn.execute("SELECT attempts,max_attempts FROM item_state WHERE marketplace=? AND asin=?", (marketplace, asin)).fetchone()
    if row is None:
        raise KeyError(f"未知商品: {marketplace}+{asin}")
    _set_status(conn, marketplace, asin, "failed", reason=reason, attempts=int(row["attempts"]) + 1, last_error=error)


def _set_rate_limited(conn: sqlite3.Connection, marketplace: str, asin: str, stage: str) -> None:
    target = "reviews_pending" if stage == "reviews" else "pending"
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
    digest = hashlib.sha256(body.encode()).hexdigest()
    safe_run_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", run_id)[:80] or "run"
    safe_asin = re.sub(r"[^A-Za-z0-9_.-]+", "_", asin)[:20] or "asin"
    relative = Path("US") / safe_asin / f"{safe_run_id}-{digest[:16]}.html"
    path = raw_html_dir / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(body, encoding="utf-8")
    temporary.replace(path)
    return relative.as_posix()


def _insert_evidence(conn: sqlite3.Connection, run_id: str, asin: str, url: str, status: int | None, body: str, block_reason: str | None, error_code: str | None = None, source_type: str = "selenium_dom", raw_html_dir: Path | None = None, raw_html_path: str | None = None) -> str | None:
    if raw_html_path is None:
        raw_html_path = _persist_raw_html(raw_html_dir, run_id, asin, body)
    conn.execute("INSERT INTO collection_evidence(run_id,marketplace,asin,url,http_status,retrieved_at,source_type,content_hash,raw_html_path,block_reason,parser_version,error_code) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (run_id, "US", asin, url, status, utc_now(), source_type, hashlib.sha256(body.encode()).hexdigest(), raw_html_path, block_reason, PARSER_VERSION, error_code))
    return raw_html_path


def _write_product_action(conn: sqlite3.Connection, run_id: str, task: sqlite3.Row, data: dict[str, Any], body: str, status: int | None, block_reason: str | None, error_code: str | None = None, source_type: str = "selenium_dom", raw_html_dir: Path | None = None) -> None:
    asin = task["asin"]
    with conn:
        raw_html_path = _insert_evidence(conn, run_id, asin, task["url"], status, body, block_reason, error_code, source_type, raw_html_dir)
        if block_reason:
            if status == 429 or block_reason == "too_many_requests":
                _set_rate_limited(conn, "US", asin, "product")
            else:
                _set_status(conn, "US", asin, "blocked", reason=block_reason, block_reason=block_reason, last_error=block_reason)
            return
        if error_code and error_code.startswith("context_mismatch"):
            _record_failure(conn, "US", asin, "context_mismatch", error_code)
            return
        parsed_asin = (data.get("asin") or "").upper()
        canonical = data.get("canonical_url") or ""
        canonical_parts = urlsplit(canonical)
        canonical_host = (canonical_parts.hostname or "").lower().removeprefix("www.")
        path_match = re.search(r"/dp/([A-Za-z0-9]{10})(?:/|$)", canonical_parts.path)
        if (
            parsed_asin != asin
            or canonical_parts.scheme != "https"
            or canonical_host != "amazon.com"
            or not path_match
            or path_match.group(1).upper() != asin
        ):
            _insert_evidence(conn, run_id, asin, task["url"], status, body, None, "asin_mismatch", source_type, raw_html_dir, raw_html_path)
            _record_failure(conn, "US", asin, "asin_mismatch", "asin_mismatch")
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


def _write_review_action(conn: sqlite3.Connection, run_id: str, task: sqlite3.Row, page: int, url: str, records: list[dict[str, Any]], next_url: str | None, body: str, status: int | None, block_reason: str | None, page_limit: int, source_type: str = "selenium_dom", raw_html_dir: Path | None = None) -> None:
    asin = task["asin"]
    with conn:
        raw_html_path = _insert_evidence(conn, run_id, asin, url, status, body, block_reason, source_type=source_type, raw_html_dir=raw_html_dir)
        if block_reason:
            if status == 429 or block_reason == "too_many_requests":
                conn.execute("INSERT OR REPLACE INTO review_page_state VALUES(?,?,?,?,?,?,?)", ("US", asin, page, url, "deferred", url, utc_now()))
                conn.execute("INSERT OR REPLACE INTO review_summary VALUES(?,?,?,?,?,?,?,?,?,?)", ("US", asin, task["reported_rating_count"], task["reported_review_count"], task["reported_count_source"], task["fetched_review_count"], task["review_pages_fetched"], url, "rate_limited", utc_now()))
                _set_rate_limited(conn, "US", asin, "reviews")
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
            _insert_evidence(conn, run_id, asin, url, status, body, None, "empty_review_page", source_type, raw_html_dir, raw_html_path)
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


class SeleniumFirefoxAdapter:
    def __init__(self, config: dict[str, Any] | None = None, headless: bool | None = None, timeout: int | None = None) -> None:
        config = config or DEFAULTS
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

    def _ensure_delivery_context(self) -> bool:
        """Set the configured ZIP in this isolated browser session once."""
        postal_code = str((self.config.get("context") or {}).get("postal_code") or "").strip()
        if not postal_code:
            return False
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.support.ui import WebDriverWait

        try:
            current = self.driver.find_element(By.ID, "glow-ingress-line2").text or ""
        except Exception:
            current = ""
        if postal_code in current:
            self._context_initialized = True
            return False
        self.driver.find_element(By.ID, "nav-global-location-popover-link").click()
        field = WebDriverWait(self.driver, 10).until(EC.presence_of_element_located((By.ID, "GLUXZipUpdateInput")))
        field.clear()
        field.send_keys(postal_code)
        self.driver.find_element(By.CSS_SELECTOR, "#GLUXZipUpdate input[type='submit']").click()
        # Amazon may show a second confirmation modal after Apply. The visible
        # Done button is the commit point; GLUXConfirmClose is only a fallback
        # for older page variants where that button is rendered as an input.
        try:
            done = WebDriverWait(self.driver, 10).until(
                EC.element_to_be_clickable((By.XPATH, "//button[normalize-space()='Done' or normalize-space()='完成']"))
            )
            done.click()
        except Exception:
            try:
                self.driver.find_element(By.ID, "GLUXConfirmClose").click()
            except Exception:
                pass
        WebDriverWait(self.driver, 10).until(
            lambda driver: postal_code in (driver.find_element(By.ID, "glow-ingress-line2").text or "")
        )
        self._context_initialized = True
        return True

    def fetch(self, url: str) -> tuple[str, int | None]:
        try:
            self.driver.get(url)
            self._ensure_delivery_context()
        except Exception as exc:
            raise AdapterFetchError(str(exc)) from exc
        return self.driver.page_source, extract_response_status(self.driver)

    def close(self) -> None:
        try:
            self.driver.quit()
        finally:
            self._temp_profile.cleanup()


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
        proxy_url = str(self.config.get("proxy_url") or "").strip()
        if proxy_url:
            proxy_handler = urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
            username_env = str(self.config.get("proxy_username_env") or "").strip()
            password_env = str(self.config.get("proxy_password_env") or "").strip()
            if bool(username_env) != bool(password_env):
                raise ValueError("proxy_username_env and proxy_password_env must be configured together")
            if username_env:
                username = os.environ.get(username_env, "")
                password = os.environ.get(password_env, "")
                if not username or not password:
                    raise ValueError("proxy credential environment variables are not both populated")
                password_manager = urllib.request.HTTPPasswordMgrWithDefaultRealm()
                password_manager.add_password(None, proxy_url, username, password)
                auth_handler = urllib.request.ProxyBasicAuthHandler(password_manager)
                self.opener = urllib.request.build_opener(proxy_handler, auth_handler)
            else:
                self.opener = urllib.request.build_opener(proxy_handler)
        else:
            self.opener = urllib.request.build_opener()
        self.egress_id = str(self.config.get("egress_id") or "direct")
        self.limiter = EgressLimiter(
            float(self.config.get("global_requests_per_second", 0.0)),
            float(self.config.get("egress_requests_per_second", 0.0)),
            int(self.config.get("rate_burst", 1)),
        )
        self.browser: SeleniumFirefoxAdapter | None = None

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
        self.limiter.acquire(self.egress_id)
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Encoding": "identity",
                "User-Agent": self.user_agent,
            },
            method="GET",
        )
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                body = response.read()
                self.source_type = "http_html"
                return self._decode(response, body), int(response.getcode() or 200)
        except urllib.error.HTTPError as exc:
            body = exc.read()
            self.source_type = "http_html"
            return self._decode(exc, body), int(exc.code)
        except (urllib.error.URLError, http.client.IncompleteRead, ConnectionResetError, TimeoutError, OSError) as exc:
            raise AdapterFetchError(str(exc)) from exc

    def needs_browser_fallback(self, data: dict[str, Any]) -> bool:
        # A page without these anchors cannot be safely accepted as a product page.
        return not all(data.get(key) for key in ("asin", "canonical_url", "title"))

    def fetch_browser(self, url: str) -> tuple[str, int | None]:
        if self.browser is None:
            try:
                self.browser = SeleniumFirefoxAdapter(self.config)
            except (RuntimeError, OSError) as exc:
                raise AdapterFetchError(str(exc)) from exc
        self.source_type = "selenium_dom"
        return self.browser.fetch(url)

    def close(self) -> None:
        if self.browser is not None:
            self.browser.close()


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
        "collection_evidence": "SELECT run_id,asin,marketplace,url,http_status,retrieved_at,source_type,content_hash,raw_html_path,block_reason,parser_version,error_code FROM collection_evidence ORDER BY id",
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
        attempts=0, task_stage="product", resume_status=None, next_review_url=None, next_review_page=None,
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
    rows = list(conn.execute("SELECT * FROM item_state WHERE status IN ('pending','reviews_pending') OR (status='failed' AND attempts < max_attempts) ORDER BY asin LIMIT ?", (max_actions + len(exclude_asins or set()),)))
    return [row for row in rows if row["asin"] not in (exclude_asins or set())][:max_actions]


def run_actions(conn: sqlite3.Connection, adapter: Any, config: dict[str, Any], *, limit: int | None = None, run_id: str | None = None) -> int:
    run_id = run_id or f"run-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}"
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
        row = conn.execute("SELECT * FROM item_state WHERE marketplace='US' AND asin=?", (initial["asin"],)).fetchone()
        if row["status"] == "failed" and row["attempts"] >= row["max_attempts"]:
            continue
        _claim_action(conn, row)
        row = conn.execute("SELECT * FROM item_state WHERE marketplace='US' AND asin=?", (row["asin"],)).fetchone()
        if row["task_stage"] == "reviews" and row["next_review_url"]:
            page, url = int(row["next_review_page"] or 1), row["next_review_url"]
            try:
                body, response_status = adapter.fetch(url)
            except AdapterFetchError as exc:
                _record_failure(conn, "US", row["asin"], "review_fetch_error", str(exc))
                if refresh_job_id and row["asin"] == refresh_asin:
                    _finish_refresh_request(conn, refresh_job_id, "failed")
                actions += 1
                continue
            reason = classify_block(response_status, body)
            records, next_url = parse_reviews_html(body, page, url) if not reason else ([], None)
            if (
                not reason
                and not records
                and int(row["reported_review_count"] or 0) > 0
                and hasattr(adapter, "fetch_browser")
            ):
                try:
                    browser_body, browser_status = adapter.fetch_browser(url)
                except AdapterFetchError:
                    pass
                else:
                    browser_reason = classify_block(browser_status, browser_body)
                    if not browser_reason:
                        browser_records, browser_next_url = parse_reviews_html(browser_body, page, url)
                        if browser_records or browser_next_url:
                            body, response_status, records, next_url = browser_body, browser_status, browser_records, browser_next_url
                            reason = browser_reason
            source_type = getattr(adapter, "source_type", "selenium_dom")
            _write_review_action(conn, run_id, row, page, url, records, next_url, body, response_status, reason, int(config["review_page_limit"]), source_type, raw_html_dir)
            if refresh_job_id and row["asin"] == refresh_asin:
                _finish_refresh_request(conn, refresh_job_id, "queued" if reason == "http_429" else "failed" if reason else "completed")
            blocked = blocked or bool(reason)
            actions += 1
            if blocked and (bool(config["stop_on_block"]) or response_status == 429 or reason == "too_many_requests"):
                break
            continue
        try:
            body, response_status = adapter.fetch(row["url"])
        except AdapterFetchError as exc:
            _record_failure(conn, "US", row["asin"], "fetch_error", str(exc))
            if refresh_job_id and row["asin"] == refresh_asin:
                _finish_refresh_request(conn, refresh_job_id, "failed")
            actions += 1
            continue
        reason = classify_block(response_status, body)
        data = parse_product_html(body, row["url"]) if not reason else {"asin": "", "canonical_url": ""}
        if not reason and hasattr(adapter, "needs_browser_fallback") and adapter.needs_browser_fallback(data):
            try:
                browser_body, browser_status = adapter.fetch_browser(row["url"])
            except AdapterFetchError:
                pass
            else:
                browser_reason = classify_block(browser_status, browser_body)
                if not browser_reason:
                    body, response_status, data, reason = browser_body, browser_status, parse_product_html(browser_body, row["url"]), browser_reason
        context_errors = validate_context(data, config.get("context")) if not reason else []
        if context_errors and hasattr(adapter, "fetch_browser"):
            try:
                browser_body, browser_status = adapter.fetch_browser(row["url"])
            except AdapterFetchError:
                pass
            else:
                browser_reason = classify_block(browser_status, browser_body)
                browser_data = parse_product_html(browser_body, row["url"]) if not browser_reason else data
                browser_context_errors = validate_context(browser_data, config.get("context")) if not browser_reason else context_errors
                if not browser_reason and not browser_context_errors:
                    body, response_status, data, reason, context_errors = browser_body, browser_status, browser_data, browser_reason, []
        if context_errors:
            error_code = "context_mismatch:" + ",".join(context_errors)
        else:
            error_code = None
        source_type = getattr(adapter, "source_type", "selenium_dom")
        _write_product_action(conn, run_id, row, data, body, response_status, reason, error_code=error_code, source_type=source_type, raw_html_dir=raw_html_dir)
        if refresh_job_id and row["asin"] == refresh_asin:
            _finish_refresh_request(conn, refresh_job_id, "queued" if reason == "http_429" else "failed" if reason else "completed")
        blocked = blocked or bool(reason)
        actions += 1
        if blocked and (bool(config["stop_on_block"]) or response_status == 429 or reason == "too_many_requests"):
            break
    materialize_csvs(conn, Path(config["output_dir"]))
    return -1 if blocked else actions


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
    return parser


def run(args: argparse.Namespace) -> int:
    config = load_config(resolve_path(args.config))
    manifest, state, output = _prepare_paths(args, config)
    config["output_dir"] = output
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
    adapter = HttpFirstAdapter(config)
    result = 0
    try:
        limit = args.limit
        action_result = run_actions(conn, adapter, config, limit=limit)
        result = 3 if action_result == -1 else 0
    finally:
        adapter.close()
        conn.close()
    return result


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.limit is not None and args.limit < 1:
        print("错误: --limit 必须为正整数", file=sys.stderr)
        return 2
    try:
        return run(args)
    except (OSError, csv.Error, sqlite3.Error, ValueError, RuntimeError, KeyError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
