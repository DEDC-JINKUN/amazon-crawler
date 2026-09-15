#!/usr/bin/env python3
"""Fail-closed Amazon US written-review collector backed by Woot JSON.

Planning is the default CLI behavior. Network access requires ``--execute``.
Every HTTP response is stored as immutable evidence before it is interpreted.
The provider never claims complete coverage without a caller-supplied,
traceable written-review denominator.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping


BASE_URL = "https://www.woot.com/review/Reviews/"
PROVIDER = "woot_reviews"
PLAN_SCHEMA = "woot-review-plan-v1"
OUTPUT_SCHEMA = "woot-review-output-v1"
RECEIPT_SCHEMA = "woot-review-receipt-v1"
PARSER_VERSION = "woot-json-v1"
DEFAULT_USER_AGENT = "Amazon-US-Review-Provider/1.0 Agent/woot-review"
QUERY_RESULT_CAP = 100
ASIN_RE = re.compile(r"^[A-Z0-9]{10}$")
SAFE_RESPONSE_HEADERS = {"content-type", "date", "etag", "last-modified", "retry-after"}
AGENT_MARKER_RE = re.compile(r"(?:^|\s)Agent/[A-Za-z0-9_.:-]{1,64}(?:\s|$)")


class WootProviderError(RuntimeError):
    """A classified provider failure that must not be treated as zero reviews."""

    def __init__(self, code: str, message: str, *, http_status: int | None = None):
        super().__init__(message)
        self.code = code
        self.http_status = http_status


@dataclass(frozen=True)
class QuerySpec:
    index: int
    star: int
    sort: int

    @property
    def key(self) -> str:
        return f"star-{self.star}_sort-{self.sort}"


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


Transport = Callable[[urllib.request.Request, float], HttpResponse]
EvidenceHook = Callable[[QuerySpec, int, str, HttpResponse], None]
FailureEvidenceHook = Callable[[QuerySpec, int, str, WootProviderError], None]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _write_new_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")


def _write_new_bytes(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        handle.write(value)


def validate_asin(value: str) -> str:
    asin = str(value or "").strip().upper()
    if not ASIN_RE.fullmatch(asin):
        raise ValueError("ASIN must contain exactly 10 uppercase letters or digits")
    return asin


def validate_user_agent(value: str) -> str:
    user_agent = str(value or "").strip()
    if not user_agent or not AGENT_MARKER_RE.search(user_agent):
        raise ValueError("user_agent must contain a transparent Agent/<name> marker")
    return user_agent


def query_matrix(mode: str) -> list[QuerySpec]:
    if mode == "basic":
        pairs = [(0, 0)]
    elif mode == "full":
        pairs = [(star, 0) for star in (5, 4, 3, 2, 1)]
    elif mode == "max":
        pairs = [(star, sort) for star in (5, 4, 3, 2, 1) for sort in (0, 1, 2, 3)]
    elif mode == "adaptive":
        pairs = [(0, 0)] + [
            (star, sort) for star in (5, 4, 3, 2, 1) for sort in (0, 1, 2, 3)
        ]
    else:
        raise ValueError(f"unsupported mode: {mode}")
    return [QuerySpec(index=index + 1, star=star, sort=sort) for index, (star, sort) in enumerate(pairs)]


def build_plan(
    asin: str,
    *,
    mode: str = "max",
    expected_written_reviews: int | None = None,
    expected_count_source: str | None = None,
    max_requests: int = 200,
    request_delay_seconds: float = 0.2,
    user_agent: str = DEFAULT_USER_AGENT,
) -> dict[str, Any]:
    asin = validate_asin(asin)
    if expected_written_reviews is not None and expected_written_reviews < 0:
        raise ValueError("expected_written_reviews must be non-negative")
    if max_requests <= 0:
        raise ValueError("max_requests must be positive")
    if request_delay_seconds < 0:
        raise ValueError("request_delay_seconds must be non-negative")
    queries = query_matrix(mode)
    user_agent = validate_user_agent(user_agent)
    return {
        "schema_version": PLAN_SCHEMA,
        "provider": PROVIDER,
        "marketplace": "US",
        "asin": asin,
        "mode": mode,
        "queries": [asdict(item) | {"key": item.key} for item in queries],
        "query_count": len(queries),
        "max_requests": max_requests,
        "request_delay_seconds": request_delay_seconds,
        "user_agent": user_agent,
        "expected_written_reviews": expected_written_reviews,
        "expected_count_source": str(expected_count_source or "").strip() or None,
        "network_calls_performed": 0,
        "database_writes_performed": 0,
        "pdp_truth_written": False,
        "complete_requires_traceable_denominator": True,
    }


def _default_transport(request: urllib.request.Request, timeout: float) -> HttpResponse:
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return HttpResponse(
                status=int(getattr(response, "status", 200)),
                headers={str(key).lower(): str(value) for key, value in response.headers.items()},
                body=response.read(),
            )
    except urllib.error.HTTPError as exc:
        return HttpResponse(
            status=int(exc.code),
            headers={str(key).lower(): str(value) for key, value in (exc.headers or {}).items()},
            body=exc.read(),
        )
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise WootProviderError("network_error", f"provider request failed: {type(exc).__name__}") from exc


def _request_url(asin: str, query: QuerySpec, paging_next: str | None) -> str:
    params = {"filter": str(query.star), "isVerified": "false", "sort": str(query.sort)}
    if paging_next:
        params["pagingNext"] = paging_next
    else:
        params["page"] = "1"
    return f"{BASE_URL}{asin}?{urllib.parse.urlencode(params)}"


def _request(
    asin: str, query: QuerySpec, paging_next: str | None, user_agent: str,
) -> urllib.request.Request:
    url = _request_url(asin, query, paging_next)
    headers = {
        "User-Agent": validate_user_agent(user_agent),
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": f"https://www.woot.com/review/{asin}",
    }
    return urllib.request.Request(url, headers=headers, method="GET")


def _decode_payload(response: HttpResponse) -> dict[str, Any]:
    if response.status == 403:
        raise WootProviderError("provider_blocked", "Woot returned HTTP 403", http_status=403)
    if response.status == 429:
        raise WootProviderError("provider_rate_limited", "Woot returned HTTP 429", http_status=429)
    if response.status < 200 or response.status >= 300:
        raise WootProviderError(
            "provider_http_error", f"Woot returned HTTP {response.status}", http_status=response.status
        )
    try:
        payload = json.loads(response.body.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WootProviderError("invalid_json", "Woot response is not valid UTF-8 JSON") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("Reviews"), list):
        raise WootProviderError("schema_mismatch", "Woot response must contain a Reviews array")
    cursor = payload.get("PagingNext")
    if cursor is not None and not isinstance(cursor, (str, int, float)):
        raise WootProviderError("schema_mismatch", "PagingNext must be scalar or null")
    return payload


def _text(value: Any) -> str:
    if value is None:
        return ""
    return html.unescape(str(value)).strip()


def _compact_text(value: Any) -> str:
    return " ".join(_text(value).split()).casefold()


def _rating(value: Any) -> int | float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        match = re.search(r"[1-5](?:\.\d+)?", str(value))
        if not match:
            return None
        number = float(match.group(0))
    return int(number) if number.is_integer() else number


def _media_values(value: Any) -> list[Any]:
    if value is None:
        return []
    items = value if isinstance(value, list) else [value]
    result: list[Any] = []
    seen: set[str] = set()
    for item in items:
        if item in (None, ""):
            continue
        normalized = item if isinstance(item, (str, int, float, bool, dict, list)) else str(item)
        key = _canonical_json(normalized)
        if key not in seen:
            seen.add(key)
            result.append(normalized)
    return result


def _provider_review_id(raw: Mapping[str, Any]) -> str | None:
    for key in ("ReviewId", "ReviewID", "reviewId", "review_id", "Id", "ID"):
        value = _text(raw.get(key))
        if value:
            return value
    return None


def _fallback_digest(asin: str, raw: Mapping[str, Any]) -> str:
    signature = {
        "marketplace": "US",
        "asin": asin,
        "author": _compact_text(raw.get("Author")),
        "date": _compact_text(raw.get("OriginDescription")),
        "rating": _rating(raw.get("OverallRating")),
        "title": _compact_text(raw.get("Title")),
        "body": _compact_text(raw.get("Text")),
    }
    return hashlib.sha256(_canonical_json(signature).encode("utf-8")).hexdigest()


def normalize_review(asin: str, raw: Mapping[str, Any], source_ref: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise WootProviderError("schema_mismatch", "each Reviews item must be an object")
    title = _text(raw.get("Title"))
    body = _text(raw.get("Text"))
    if not title and not body:
        raise WootProviderError("schema_mismatch", "written review must contain Title or Text")
    provider_id = _provider_review_id(raw)
    fallback = _fallback_digest(asin, raw)
    review_id = f"woot:{provider_id}" if provider_id else f"woot-fallback:{fallback}"
    return {
        "provider": PROVIDER,
        "marketplace": "US",
        "asin": asin,
        "review_id": review_id,
        "provider_review_id": provider_id,
        "id_kind": "provider" if provider_id else "fallback_hash",
        "unique_key": f"US|{asin}|{review_id}",
        "author": _text(raw.get("Author")) or None,
        "rating": _rating(raw.get("OverallRating")),
        "title": title,
        "body": body,
        "review_url": _text(raw.get("ReviewUrl") or raw.get("Url")),
        "review_date": _text(raw.get("OriginDescription")),
        "origin_description": _text(raw.get("OriginDescription")),
        "locale": _text(raw.get("Locale")),
        "verified": raw.get("IsVerifiedPurchase") if isinstance(raw.get("IsVerifiedPurchase"), bool) else None,
        "vine": raw.get("IsVineReview") if isinstance(raw.get("IsVineReview"), bool) else None,
        "helpful_votes": (
            raw.get("HelpfulVotes")
            if isinstance(raw.get("HelpfulVotes"), int) and not isinstance(raw.get("HelpfulVotes"), bool)
            else None
        ),
        "body_truncated": False,
        "review_images": _media_values(raw.get("ImageUrls")),
        "review_videos": _media_values(raw.get("MediaUrls")),
        "source_refs": [dict(source_ref)],
    }


def _merge_unique(target: list[Any], additions: Iterable[Any]) -> None:
    seen = {_canonical_json(item) for item in target}
    for item in additions:
        key = _canonical_json(item)
        if key not in seen:
            target.append(item)
            seen.add(key)


def _merge_duplicate(existing: dict[str, Any], incoming: Mapping[str, Any]) -> None:
    _merge_unique(existing["source_refs"], incoming.get("source_refs") or [])
    _merge_unique(existing["review_images"], incoming.get("review_images") or [])
    _merge_unique(existing["review_videos"], incoming.get("review_videos") or [])
    for key in ("author", "rating", "title", "body", "review_url", "review_date", "locale", "verified", "vine", "helpful_votes"):
        if existing.get(key) in (None, "") and incoming.get(key) not in (None, ""):
            existing[key] = incoming[key]


def collect_reviews(
    asin: str,
    *,
    mode: str = "max",
    expected_written_reviews: int | None = None,
    expected_count_source: str | None = None,
    max_requests: int = 200,
    request_delay_seconds: float = 0.2,
    user_agent: str = DEFAULT_USER_AGENT,
    timeout_seconds: float = 15.0,
    transport: Transport = _default_transport,
    evidence_hook: EvidenceHook | None = None,
    failure_evidence_hook: FailureEvidenceHook | None = None,
) -> dict[str, Any]:
    plan = build_plan(
        asin,
        mode=mode,
        expected_written_reviews=expected_written_reviews,
        expected_count_source=expected_count_source,
        max_requests=max_requests,
        request_delay_seconds=request_delay_seconds,
        user_agent=user_agent,
    )
    asin = plan["asin"]
    reviews_by_id: dict[str, dict[str, Any]] = {}
    raw_records_seen = 0
    duplicate_occurrences = 0
    request_count = 0
    exhausted_queries = 0
    capped_queries: list[str] = []
    query_http_404s: list[dict[str, Any]] = []
    budget_exhausted = False
    adaptive_short_circuit = False
    queries = query_matrix(mode)

    for query in queries:
        paging_next: str | None = None
        seen_cursors: set[str] = set()
        query_records = 0
        page = 0
        while True:
            if request_count >= max_requests:
                budget_exhausted = True
                break
            page += 1
            if request_count and request_delay_seconds:
                time.sleep(request_delay_seconds)
            request = _request(asin, query, paging_next, plan["user_agent"])
            request_count += 1
            try:
                response = transport(request, timeout_seconds)
            except WootProviderError as exc:
                if failure_evidence_hook is not None:
                    failure_evidence_hook(query, page, request.full_url, exc)
                raise
            except Exception as exc:
                classified = WootProviderError(
                    "network_error", f"provider request failed: {type(exc).__name__}"
                )
                if failure_evidence_hook is not None:
                    failure_evidence_hook(query, page, request.full_url, classified)
                raise classified from exc
            if evidence_hook is not None:
                evidence_hook(query, page, request.full_url, response)
            if response.status == 404:
                query_http_404s.append({
                    "query": query.key,
                    "page": page,
                    "kind": "query_truncated" if query_records or paging_next else "query_unavailable",
                })
                break
            payload = _decode_payload(response)
            batch = payload["Reviews"]
            cursor_value = payload.get("PagingNext")
            next_cursor = _text(cursor_value) or None
            if not batch and next_cursor:
                raise WootProviderError("schema_mismatch", "empty Reviews page cannot advertise PagingNext")
            for raw in batch:
                raw_records_seen += 1
                query_records += 1
                source_ref = {"query": query.key, "page": page}
                normalized = normalize_review(asin, raw, source_ref)
                existing = reviews_by_id.get(normalized["review_id"])
                if existing is None:
                    reviews_by_id[normalized["review_id"]] = normalized
                else:
                    duplicate_occurrences += 1
                    _merge_duplicate(existing, normalized)
            if not next_cursor:
                exhausted_queries += 1
                break
            if next_cursor in seen_cursors:
                raise WootProviderError("cursor_loop", f"Woot repeated PagingNext for {query.key}")
            seen_cursors.add(next_cursor)
            paging_next = next_cursor
        if query_records >= QUERY_RESULT_CAP:
            capped_queries.append(query.key)
        if budget_exhausted:
            break
        if (
            mode == "adaptive"
            and query.index == 1
            and query_records < QUERY_RESULT_CAP
            and not any(item["query"] == query.key for item in query_http_404s)
        ):
            adaptive_short_circuit = True
            break

    reviews = sorted(reviews_by_id.values(), key=lambda item: item["review_id"])
    fetched = len(reviews)
    count_source = str(expected_count_source or "").strip() or None
    if fetched == 0 and expected_written_reviews and expected_written_reviews > 0:
        raise WootProviderError("unexpected_empty", "provider returned zero reviews for a positive denominator")
    denominator_conflict = (
        expected_written_reviews is not None and fetched > expected_written_reviews
    )
    if fetched == 0 and expected_written_reviews == 0 and count_source:
        coverage_status = "no_reviews_verified"
    elif (
        expected_written_reviews is not None
        and count_source
        and fetched == expected_written_reviews
        and not budget_exhausted
    ):
        coverage_status = "complete"
    elif budget_exhausted or capped_queries or query_http_404s:
        coverage_status = "source_limited"
    else:
        coverage_status = "partial"
    return {
        "schema_version": OUTPUT_SCHEMA,
        "provider": PROVIDER,
        "parser_version": PARSER_VERSION,
        "marketplace": "US",
        "asin": asin,
        "mode": mode,
        "coverage": {
            "status": coverage_status,
            "expected_written_reviews": expected_written_reviews,
            "expected_count_source": count_source,
            "fetched_unique_count": fetched,
            "count_delta": None if expected_written_reviews is None else fetched - expected_written_reviews,
            "denominator_conflict": denominator_conflict,
            "all_queries_exhausted": (
                adaptive_short_circuit and exhausted_queries == 1
            ) or exhausted_queries == len(queries),
            "exhausted_queries": exhausted_queries,
            "adaptive_short_circuit": adaptive_short_circuit,
            "capped_queries": capped_queries,
            "query_http_404s": query_http_404s,
            "request_budget_exhausted": budget_exhausted,
            "claim_100_percent": coverage_status in {"complete", "no_reviews_verified"},
        },
        "request_count": request_count,
        "raw_records_seen": raw_records_seen,
        "duplicate_occurrences": duplicate_occurrences,
        "reviews": reviews,
    }


class EvidenceRecorder:
    def __init__(self, run_dir: Path):
        self.run_dir = run_dir
        self.items: list[dict[str, Any]] = []

    def __call__(self, query: QuerySpec, page: int, url: str, response: HttpResponse) -> None:
        stem = f"q{query.index:02d}_{query.key}_p{page:03d}"
        body_path = self.run_dir / "evidence" / f"{stem}.body"
        metadata_path = self.run_dir / "evidence" / f"{stem}.json"
        _write_new_bytes(body_path, response.body)
        safe_headers = {
            str(key).lower(): str(value)
            for key, value in response.headers.items()
            if str(key).lower() in SAFE_RESPONSE_HEADERS
        }
        metadata = {
            "provider": PROVIDER,
            "query": query.key,
            "star": query.star,
            "sort": query.sort,
            "page": page,
            "url": url,
            "http_status": response.status,
            "response_headers": safe_headers,
            "body_bytes": len(response.body),
            "body_sha256": _sha256(response.body),
            "body_path": str(body_path.relative_to(self.run_dir)),
        }
        _write_new_json(metadata_path, metadata)
        self.items.append(metadata | {"metadata_path": str(metadata_path.relative_to(self.run_dir))})

    def record_failure(self, query: QuerySpec, page: int, url: str, error: WootProviderError) -> None:
        stem = f"q{query.index:02d}_{query.key}_p{page:03d}.failed"
        metadata_path = self.run_dir / "evidence" / f"{stem}.json"
        metadata = {
            "provider": PROVIDER,
            "query": query.key,
            "star": query.star,
            "sort": query.sort,
            "page": page,
            "url": url,
            "http_status": error.http_status,
            "error_code": error.code,
            "error": str(error),
            "body_bytes": None,
            "body_sha256": None,
            "body_path": None,
        }
        _write_new_json(metadata_path, metadata)
        self.items.append(metadata | {"metadata_path": str(metadata_path.relative_to(self.run_dir))})


def execute_collection(
    plan: Mapping[str, Any],
    *,
    output_root: Path,
    transport: Transport = _default_transport,
    now: datetime | None = None,
) -> dict[str, Any]:
    current = now or _utc_now()
    current_plan = build_plan(
        str(plan.get("asin") or ""),
        mode=str(plan.get("mode") or "max"),
        expected_written_reviews=plan.get("expected_written_reviews"),
        expected_count_source=plan.get("expected_count_source"),
        max_requests=int(plan.get("max_requests") or 200),
        request_delay_seconds=float(plan.get("request_delay_seconds") or 0),
        user_agent=str(plan.get("user_agent") or DEFAULT_USER_AGENT),
    )
    if dict(plan) != current_plan:
        raise WootProviderError("plan_mismatch", "supplied plan does not match the canonical plan")
    run_id = f"woot-{current.strftime('%Y%m%dT%H%M%S%fZ')}-{current_plan['asin']}-{uuid.uuid4().hex[:8]}"
    run_dir = Path(output_root) / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    _write_new_json(run_dir / "plan.json", current_plan)
    recorder = EvidenceRecorder(run_dir)
    try:
        output = collect_reviews(
            current_plan["asin"],
            mode=current_plan["mode"],
            expected_written_reviews=current_plan["expected_written_reviews"],
            expected_count_source=current_plan["expected_count_source"],
            max_requests=current_plan["max_requests"],
            request_delay_seconds=current_plan["request_delay_seconds"],
            user_agent=current_plan["user_agent"],
            transport=transport,
            evidence_hook=recorder,
            failure_evidence_hook=recorder.record_failure,
        )
        _write_new_json(run_dir / "evidence_manifest.json", recorder.items)
        _write_new_json(run_dir / "reviews.normalized.json", output)
        receipt = {
            "schema_version": RECEIPT_SCHEMA,
            "run_id": run_id,
            "status": "success",
            "provider": PROVIDER,
            "asin": current_plan["asin"],
            "started_at": _iso(current),
            "finished_at": _iso(_utc_now()),
            "evidence_files": len(recorder.items),
            "evidence_manifest_sha256": _sha256((run_dir / "evidence_manifest.json").read_bytes()),
            "normalized_output_sha256": _sha256((run_dir / "reviews.normalized.json").read_bytes()),
            "coverage": output["coverage"],
            "database_writes_performed": 0,
            "pdp_truth_written": False,
        }
        _write_new_json(run_dir / "receipt.json", receipt)
        return receipt | {"run_dir": str(run_dir)}
    except Exception as exc:
        _write_new_json(run_dir / "evidence_manifest.json", recorder.items)
        failure = {
            "schema_version": RECEIPT_SCHEMA,
            "run_id": run_id,
            "status": "failed",
            "provider": PROVIDER,
            "asin": current_plan["asin"],
            "started_at": _iso(current),
            "finished_at": _iso(_utc_now()),
            "error_code": getattr(exc, "code", type(exc).__name__),
            "http_status": getattr(exc, "http_status", None),
            "error": str(exc),
            "coverage": {
                "status": "blocked",
                "claim_100_percent": False,
                "reason": getattr(exc, "code", type(exc).__name__),
            },
            "evidence_files": len(recorder.items),
            "database_writes_performed": 0,
            "pdp_truth_written": False,
        }
        _write_new_json(run_dir / "receipt.failed.json", failure)
        # The worker must be able to persist the failed receipt and immutable
        # response evidence before it releases the database lease.
        try:
            setattr(exc, "run_dir", str(run_dir))
        except Exception:
            pass
        raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Plan or run fail-closed Amazon US Woot review collection")
    parser.add_argument("--asin", required=True)
    parser.add_argument("--mode", choices=("basic", "full", "max", "adaptive"), default="max")
    parser.add_argument("--expected-written-reviews", type=int)
    parser.add_argument("--expected-count-source")
    parser.add_argument("--max-requests", type=int, default=200)
    parser.add_argument("--request-delay-seconds", type=float, default=0.2)
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT)
    parser.add_argument("--output-root", type=Path, default=Path("outputs") / "woot-reviews")
    parser.add_argument("--plan-output", type=Path)
    parser.add_argument("--execute", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        plan = build_plan(
            args.asin,
            mode=args.mode,
            expected_written_reviews=args.expected_written_reviews,
            expected_count_source=args.expected_count_source,
            max_requests=args.max_requests,
            request_delay_seconds=args.request_delay_seconds,
            user_agent=args.user_agent,
        )
        if not args.execute:
            if args.plan_output:
                _write_new_json(args.plan_output, plan)
            print(json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True))
            return 0
        result = execute_collection(plan, output_root=args.output_root)
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except (ValueError, WootProviderError, FileExistsError) as exc:
        print(f"woot review collection failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
