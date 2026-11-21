"""In-process sliding-window rate limiter for the v1 API.

Single-process by design: counters live in memory and reset on restart. The
number of tracked keys is capped. When the cap is reached the least recently
used key is evicted, so a flood of distinct keys cannot grow memory without bound.
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict, deque
from typing import Deque, Optional, Tuple

DEFAULT_MAX_KEYS = 10_000


class SlidingWindowLimiter:
    def __init__(self, window_seconds: float = 60.0, max_keys: int = DEFAULT_MAX_KEYS) -> None:
        self._window = float(window_seconds)
        self._max_keys = max(1, int(max_keys))
        self._hits: "OrderedDict[str, Deque[float]]" = OrderedDict()
        self._lock = threading.Lock()

    def check(self, key: str, limit: int, now: Optional[float] = None) -> Tuple[bool, int]:
        """Record one request for ``key``. Returns ``(allowed, retry_after_seconds)``.

        A ``limit`` of zero or less disables the limiter for that key.
        """
        if limit <= 0:
            return True, 0
        current = time.monotonic() if now is None else float(now)
        cutoff = current - self._window
        with self._lock:
            bucket = self._hits.get(key)
            if bucket is None:
                bucket = deque()
                self._hits[key] = bucket
                self._evict(cutoff, keep=key)
            else:
                self._hits.move_to_end(key)
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            if len(bucket) >= limit:
                retry_after = int(bucket[0] + self._window - current) + 1
                return False, max(1, retry_after)
            bucket.append(current)
            return True, 0

    def tracked_keys(self) -> int:
        with self._lock:
            return len(self._hits)

    def _evict(self, cutoff: float, keep: str) -> None:
        if len(self._hits) <= self._max_keys:
            return
        for key in list(self._hits.keys()):
            if key == keep:
                continue
            bucket = self._hits[key]
            if not bucket or bucket[-1] <= cutoff:
                del self._hits[key]
            if len(self._hits) <= self._max_keys:
                return
        for key in list(self._hits.keys()):
            if key == keep:
                continue
            del self._hits[key]
            if len(self._hits) <= self._max_keys:
                return
