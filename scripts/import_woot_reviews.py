#!/usr/bin/env python3
"""Validate or atomically import one verified Woot aggregate into PostgreSQL."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from contextlib import nullcontext
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

try:
    from collection_console import WootReviewConsoleRepository
except ModuleNotFoundError:  # pragma: no cover
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from collection_console import WootReviewConsoleRepository

from review_database_identity import DEFAULT_PRODUCTION_DATABASE, assert_review_database_identity


SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,120}$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
PROVIDER = "woot_reviews"


class ReviewImportError(ValueError):
    pass


class ReviewImportConflictError(ReviewImportError):
    pass


@dataclass(frozen=True)
class ReviewImportResult:
    tenant_id: str
    import_batch_id: str
    run_id: str
    manifest_sha256: str
    idempotent_replay: bool
    asin_rows: int
    review_rows: int
    observation_rows: int
    evidence_rows: int

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _safe_id(value: str, field: str) -> str:
    result = str(value or "").strip()
    if not SAFE_ID_RE.fullmatch(result):
        raise ReviewImportError(f"{field} must be a stable safe identifier")
    return result


def _load_manifest(root: Path) -> tuple[dict[str, Any], str]:
    path = root / "manifest.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("files"), list):
        raise ReviewImportError("aggregate manifest is invalid")
    return payload, _sha256(path)


def build_import_plan(
    aggregate_dir: str | Path,
    *,
    tenant_id: str,
    import_batch_id: str,
) -> dict[str, Any]:
    root = Path(aggregate_dir).resolve()
    tenant = _safe_id(tenant_id, "tenant_id")
    batch_id = _safe_id(import_batch_id, "import_batch_id")
    repository = WootReviewConsoleRepository(root)
    overview = repository.load_overview()
    if overview.get("availability") != "available":
        raise ReviewImportError("aggregate manifest or review data failed validation")
    manifest, manifest_hash = _load_manifest(root)
    summary = overview["summary"]
    source_hash = str(summary.get("source_sha256") or "")
    if not SHA_RE.fullmatch(source_hash):
        raise ReviewImportError("aggregate source_sha256 is invalid")
    return {
        "schema_version": "amazon-reviews-import-plan-v1",
        "tenant_id": tenant,
        "import_batch_id": batch_id,
        "run_id": f"import-{batch_id}",
        "provider": PROVIDER,
        "marketplace": "US",
        "mode": "import",
        "aggregate_dir": str(root),
        "aggregate_schema_version": summary.get("schema_version"),
        "source_filename": summary.get("source_filename"),
        "source_sha256": source_hash,
        "manifest_sha256": manifest_hash,
        "observed_at": overview.get("observed_at"),
        "asin_count": int(summary.get("source_unique_asins") or 0),
        "review_count": int(summary.get("unique_reviews") or 0),
        "coverage_status_counts": dict(summary.get("coverage_status_counts") or {}),
        "evidence_files": [
            {"path": item.get("path"), "bytes": int(item.get("bytes") or 0), "sha256": item.get("sha256")}
            for item in manifest["files"]
        ],
        "database_writes_performed": 0,
        "pdp_truth_written": False,
    }


def _connect_from_env(dsn_env: str):
    dsn = os.environ.get(dsn_env, "").strip()
    if not dsn:
        raise ReviewImportError(f"PostgreSQL DSN environment variable is required: {dsn_env}")
    try:
        import psycopg
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("psycopg is required for PostgreSQL import") from exc
    return psycopg.connect(dsn)


def _transaction(connection):
    transaction = getattr(connection, "transaction", None)
    return transaction() if callable(transaction) else nullcontext()


def _first(row: Any) -> Any:
    if row is None:
        return None
    if isinstance(row, Mapping):
        return next(iter(row.values()), None)
    return row[0]


def _chunks(rows: Iterable[tuple[Any, ...]], size: int = 500):
    chunk = []
    for row in rows:
        chunk.append(row)
        if len(chunk) >= size:
            yield chunk
            chunk = []
    if chunk:
        yield chunk


def _job_status_for_coverage(coverage: str) -> str:
    if coverage in {"complete", "no_reviews_verified"}:
        return "succeeded"
    if coverage in {"partial", "source_limited"}:
        return "source_limited"
    if coverage == "blocked":
        return "blocked"
    raise ReviewImportError(f"unsupported coverage status: {coverage}")


def _run_status_for_coverage_counts(counts: Mapping[str, Any]) -> str:
    positive = {str(key) for key, value in counts.items() if int(value or 0) > 0}
    if positive and positive <= {"complete", "no_reviews_verified"}:
        return "success"
    if positive == {"blocked"}:
        return "blocked"
    return "partial_success"


def _review_rows(repository: WootReviewConsoleRepository, plan: Mapping[str, Any]):
    run_id = str(plan["run_id"])
    observed_at = plan["observed_at"]
    for status in repository.statuses:
        asin = status["asin"]
        offset = 0
        while True:
            page = repository.load_asin(asin, limit=100, offset=offset)
            for review in page["items"]:
                provider = str(review.get("provider") or PROVIDER)
                if provider != PROVIDER:
                    raise ReviewImportError("aggregate contains an unexpected review provider")
                review_id = str(review.get("review_id") or "")
                id_kind = str(review.get("id_kind") or "")
                source_hash = str(review.get("source_output_sha256") or "")
                coverage = str(review.get("coverage_status") or status["coverage_status"])
                if not review_id or id_kind not in {"provider", "fallback_hash"}:
                    raise ReviewImportError("aggregate review identity is invalid")
                if not SHA_RE.fullmatch(source_hash):
                    raise ReviewImportError("aggregate review source_output_sha256 is invalid")
                record_payload = {
                    key: review.get(key) for key in (
                        "provider", "marketplace", "asin", "review_id", "provider_review_id", "id_kind",
                        "author", "rating", "title", "body", "review_url", "review_date", "locale",
                        "verified", "vine", "helpful_votes", "review_images", "review_videos",
                    )
                }
                record_hash = hashlib.sha256(_canonical(record_payload).encode("utf-8")).hexdigest()
                yield (
                    (
                        plan["tenant_id"], "US", asin, PROVIDER, review_id,
                        review.get("provider_review_id"), id_kind, review.get("author"), review.get("rating"),
                        review.get("title"), review.get("body"), review.get("review_url"),
                        review.get("review_date") or review.get("origin_description"), review.get("locale"),
                        review.get("verified"), review.get("vine"), review.get("helpful_votes"),
                        _canonical(review.get("review_images") or []), _canonical(review.get("review_videos") or []),
                        record_hash, run_id, run_id, observed_at, observed_at,
                    ),
                    (
                        plan["tenant_id"], run_id, "US", asin, PROVIDER, review_id, coverage,
                        source_hash, observed_at, _canonical(review),
                    ),
                )
            if page["next_offset"] is None:
                break
            offset = int(page["next_offset"])


RECORD_SQL = """
INSERT INTO amazon_reviews.review_record
  (tenant_id,marketplace,asin,provider,review_id,provider_review_id,id_kind,author,rating,title,body,
   review_url,review_date_text,locale,verified,vine,helpful_votes,review_images,review_videos,record_sha256,
   first_seen_run_id,last_seen_run_id,first_seen_at,last_seen_at)
VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s,%s,%s,%s)
ON CONFLICT (tenant_id,marketplace,asin,provider,review_id) DO UPDATE SET
  provider_review_id=EXCLUDED.provider_review_id,id_kind=EXCLUDED.id_kind,author=EXCLUDED.author,
  rating=EXCLUDED.rating,title=EXCLUDED.title,body=EXCLUDED.body,review_url=EXCLUDED.review_url,
  review_date_text=EXCLUDED.review_date_text,locale=EXCLUDED.locale,verified=EXCLUDED.verified,
  vine=EXCLUDED.vine,helpful_votes=EXCLUDED.helpful_votes,review_images=EXCLUDED.review_images,
  review_videos=EXCLUDED.review_videos,record_sha256=EXCLUDED.record_sha256,
  last_seen_run_id=EXCLUDED.last_seen_run_id,last_seen_at=EXCLUDED.last_seen_at
WHERE review_record.last_seen_at <= EXCLUDED.last_seen_at
"""

OBSERVATION_SQL = """
INSERT INTO amazon_reviews.review_observation
  (tenant_id,run_id,marketplace,asin,provider,review_id,coverage_status,source_output_sha256,observed_at,review_json)
VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
"""


def import_aggregate(
    aggregate_dir: str | Path,
    *,
    tenant_id: str,
    import_batch_id: str,
    dsn_env: str = "AMAZON_US_POSTGRES_DSN",
    expected_database: str = DEFAULT_PRODUCTION_DATABASE,
    connection_factory: Callable[[str], Any] | None = None,
) -> ReviewImportResult:
    plan = build_import_plan(
        aggregate_dir, tenant_id=tenant_id, import_batch_id=import_batch_id,
    )
    repository = WootReviewConsoleRepository(Path(plan["aggregate_dir"]))
    dsn = os.environ.get(dsn_env, "").strip()
    connection = connection_factory(dsn) if connection_factory is not None else _connect_from_env(dsn_env)
    try:
        with _transaction(connection), connection.cursor() as cursor:
            assert_review_database_identity(cursor, expected_database)
            cursor.execute(
                "SELECT manifest_sha256 FROM amazon_reviews.review_import_batch "
                "WHERE tenant_id=%s AND import_batch_id=%s",
                (plan["tenant_id"], plan["import_batch_id"]),
            )
            existing = _first(cursor.fetchone())
            if existing is not None:
                if str(existing) != plan["manifest_sha256"]:
                    raise ReviewImportConflictError(
                        "import_batch_id already exists with a different manifest hash"
                    )
                return ReviewImportResult(
                    str(plan["tenant_id"]), str(plan["import_batch_id"]), str(plan["run_id"]),
                    str(plan["manifest_sha256"]), True, 0, 0, 0, 0,
                )
            cursor.execute(
                """
                INSERT INTO amazon_reviews.review_collection_run
                  (tenant_id,run_id,provider,marketplace,mode,status,started_at,finished_at,
                   successful_asins,failed_asins,unique_reviews,coverage_counts,
                   source_manifest_sha256,metadata_json)
                VALUES (%s,%s,%s,'US','import',%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s::jsonb)
                """,
                (
                    plan["tenant_id"], plan["run_id"], PROVIDER,
                    _run_status_for_coverage_counts(plan["coverage_status_counts"]),
                    plan["observed_at"], plan["observed_at"],
                    plan["asin_count"] - int(plan["coverage_status_counts"].get("blocked") or 0),
                    int(plan["coverage_status_counts"].get("blocked") or 0),
                    plan["review_count"], _canonical(plan["coverage_status_counts"]),
                    plan["manifest_sha256"], _canonical({
                        "source_filename": plan["source_filename"],
                        "source_sha256": plan["source_sha256"],
                        "time_source": "aggregate_file_mtime",
                        "pdp_truth_written": False,
                    }),
                ),
            )
            cursor.execute(
                """
                INSERT INTO amazon_reviews.review_import_batch
                  (tenant_id,import_batch_id,run_id,provider,marketplace,schema_version,
                   source_filename,source_sha256,manifest_sha256,asin_count,review_count)
                VALUES (%s,%s,%s,%s,'US',%s,%s,%s,%s,%s,%s)
                """,
                (
                    plan["tenant_id"], plan["import_batch_id"], plan["run_id"], PROVIDER,
                    plan["aggregate_schema_version"], plan["source_filename"], plan["source_sha256"],
                    plan["manifest_sha256"], plan["asin_count"], plan["review_count"],
                ),
            )
            evidence_rows = []
            for item in plan["evidence_files"]:
                evidence_rows.append((
                    plan["tenant_id"], plan["run_id"], f"aggregate:{item['path']}",
                    item["bytes"], item["sha256"], item["path"], plan["observed_at"],
                ))
            cursor.executemany(
                """
                INSERT INTO amazon_reviews.review_raw_evidence
                  (tenant_id,run_id,evidence_key,body_bytes,body_sha256,raw_ref,captured_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
                """,
                evidence_rows,
            )
            job_rows = []
            for status in repository.statuses:
                coverage = status["coverage_status"]
                job_status = _job_status_for_coverage(coverage)
                job_rows.append((
                    plan["tenant_id"], status["asin"], PROVIDER, job_status, plan["run_id"],
                    coverage, status["fetched_unique_count"], plan["observed_at"],
                ))
            cursor.executemany(
                """
                INSERT INTO amazon_reviews.review_job_state
                  (tenant_id,marketplace,asin,provider,status,last_run_id,coverage_status,
                   fetched_review_count,next_due_at,updated_at)
                VALUES (%s,'US',%s,%s,%s,%s,%s,%s,NULL,%s)
                ON CONFLICT (tenant_id,marketplace,asin,provider) DO UPDATE SET
                  status=EXCLUDED.status,attempts=0,next_due_at=NULL,last_run_id=EXCLUDED.last_run_id,
                  coverage_status=EXCLUDED.coverage_status,
                  fetched_review_count=EXCLUDED.fetched_review_count,last_error=NULL,
                  lease_token=NULL,lease_owner=NULL,lease_expires_at=NULL,updated_at=EXCLUDED.updated_at
                WHERE review_job_state.status <> 'running'
                """,
                job_rows,
            )
            record_count = 0
            observation_count = 0
            pairs = _review_rows(repository, plan)
            for chunk in _chunks(pairs):
                cursor.executemany(RECORD_SQL, [item[0] for item in chunk])
                cursor.executemany(OBSERVATION_SQL, [item[1] for item in chunk])
                record_count += len(chunk)
                observation_count += len(chunk)
            if record_count != plan["review_count"]:
                raise ReviewImportError("imported review count does not match aggregate plan")
        return ReviewImportResult(
            str(plan["tenant_id"]), str(plan["import_batch_id"]), str(plan["run_id"]),
            str(plan["manifest_sha256"]), False, len(repository.statuses), record_count,
            observation_count, len(plan["evidence_files"]),
        )
    finally:
        close = getattr(connection, "close", None)
        if callable(close):
            close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("aggregate_dir", type=Path)
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--import-batch-id", required=True)
    parser.add_argument("--dsn-env", default="AMAZON_US_POSTGRES_DSN")
    parser.add_argument("--expected-database", default=DEFAULT_PRODUCTION_DATABASE)
    parser.add_argument("--validate-only", action="store_true")
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    try:
        plan = build_import_plan(
            args.aggregate_dir,
            tenant_id=args.tenant_id,
            import_batch_id=args.import_batch_id,
        )
        if args.validate_only:
            print(json.dumps({"ok": True, **plan}, ensure_ascii=False))
            return 0
        result = import_aggregate(
            args.aggregate_dir,
            tenant_id=args.tenant_id,
            import_batch_id=args.import_batch_id,
            dsn_env=args.dsn_env,
            expected_database=args.expected_database,
        )
    except (ReviewImportError, ValueError, RuntimeError, OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    except Exception:
        print(json.dumps({"ok": False, "error": "database_unavailable"}), file=sys.stderr)
        return 2
    print(json.dumps({"ok": True, **result.as_dict()}, ensure_ascii=False))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
