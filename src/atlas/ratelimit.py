"""Per-minute request limits for the authenticated API.

Each user's access to a workspace through a session or an API key gets the plan's
requests_per_minute. Two stores:

- "memory" (default): a token bucket per process. Exact and free, but each API replica counts
  on its own, so N replicas allow N times the limit.
- "database" (ATLAS_RATE_LIMIT_STORE=database): a sliding-window count in the shared database,
  so every replica sees the same total. It costs one or two small writes per request; rejected
  requests are not counted, so a client that keeps retrying gets back in once its rate drops.
"""

from __future__ import annotations

import math
import os
import random
import threading
import time
from dataclasses import dataclass

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError

_lock = threading.Lock()
_buckets: dict[str, "Bucket"] = {}
_MAX_BUCKETS = 50_000
clock = time.monotonic  # tests replace this
wall_clock = time.time  # the database store needs a clock every replica agrees on


@dataclass
class Bucket:
    tokens: float
    updated: float


def store() -> str:
    return "database" if os.getenv("ATLAS_RATE_LIMIT_STORE", "memory").lower() == "database" else "memory"


def take(key: str, per_minute: int) -> tuple[bool, int, int]:
    """Consume one request. Returns (allowed, remaining, seconds until a request would be allowed)."""
    return _take_db(key, per_minute) if store() == "database" else _take_memory(key, per_minute)


def _take_memory(key: str, per_minute: int) -> tuple[bool, int, int]:
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


def _take_db(key: str, per_minute: int) -> tuple[bool, int, int]:
    """Sliding window: this minute's count plus the previous minute's, weighted by how much of it still overlaps."""
    from atlas.data.db import RateLimitWindow, session_scope

    now = wall_clock()
    window, into = int(now // 60), (now % 60) / 60
    with session_scope() as s:
        bumped = s.execute(update(RateLimitWindow).where(RateLimitWindow.key == key, RateLimitWindow.window == window)
                           .values(count=RateLimitWindow.count + 1)).rowcount  # type: ignore[attr-defined]
        if not bumped:
            try:
                with s.begin_nested():
                    s.add(RateLimitWindow(key=key, window=window, count=1))
            except IntegrityError:  # another replica created it first
                s.execute(update(RateLimitWindow).where(RateLimitWindow.key == key, RateLimitWindow.window == window)
                          .values(count=RateLimitWindow.count + 1))
            if random.random() < 0.01:
                s.execute(delete(RateLimitWindow).where(RateLimitWindow.window < window - 1))
        current = s.scalar(select(RateLimitWindow.count).where(RateLimitWindow.key == key, RateLimitWindow.window == window)) or 0
        previous = s.scalar(select(RateLimitWindow.count).where(RateLimitWindow.key == key, RateLimitWindow.window == window - 1)) or 0
        used = previous * (1 - into) + current
        if used <= per_minute:
            return True, max(0, int(per_minute - used)), 0
        s.execute(update(RateLimitWindow).where(RateLimitWindow.key == key, RateLimitWindow.window == window)
                  .values(count=RateLimitWindow.count - 1))
    # Wait until the previous minute's weight has fallen enough, or the next minute if this one alone is full.
    over = used - per_minute
    wait = 60 * over / previous if previous else 60 * (1 - into)
    return False, 0, max(1, min(60, math.ceil(wait)))


def reset() -> None:
    with _lock:
        _buckets.clear()
    if store() == "database":
        from atlas.data.db import RateLimitWindow, session_scope

        with session_scope() as s:
            s.execute(delete(RateLimitWindow))
