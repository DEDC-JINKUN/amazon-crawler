"""PostgreSQL persistence for resumable, auditable search-rank batches."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from typing import Any, Callable, Mapping


def evidence_id(batch_id: str, page: int, body: str) -> str:
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    return f"search-page:{batch_id}:{page}:{digest}"


class SearchRankStorage:
    def __init__(self, dsn: str, *, connect: Callable[[], Any] | None = None) -> None:
        if not dsn and connect is None:
            raise ValueError("dsn is required")
        self.dsn = dsn
        self._connect_factory = connect or self._connect

    def _connect(self) -> Any:
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:
            raise RuntimeError("SearchRankStorage requires psycopg for PostgreSQL") from exc
        return psycopg.connect(self.dsn, row_factory=dict_row)

    @staticmethod
    def _row(value: Any) -> dict[str, Any]:
        return dict(value) if not isinstance(value, dict) else value

    def start_batch(self, *, batch_id: str, run_id: str, tenant_id: str, marketplace: str, keyword: str, search_conditions: Mapping[str, Any], requested_depth: int, target_asins: list[str]) -> None:
        with self._connect_factory() as connection:
            with connection.cursor() as cursor:
                cursor.execute("""
                    INSERT INTO amazon_us.search_rank_batch
                    (batch_id,run_id,tenant_id,marketplace,keyword,search_conditions_json,requested_depth,target_asins_json,status)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'running')
                    ON CONFLICT (batch_id) DO NOTHING
                """, (batch_id, run_id, tenant_id, marketplace, keyword, json.dumps(dict(search_conditions), sort_keys=True), requested_depth, json.dumps(target_asins)))
            connection.commit()

    def completed_pages(self, batch_id: str) -> set[int]:
        with self._connect_factory() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT page FROM amazon_us.search_rank_page WHERE batch_id=%s AND status='completed'", (batch_id,))
            return {int(self._row(row)["page"]) for row in cursor.fetchall()}

    def load_pages(self, batch_id: str) -> dict[int, dict[str, Any]]:
        with self._connect_factory() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT page,status,source_evidence_id,cards_json,http_status,transfer_bytes,error_code,evidence_context_json FROM amazon_us.search_rank_page WHERE batch_id=%s", (batch_id,))
            rows = [self._row(row) for row in cursor.fetchall()]
        output: dict[int, dict[str, Any]] = {}
        for row in rows:
            for key in ("cards_json", "evidence_context_json"):
                if isinstance(row.get(key), str):
                    row[key] = json.loads(row[key])
            output[int(row["page"])] = row
        return output

    def record_page(self, *, batch_id: str, page: int, status: str, url: str, retrieved_at: datetime | None = None, http_status: int | None = None, transfer_bytes: int | None = None, raw_html_path: str | None = None, content_hash: str | None = None, cards: list[dict[str, Any]] | None = None, error_code: str | None = None, evidence_context: Mapping[str, Any] | None = None) -> str:
        if status not in {"completed", "failed"}:
            raise ValueError("status must be completed or failed")
        timestamp = retrieved_at or datetime.now(timezone.utc)
        stable = f"search-page:{batch_id}:{page}:{content_hash or error_code or 'no-content'}"
        with self._connect_factory() as connection:
            with connection.cursor() as cursor:
                cursor.execute("""
                    INSERT INTO amazon_us.search_rank_page
                    (batch_id,page,status,url,http_status,transfer_bytes,retrieved_at,source_evidence_id,content_hash,raw_html_path,cards_json,error_code,evidence_context_json)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (batch_id,page) DO UPDATE SET
                      status=EXCLUDED.status,url=EXCLUDED.url,http_status=EXCLUDED.http_status,transfer_bytes=EXCLUDED.transfer_bytes,
                      retrieved_at=EXCLUDED.retrieved_at,source_evidence_id=EXCLUDED.source_evidence_id,content_hash=EXCLUDED.content_hash,
                      raw_html_path=EXCLUDED.raw_html_path,cards_json=EXCLUDED.cards_json,error_code=EXCLUDED.error_code,evidence_context_json=EXCLUDED.evidence_context_json
                """, (batch_id, page, status, url, http_status, transfer_bytes, timestamp, stable, content_hash, raw_html_path, json.dumps(cards or []), error_code, json.dumps(dict(evidence_context or {}), sort_keys=True)))
            connection.commit()
        return stable

    def load_completed_cards(self, batch_id: str, page: int) -> list[dict[str, Any]]:
        row = self.load_pages(batch_id).get(page)
        return list(row.get("cards_json") or []) if row and row.get("status") == "completed" else []

    def finish_batch(self, batch_id: str, *, status: str, metrics: Mapping[str, Any]) -> None:
        if status not in {"completed", "partial", "failed"}:
            raise ValueError("invalid batch status")
        with self._connect_factory() as connection:
            with connection.cursor() as cursor:
                cursor.execute("UPDATE amazon_us.search_rank_batch SET status=%s, finished_at=CURRENT_TIMESTAMP, metrics_json=%s WHERE batch_id=%s", (status, json.dumps(dict(metrics), sort_keys=True), batch_id))
            connection.commit()
