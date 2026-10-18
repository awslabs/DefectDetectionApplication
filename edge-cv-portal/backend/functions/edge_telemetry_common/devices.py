"""Which devices belong to a use case (workflow-telemetry-dashboards design
§10.2).

Thing names with a Device_Record naming the use case: a filtered ``Scan`` of
the Devices table. The table is small (one row per enrolled device) and its
``usecase-devices-index`` is sparse and empty (its sort key
``last_heartbeat`` is never written), so nothing here queries the index and
the grant is ``dynamodb:Scan`` plus ``GetItem``. The result is cached 60 s
per use case in the container.
"""
import os
import threading
import time
from typing import Any, Callable, Dict, List, Tuple

import boto3
from boto3.dynamodb.conditions import Attr

__all__ = ["usecase_devices", "clear_cache", "CACHE_SECONDS"]

CACHE_SECONDS = 60

_lock = threading.Lock()
_cache: Dict[str, Tuple[float, List[str]]] = {}


def _table():
    return boto3.resource("dynamodb").Table(os.environ["DEVICES_TABLE"])


def _scan_device_ids(usecase_id: str) -> List[str]:
    table = _table()
    kwargs: Dict[str, Any] = {
        "FilterExpression": Attr("usecase_id").eq(usecase_id),
        "ProjectionExpression": "device_id",
    }
    found = set()
    while True:
        response = table.scan(**kwargs)
        for item in response.get("Items", []) or []:
            device_id = item.get("device_id")
            if device_id:
                found.add(str(device_id))
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            break
        kwargs["ExclusiveStartKey"] = last_key
    return sorted(found)


def usecase_devices(usecase: Dict[str, Any], *, clock: Callable[[], float] = time.time) -> List[str]:
    """Sorted thing names whose Device_Record names ``usecase['usecase_id']``,
    cached for ``CACHE_SECONDS`` per use case."""
    usecase_id = str(usecase["usecase_id"])
    now = clock()
    with _lock:
        cached = _cache.get(usecase_id)
        if cached is not None and cached[0] > now:
            return list(cached[1])
    devices = _scan_device_ids(usecase_id)
    with _lock:
        _cache[usecase_id] = (now + CACHE_SECONDS, devices)
    return list(devices)


def clear_cache() -> None:
    """Forget every cached listing (tests)."""
    with _lock:
        _cache.clear()
