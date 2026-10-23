"""Per-minute request limits for the authenticated API (token bucket, in process).

Each user's access to a workspace through a session or an API key gets a bucket that holds the
plan's requests_per_minute and refills continuously. One API process keeps its own buckets; when
running several API replicas, put a shared limiter (e.g. at the load balancer or in Redis) in front.
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass

_lock = threading.Lock()
_buckets: dict[str, "Bucket"] = {}
_MAX_BUCKETS = 50_000
clock = time.monotonic  # tests replace this


@dataclass
class Bucket:
    tokens: float
    updated: float


def take(key: str, per_minute: int) -> tuple[bool, int, int]:
    """Consume one request. Returns (allowed, remaining, seconds until the next token)."""
    rate = per_minute / 60.0
    now = clock()
    with _lock:
        b = _buckets.get(key)
        if b is None:
            if len(_buckets) >= _MAX_BUCKETS:  # drop idle buckets (full ones carry no state worth keeping)
                for k in [k for k, v in _buckets.items() if now - v.updated > 120][: _MAX_BUCKETS // 2]:
                    del _buckets[k]
            b = _buckets[key] = Bucket(tokens=float(per_minute), updated=now)
        b.tokens = min(float(per_minute), b.tokens + (now - b.updated) * rate)
        b.updated = now
        if b.tokens >= 1:
            b.tokens -= 1
            return True, int(b.tokens), 0
        return False, 0, max(1, math.ceil((1 - b.tokens) / rate))


def reset() -> None:
    with _lock:
        _buckets.clear()
