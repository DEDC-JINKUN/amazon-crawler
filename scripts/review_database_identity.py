"""Shared hard identity gate for the independent review database schema."""

from __future__ import annotations


SCHEMA_MARKER = "amazon-reviews-schema-v1"
DEFAULT_PRODUCTION_DATABASE = "postgres"


class ReviewDatabaseIdentityError(RuntimeError):
    pass


def assert_review_database_identity(cursor, expected_database: str) -> None:
    expected = str(expected_database or "").strip()
    if not expected:
        raise ReviewDatabaseIdentityError("expected review database name is required")
    cursor.execute(
        "SELECT current_database(),obj_description('amazon_reviews'::regnamespace,'pg_namespace')"
    )
    row = cursor.fetchone()
    if row is None or str(row[0]) != expected or str(row[1]) != SCHEMA_MARKER:
        raise ReviewDatabaseIdentityError("review database identity mismatch")
