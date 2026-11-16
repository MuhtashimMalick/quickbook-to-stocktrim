"""Transport for the StockTrim API: auth headers, rate limit, retry, bulk helper. No business logic, no QuickBooks imports."""
import logging
import math
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, Any, Callable, Iterable, Optional, TypeVar

import httpx
from tenacity import RetryCallState, RetryError, Retrying, retry_if_exception_type

if TYPE_CHECKING:  # type-only: keeps this package free of runtime imports from the rest of the app
    from ..config import Settings

from .errors import (
    StockTrimAborted,
    StockTrimAuthError,
    StockTrimBulkError,
    StockTrimConfigError,
    StockTrimRejected,
    StockTrimUnavailable,
)

log = logging.getLogger("app.stocktrim")
T, R = TypeVar("T"), TypeVar("R")
TIMEOUT = httpx.Timeout(30.0, connect=10.0)
MAX_ATTEMPTS = 5
MAX_CALL_SECONDS = 90.0  # wall-clock cap for one logical call (all attempts + waits)
MAX_RETRY_AFTER = 60.0
CREDS_MISSING = "StockTrim credentials are not configured (STOCKTRIM_AUTH_ID / STOCKTRIM_AUTH_SIGNATURE)"
_HINT = " (a 500 from StockTrim can also mean STOCKTRIM_AUTH_ID is wrong)"
_NEVER_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)  # the request cannot have reached the server
_FATAL = (StockTrimUnavailable, StockTrimConfigError, StockTrimAuthError)  # a bulk run stops on these


def read_only_guard(request: httpx.Request) -> None:
    """httpx request hook for read-only callers (the pre-flight): anything but GET is refused before it leaves the machine."""
    if request.method != "GET":
        raise RuntimeError(f"read-only StockTrim client: refusing {request.method} {request.url.path}")


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


class _Transient(Exception):
    def __init__(self, reason: str, retry_after: float = 0.0):
        self.reason, self.retry_after = reason, retry_after
        super().__init__(reason)


def _retry_after(resp: httpx.Response, now: Optional[datetime] = None) -> float:
    """Retry-After as seconds or as an HTTP-date; garbage/non-finite -> 0 (use the backoff); clamped to [0, MAX_RETRY_AFTER]."""
    v = resp.headers.get("Retry-After")
    if not v:
        return 0.0
    try:
        secs = float(v)
    except ValueError:
        try:
            when = parsedate_to_datetime(v)
        except (TypeError, ValueError):
            return 0.0
        secs = ((when if when.tzinfo else when.replace(tzinfo=timezone.utc)) - (now or datetime.now(timezone.utc))).total_seconds()
    return min(max(secs, 0.0), MAX_RETRY_AFTER) if math.isfinite(secs) else 0.0


def _backoff(rs: RetryCallState) -> float:
    exc = rs.outcome.exception() if rs.outcome else None
    expo = min(1.0 * 2 ** (rs.attempt_number - 1), 30.0) * random.uniform(0.5, 1.0)  # exponential + jitter
    return max(expo, exc.retry_after if isinstance(exc, _Transient) else 0.0)


@dataclass
class BulkItem:
    item: Any
    result: Any = None
    error: Optional[Exception] = None

    @property
    def ok(self) -> bool:
        return self.error is None


