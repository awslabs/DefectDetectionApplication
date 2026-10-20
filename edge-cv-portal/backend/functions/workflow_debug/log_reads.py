"""Pacing of the API's CloudWatch Logs reads (workflow-debug-mode design §10.1).

``GetLogEvents`` and ``FilterLogEvents`` draw on account-wide,
non-adjustable quotas shared with every other log reader of the Portal (Build
Fleet, device logs, plugin builds, git sync, training views). The API
therefore keeps one token bucket per container and per API, ``GetLogEvents``
at 4 calls per second and ``FilterLogEvents`` at 2 (the deployment values
``DEBUG_GET_LOG_EVENTS_PER_SECOND`` and ``DEBUG_FILTER_LOG_EVENTS_PER_SECOND``),
refilled continuously with a one-second burst. A call that finds its bucket
empty is **not** made against CloudWatch: the API answers
``429 log_read_throttled {retry_after_ms}`` (the time until the next token,
rounded up to 250 ms). A ``ThrottlingException`` that still reaches the API
maps to the same 429 with ``retry_after_ms: 3000``.
"""
import math
import os
import threading
import time
from typing import Callable, Dict, Optional

from botocore.exceptions import ClientError

__all__ = [
    "GET", "FILTER", "DEFAULT_RATES", "RETRY_STEP_MS", "THROTTLED_RETRY_MS",
    "DOWNLOAD_PAGE_SIZE", "DOWNLOAD_CALLS_PER_SECOND", "DOWNLOAD_MAX_EVENTS",
    "ReadThrottled", "TokenBucket", "ReadPacer", "pacer", "reset", "retry_after_ms",
    "take", "map_client_error", "paced_drain",
]

GET = "get"
FILTER = "filter"
DEFAULT_RATES = {GET: 4.0, FILTER: 2.0}
_ENV_NAMES = {GET: "DEBUG_GET_LOG_EVENTS_PER_SECOND", FILTER: "DEBUG_FILTER_LOG_EVENTS_PER_SECOND"}
RETRY_STEP_MS = 250
THROTTLED_RETRY_MS = 3000

_THROTTLING_CODES = frozenset({"ThrottlingException", "Throttling", "ThrottledException",
                               "TooManyRequestsException", "RequestLimitExceeded"})


class ReadThrottled(Exception):
    """The caller must wait ``retry_after_ms`` before the next read."""

    def __init__(self, retry_after_ms: int, api: str = "") -> None:
        super().__init__("log read throttled; retry after {0} ms".format(retry_after_ms))
        self.retry_after_ms = int(retry_after_ms)
        self.api = api


def retry_after_ms(seconds: float) -> int:
    """``seconds`` rounded up to the next 250 ms step, at least 250."""
    if seconds <= 0:
        return RETRY_STEP_MS
    return max(RETRY_STEP_MS, int(math.ceil(seconds * 1000.0 / RETRY_STEP_MS)) * RETRY_STEP_MS)


class TokenBucket:
    """A continuously refilled bucket of ``rate`` tokens per second with a
    one-second burst (capacity ``rate``)."""

    def __init__(self, rate: float, clock: Callable[[], float] = time.monotonic) -> None:
        if rate <= 0:
            raise ValueError("rate must be positive")
        self.rate = float(rate)
        self.capacity = float(rate)
        self._tokens = float(rate)
        self._clock = clock
        self._last = clock()
        self._lock = threading.Lock()

    def _refill(self, now: float) -> None:
        elapsed = max(0.0, now - self._last)
        self._last = now
        self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)

    def try_take(self) -> Optional[int]:
        """``None`` when a token was taken, else the ``retry_after_ms``."""
        with self._lock:
            now = self._clock()
            self._refill(now)
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return None
            wait = (1.0 - self._tokens) / self.rate
            return retry_after_ms(wait)

    @property
    def tokens(self) -> float:
        with self._lock:
            self._refill(self._clock())
            return self._tokens


def _rate_from_env(api: str) -> float:
    raw = os.environ.get(_ENV_NAMES[api])
    if raw is None or not str(raw).strip():
        return DEFAULT_RATES[api]
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_RATES[api]
    return value if value > 0 else DEFAULT_RATES[api]


class ReadPacer:
    """The container's two buckets."""

    def __init__(self, clock: Callable[[], float] = time.monotonic,
                 rates: Optional[Dict[str, float]] = None) -> None:
        rates = rates or {GET: _rate_from_env(GET), FILTER: _rate_from_env(FILTER)}
        self.buckets = {api: TokenBucket(rates[api], clock) for api in (GET, FILTER)}

    def take(self, api: str) -> None:
        """Take one token for ``api`` or raise :class:`ReadThrottled`."""
        bucket = self.buckets[api]
        wait = bucket.try_take()
        if wait is not None:
            raise ReadThrottled(wait, api)


_pacer: Optional[ReadPacer] = None
_pacer_lock = threading.Lock()


def pacer() -> ReadPacer:
    global _pacer
    with _pacer_lock:
        if _pacer is None:
            _pacer = ReadPacer()
        return _pacer


def reset(clock: Callable[[], float] = time.monotonic, rates: Optional[Dict[str, float]] = None) -> ReadPacer:
    """Replace the container's pacer (tests: a fake clock or fixed rates)."""
    global _pacer
    with _pacer_lock:
        _pacer = ReadPacer(clock, rates)
        return _pacer


def take(api: str) -> None:
    pacer().take(api)


def map_client_error(error: ClientError, api: str = "") -> Optional[ReadThrottled]:
    """A ``ThrottlingException`` from CloudWatch Logs -> ``ReadThrottled(3000)``;
    anything else -> ``None`` (the caller re-raises)."""
    code = str(((error.response or {}).get("Error") or {}).get("Code") or "")
    if code in _THROTTLING_CODES:
        return ReadThrottled(THROTTLED_RETRY_MS, api)
    return None


DOWNLOAD_PAGE_SIZE = 10_000
DOWNLOAD_CALLS_PER_SECOND = 2.0
DOWNLOAD_MAX_EVENTS = 50_000


def paced_drain(fetch_page, *, limit: int = DOWNLOAD_PAGE_SIZE,
                calls_per_second: float = DOWNLOAD_CALLS_PER_SECOND,
                max_events: int = DOWNLOAD_MAX_EVENTS, api: str = GET,
                clock: Callable[[], float] = time.monotonic,
                sleep: Callable[[float], None] = time.sleep,
                pacer_obj: Optional[ReadPacer] = None):
    """The reference drain of one window at the download's pace (§10.1): pages
    of ``limit`` events, call starts spaced at least ``1 / calls_per_second``
    apart (no burst), each call first taking a token from the container's
    bucket and sleeping ``retry_after_ms`` when it is empty, stopping at the
    window's end (``fetch_page`` returns ``(events, None)``) or at
    ``max_events``. ``fetch_page(token, limit)`` returns ``(events, next_token)``.
    Returns ``(events, calls)``."""
    interval = 1.0 / float(calls_per_second)
    events: list = []
    token = None
    calls = 0
    next_allowed = clock()
    gate = pacer_obj or pacer()
    while True:
        now = clock()
        if now < next_allowed:
            sleep(next_allowed - now)
        while True:
            try:
                gate.take(api)
                break
            except ReadThrottled as throttled:
                sleep(throttled.retry_after_ms / 1000.0)
        started = clock()
        page, token = fetch_page(token, limit)
        calls += 1
        next_allowed = started + interval
        events.extend(page)
        if token is None or len(events) >= max_events:
            break
    return events[:max_events], calls
