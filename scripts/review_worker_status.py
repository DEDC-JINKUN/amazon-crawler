"""Read-only operational status for the independent review worker."""

from __future__ import annotations

import argparse
import json
import os

from review_database_identity import DEFAULT_PRODUCTION_DATABASE, assert_review_database_identity


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--dsn-env", default="AMAZON_US_POSTGRES_DSN")
    parser.add_argument("--expected-database", default=DEFAULT_PRODUCTION_DATABASE)
    parser.add_argument("--provider", default="woot_reviews")
    parser.add_argument("--recent-limit", type=int, default=10)
    args = parser.parse_args(argv)
    if args.recent_limit < 1 or args.recent_limit > 100:
        print(json.dumps({"ok": False, "reason": "recent_limit_out_of_range"}))
        return 2
    try:
        import psycopg

        dsn = os.environ.get(args.dsn_env, "").strip()
        if not dsn:
            raise ValueError("database_credentials_missing")
        with psycopg.connect(dsn, connect_timeout=5) as connection:
            connection.execute("SET TRANSACTION READ ONLY")
            with connection.cursor() as cursor:
                assert_review_database_identity(cursor, args.expected_database)
            status_counts = dict(connection.execute(
                """
                SELECT status,count(*) FROM amazon_reviews.review_job_state
                WHERE tenant_id=%s AND provider=%s GROUP BY status ORDER BY status
                """,
                (args.tenant_id, args.provider),
            ).fetchall())
            due_count = connection.execute(
                """
                SELECT count(*) FROM amazon_reviews.review_job_state
                WHERE tenant_id=%s AND provider=%s
                  AND next_due_at IS NOT NULL AND next_due_at <= CURRENT_TIMESTAMP
                """,
                (args.tenant_id, args.provider),
            ).fetchone()[0]
            running = [
                {
                    "asin": row[0], "lease_owner": row[1], "lease_expires_at": row[2],
                    "lease_state": "expired" if row[3] else "active",
                }
                for row in connection.execute(
                    """
                    SELECT asin,lease_owner,lease_expires_at,
                           lease_expires_at <= CURRENT_TIMESTAMP AS lease_expired
                    FROM amazon_reviews.review_job_state
                    WHERE tenant_id=%s AND provider=%s AND status='running'
                    ORDER BY asin
                    """,
                    (args.tenant_id, args.provider),
                ).fetchall()
            ]
            recent_runs = [
                {
                    "run_id": row[0], "status": row[1], "finished_at": row[2],
                    "requests": row[3], "response_body_bytes": row[4], "unique_reviews": row[5],
                }
                for row in connection.execute(
                    """
                    SELECT run_id,status,finished_at,request_count,response_body_bytes,unique_reviews
                    FROM amazon_reviews.review_collection_run
                    WHERE tenant_id=%s AND provider=%s AND mode <> 'import'
                    ORDER BY finished_at DESC NULLS LAST LIMIT %s
                    """,
                    (args.tenant_id, args.provider, args.recent_limit),
                ).fetchall()
            ]
        print(json.dumps({
            "ok": True,
            "tenant_id": args.tenant_id,
            "provider": args.provider,
            "status_counts": status_counts,
            "due_count": due_count,
            "running": running,
            "active_running_count": sum(item["lease_state"] == "active" for item in running),
            "expired_running_count": sum(item["lease_state"] == "expired" for item in running),
            "recent_runs": recent_runs,
        }, ensure_ascii=False, indent=2, default=str))
        return 0
    except Exception as exc:
        reason = str(exc) if type(exc) is ValueError else "status_query_failed"
        print(json.dumps({"ok": False, "reason": reason, "sqlstate": getattr(exc, "sqlstate", None)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
