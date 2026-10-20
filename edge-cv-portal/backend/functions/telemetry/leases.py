"""Live-lease slots (workflow-telemetry-dashboards design §9.3, §7.4; tasks.md 3.4).

``N = TELEMETRY_LIVE_MAX_LEASES`` slots ``SLOT#0 ... SLOT#<N-1>`` per use case
(read from the environment, default 5, range 1-20; never a hard-coded five)
under ``LEASE#<uc>``, plus one ``HOLDER#<dashboard_id>#<user sub>`` item per
viewer. A lease lasts 60 s and is renewed every 20 s while the tile is
visible. ``acquire`` is renew-or-take in one ``TransactWriteItems``: a holder
with an unexpired slot renews it (condition ``holder = :me``, keeping
``started_at``); otherwise the first free slot (``attribute_not_exists(holder)
OR leaseExpiresMs < :now``) is taken and the holder item written in the same
transaction; after ``TELEMETRY_LIVE_MAX_VIEW_MINUTES`` (30) continuous
minutes the renewal is refused (``live_viewing_limit``); a Device whose
other Workflows already hold 8 leased slots is refused
(``device_live_limit``); no free slot is ``usecase_slots``.

The lease expiry is ``leaseExpiresMs`` (epoch ms); ``expires_at`` is the
item's TTL (the expiry plus an hour of grace), so a lapsed slot is still
readable for the maintenance tidy-up before TTL removes it.
"""
import os
import threading
from collections import namedtuple
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

import boto3
from boto3.dynamodb.conditions import Key
from boto3.dynamodb.types import TypeSerializer
from botocore.exceptions import ClientError

from telemetry import configdb, constants

__all__ = [
    "DEFAULT_MAX_LEASES", "MAX_LEASES_RANGE", "DEFAULT_MAX_VIEW_MINUTES", "MAX_VIEW_MINUTES_RANGE",
    "LEASE_SECONDS", "DEVICE_LIVE_LIMIT", "LeaseResult", "max_leases", "max_view_minutes", "holder_name",
    "read_partition", "slots_of", "holder_of", "pair_expiry", "device_other_workflows", "lapsed_pairs",
    "acquire", "release", "tidy_pair",
]

DEFAULT_MAX_LEASES = 5
MAX_LEASES_RANGE = (1, 20)
DEFAULT_MAX_VIEW_MINUTES = 30
MAX_VIEW_MINUTES_RANGE = (5, 240)
LEASE_SECONDS = constants.LIVE_LEASE_SECONDS
DEVICE_LIVE_LIMIT = constants.DEVICE_LIVE_LIMIT

#: ``granted`` with ``slot``, ``started_at``, ``lease_expires_ms``,
#: ``pair_expires_ms`` (the latest expiry among the Pair's slots, what the
#: shadow entry carries) and ``first_grant`` (the audit fires once per holder
#: slot); or ``reason`` in ``live_viewing_limit``, ``device_live_limit``,
#: ``usecase_slots``.
LeaseResult = namedtuple("LeaseResult", ["granted", "reason", "slot", "started_at", "lease_expires_ms",
                                         "pair_expires_ms", "first_grant"])

_serializer = TypeSerializer()
_lock = threading.Lock()
_client = None


def _ddb_client():
    global _client
    with _lock:
        if _client is None:
            _client = boto3.client("dynamodb")
        return _client


def reset_clients() -> None:
    global _client
    with _lock:
        _client = None


def _env_int(name: str, default: int, bounds: Tuple[int, int]) -> int:
    raw = os.environ.get(name)
    try:
        value = int(raw) if raw not in (None, "") else default
    except ValueError:
        value = default
    return max(bounds[0], min(bounds[1], value))


def max_leases() -> int:
    return _env_int("TELEMETRY_LIVE_MAX_LEASES", DEFAULT_MAX_LEASES, MAX_LEASES_RANGE)


def max_view_minutes() -> int:
    return _env_int("TELEMETRY_LIVE_MAX_VIEW_MINUTES", DEFAULT_MAX_VIEW_MINUTES, MAX_VIEW_MINUTES_RANGE)


def holder_name(dashboard_id: str, user_sub: str) -> str:
    return "{0}#{1}".format(dashboard_id, user_sub)


# --------------------------------------------------------------------------- reads
def read_partition(table: Any, usecase_id: str) -> List[Dict[str, Any]]:
    """Every ``SLOT#`` and ``HOLDER#`` item of the use case (one Query)."""
    return configdb.query_all(table, KeyConditionExpression=Key("pk").eq(configdb.lease_pk(usecase_id)))


def slots_of(items: Iterable[Dict[str, Any]]) -> Dict[int, Dict[str, Any]]:
    slots: Dict[int, Dict[str, Any]] = {}
    for item in items:
        sk = str(item.get("sk") or "")
        if sk.startswith("SLOT#"):
            try:
                slots[int(sk[5:])] = item
            except ValueError:
                continue
    return slots


