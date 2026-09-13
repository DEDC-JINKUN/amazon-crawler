"""PostgreSQL outbox for recoverable, deduplicated watch delivery."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from .delivery import DeliveryChannel
from .reporting import report_for_events


def _stable(prefix: str, value: Mapping[str, Any]) -> str:
    payload = json.dumps(value, default=str, sort_keys=True, separators=(",", ":"))
    return f"{prefix}_{hashlib.sha256(payload.encode()).hexdigest()}"


class WatchDeliveryRepository:
    def __init__(self, dsn: str, *, connect: Callable[[], Any] | None = None) -> None:
        if not dsn and connect is None:
            raise ValueError("dsn is required")
        self.dsn = dsn
        self._connect_factory = connect or self._connect

    def _connect(self) -> Any:
        import psycopg
        from psycopg.rows import dict_row
        return psycopg.connect(self.dsn, row_factory=dict_row)

    @staticmethod
    def _row(row: Any) -> dict[str, Any]:
        value = dict(row)
        for key in ("event_ids", "observed_window_json", "content_json", "channel_receipt_json", "evidence_ids", "comparison_context_json", "old_value_json", "new_value_json", "delta_json"):
            if isinstance(value.get(key), str):
                value[key] = json.loads(value[key])
        return value

    @staticmethod
    def _json(value: Any) -> Any:
        try:
            from psycopg.types.json import Jsonb
            return Jsonb(value)
        except ImportError:
            return json.dumps(value, default=str, sort_keys=True)

    def sync_event_reports(self, *, tenant_id: str, marketplace: str, config: Mapping[str, Any]) -> int:
        """Recover the event-to-outbox handoff after any process crash.

        Events are durable first in B's repository. This query finds every
        unhandled event and atomically creates its report and pending outbox row.
        Re-running is safe because both the event handling row and delivery key
        are unique.
        """
        configured = bool(str(config.get("recipient") or "").strip() and str(config.get("channel") or "").strip())
        recipient = str(config.get("recipient") or "unconfigured")
        channel = str(config.get("channel") or "local_review")
        config_version = str(config.get("config_version") or "watch-config-v1")
        timezone_name = str(config.get("timezone") or "UTC")
        created = 0
        with self._connect_factory() as connection:
            try:
                with connection.cursor() as cursor:
                    cursor.execute("""
                        SELECT e.* FROM amazon_us.product_watch_event e
                        JOIN amazon_us.product_watch_observation o ON o.observation_id=e.after_observation_id
                        LEFT JOIN amazon_us.watch_event_handling h ON h.event_id=e.event_id
                        WHERE o.tenant_id=%s AND o.marketplace=%s AND (
                          h.event_id IS NULL OR NOT EXISTS (
                            SELECT 1 FROM amazon_us.watch_delivery d
                            WHERE d.report_id=h.report_id AND d.channel=%s AND d.recipient=%s
                          )
                        )
                        ORDER BY e.created_at,e.event_id FOR UPDATE OF e SKIP LOCKED
                    """, (tenant_id, marketplace, channel, recipient))
                    for raw in cursor.fetchall():
                        event = self._row(raw)
                        kind = "immediate" if event.get("severity") == "important" else "summary"
                        rendered = report_for_events([event], report_kind=kind, timezone_name=timezone_name)
                        report_id = _stable("rpt", {"event_id": event["event_id"], "kind": kind, "config_version": config_version})
                        delivery_id = _stable("dly", {"report_id": report_id, "channel": channel, "recipient": recipient})
                        dedupe_key = _stable("delivery", {"event_id": event["event_id"], "channel": channel, "recipient": recipient, "config_version": config_version})
                        cursor.execute("""
                            INSERT INTO amazon_us.watch_report
                            (report_id,tenant_id,marketplace,report_kind,event_ids,rule_version,config_version,observed_window_json,content_json,content_text)
                            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (report_id) DO NOTHING
                        """, (report_id, tenant_id, marketplace, kind, self._json([event["event_id"]]), event.get("rule_version") or "product-watch-v1", config_version, self._json(event.get("observed_window_json") or {}), self._json(rendered), rendered["content_text"]))
                        cursor.execute("""
                            INSERT INTO amazon_us.watch_event_handling (event_id,report_id,handling_status,reason)
                            VALUES (%s,%s,'reported',%s) ON CONFLICT (event_id) DO UPDATE SET
                              report_id=EXCLUDED.report_id,handling_status='reported',updated_at=CURRENT_TIMESTAMP,reason=EXCLUDED.reason
                        """, (event["event_id"], report_id, "outbox_created" if configured else "local_review_pending_configuration"))
                        cursor.execute("""
                            INSERT INTO amazon_us.watch_delivery (delivery_id,report_id,tenant_id,channel,recipient,dedupe_key,status)
                            VALUES (%s,%s,%s,%s,%s,%s,'pending') ON CONFLICT (tenant_id,channel,recipient,dedupe_key) DO NOTHING
                        """, (delivery_id, report_id, tenant_id, channel, recipient, dedupe_key))
                        created += 1
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return created

    def claim_pending(self, *, tenant_id: str, limit: int = 10) -> list[dict[str, Any]]:
        self.reclaim_abandoned_sending(tenant_id=tenant_id)
        with self._connect_factory() as connection:
            try:
                with connection.cursor() as cursor:
                    cursor.execute("""
                        WITH claimed AS (
                          SELECT delivery_id FROM amazon_us.watch_delivery
                          WHERE tenant_id=%s AND channel<>'local_review' AND status IN ('pending','failed') AND attempts<3 AND available_at<=CURRENT_TIMESTAMP
                          ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT %s
                        )
                        UPDATE amazon_us.watch_delivery d SET status='sending',attempts=d.attempts+1,claimed_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP
                        FROM claimed WHERE d.delivery_id=claimed.delivery_id
                        RETURNING d.*
                    """, (tenant_id, max(1, min(100, int(limit)))))
                    items = [self._row(row) for row in cursor.fetchall()]
                connection.commit()
                return items
            except Exception:
                connection.rollback(); raise

    def load_report(self, report_id: str, *, tenant_id: str | None = None) -> dict[str, Any] | None:
        with self._connect_factory() as connection, connection.cursor() as cursor:
            if tenant_id:
                cursor.execute("SELECT * FROM amazon_us.watch_report WHERE report_id=%s AND tenant_id=%s", (report_id, tenant_id))
            else:
                cursor.execute("SELECT * FROM amazon_us.watch_report WHERE report_id=%s", (report_id,))
            row = cursor.fetchone()
            return self._row(row) if row else None

    def complete_delivery(self, delivery_id: str, receipt: Mapping[str, Any]) -> str:
        confirmed = bool(receipt.get("confirmed"))
        status = "sent" if confirmed else "unknown" if str(receipt.get("status")) == "unknown" else "failed"
        with self._connect_factory() as connection, connection.cursor() as cursor:
            cursor.execute("""
                UPDATE amazon_us.watch_delivery SET status=%s,sent_at=CASE WHEN %s='sent' THEN CURRENT_TIMESTAMP ELSE sent_at END,
                  available_at=CASE WHEN %s='failed' THEN CURRENT_TIMESTAMP + (LEAST(attempts, 6) * INTERVAL '60 seconds') ELSE available_at END,
                  channel_receipt_json=%s,last_error=%s,updated_at=CURRENT_TIMESTAMP
                WHERE delivery_id=%s AND status='sending'
            """, (status, status, status, self._json(dict(receipt)), None if confirmed else str(receipt.get("reason") or "unconfirmed")[:500], delivery_id))
            connection.commit()
        return status

    def reclaim_abandoned_sending(self, *, tenant_id: str) -> int:
        """Preserve ambiguous delivery instead of blindly resending after a crash."""
        with self._connect_factory() as connection, connection.cursor() as cursor:
            cursor.execute("UPDATE amazon_us.watch_delivery SET status='unknown',last_error='sending_lease_expired',updated_at=CURRENT_TIMESTAMP WHERE tenant_id=%s AND status='sending' AND claimed_at < CURRENT_TIMESTAMP - INTERVAL '15 minutes'", (tenant_id,))
            changed = cursor.rowcount
            connection.commit()
            return changed

    def process_pending(self, *, tenant_id: str, channel: DeliveryChannel, limit: int = 10) -> dict[str, int]:
        result = {"claimed": 0, "sent": 0, "failed": 0, "unknown": 0}
        for delivery in self.claim_pending(tenant_id=tenant_id, limit=limit):
            result["claimed"] += 1
            try:
                report = self.load_report(delivery["report_id"])
                if report is None:
                    receipt: Mapping[str, Any] = {"confirmed": False, "reason": "report_missing"}
                else:
                    receipt = channel.send(delivery, report)
            except TimeoutError:
                receipt = {"confirmed": False, "status": "unknown", "reason": "channel_timeout"}
            except Exception as exc:
                receipt = {"confirmed": False, "reason": f"channel_error:{type(exc).__name__}"}
            result[self.complete_delivery(delivery["delivery_id"], receipt)] += 1
        return result

    def list_events(self, *, tenant_id: str, marketplace: str, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect_factory() as connection, connection.cursor() as cursor:
            cursor.execute("""
                SELECT e.event_id,e.event_type,e.watch_id,e.old_value_json,e.new_value_json,e.delta_json,e.comparison_context_json,
                       e.observed_window_json,e.quality,e.evidence_ids,e.rule_version,e.severity,e.reason,e.created_at,h.handling_status,h.report_id
                FROM amazon_us.product_watch_event e JOIN amazon_us.product_watch_observation o ON o.observation_id=e.after_observation_id
                LEFT JOIN amazon_us.watch_event_handling h ON h.event_id=e.event_id
                WHERE o.tenant_id=%s AND o.marketplace=%s ORDER BY e.created_at DESC LIMIT %s
            """, (tenant_id, marketplace, max(1, min(500, int(limit)))))
            return [self._row(row) for row in cursor.fetchall()]

    def list_deliveries(self, *, tenant_id: str, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect_factory() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT delivery_id,report_id,channel,recipient,status,attempts,available_at,claimed_at,sent_at,channel_receipt_json,last_error,created_at,updated_at FROM amazon_us.watch_delivery WHERE tenant_id=%s ORDER BY created_at DESC LIMIT %s", (tenant_id, max(1, min(500, int(limit)))))
            return [self._row(row) for row in cursor.fetchall()]

    def summary(self, *, tenant_id: str, marketplace: str) -> dict[str, Any]:
        events = self.list_events(tenant_id=tenant_id, marketplace=marketplace, limit=100)
        deliveries = self.list_deliveries(tenant_id=tenant_id, limit=100)
        return {"events": events, "delivery_counts": {status: sum(1 for item in deliveries if item["status"] == status) for status in ("pending","sending","sent","failed","unknown")}}

    def acknowledge_event(self, *, tenant_id: str, event_id: str, actor: str, reason: str = "") -> bool:
        with self._connect_factory() as connection, connection.cursor() as cursor:
            cursor.execute("""
                UPDATE amazon_us.watch_event_handling SET handling_status='acknowledged',updated_at=CURRENT_TIMESTAMP,
                  reason=%s WHERE event_id=%s AND EXISTS (
                    SELECT 1 FROM amazon_us.product_watch_event e JOIN amazon_us.product_watch_observation o ON o.observation_id=e.after_observation_id
                    WHERE e.event_id=amazon_us.watch_event_handling.event_id AND o.tenant_id=%s)
            """, (f"acknowledged_by:{actor}:{reason}"[:500], event_id, tenant_id))
            updated = cursor.rowcount == 1
            connection.commit()
            return updated
