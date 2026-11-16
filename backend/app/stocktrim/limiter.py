"""Min-interval rate limiter, shared process-wide per StockTrim base URL so several clients never exceed STOCKTRIM_MAX_RPS together."""
import logging
import threading
import time
from typing import Callable

import httpx

log = logging.getLogger("app.stocktrim")


class RateLimiter:
    """Thread-safe min-interval limiter: each caller reserves the next free slot, then sleeps outside the lock."""

    def __init__(self, rps: float, clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep):
        self.interval, self._clock, self._sleep = 1.0 / rps, clock, sleep
        self._next, self._lock = 0.0, threading.Lock()

    def acquire(self) -> None:
        with self._lock:
            now = self._clock()
            slot = max(now, self._next)
            self._next = slot + self.interval
        if slot > now:
            self._sleep(slot - now)

    def defer(self, seconds: float) -> None:
        """Push the next free slot to at least `seconds` from now (Stage 9 calls this on a 429 Retry-After)."""
        with self._lock:
            self._next = max(self._next, self._clock() + seconds)


_shared: dict[str, RateLimiter] = {}
_shared_lock = threading.Lock()


def shared_limiter(base_url: str, rps: float) -> RateLimiter:
    """The one limiter for this base URL (key: lowercase scheme/host, no trailing slash). A later, lower rps tightens it (never
    loosens, one WARNING per tightening): the strictest request wins."""
    key = str(httpx.URL(base_url)).rstrip("/")
    with _shared_lock:
        lim = _shared.get(key)
        if lim is None:
            lim = _shared[key] = RateLimiter(rps)
        elif 1.0 / rps > lim.interval:
            log.warning("stocktrim rate limit for %s tightened from %.3g to %.3g rps", key, 1.0 / lim.interval, rps)
            lim.interval = 1.0 / rps
        return lim


def reset_shared_limiters() -> None:
    """Tests only."""
    with _shared_lock:
        _shared.clear()
