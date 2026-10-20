"""Certificate thumbprint to thing name (workflow-telemetry-dashboards design
§6.3, §6.4 step 4).

A message published through the Greengrass nucleus arrives with
``principal()`` = the core device certificate's thumbprint and ``clientid()``
= the thing name, or ``<thing>#<n>`` after each 50 subscriptions.
``resolve_thing`` turns the thumbprint into the one thing the certificate is
attached to, through ``ListPrincipalThings`` on the use case's account with
the ``ingest`` credentials; the caller compares it with
``strip_client_suffix(clientid)``.

Caching per container: a positive answer 1 h, a negative one 5 min, a denied
lookup 5 min (so the retries of other messages do not hammer STS or IoT).
Throttling, 5xx and timeouts raise :class:`LookupRetryable` (the function
raises so Lambda retries the event); AccessDenied raises
:class:`LookupDenied` (terminal, ``identity_unavailable``).
"""
import os
import re
import threading
import time
from typing import Any, Callable, Dict, Optional, Tuple

from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    ConnectionError as BotocoreConnectionError,
    ConnectTimeoutError,
    EndpointConnectionError,
    ReadTimeoutError,
)

from . import credentials

__all__ = [
    "LookupRetryable",
    "LookupDenied",
    "resolve_thing",
    "strip_client_suffix",
    "classify_client_error",
    "clear_cache",
    "POSITIVE_TTL_SECONDS",
    "NEGATIVE_TTL_SECONDS",
    "DENIED_TTL_SECONDS",
]

POSITIVE_TTL_SECONDS = 60 * 60
NEGATIVE_TTL_SECONDS = 5 * 60
DENIED_TTL_SECONDS = 5 * 60

_THUMBPRINT_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_CLIENT_SUFFIX_RE = re.compile(r"#\d+$")

_RETRYABLE_CODES = frozenset({
    "ThrottlingException", "Throttling", "ThrottledException", "TooManyRequestsException",
    "RequestLimitExceeded", "ServiceUnavailableException", "ServiceUnavailable",
    "InternalFailureException", "InternalFailure", "InternalException",
    "InternalServerException", "InternalError", "RequestTimeout",
    "RequestTimeoutException", "LimitExceededException",
})
_DENIED_CODES = frozenset({
    "AccessDeniedException", "AccessDenied", "UnauthorizedException",
    "UnauthorizedOperation", "ForbiddenException", "Forbidden",
})


class LookupRetryable(Exception):
    """A transient failure (throttling, 5xx, timeout): raise so Lambda retries."""


class LookupDenied(Exception):
    """The lookup was refused (AccessDenied): terminal for this message."""


def strip_client_suffix(client_id: Any) -> str:
    """``<thing>#<n>`` -> ``<thing>``; anything else unchanged (as a string)."""
    if not isinstance(client_id, str):
        return ""
    return _CLIENT_SUFFIX_RE.sub("", client_id)


def classify_client_error(error: BaseException) -> str:
    """``'retryable'``, ``'denied'`` or ``'other'`` for a botocore error."""
    if isinstance(error, (ConnectTimeoutError, ReadTimeoutError, EndpointConnectionError,
                          BotocoreConnectionError)):
        return "retryable"
    if isinstance(error, ClientError):
        response = error.response or {}
        code = str((response.get("Error") or {}).get("Code") or "")
        status = (response.get("ResponseMetadata") or {}).get("HTTPStatusCode")
        if code in _DENIED_CODES:
            return "denied"
        if code in _RETRYABLE_CODES or (isinstance(status, int) and status >= 500):
            return "retryable"
        return "other"
    if isinstance(error, BotoCoreError):
        return "retryable"
    return "other"


class _Cache:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._things: Dict[Tuple[str, str], Tuple[float, Optional[str]]] = {}
        self._denied: Dict[str, Tuple[float, str]] = {}

    def thing(self, key: Tuple[str, str], now: float) -> Tuple[bool, Optional[str]]:
        with self._lock:
            entry = self._things.get(key)
            if entry is None or entry[0] <= now:
                return False, None
            return True, entry[1]

    def remember_thing(self, key: Tuple[str, str], thing: Optional[str], now: float) -> None:
        ttl = POSITIVE_TTL_SECONDS if thing else NEGATIVE_TTL_SECONDS
        with self._lock:
            self._things[key] = (now + ttl, thing)

    def denied(self, account: str, now: float) -> Optional[str]:
        with self._lock:
            entry = self._denied.get(account)
            if entry is None or entry[0] <= now:
                return None
            return entry[1]

    def remember_denied(self, account: str, message: str, now: float) -> None:
        with self._lock:
            self._denied[account] = (now + DENIED_TTL_SECONDS, message)

    def clear(self) -> None:
        with self._lock:
            self._things.clear()
            self._denied.clear()


_cache = _Cache()


def clear_cache() -> None:
    """Forget every cached answer (tests)."""
    _cache.clear()


def _list_things(iot: Any, principal_arn: str) -> Tuple[str, ...]:
    things = []
    kwargs: Dict[str, Any] = {"principal": principal_arn, "maxResults": 100}
    while True:
        response = iot.list_principal_things(**kwargs)
        things.extend(str(name) for name in (response.get("things") or []))
        token = response.get("nextToken")
        if not token or len(things) > 1:
            break
        kwargs["nextToken"] = token
    return tuple(things)


def resolve_thing(pr: Any, usecase: Dict[str, Any], *,
                  clock: Callable[[], float] = time.time,
                  iot_client: Optional[Callable[[], Any]] = None) -> Optional[str]:
    """The thing name the certificate thumbprint ``pr`` is attached to in the
    use case's account, or ``None`` when it resolves to no thing or to more
    than one (``unattributed``). ``iot_client`` is an optional factory for
    tests; by default the client comes from
    ``credentials.client('iot', usecase, 'ingest')``."""
    if not isinstance(pr, str) or not _THUMBPRINT_RE.match(pr):
        return None
    pr = pr.lower()
    account = str(usecase.get("account_id") or "")
    region = str(usecase.get("region") or os.environ.get("AWS_REGION") or "")
    key = (account, pr)
    now = clock()
    hit, cached = _cache.thing(key, now)
    if hit:
        return cached
    denied = _cache.denied(account, now)
    if denied is not None:
        raise LookupDenied(denied)
    principal_arn = "arn:aws:iot:{0}:{1}:cert/{2}".format(region, account, pr)
    try:
        iot = iot_client() if iot_client is not None else credentials.client("iot", usecase, "ingest")
        things = _list_things(iot, principal_arn)
    except (ClientError, BotoCoreError) as error:
        kind = classify_client_error(error)
        if kind == "denied":
            message = "identity lookup denied for account {0}".format(account)
            _cache.remember_denied(account, message, now)
            raise LookupDenied(message) from error
        if kind == "retryable":
            raise LookupRetryable(error.__class__.__name__) from error
        # Any other 4xx (ResourceNotFoundException, a malformed principal):
        # deterministic, so retrying is pointless; the thing stays unresolved
        # and the negative answer is cached like a not-found.
        _cache.remember_thing(key, None, now)
        return None
    thing = things[0] if len(things) == 1 else None
    _cache.remember_thing(key, thing, now)
    return thing
