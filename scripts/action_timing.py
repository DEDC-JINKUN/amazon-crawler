"""Action-scoped monotonic timing with a bounded evidence vocabulary."""
from __future__ import annotations

from contextlib import contextmanager
import threading
import time
from typing import Callable, Iterator
import uuid


STAGES = frozenset(
    {
        "proxy_session_select",
        "http_connect_headers",
        "http_body",
        "http_total",
        "firefox_start",
        "firefox_navigation",
        "firefox_delivery_context",
        "firefox_capture",
        "firefox_total",
        "parse",
    }
)


class ActionTimingRecorder:
    """Collect safe stage durations for one collection action.

    Total stages intentionally overlap their child stages. The snapshot says so
    explicitly to prevent callers from summing every stage as wall-clock time.
    """

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        with self._lock:
            self._action_id = uuid.uuid4().hex
            self._started_at = self._clock()
            self._stages: dict[str, list[float | int]] = {}

    @contextmanager
    def measure(self, stage: str) -> Iterator[None]:
        if stage not in STAGES:
            raise ValueError(f"unknown action timing stage: {stage}")
        started = self._clock()
        try:
            yield
        finally:
            elapsed_ms = max(0.0, (self._clock() - started) * 1000.0)
            with self._lock:
                aggregate = self._stages.setdefault(stage, [0.0, 0])
                aggregate[0] = float(aggregate[0]) + elapsed_ms
                aggregate[1] = int(aggregate[1]) + 1

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            elapsed_ms = max(0.0, (self._clock() - self._started_at) * 1000.0)
            stages = {
                name: {"elapsed_ms": round(float(values[0]), 3), "count": int(values[1])}
                for name, values in self._stages.items()
            }
        return {
            "schema_version": "amazon-action-timing-v1",
            "action_id": self._action_id,
            "clock": "monotonic",
            "measured_elapsed_ms": round(elapsed_ms, 3),
            "stages": stages,
            "overlapping_totals": True,
            "not_measured": ["storage"],
        }


__all__ = ["ActionTimingRecorder", "STAGES"]
