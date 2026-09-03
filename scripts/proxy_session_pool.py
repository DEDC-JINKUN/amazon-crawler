"""Bounded, run-scoped proxy sessions behind the existing fetch adapter seam."""
from __future__ import annotations

import re
import time
from collections import deque
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

MAX_PROXY_SESSION_PORTS = 40


class ProxyCircuitOpen(RuntimeError):
    """No further network work is allowed for this run."""


class _Slot:
    def __init__(self, session_id: str, adapter: Any) -> None:
        self.session_id = session_id
        self.adapter = adapter
        self.health = "healthy"
        self.asins: set[str] = set()
        self.request_count = 0
        self.completed = 0
        self.variant_redirect = 0
        self.failed = 0
        self.blocked = 0
        self.network_error = 0
        self.bytes = 0
        self.latency_ms = 0
        self.quarantine_reason: str | None = None
        self.firefox_verification: str | None = None
        self.firefox_attempts = 0
        self.action_generation = -1

    def public(self, mode: str) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "mode": mode,
            "health": self.health,
            "asin_count": len(self.asins),
            "request_count": self.request_count,
            "completed": self.completed,
            "variant_redirect": self.variant_redirect,
            "failed": self.failed,
            "blocked": self.blocked,
            "network_error": self.network_error,
            "bytes": self.bytes,
            "latency_ms": self.latency_ms,
            "quarantine_reason": self.quarantine_reason,
            "firefox_verification": self.firefox_verification,
            "firefox_attempts": self.firefox_attempts,
        }


