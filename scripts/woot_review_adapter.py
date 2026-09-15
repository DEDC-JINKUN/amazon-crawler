#!/usr/bin/env python3
"""Experimental, fail-closed Woot AJAX adapter for written Amazon US reviews."""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
import re
from typing import Any, Callable
import urllib.error
import urllib.parse
import urllib.request


BASE_URL = "https://www.woot.com/review/Reviews/"
ASIN_RE = re.compile(r"[A-Z0-9]{10}")
TRANSPARENT_AGENT_RE = re.compile(r"(?:^|\s)Agent/[A-Za-z0-9_.-]+(?:\s|$)")


class WootReviewError(RuntimeError):
    pass


class WootUpstreamBlocked(WootReviewError):
    pass


class WootInvalidResponse(WootReviewError):
    pass


def _review_date(description: Any) -> str | None:
    text = str(description or "")
    match = re.search(r"\bon ([A-Z][a-z]+ \d{1,2},? \d{4})\b", text)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1).replace(",", ""), "%B %d %Y").date().isoformat()
    except ValueError:
        return None


def _review_identity(asin: str, review: dict[str, Any]) -> str:
    identity = {
        "asin": asin,
        "author": review.get("Author"),
        "title": review.get("Title"),
        "text": review.get("Text"),
        "rating": review.get("OverallRating"),
        "origin": review.get("OriginDescription"),
    }
    digest = hashlib.sha256(
        json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return "woot-" + digest[:32]


def _normalize_review(asin: str, review: dict[str, Any], page: int) -> dict[str, Any]:
    review_id = _review_identity(asin, review)
    images = review.get("Images") or review.get("ReviewImages") or []
    if not isinstance(images, list):
        images = []
    return {
        "asin": asin,
        "marketplace": "US",
        "review_id": review_id,
        "rating": str(review.get("OverallRating") or ""),
        "title": str(review.get("Title") or ""),
        "body": str(review.get("Text") or ""),
        "review_url": f"https://www.woot.com/review/{asin}",
        "review_date": _review_date(review.get("OriginDescription")),
        "locale": "US" if "United States" in str(review.get("OriginDescription") or "") else None,
        "verified": bool(review.get("IsVerifiedPurchase")),
        "body_truncated": False,
        "review_images": images,
        "page": page,
        "source_type": "woot_ajax",
        "source_record_id": None,
        "unique_key": f"US|{asin}|woot_ajax|{review_id}",
    }


class WootReviewAdapter:
    def __init__(
        self, *, user_agent: str, timeout_seconds: float = 15.0,
        open_request: Callable[..., Any] = urllib.request.urlopen,
    ) -> None:
        if not TRANSPARENT_AGENT_RE.search(str(user_agent or "")):
            raise ValueError("Woot user agent must preserve Agent/<name> transparency")
        self.user_agent = str(user_agent)
        self.timeout_seconds = max(1.0, min(float(timeout_seconds), 30.0))
        self.open_request = open_request

    def fetch_page(
        self, asin: str, *, star: int = 0, sort: int = 0,
        page: int = 1, next_token: str | None = None,
    ) -> dict[str, Any]:
        asin = str(asin or "").strip().upper()
        if not ASIN_RE.fullmatch(asin) or star not in {0, 1, 2, 3, 4, 5} or sort not in {0, 1, 2, 3} or page < 1:
            raise ValueError("invalid Woot review request")
        params = {"filter": str(star), "isVerified": "false", "sort": str(sort)}
        if next_token:
            params["pagingNext"] = str(next_token)[:500]
        else:
            params["page"] = str(page)
        request = urllib.request.Request(
            BASE_URL + asin + "?" + urllib.parse.urlencode(params),
            headers={
                "User-Agent": self.user_agent,
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "X-Requested-With": "XMLHttpRequest",
                "Referer": f"https://www.woot.com/review/{asin}",
            },
        )
        try:
            response_context = self.open_request(request, timeout=self.timeout_seconds)
            with response_context as response:
                status = int(getattr(response, "status", 200))
                content_type = str(getattr(response, "headers", {}).get("Content-Type", "")).lower()
                body = response.read()
        except urllib.error.HTTPError as exc:
            if exc.code in {401, 403, 429}:
                raise WootUpstreamBlocked(f"woot_http_{exc.code}") from None
            raise WootReviewError("woot_transport_error") from None
        except (OSError, TimeoutError, urllib.error.URLError):
            raise WootReviewError("woot_transport_error") from None
        if status in {401, 403, 429}:
            raise WootUpstreamBlocked(f"woot_http_{status}")
        if status != 200 or "json" not in content_type:
            raise WootInvalidResponse("woot_response_is_not_json")
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise WootInvalidResponse("woot_json_is_invalid") from None
        if not isinstance(payload, dict) or not isinstance(payload.get("Reviews"), list):
            raise WootInvalidResponse("woot_reviews_shape_is_invalid")
        reviews = []
        for value in payload["Reviews"]:
            if not isinstance(value, dict):
                raise WootInvalidResponse("woot_review_shape_is_invalid")
            reviews.append(_normalize_review(asin, value, page))
        return {
            "source_type": "woot_ajax",
            "asin": asin,
            "reviews": reviews,
            "next_token": str(payload.get("PagingNext") or "") or None,
        }
