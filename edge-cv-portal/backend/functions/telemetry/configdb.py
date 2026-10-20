"""The configuration table ``dda-portal-telemetry-config``
(workflow-telemetry-dashboards design §7.4; tasks.md 3.3, 3.4).

Item keys (``pk`` / ``sk``):

| Item | ``pk`` | ``sk`` |
|---|---|---|
| Telemetry_Settings | ``UC#<uc>`` | ``SETTINGS`` (``by-type``: ``USECASE`` / ``UC#<uc>``) |
| Dashboard counter, name marker | ``UC#<uc>`` | ``DASHCOUNT``, ``DASHNAME#<NFKC case-folded name>`` |
| Dashboard | ``DASH#<id>`` | ``META`` (``by-usecase``: ``UC#<uc>`` / ``DASH#<created13>#<id>``) |
| Live-lease slots and holders | ``LEASE#<uc>`` | ``SLOT#<n>``; ``HOLDER#<dashboard_id>#<user sub>`` |
| Maintenance markers | ``UC#<uc>``; ``MAINT`` | ``RESYNC``; ``SWEEP`` |
| Control_Shadow write state | ``UC#<uc>`` | ``DEVSHADOW#<device>`` |
| Rejection counters and samples | ``UC#<uc>`` or ``X#<account>`` | ``REJ#<minute>``; ``SAMPLE#<minute>#<reason>`` |
| Readiness marker | ``UC#<uc>`` | ``SEEN`` |
| Core-device status cache | ``GG#<uc>#<device>`` | ``STATUS`` |

Every read filters ``expires_at`` (``unexpired``), because TTL deletes lag.
The samples are keyed per (scope, reason, minute) so that the "one sample
per (scope, reason, minute)" rule is a conditional put (the §7.4 sketch
``SAMPLE#<receivedMs>#<m>`` could not express it).
"""
import json
import os
import time
import unicodedata
from typing import Any, Dict, Iterable, List, Optional

__all__ = [
    "SETTINGS_SK", "DASHCOUNT_SK", "META_SK", "RESYNC_SK", "SWEEP_SK", "SEEN_SK", "GG_STATUS_SK", "MAINT_PK",
    "BY_TYPE_USECASE", "REJ_TTL_SECONDS", "SAMPLE_TTL_SECONDS", "SEEN_TTL_SECONDS", "GG_TTL_SECONDS",
    "LEASE_TTL_GRACE_SECONDS", "SAMPLE_MAX_BYTES",
    "usecase_pk", "account_pk", "dash_pk", "dashname_sk", "lease_pk", "slot_sk", "holder_sk", "devshadow_sk",
    "rej_sk", "sample_sk", "gg_pk", "by_usecase_sk", "fold_name", "minute_of", "unexpired", "is_unexpired",
    "settings_key", "seen_item", "rejection_update", "sample_item", "query_all", "table_name",
]

SETTINGS_SK = "SETTINGS"
DASHCOUNT_SK = "DASHCOUNT"
META_SK = "META"
RESYNC_SK = "RESYNC"
SWEEP_SK = "SWEEP"
SEEN_SK = "SEEN"
GG_STATUS_SK = "STATUS"
MAINT_PK = "MAINT"
BY_TYPE_USECASE = "USECASE"

REJ_TTL_SECONDS = 24 * 3600
SAMPLE_TTL_SECONDS = 14 * 24 * 3600
SEEN_TTL_SECONDS = 24 * 3600
GG_TTL_SECONDS = 120
LEASE_TTL_GRACE_SECONDS = 3600
SAMPLE_MAX_BYTES = 1024


def table_name() -> str:
    return os.environ["TELEMETRY_CONFIG_TABLE"]


# --------------------------------------------------------------------------- keys
def usecase_pk(usecase_id: str) -> str:
    return "UC#{0}".format(usecase_id)


def account_pk(account_id: str) -> str:
    return "X#{0}".format(account_id)


def dash_pk(dashboard_id: str) -> str:
    return "DASH#{0}".format(dashboard_id)


def fold_name(name: str) -> str:
    """NFKC normalization and case folding of a Dashboard name (uniqueness)."""
    return unicodedata.normalize("NFKC", str(name)).casefold().strip()


def dashname_sk(name: str) -> str:
    return "DASHNAME#{0}".format(fold_name(name))


def by_usecase_sk(created_ms: int, dashboard_id: str) -> str:
    return "DASH#{0:013d}#{1}".format(int(created_ms), dashboard_id)