def holder_of(items: Iterable[Dict[str, Any]], dashboard_id: str, user_sub: str) -> Optional[Dict[str, Any]]:
    wanted = configdb.holder_sk(dashboard_id, user_sub)
    for item in items:
        if item.get("sk") == wanted:
            return item
    return None


def _lease_expiry(item: Optional[Dict[str, Any]]) -> int:
    try:
        return int((item or {}).get("leaseExpiresMs") or 0)
    except (TypeError, ValueError):
        return 0


def _is_live(item: Optional[Dict[str, Any]], now_ms: int) -> bool:
    return bool(item) and bool(item.get("holder")) and _lease_expiry(item) > now_ms


def pair_expiry(slots: Dict[int, Dict[str, Any]], device_id: str, workflow_id: str, now_ms: int) -> int:
    """The latest lease expiry among the live slots of the Pair, else 0."""
    latest = 0
    for slot in slots.values():
        if _is_live(slot, now_ms) and slot.get("device_id") == device_id and slot.get("workflow_id") == workflow_id:
            latest = max(latest, _lease_expiry(slot))
    return latest


def device_other_workflows(slots: Dict[int, Dict[str, Any]], device_id: str, workflow_id: str, now_ms: int) -> int:
    """How many other Workflows of the Device hold a live slot (§9.3 per-Device cap)."""
    others = set()
    for slot in slots.values():
        if _is_live(slot, now_ms) and slot.get("device_id") == device_id and slot.get("workflow_id") != workflow_id:
            others.add(str(slot.get("workflow_id")))
    return len(others)


def lapsed_pairs(slots: Dict[int, Dict[str, Any]], now_ms: int) -> List[Tuple[str, str, List[int]]]:
    """``(device_id, workflow_id, [slot indexes])`` of every Pair whose slots
    have all lapsed (the maintenance writes ``null`` and deletes them)."""
    by_pair: Dict[Tuple[str, str], List[Tuple[int, bool]]] = {}
    for index, slot in slots.items():
        if not slot.get("holder"):
            continue
        key = (str(slot.get("device_id")), str(slot.get("workflow_id")))
        by_pair.setdefault(key, []).append((index, _is_live(slot, now_ms)))
    lapsed = []
    for (device_id, workflow_id), entries in sorted(by_pair.items()):
        if not any(live for _, live in entries):
            lapsed.append((device_id, workflow_id, sorted(index for index, _ in entries)))
    return lapsed


# --------------------------------------------------------------------------- writes
def _typed(item: Dict[str, Any]) -> Dict[str, Any]:
    return {k: _serializer.serialize(v) for k, v in item.items()}


