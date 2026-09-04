"""Provision a marked, local-only test database; never run DDL in production."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

DATABASE = "amazon_recovery_test_20260904_13c0"
MARKER = "amazon-recovery-isolated-tests:20260904:13c0"


def main() -> int:
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    try:
        source = os.environ.get("AMAZON_US_POSTGRES_DSN", "")
        if not source:
            raise ValueError("source_missing")
        options = conninfo_to_dict(source)
        if options.get("host") not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("local_database_required")
        if options.get("dbname") == DATABASE:
            raise ValueError("source_is_test_database")
        with psycopg.connect(source, autocommit=True, connect_timeout=5) as conn:
            row = conn.execute(
                "SELECT shobj_description(oid,'pg_database') FROM pg_database WHERE datname=%s",
                (DATABASE,),
            ).fetchone()
            if row is None:
                conn.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(DATABASE)))
                conn.execute(sql.SQL("COMMENT ON DATABASE {} IS {}").format(sql.Identifier(DATABASE), sql.Literal(MARKER)))
            elif row[0] != MARKER:
                raise ValueError("existing_database_not_owned")
        test_dsn = make_conninfo(source, dbname=DATABASE)
        with psycopg.connect(test_dsn, connect_timeout=5) as conn:
            if conn.execute("SELECT current_database()").fetchone()[0] != DATABASE:
                raise ValueError("database_identity_mismatch")
            if sys.argv[1:] != ["--provision-only"]:
                root = Path(__file__).resolve().parents[1]
                conn.execute((root / 'schema/postgres_schema.sql').read_text(encoding='utf-8'))
                conn.execute((root / 'schema/migrations/20260904_recovery.sql').read_text(encoding='utf-8'))
        print(json.dumps({"test_database": DATABASE, "isolated": True}), flush=True)
        if sys.argv[1:] == ["--provision-only"]:
            return 0
        env = os.environ.copy()
        env["AMAZON_TEST_POSTGRES_DSN"] = test_dsn
        for name in ("AMAZON_US_POSTGRES_DSN", "AMAZON_PROXY_USER", "AMAZON_PROXY_PASS", "AMAZON_COLLECTION_API_KEY"):
            env.pop(name, None)
        selection = sys.argv[1:] or ["tests/test_postgres_worker_integration.py"]
        result = subprocess.run(
            [sys.executable, "-m", "pytest", *selection, "-q", "--tb=short", "-p", "no:cacheprovider"],
            cwd=Path(__file__).resolve().parents[1], env=env, check=False,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        output = result.stdout.decode('utf-8', errors='replace')
        for secret in (source, test_dsn, options.get('password'), options.get('user')):
            if secret:
                output = output.replace(secret, '[redacted]')
        print(output.encode('ascii', errors='backslashreplace').decode('ascii'))
        return result.returncode
    except Exception as exc:
        reason = str(exc) if type(exc) is ValueError else "database_setup_failed"
        print(json.dumps({"status": "denied", "reason": reason, "sqlstate": getattr(exc, "sqlstate", None)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