class ProxySessionPool:
    """Own isolated adapters, bounded retries, health and circuit-breaker state."""

    def __init__(
        self,
        config: dict[str, Any],
        adapter_factory: Callable[[dict[str, Any]], Any],
        classify_block: Callable[[int | None, str], str | None],
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = dict(config)
        self._factory = adapter_factory
        self._classify_block = classify_block
        self._clock = clock
        self.mode = str(config.get("proxy_session_mode") or "sticky").strip().lower()
        if self.mode != "sticky":
            raise ValueError("proxy_session_mode must be sticky")
        ports = list(config.get("proxy_session_ports") or [])
        if not ports or len(ports) > MAX_PROXY_SESSION_PORTS:
            raise ValueError(f"proxy_session_ports must contain 1 to {MAX_PROXY_SESSION_PORTS} approved ports")
        self._all_ports = [int(port) for port in ports]
        if len(set(self._all_ports)) != len(self._all_ports) or any(port < 1 or port > 65535 for port in self._all_ports):
            raise ValueError("proxy_session_ports must be unique valid ports")
        self._ports = list(self._all_ports)
        self._session_ids = [f"session-{index + 1:02d}" for index in range(len(self._all_ports))]
        self._capacity_validator: Callable[[], Any] | None = None
        self.max_asins = self._bounded(config, "proxy_session_max_asins", 3, 1, 5)
        self.product_scope = str(config.get("proxy_product_session_scope") or "per_asin").strip().lower()
        if self.product_scope not in {"per_asin", "bounded"}:
            raise ValueError("proxy_product_session_scope must be per_asin or bounded")
        self.retry_per_asin = self._bounded(config, "proxy_session_retry_per_asin", 1, 0, 1)
        self.consecutive_limit = self._bounded(config, "proxy_session_consecutive_block_limit", 2, 1, 5)
        self.window_size = self._bounded(config, "proxy_session_window_size", 20, 1, 100)
        self.window_block_limit = self._bounded(config, "proxy_session_window_block_limit", 3, 1, 20)
        if self.window_block_limit > self.window_size:
            raise ValueError("proxy session window block limit exceeds window size")
        base = urlsplit(str(config.get("proxy_url") or ""))
        if base.scheme not in {"http", "https"} or not base.hostname or base.username or base.password:
            raise ValueError("proxy_url must be an approved credential-free HTTP(S) endpoint")
        self._base_proxy = base
        self._slots: list[_Slot] = []
        self._asin_slots: dict[str, _Slot] = {}
        self._current: _Slot | None = None
        self._run_scope: tuple[str, str, str] | None = None
        self._next_port = 0
        self._retries: dict[str, int] = {}
        self._block_window: deque[bool] = deque(maxlen=self.window_size)
        self._consecutive_blocks = 0
        self._breaker_counted_asins: set[str] = set()
        self._intermediate: list[dict[str, Any]] = []
        self._persisted_attempts: list[dict[str, Any]] = []
        self._action_generation = 0
        self.circuit_open_reason: str | None = None
        self.unrequested_count = 0
        self._action_http_bytes = 0
        self._last_transfer_bytes: int | None = None
        self._browser_traffic_parts: list[dict[str, Any]] = []

    @staticmethod
    def _bounded(config: dict[str, Any], name: str, default: int, minimum: int, maximum: int) -> int:
        value = int(config.get(name, default))
        if value < minimum or value > maximum:
            raise ValueError(f"{name} must be between {minimum} and {maximum}")
        return value

    def _proxy_url(self, port: int) -> str:
        host = self._base_proxy.hostname or ""
        netloc = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
        return urlunsplit((self._base_proxy.scheme, netloc, self._base_proxy.path, "", ""))

    def begin_run(self, run_id: str, tenant_id: str, worker_id: str) -> None:
        self.close()
        self._run_scope = (str(run_id), str(tenant_id), str(worker_id))
        self._slots = []
        self._asin_slots = {}
        self._current = None
        self._next_port = 0
        self._retries = {}
        self._block_window = deque(maxlen=self.window_size)
        self._consecutive_blocks = 0
        self._breaker_counted_asins = set()
        self._intermediate = []
        self._persisted_attempts = []
        self._action_generation = 0
        self.circuit_open_reason = None
        self.unrequested_count = 0
        self._action_http_bytes = 0
        self._last_transfer_bytes = None
        self._browser_traffic_parts = []

    def configure_capacity_reservation(
        self,
        slot_ids: list[str],
        validator: Callable[[], Any],
    ) -> None:
        if self._slots or self._current is not None or self._run_scope is not None:
            raise RuntimeError("capacity reservation must be configured before begin_run")
        if not slot_ids or len(set(slot_ids)) != len(slot_ids) or not callable(validator):
            raise ValueError("capacity reservation requires unique slot ids and a validator")
        selected_ports: list[int] = []
        normalized_ids: list[str] = []
        for slot_id in slot_ids:
            match = re.fullmatch(r"session-(\d{2})", str(slot_id))
            index = int(match.group(1)) - 1 if match else -1
            if index < 0 or index >= len(self._all_ports):
                raise ValueError("capacity reservation contains an unknown slot id")
            normalized_ids.append(f"session-{index + 1:02d}")
            selected_ports.append(self._all_ports[index])
        self._ports = selected_ports
        self._session_ids = normalized_ids
        self._capacity_validator = validator

    def release_capacity_reservation(self) -> None:
        self.close()
        self._slots = []
        self._asin_slots = {}
        self._current = None
        self._run_scope = None
        self._next_port = 0
        self._ports = list(self._all_ports)
        self._session_ids = [f"session-{index + 1:02d}" for index in range(len(self._all_ports))]
        self._capacity_validator = None
        self._breaker_counted_asins = set()
        self._browser_traffic_parts = []

    def begin_action(self) -> None:
        self._action_generation += 1
        self._persisted_attempts = []
        self._action_http_bytes = 0
        self._last_transfer_bytes = None
        self._browser_traffic_parts = []
        for name in ("last_fallback_reason", "action_fallback_reasons"):
            self.__dict__.pop(name, None)
        self.browser_attempted = False
        self.browser_attempt_count = 0
        if self._current is not None:
            self._prepare(self._current)

    def _new_slot(self) -> _Slot:
        if self._capacity_validator is not None:
            self._capacity_validator()
        if self._next_port >= len(self._ports):
            self.circuit_open_reason = self.circuit_open_reason or "session_pool_exhausted"
            raise ProxyCircuitOpen(self.circuit_open_reason)
        slot_config = dict(self.config)
        slot_config["proxy_url"] = self._proxy_url(self._ports[self._next_port])
        slot = _Slot(self._session_ids[self._next_port], self._factory(slot_config))
        self._next_port += 1
        if self._run_scope and hasattr(slot.adapter, "begin_run"):
            slot.adapter.begin_run(*self._run_scope)
        self._slots.append(slot)
        self._current = slot
        self._prepare(slot)
        return slot

    def _prepare(self, slot: _Slot) -> None:
        if slot.action_generation != self._action_generation and hasattr(slot.adapter, "begin_action"):
            slot.adapter.begin_action()
            slot.action_generation = self._action_generation

    @staticmethod
    def _asin(url: str) -> str:
        match = re.search(r"/(?:dp|clp|product-reviews|portal/customer-reviews)/([A-Za-z0-9]{10})(?:/|$|[?#])", url)
        return match.group(1).upper() if match else "unknown"

    def _select(self, asin: str, *, force_new: bool = False) -> _Slot:
        if self.product_scope == "per_asin" and not force_new:
            bound = self._asin_slots.get(asin)
            if bound is not None and bound.health == "healthy":
                self._current = bound
                self._prepare(bound)
                return bound
        slot = self._current
        different_asin = slot is not None and asin not in slot.asins
        if (
            force_new or slot is None or slot.health != "healthy"
            or (different_asin and self.product_scope == "per_asin")
            or (different_asin and len(slot.asins) >= self.max_asins)
        ):
            if slot is not None:
                preserve_other_asin = (
                    self.product_scope == "per_asin" and slot.health == "healthy"
                    and asin not in slot.asins and not force_new
                )
                if slot.health == "healthy" and not force_new and not preserve_other_asin:
                    slot.health = "exhausted"
                if not preserve_other_asin:
                    slot.adapter.close()
            slot = self._new_slot()
        slot.asins.add(asin)
        if self.product_scope == "per_asin":
            self._asin_slots[asin] = slot
        self._prepare(slot)
        return slot

    def _quarantine(self, slot: _Slot, reason: str) -> None:
        slot.health = "quarantined"
        slot.quarantine_reason = reason

    def fetch(self, url: str) -> tuple[str, int | None]:
        if self.circuit_open_reason:
            raise ProxyCircuitOpen(self.circuit_open_reason)
        asin = self._asin(url)
        slot = self._select(asin)
        started = self._clock()
        try:
            body, status = slot.adapter.fetch(url)
        except Exception:
            raw_byte_count = getattr(slot.adapter, "last_transfer_bytes", None)
            byte_count = None if raw_byte_count is None else max(0, int(raw_byte_count))
            self._last_transfer_bytes = byte_count
            if byte_count is not None:
                self._action_http_bytes += byte_count
            slot.request_count += 1
            slot.network_error += 1
            if byte_count is not None:
                slot.bytes += byte_count
            slot.latency_ms += max(0, round((self._clock() - started) * 1000))
            slot.health = "quarantined"
            slot.quarantine_reason = "transport_error"
            raise
        latency = max(0, round((self._clock() - started) * 1000))
        byte_count = max(0, int(getattr(slot.adapter, "last_transfer_bytes", 0) or 0))
        self._last_transfer_bytes = byte_count
        self._action_http_bytes += byte_count
        slot.request_count += 1
        slot.bytes += byte_count
        slot.latency_ms += latency
        block_reason = self._classify_block(status, body)
        if block_reason:
            slot.blocked += 1
            self._quarantine(slot, block_reason)
            self._intermediate.append({
                "session_id": slot.session_id,
                "mode": "http",
                "url": url,
                "http_status": status,
                "transfer_bytes": byte_count,
                "latency_ms": latency,
                "block_reason": block_reason,
                "body": body,
            })
        return body, status

    def rotate_after_browser_failure(self, url: str) -> bool:
        """Select one final recovery slot explicitly after same-slot Firefox fails."""
        if self.circuit_open_reason:
            return False
        asin = self._asin(url)
        retries = self._retries.get(asin, 0)
        if retries >= self.retry_per_asin:
            return False
        if self._current is None or self._current.health != "quarantined":
            raise RuntimeError("browser retry requires a quarantined current slot")
        if self._next_port >= len(self._ports):
            self.circuit_open_reason = "session_pool_exhausted"
            return False
        self._retries[asin] = retries + 1
        self._select(asin, force_new=True)
        return True

    def preserve_browser_attempt(
        self,
        url: str,
        body: str,
        status: int | None,
        block_reason: str | None,
        *,
        error_code: str | None = None,
    ) -> None:
        if self._current is None:
            return
        self._intermediate.append({
            "session_id": self._current.session_id,
            "mode": "firefox",
            "url": url,
            "http_status": status,
            "transfer_bytes": getattr(self._current.adapter, "last_transfer_bytes", None),
            "latency_ms": None,
            "block_reason": block_reason,
            "error_code": error_code,
            "body": body,
        })

    def record_browser_verification(self, succeeded: bool) -> None:
        if self._current is None:
            return
        self._current.firefox_attempts += 1
        self._browser_traffic_parts.append(dict(getattr(self._current.adapter, "last_browser_traffic", None) or {}))
        self._current.firefox_verification = "succeeded" if succeeded else "failed"
        if not succeeded:
            self._current.health = "quarantined"
            self._current.quarantine_reason = self._current.quarantine_reason or "firefox_verification_failed"
            self._current.adapter.close()
            return
        self._current.health = "healthy"
        self._current.quarantine_reason = None

    def record_outcome(self, outcome: str, asin: str) -> None:
        asin_key = str(asin or "").strip().upper() or f"unknown:{self._action_generation}"
        if asin_key not in self._breaker_counted_asins:
            self._breaker_counted_asins.add(asin_key)
            if outcome == "blocked":
                self._consecutive_blocks += 1
                self._block_window.append(True)
                if self._consecutive_blocks >= self.consecutive_limit:
                    self.circuit_open_reason = "consecutive_blocked_asins"
                elif sum(self._block_window) >= self.window_block_limit:
                    self.circuit_open_reason = "rolling_blocked_asin_limit"
            else:
                self._consecutive_blocks = 0
                self._block_window.append(False)
        if self._current is None or self._current.health == "quarantined":
            return
        if outcome == "completed":
            self._current.completed += 1
        elif outcome == "variant_redirect":
            self._current.variant_redirect += 1
        else:
            self._current.failed += 1

    def drain_intermediate_attempts(self) -> list[dict[str, Any]]:
        attempts, self._intermediate = self._intermediate, []
        return attempts

    def evidence_context(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "product_session_scope": self.product_scope,
            "product_asins_per_session": 1 if self.product_scope == "per_asin" else self.max_asins,
            "review_session_scope": "same_asin_sticky",
            "current_session_id": self._current.session_id if self._current else None,
            "circuit_open_reason": self.circuit_open_reason,
            "unrequested_count": self.unrequested_count,
            "attempts": list(self._persisted_attempts),
            "sessions": [slot.public(self.mode) for slot in self._slots],
        }

    def note_unrequested(self, count: int) -> None:
        self.unrequested_count = max(self.unrequested_count, max(0, int(count)))

    def can_claim_new_asin(self) -> bool:
        if self.product_scope == "per_asin":
            required = 1 + self.retry_per_asin
            return bool(self.circuit_open_reason is None and len(self._ports) - self._next_port >= required)
        return bool(
            self.circuit_open_reason is None
            and (
                self._current is None
                or (self._current.health == "healthy" and len(self._current.asins) < self.max_asins)
                or self._next_port < len(self._ports)
            )
        )

    def add_attempt_evidence(self, attempts: list[dict[str, Any]]) -> None:
        self._persisted_attempts.extend(dict(item) for item in attempts)

    def fetch_browser(self, *args: Any, **kwargs: Any) -> Any:
        if self._current is None:
            raise ProxyCircuitOpen("proxy session is not initialized")
        return self._current.adapter.fetch_browser(*args, **kwargs)

    def browser_fallback_available(self) -> bool:
        return self._current is not None and callable(getattr(self._current.adapter, "fetch_browser", None))

    def commit_browser_context(self, *args: Any, **kwargs: Any) -> Any:
        if self._current is None:
            return 0
        return self._current.adapter.commit_browser_context(*args, **kwargs)

    def close(self) -> None:
        for slot in getattr(self, "_slots", []):
            try:
                slot.adapter.close()
            except Exception:
                pass

    @property
    def action_http_transfer_bytes(self) -> int:
        return self._action_http_bytes

    @property
    def last_transfer_bytes(self) -> int | None:
        if self._current is not None and getattr(self._current.adapter, "source_type", "http_html") != "http_html":
            return getattr(self._current.adapter, "last_transfer_bytes", None)
        return self._last_transfer_bytes

    @property
    def last_browser_traffic(self) -> dict[str, Any] | None:
        if not self._browser_traffic_parts:
            if self._current is None:
                return None
            return dict(getattr(self._current.adapter, "last_browser_traffic", None) or {})
        result: dict[str, Any] = {}
        for prefix in ("main_document", "subresource"):
            byte_key = f"{prefix}_bytes"
            known_key = f"{prefix}_known_count"
            unknown_key = f"{prefix}_unknown_count"
            known_bytes = 0
            known_count = 0
            unknown_count = 0
            for part in self._browser_traffic_parts:
                value = part.get(byte_key)
                known_count += int(part.get(known_key) or (1 if value is not None else 0))
                unknown_count += int(part.get(unknown_key) or (1 if value is None else 0))
                if value is not None:
                    known_bytes += max(0, int(value))
            result[byte_key] = None if unknown_count else known_bytes
            result[known_key] = known_count
            result[unknown_key] = unknown_count
        blocked_counts: dict[str, int] = {}
        for part in self._browser_traffic_parts:
            for name, count in dict(part.get("blocked_resource_counts") or {}).items():
                blocked_counts[str(name)] = blocked_counts.get(str(name), 0) + int(count)
        result["blocked_resource_counts"] = blocked_counts
        result["blocked_request_race_count"] = sum(int(part.get("blocked_request_race_count") or 0) for part in self._browser_traffic_parts)
        result["continued_request_race_count"] = sum(int(part.get("continued_request_race_count") or 0) for part in self._browser_traffic_parts)
        return result

    @property
    def source_type(self) -> str:
        return str(getattr(self._current.adapter, "source_type", "http_html")) if self._current else "http_html"

    def __getattr__(self, name: str) -> Any:
        current = self.__dict__.get("_current")
        if current is None:
            raise AttributeError(name)
        return getattr(current.adapter, name)


__all__ = ["ProxyCircuitOpen", "ProxySessionPool"]
