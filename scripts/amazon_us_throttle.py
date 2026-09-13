"""Small, testable primitives for bounded crawler concurrency and rate limits."""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from itertools import islice
from threading import Lock
from typing import Callable, Iterable, TypeVar

T = TypeVar("T")
R = TypeVar("R")


class TokenBucket:
    """Thread-safe token bucket. A non-positive rate disables waiting."""

    def __init__(self, rate_per_second: float, capacity: int = 1, *, clock: Callable[[], float] = time.monotonic, sleeper: Callable[[float], None] = time.sleep) -> None:
        if rate_per_second < 0:
            raise ValueError("rate_per_second must be >= 0")
        if capacity < 1:
            raise ValueError("capacity must be >= 1")
        self.rate = float(rate_per_second)
        self.capacity = float(capacity)
        self.tokens = self.capacity
        self.clock = clock
        self.sleeper = sleeper
        self.updated_at = clock()
        self.lock = Lock()

    def acquire(self, tokens: float = 1.0, timeout: float | None = None) -> float:
        if tokens <= 0:
            raise ValueError("tokens must be > 0")
        if self.rate <= 0:
            return 0.0
        if tokens > self.capacity:
            raise ValueError("tokens cannot exceed bucket capacity")
        waited = 0.0
        deadline = None if timeout is None else self.clock() + max(0.0, timeout)
        while True:
            with self.lock:
                now = self.clock()
                self.tokens = min(self.capacity, self.tokens + (now - self.updated_at) * self.rate)
                self.updated_at = now
                if self.tokens >= tokens:
                    self.tokens -= tokens
                    return waited
                delay = (tokens - self.tokens) / self.rate
            if deadline is not None and self.clock() + delay > deadline:
                raise TimeoutError("token bucket acquire timed out")
            self.sleeper(delay)
            waited += delay


class EgressLimiter:
    """Apply one global and one independent bucket per approved egress."""

    def __init__(self, global_rate_per_second: float = 0.0, egress_rate_per_second: float = 0.0, burst: int = 1) -> None:
        self.global_bucket = TokenBucket(global_rate_per_second, burst)
        self.egress_rate = float(egress_rate_per_second)
        self.burst = int(burst)
        self._buckets: dict[str, TokenBucket] = {}
        self._lock = Lock()

    def _egress_bucket(self, egress_id: str) -> TokenBucket:
        with self._lock:
            return self._buckets.setdefault(egress_id, TokenBucket(self.egress_rate, self.burst))

    def acquire(self, egress_id: str = "direct", timeout: float | None = None) -> float:
        waited = self.global_bucket.acquire(timeout=timeout)
        waited += self._egress_bucket(egress_id).acquire(timeout=timeout)
        return waited


@dataclass(frozen=True)
class WorkerPoolConfig:
    """Explicit concurrency limits; browser workers should be configured lower."""

    http_workers: int = 1
    browser_workers: int = 1
    max_inflight: int = 1

    def __post_init__(self) -> None:
        if self.http_workers < 1 or self.browser_workers < 1 or self.max_inflight < 1:
            raise ValueError("worker counts must be >= 1")
        if self.max_inflight > self.http_workers + self.browser_workers:
            raise ValueError("max_inflight cannot exceed total workers")


class WorkerPool:
    """Bounded thread pool for independent fetch calls.

    It deliberately does not rotate proxies or share browser sessions. The
    caller owns durable database writes and should keep them transactional.
    """

    def __init__(self, workers: int, limiter: EgressLimiter | None = None, max_inflight: int | None = None) -> None:
        if workers < 1:
            raise ValueError("workers must be >= 1")
        if max_inflight is not None and max_inflight < 1:
            raise ValueError("max_inflight must be >= 1")
        self.workers = workers
        self.limiter = limiter
        self.max_inflight = min(workers, max_inflight or workers)

    def map(self, items: Iterable[T], fn: Callable[[T], R], *, egress_id: str = "direct") -> list[R]:
        def invoke(item: T) -> R:
            if self.limiter is not None:
                self.limiter.acquire(egress_id)
            return fn(item)

        results: list[R] = []
        iterator = iter(items)
        with ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix="amazon-fetch") as executor:
            while batch := list(islice(iterator, self.max_inflight)):
                results.extend(executor.map(invoke, batch))
        return results
