"""Approved egress registry with one-shot, auditable failover.

This module never discovers, scores, or endlessly rotates public proxies. The
caller must provide a small, approved list of explicit HTTP(S) egresses.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from threading import Lock
from typing import Callable
from urllib.parse import urlsplit


@dataclass
class ApprovedEgress:
    egress_id: str
    proxy_url: str
    enabled: bool = True
    blocked_until: float = 0.0
    block_count: int = 0
    last_used_at: float = 0.0
    _clock: Callable[[], float] = field(default=time.monotonic, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not self.egress_id.strip():
            raise ValueError("egress_id must not be empty")
        parts = urlsplit(self.proxy_url)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            raise ValueError("proxy_url must be an explicit http(s) URL")
        if parts.username or parts.password:
            raise ValueError("proxy credentials must not be embedded in proxy_url")

    @property
    def available(self) -> bool:
        return self.enabled and self._clock() >= self.blocked_until

    def mark_blocked(self, cooldown_seconds: float) -> None:
        if cooldown_seconds < 0:
            raise ValueError("cooldown_seconds must be >= 0")
        self.block_count += 1
        self.blocked_until = self._clock() + cooldown_seconds

    def mark_healthy(self) -> None:
        self.blocked_until = 0.0


class ApprovedEgressPool:
    """Deterministic pool with at most one automatic failover per run."""

    def __init__(self, egresses: list[ApprovedEgress], *, max_failovers_per_run: int = 1) -> None:
        if not egresses:
            raise ValueError("at least one approved egress is required")
        if max_failovers_per_run < 0:
            raise ValueError("max_failovers_per_run must be >= 0")
        ids = [item.egress_id for item in egresses]
        if len(ids) != len(set(ids)):
            raise ValueError("egress_id values must be unique")
        self.egresses = egresses
        self.max_failovers_per_run = max_failovers_per_run
        self.failovers_used = 0
        self._lock = Lock()

    def start_run(self) -> None:
        with self._lock:
            self.failovers_used = 0

    def get(self, egress_id: str) -> ApprovedEgress:
        for egress in self.egresses:
            if egress.egress_id == egress_id:
                return egress
        raise KeyError(f"unknown egress_id: {egress_id}")

    def choose(self, current_id: str | None = None) -> ApprovedEgress | None:
        """Keep a healthy current egress; otherwise perform bounded failover."""
        with self._lock:
            if current_id is not None:
                current = self.get(current_id)
                if current.available:
                    current.last_used_at = current._clock()
                    return current
                if self.failovers_used >= self.max_failovers_per_run:
                    return None
            for egress in self.egresses:
                if egress.egress_id == current_id or not egress.available:
                    continue
                if current_id is not None:
                    self.failovers_used += 1
                egress.last_used_at = egress._clock()
                return egress
            return None

    def mark_blocked(self, egress_id: str, cooldown_seconds: float) -> None:
        self.get(egress_id).mark_blocked(cooldown_seconds)

    def mark_healthy(self, egress_id: str) -> None:
        self.get(egress_id).mark_healthy()

    def summary(self) -> list[dict[str, object]]:
        """Return audit-safe status without exposing proxy URLs."""
        return [
            {
                "egress_id": egress.egress_id,
                "enabled": egress.enabled,
                "available": egress.available,
                "block_count": egress.block_count,
                "last_used_at": egress.last_used_at,
            }
            for egress in self.egresses
        ]