def _slot_item(usecase_id: str, index: int, holder: str, device_id: str, workflow_id: str, started_at: int,
               lease_expires_ms: int) -> Dict[str, Any]:
    return {"pk": configdb.lease_pk(usecase_id), "sk": configdb.slot_sk(index), "holder": holder,
            "device_id": device_id, "workflow_id": workflow_id, "started_at": int(started_at),
            "leaseExpiresMs": int(lease_expires_ms),
            "expires_at": int(lease_expires_ms) // 1000 + configdb.LEASE_TTL_GRACE_SECONDS}


def _holder_item(usecase_id: str, dashboard_id: str, user_sub: str, index: int, device_id: str, workflow_id: str,
                 started_at: int, lease_expires_ms: int) -> Dict[str, Any]:
    return {"pk": configdb.lease_pk(usecase_id), "sk": configdb.holder_sk(dashboard_id, user_sub), "slot": int(index),
            "holder": holder_name(dashboard_id, user_sub), "device_id": device_id, "workflow_id": workflow_id,
            "started_at": int(started_at), "leaseExpiresMs": int(lease_expires_ms),
            "expires_at": int(lease_expires_ms) // 1000 + configdb.LEASE_TTL_GRACE_SECONDS}


def _is_cancelled(error: ClientError) -> bool:
    return str((error.response.get("Error") or {}).get("Code") or "") in (
        "TransactionCanceledException", "ConditionalCheckFailedException")


def acquire(table: Any, usecase_id: str, dashboard_id: str, user_sub: str, device_id: str, workflow_id: str,
            now_ms: int, *, slots: Optional[int] = None, lease_seconds: int = LEASE_SECONDS,
            view_minutes: Optional[int] = None, transact: Optional[Callable[[List[Dict[str, Any]]], Any]] = None,
            attempts: int = 2) -> LeaseResult:
    """Renew the caller's slot or take a free one (§9.3)."""
    total = int(slots if slots is not None else max_leases())
    limit_ms = int(view_minutes if view_minutes is not None else max_view_minutes()) * 60_000
    transact = transact or (lambda items: _ddb_client().transact_write_items(TransactItems=items))
    table_name = table.name if hasattr(table, "name") else configdb.table_name()
    me = holder_name(dashboard_id, user_sub)
    lease_expires_ms = int(now_ms) + int(lease_seconds) * 1000
    for _attempt in range(max(1, attempts)):
        items = read_partition(table, usecase_id)
        slot_items = slots_of(items)
        holder = holder_of(items, dashboard_id, user_sub)
        # Renew.
        if holder is not None and _lease_expiry(holder) > now_ms:
            index = int(holder.get("slot", -1))
            slot = slot_items.get(index)
            if slot is not None and slot.get("holder") == me and _is_live(slot, now_ms):
                started_at = int(holder.get("started_at") or now_ms)
                if now_ms - started_at >= limit_ms:
                    return LeaseResult(False, "live_viewing_limit", index, started_at, 0,
                                       pair_expiry(slot_items, device_id, workflow_id, now_ms), False)
                new_slot = _slot_item(usecase_id, index, me, device_id, workflow_id, started_at, lease_expires_ms)
                new_holder = _holder_item(usecase_id, dashboard_id, user_sub, index, device_id, workflow_id, started_at,
                                          lease_expires_ms)
                try:
                    transact([
                        {"Put": {"TableName": table_name, "Item": _typed(new_slot),
                                 "ConditionExpression": "holder = :me",
                                 "ExpressionAttributeValues": {":me": {"S": me}}}},
                        {"Put": {"TableName": table_name, "Item": _typed(new_holder)}},
                    ])
                except ClientError as error:
                    if _is_cancelled(error):
                        continue
                    raise
                slot_items[index] = new_slot
                return LeaseResult(True, None, index, started_at, lease_expires_ms,
                                   pair_expiry(slot_items, device_id, workflow_id, now_ms), False)
        # Take: the per-Device cap first.
        if device_other_workflows(slot_items, device_id, workflow_id, now_ms) >= DEVICE_LIVE_LIMIT:
            return LeaseResult(False, "device_live_limit", None, None, 0,
                               pair_expiry(slot_items, device_id, workflow_id, now_ms), False)
        free = [index for index in range(total) if not _is_live(slot_items.get(index), now_ms)]
        if not free:
            return LeaseResult(False, "usecase_slots", None, None, 0,
                               pair_expiry(slot_items, device_id, workflow_id, now_ms), False)
        index = free[0]
        new_slot = _slot_item(usecase_id, index, me, device_id, workflow_id, now_ms, lease_expires_ms)
        new_holder = _holder_item(usecase_id, dashboard_id, user_sub, index, device_id, workflow_id, now_ms, lease_expires_ms)
        try:
            transact([
                {"Put": {"TableName": table_name, "Item": _typed(new_slot),
                         "ConditionExpression": "attribute_not_exists(holder) OR leaseExpiresMs < :now",
                         "ExpressionAttributeValues": {":now": {"N": str(int(now_ms))}}}},
                {"Put": {"TableName": table_name, "Item": _typed(new_holder)}},
            ])
        except ClientError as error:
            if _is_cancelled(error):
                continue
            raise
        slot_items[index] = new_slot
        return LeaseResult(True, None, index, now_ms, lease_expires_ms,
                           pair_expiry(slot_items, device_id, workflow_id, now_ms), True)
    return LeaseResult(False, "usecase_slots", None, None, 0, 0, False)


def release(table: Any, usecase_id: str, dashboard_id: str, user_sub: str) -> bool:
    """Give the caller's slot back (a failed shadow write): the slot's lease is
    ended and the holder item deleted. True when a slot was released."""
    items = read_partition(table, usecase_id)
    holder = holder_of(items, dashboard_id, user_sub)
    if holder is None:
        return False
    me = holder_name(dashboard_id, user_sub)
    index = int(holder.get("slot", -1))
    try:
        table.update_item(Key={"pk": configdb.lease_pk(usecase_id), "sk": configdb.slot_sk(index)},
                          UpdateExpression="SET leaseExpiresMs = :zero",
                          ConditionExpression="holder = :me",
                          ExpressionAttributeValues={":zero": 0, ":me": me})
    except ClientError as error:
        if not _is_cancelled(error):
            raise
    table.delete_item(Key={"pk": configdb.lease_pk(usecase_id), "sk": holder["sk"]})
    return True


def tidy_pair(table: Any, usecase_id: str, indexes: Iterable[int], now_ms: int) -> int:
    """Delete the lapsed slot items of a tidied Pair (condition: still lapsed).
    Returns the number deleted."""
    deleted = 0
    for index in indexes:
        try:
            table.delete_item(Key={"pk": configdb.lease_pk(usecase_id), "sk": configdb.slot_sk(int(index))},
                              ConditionExpression="attribute_not_exists(holder) OR leaseExpiresMs <= :now",
                              ExpressionAttributeValues={":now": int(now_ms)})
            deleted += 1
        except ClientError as error:
            if not _is_cancelled(error):
                raise
    return deleted