class StockTrimClient:
    def __init__(self, base_url: str, auth_id: str, auth_signature: str, *, max_rps: float = 5, concurrency: int = 4,
                 transport: Optional[httpx.BaseTransport] = None, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep, max_attempts: int = MAX_ATTEMPTS,
                 max_call_seconds: float = MAX_CALL_SECONDS, event_hooks: Optional[dict[str, list[Callable]]] = None):
        if not (auth_id and auth_signature):
            raise StockTrimConfigError(CREDS_MISSING)
        self._http = httpx.Client(base_url=base_url, timeout=TIMEOUT, transport=transport,
                                  event_hooks=event_hooks, headers={"api-auth-id": auth_id, "api-auth-signature": auth_signature, "Accept": "application/json"})
        self._limiter, self._sleep, self.concurrency, self._max_attempts = RateLimiter(max_rps, clock, sleep), sleep, concurrency, max_attempts
        self._clock, self._max_call_seconds = clock, max_call_seconds

    @classmethod
    def from_settings(cls, s: "Settings", **kw: Any) -> "StockTrimClient":
        if not (s.stocktrim_auth_id and s.stocktrim_auth_signature):
            raise StockTrimConfigError(CREDS_MISSING)
        return cls(s.stocktrim_base_url, s.stocktrim_auth_id.get_secret_value(), s.stocktrim_auth_signature.get_secret_value(),
                   max_rps=s.stocktrim_max_rps, concurrency=s.stocktrim_concurrency,
                   max_call_seconds=s.stocktrim_max_call_seconds, **kw)

    def close(self) -> None:
        self._http.close()

    def request(self, method: str, path: str, *, params: Optional[dict[str, Any]] = None, json: Any = None,
                identifier: Optional[str] = None, idempotent: bool = True) -> httpx.Response:
        """One logical call. Its wall-clock time is bounded by max_call_seconds: no retry sleep is started that would cross the cap and
        every attempt's timeout is clipped to the time left (limiter waits before an attempt are the only unbounded part: they are
        short, 1/max_rps per queued caller). Retries transport errors, 429 and 5xx; 401/403 raise StockTrimAuthError
        and any other 4xx StockTrimRejected at once. idempotent=False retries only what cannot have reached the server
        (connect errors, 429); anything else raises StockTrimUnavailable immediately. A DELETE that answers 404 after an
        earlier transient failure of the same call counts as success (that attempt probably did the delete)."""
        attempt, maybe_processed, saw_5xx = 0, False, False
        params = {k: v for k, v in (params or {}).items() if v is not None}
        t0 = self._clock()

        def hint() -> str:  # only a 5xx can be the "wrong STOCKTRIM_AUTH_ID" symptom, not a timeout
            return _HINT if saw_5xx else ""

        def once() -> httpx.Response:
            nonlocal attempt, maybe_processed, saw_5xx
            attempt += 1
            self._limiter.acquire()
            t = time.monotonic()
            left = max(self._max_call_seconds - (self._clock() - t0), 0.01)  # hard cap: no attempt may outlive the call budget
            timeout = httpx.Timeout(min(TIMEOUT.read, left), connect=min(TIMEOUT.connect, left))
            try:
                resp = self._http.request(method, path, params=params, json=json, timeout=timeout)
            except httpx.TransportError as e:
                log.warning("stocktrim %s %s transport error=%s attempt=%d", method, path, type(e).__name__, attempt)
                if not isinstance(e, _NEVER_SENT):
                    maybe_processed = True
                    if not idempotent:
                        raise StockTrimUnavailable(f"StockTrim call may have been processed ({type(e).__name__}); not retried: {method} {path}{hint()}", identifier) from e
                raise _Transient(type(e).__name__) from e
            code = resp.status_code
            log.info("stocktrim %s %s status=%d ms=%d attempt=%d", method, path, code, (time.monotonic() - t) * 1000, attempt)
            if code >= 500:
                maybe_processed = saw_5xx = True
                if not idempotent:
                    raise StockTrimUnavailable(f"StockTrim answered HTTP {code}; not retried (non-idempotent): {method} {path}{hint()}", identifier)
            if code == 429 or code >= 500:
                raise _Transient(f"HTTP {code}", _retry_after(resp))
            if method == "DELETE" and code == 404 and maybe_processed:
                log.info("stocktrim DELETE %s already deleted (404 after a transient failure)", path)
                return resp
            if code in (401, 403):
                raise StockTrimAuthError(code, resp.text, identifier)
            if code >= 400:
                raise StockTrimRejected(code, resp.text, identifier)
            return resp

        def stop(rs: RetryCallState) -> bool:
            # tenacity computes the wait before calling stop, so rs.upcoming_sleep is the sleep that would follow this attempt:
            # never start a sleep that would cross the cap
            return rs.attempt_number >= self._max_attempts or self._clock() - t0 + rs.upcoming_sleep >= self._max_call_seconds

        try:
            return Retrying(stop=stop, wait=_backoff, retry=retry_if_exception_type(_Transient), sleep=self._sleep)(once)
        except RetryError as e:
            reason = e.last_attempt.exception()
            raise StockTrimUnavailable(f"StockTrim unavailable after {attempt} attempts ({reason}): {method} {path}{hint()}", identifier) from reason

    def map_concurrent(self, fn: Callable[[T], R], items: Iterable[T]) -> list[BulkItem]:
        """Run fn over items on a thread pool; per-item exceptions are kept on their BulkItem (input order).
        A fatal error (StockTrimUnavailable / ConfigError / AuthError) stops the run: items not yet started are marked
        StockTrimAborted and fn is not called for them; after draining, the fatal error is re-raised with `.partial_results`.
        Calls already running when the fatal error happens are not interrupted and keep retrying (bounded by concurrency x attempts)."""
        stop = threading.Event()

        def one(item: T) -> BulkItem:
            if stop.is_set():
                return BulkItem(item, error=StockTrimAborted("not attempted: an earlier call failed fatally"))
            try:
                return BulkItem(item, result=fn(item))
            except _FATAL as e:
                stop.set()
                return BulkItem(item, error=e)
            except Exception as e:  # noqa: BLE001 - recorded, not swallowed: caller must inspect .error / use raise_on_failures
                log.warning("stocktrim bulk item failed: %s", type(e).__name__)
                return BulkItem(item, error=e)

        ex = ThreadPoolExecutor(max_workers=self.concurrency)
        try:
            results = [f.result() for f in [ex.submit(one, i) for i in items]]
        finally:  # also on KeyboardInterrupt/SystemExit: queued work is cancelled, running calls finish
            stop.set()
            ex.shutdown(wait=True, cancel_futures=True)
        if fatal := next((r.error for r in results if isinstance(r.error, _FATAL)), None):
            fatal.partial_results = results
            raise fatal
        return results


def raise_on_failures(results: list[BulkItem]) -> None:
    """Raise StockTrimBulkError (carrying every failed item) if any item failed."""
    if failed := [r for r in results if not r.ok]:
        raise StockTrimBulkError(failed, len(results))
