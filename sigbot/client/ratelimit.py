"""
sigbot.client.ratelimit
-----------------------
Thread-safe Token Bucket Rate Limiter enforcing platform budgets:
100 reads + 30 writes per minute across all keys.

Per guidelines:
- Never exceed ~80% of the platform limit (80 reads/min, 24 writes/min by default).
- Shared across all threads and clients in the process.
- Burst-tolerant up to bucket capacity with continuous refill.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Literal

log = logging.getLogger("sigbot.ratelimit")

OperationKind = Literal["read", "write"]


class TokenBucket:
    """A single token bucket supporting burst and smooth continuous refill."""

    def __init__(
        self,
        capacity: float,
        refill_rate_per_sec: float,
        initial_tokens: float | None = None,
    ):
        self.capacity = float(capacity)
        self.refill_rate = float(refill_rate_per_sec)
        self.tokens = float(capacity if initial_tokens is None else initial_tokens)
        self.last_update = time.monotonic()
        self.lock = threading.Lock()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self.last_update
        if elapsed > 0:
            self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_rate)
            self.last_update = now

    def acquire(self, n: float = 1.0, timeout: float | None = None) -> bool:
        """Acquire `n` tokens.

        If timeout is None: blocks until tokens are available.
        If timeout == 0: returns immediately with True if available, False otherwise.
        If timeout > 0: waits up to `timeout` seconds before returning False.
        """
        deadline = (time.monotonic() + timeout) if timeout is not None else None

        while True:
            with self.lock:
                self._refill()
                if self.tokens >= n:
                    self.tokens -= n
                    return True

                needed = n - self.tokens
                wait_time = needed / self.refill_rate if self.refill_rate > 0 else 1.0

            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                wait_time = min(wait_time, remaining)

            # Sleep slightly more than required to avoid busy-wait looping
            time.sleep(min(wait_time + 0.005, 1.0))

    def get_tokens(self) -> float:
        with self.lock:
            self._refill()
            return self.tokens


class PlatformRateLimiter:
    """Global platform rate limiter tracking both Reads and Writes.

    Enforces platform budget:
      - Reads: 100/min standard
      - Writes: 30/min standard
    With safety target utilization (default 80% = 80 reads/min, 24 writes/min).
    """

    def __init__(
        self,
        max_reads_per_min: float = 100.0,
        max_writes_per_min: float = 30.0,
        target_utilization: float = 0.80,
    ):
        self.target_utilization = target_utilization
        self.effective_reads_per_min = max_reads_per_min * target_utilization
        self.effective_writes_per_min = max_writes_per_min * target_utilization

        self.read_bucket = TokenBucket(
            capacity=self.effective_reads_per_min,
            refill_rate_per_sec=self.effective_reads_per_min / 60.0,
        )
        self.write_bucket = TokenBucket(
            capacity=self.effective_writes_per_min,
            refill_rate_per_sec=self.effective_writes_per_min / 60.0,
        )

        self._reads_count = 0
        self._writes_count = 0
        self._stats_lock = threading.Lock()

    def acquire(self, kind: OperationKind, n: int = 1, timeout: float | None = None) -> bool:
        """Acquire `n` tokens for either 'read' or 'write'."""
        if kind == "read":
            ok = self.read_bucket.acquire(n, timeout=timeout)
            if ok:
                with self._stats_lock:
                    self._reads_count += n
            return ok
        elif kind == "write":
            ok = self.write_bucket.acquire(n, timeout=timeout)
            if ok:
                with self._stats_lock:
                    self._writes_count += n
            return ok
        else:
            raise ValueError(f"Unknown operation kind: {kind}")

    def status(self) -> dict:
        """Current limiter state and diagnostics."""
        with self._stats_lock:
            return {
                "target_utilization": self.target_utilization,
                "read_tokens_available": round(self.read_bucket.get_tokens(), 2),
                "write_tokens_available": round(self.write_bucket.get_tokens(), 2),
                "read_capacity": self.effective_reads_per_min,
                "write_capacity": self.effective_writes_per_min,
                "total_reads_acquired": self._reads_count,
                "total_writes_acquired": self._writes_count,
            }


_GLOBAL_LIMITER: PlatformRateLimiter | None = None
_GLOBAL_LOCK = threading.Lock()


def get_rate_limiter() -> PlatformRateLimiter:
    """Return the shared process-wide rate limiter singleton."""
    global _GLOBAL_LIMITER
    with _GLOBAL_LOCK:
        if _GLOBAL_LIMITER is None:
            _GLOBAL_LIMITER = PlatformRateLimiter()
        return _GLOBAL_LIMITER


def reset_rate_limiter(target_utilization: float = 0.80) -> PlatformRateLimiter:
    """Reset or reconfigure the shared process-wide rate limiter (useful for tests)."""
    global _GLOBAL_LIMITER
    with _GLOBAL_LOCK:
        _GLOBAL_LIMITER = PlatformRateLimiter(target_utilization=target_utilization)
        return _GLOBAL_LIMITER
