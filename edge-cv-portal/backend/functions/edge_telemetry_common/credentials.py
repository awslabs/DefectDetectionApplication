"""The one credential chooser of the telemetry and debug code
(workflow-telemetry-dashboards design §2.3).

Every boto3 client that touches a use case's account is built here and
nowhere else (``test_telemetry_credentials_ast.py`` fails on a
``boto3.client`` of ``iot``, ``iot-data``, ``greengrassv2``, ``s3`` or ``sts``
outside this module): the function's own role in the ``portal`` topology,
``shared_utils.assume_usecase_role`` with the purpose's session policy in
``cross``. ``shared_utils.get_usecase_client`` is deliberately not used: it
has no session-policy parameter and branches on ``is_cross_account_setup``,
the second predicate ``topology.py`` rules out.

One set of credentials per (role ARN, purpose, policy text) per container,
refreshed when fewer than 20 minutes of the 60-minute session remain.
"""
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional, Tuple

import boto3

import shared_utils

from . import session_policies
from .topology import PORTAL, telemetry_topology

__all__ = ["client", "SessionCache", "REFRESH_MARGIN_SECONDS", "clear_cache"]

#: Refresh a cached session when fewer than this many seconds remain.
REFRESH_MARGIN_SECONDS = 20 * 60
#: STS issues 60-minute sessions (``DurationSeconds`` 3600 in shared_utils);
#: used when a credentials answer carries no ``Expiration``.
DEFAULT_SESSION_SECONDS = 3600


def _expiry_epoch(credentials: Dict[str, Any], now: float) -> float:
    expiration = credentials.get("Expiration")
    if isinstance(expiration, datetime):
        if expiration.tzinfo is None:
            expiration = expiration.replace(tzinfo=timezone.utc)
        return expiration.timestamp()
    if isinstance(expiration, (int, float)):
        return float(expiration)
    if isinstance(expiration, str):
        try:
            parsed = datetime.fromisoformat(expiration.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.timestamp()
        except ValueError:
            pass
    return now + DEFAULT_SESSION_SECONDS


class SessionCache:
    """Per-container cache of assumed-role credentials keyed by
    ``(role_arn, purpose, policy_text)``."""

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: Dict[Tuple[str, str, str], Tuple[float, Dict[str, Any]]] = {}

    def get(self, role_arn: str, purpose: str, policy_text: str,
            assume: Callable[[], Dict[str, Any]]) -> Dict[str, Any]:
        key = (role_arn, purpose, policy_text)
        now = self._clock()
        with self._lock:
            cached = self._entries.get(key)
            if cached is not None and cached[0] - now > REFRESH_MARGIN_SECONDS:
                return cached[1]
        credentials = assume()
        with self._lock:
            self._entries[key] = (_expiry_epoch(credentials, now), credentials)
        return credentials

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


_session_cache = SessionCache()


def clear_cache() -> None:
    """Forget every cached session (tests; a container never needs it)."""
    _session_cache.clear()


def client(service: str, usecase: Dict[str, Any], purpose: str,
           region: Optional[str] = None, *, bucket: Optional[str] = None) -> Any:
    """The only way telemetry and debug code reach a use case's account.

    ``region`` defaults to the use case's region, else the function's
    ``AWS_REGION``. ``bucket`` is forwarded to ``session_policies.build`` for
    the purposes whose policy names a bucket (``media-retention-read``,
    ``image-read``); it is ignored in the ``portal`` topology, where no
    session policy is built. An unknown purpose raises
    ``session_policies.UnknownPurpose`` in both topologies, so a typo fails
    in the Portal account as loudly as it would in a cross account.
    """
    if purpose not in session_policies.PURPOSES:
        raise session_policies.UnknownPurpose(
            "unknown session policy purpose: {0!r}".format(purpose))
    region = region or usecase.get("region") or os.environ["AWS_REGION"]
    if telemetry_topology(usecase) == PORTAL:
        return boto3.client(service, region_name=region)
    role_arn = usecase["cross_account_role_arn"]
    policy = session_policies.build(purpose, usecase=usecase, bucket=bucket)
    text = session_policies.policy_text(policy)
    credentials = _session_cache.get(
        role_arn, purpose, text,
        lambda: shared_utils.assume_usecase_role(
            role_arn, usecase.get("external_id"), "dda-telemetry-{0}".format(purpose),
            session_policy=policy))
    return shared_utils.create_boto3_client(service, credentials, region)