def lease_pk(usecase_id: str) -> str:
    return "LEASE#{0}".format(usecase_id)


def slot_sk(index: int) -> str:
    return "SLOT#{0}".format(int(index))


def holder_sk(dashboard_id: str, user_sub: str) -> str:
    return "HOLDER#{0}#{1}".format(dashboard_id, user_sub)


def devshadow_sk(device_id: str) -> str:
    return "DEVSHADOW#{0}".format(device_id)


def minute_of(ms: int) -> int:
    return (int(ms) // 60_000) * 60_000


def rej_sk(minute_ms: int) -> str:
    return "REJ#{0:013d}".format(minute_of(minute_ms))


def sample_sk(minute_ms: int, reason: str) -> str:
    return "SAMPLE#{0:013d}#{1}".format(minute_of(minute_ms), reason)


def gg_pk(usecase_id: str, device_id: str) -> str:
    return "GG#{0}#{1}".format(usecase_id, device_id)


def settings_key(usecase_id: str) -> Dict[str, str]:
    return {"pk": usecase_pk(usecase_id), "sk": SETTINGS_SK}


# --------------------------------------------------------------------------- expiry
def is_unexpired(item: Optional[Dict[str, Any]], now_s: Optional[float] = None) -> bool:
    """True for an item without ``expires_at`` or with one in the future."""
    if not item:
        return False
    expires = item.get("expires_at")
    if expires is None:
        return True
    try:
        return float(expires) > float(now_s if now_s is not None else time.time())
    except (TypeError, ValueError):
        return False


def unexpired(items: Iterable[Dict[str, Any]], now_s: Optional[float] = None) -> List[Dict[str, Any]]:
    now_s = now_s if now_s is not None else time.time()
    return [item for item in items if is_unexpired(item, now_s)]


def query_all(table: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    """Every page of a Query."""
    found: List[Dict[str, Any]] = []
    while True:
        response = table.query(**kwargs)
        found.extend(response.get("Items", []) or [])
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            return found
        kwargs["ExclusiveStartKey"] = last_key


# --------------------------------------------------------------------------- the ingest's items
def seen_item(usecase_id: str, received_ms: int) -> Dict[str, Any]:
    """The readiness marker: a Summary (Heartbeats included) was accepted in
    the last 24 h."""
    return {"pk": usecase_pk(usecase_id), "sk": SEEN_SK, "receivedMs": int(received_ms),
            "expires_at": int(received_ms) // 1000 + SEEN_TTL_SECONDS}


def rejection_update(pk: str, received_ms: int, reasons: Dict[str, int]) -> Dict[str, Any]:
    """The ``UpdateItem ... ADD`` arguments of one minute's rejection counters."""
    ordered = sorted(reasons)
    names = {"#r{0}".format(index): reason for index, reason in enumerate(ordered)}
    values: Dict[str, Any] = {":v{0}".format(index): int(reasons[reason]) for index, reason in enumerate(ordered)}
    adds = ", ".join("#r{0} :v{0}".format(index) for index in range(len(ordered)))
    names["#ttl"] = "expires_at"
    names["#min"] = "minuteMs"
    values[":ttl"] = int(received_ms) // 1000 + REJ_TTL_SECONDS
    values[":minute"] = minute_of(received_ms)
    return {
        "Key": {"pk": pk, "sk": rej_sk(received_ms)},
        "UpdateExpression": "ADD {0} SET #ttl = :ttl, #min = :minute".format(adds),
        "ExpressionAttributeNames": names,
        "ExpressionAttributeValues": values,
    }


def sample_item(pk: str, received_ms: int, reason: str, sample: Dict[str, Any]) -> Dict[str, Any]:
    """One sample per (scope, reason, minute), at most 1 KB, no payload."""
    body = dict(sample)
    text = json.dumps(body, default=str, separators=(",", ":"))
    while len(text.encode("utf-8")) > SAMPLE_MAX_BYTES and body:
        longest = max(body, key=lambda key: len(str(body[key])))
        value = str(body[longest])
        if len(value) <= 16:
            del body[longest]
        else:
            body[longest] = value[: max(16, len(value) // 2)]
        text = json.dumps(body, default=str, separators=(",", ":"))
    return {"pk": pk, "sk": sample_sk(received_ms, reason), "reason": reason, "receivedMs": int(received_ms),
            "sample": body, "expires_at": int(received_ms) // 1000 + SAMPLE_TTL_SECONDS}
