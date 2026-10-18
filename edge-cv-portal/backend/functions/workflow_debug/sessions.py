"""The Portal-side session model (workflow-debug-mode design §7.1).

Table ``dda-portal-workflow-debug-sessions`` (``pk``/``sk``, TTL ``expires_at``):

| Item          | ``pk``                          | ``sk``                     | index keys |
|---------------|---------------------------------|----------------------------|------------|
| Session       | ``SESSION#<id>``                | ``META``                   | ``by-usecase``: ``gsi_usecase = UC#<uc>`` / ``gsi_sort = <expiresAt13>#<id>`` |
| Session device| ``SESSION#<id>``                | ``PAIR#<device>``          | ``by-pair``: ``gsi_pair = P#<uc>#<device>#<wf>`` / ``gsi_sort`` |
| Dedup         | ``SESSION#<id>``                | ``SEQ#<instance>#<seq>``   | TTL 10 min |
| Session count | ``UC#<uc>`` / ``DEV#<device>``  | ``ACTIVE``                 | ``count`` |
| Pair marker   | ``PAIRLOCK#<uc>#<device>#<wf>`` | ``LOCK``                   | one live session per Pair |

States: ``pending`` (created; at least one shadow write not yet ``ok``) ->
``active`` (every write ``ok``) -> ``stopped`` (DELETE) or ``expired``
(maintenance). ``pending`` and ``active`` count against the caps and accept
messages.

Creation is one ``TransactWriteItems`` of ``3N + 2`` actions for N devices:
``Put META`` (absent), one ``Put PAIR#`` per device, ``Update UC#/ACTIVE``
(``count < max``), one ``Update DEV#/ACTIVE`` per device (``count < 2``) and
one **conditional ``Put``** of the ``PAIRLOCK`` marker per device
(``attribute_not_exists(pk)``: a ``ConditionCheck`` on a missing item would
pass and create nothing, so two concurrent creations for one Pair would both
succeed; two conditional ``Put``s of one key cannot both commit). The 100-action
limit therefore caps ``debugMaxDevicesPerSession`` at 32. Stop and expiry run
one transaction too: the state on ``META`` and every ``PAIR#``, the
``PAIRLOCK`` deletes and the counter decrements.
"""
import json
from typing import Any, Dict, Iterable, List, Optional, Tuple

from boto3.dynamodb.types import TypeDeserializer, TypeSerializer

from . import logs_writer

__all__ = [
    "PENDING", "ACTIVE", "STOPPED", "EXPIRED", "LIVE_STATES", "TERMINAL_STATES", "TRANSITIONS",
    "META_SK", "ACTIVE_SK", "LOCK_SK", "MAX_TRANSACTION_ACTIONS", "MAX_DEVICES_PER_SESSION",
    "DEVICE_SESSION_CAP", "HISTORY_DAYS", "SEQ_TTL_SECONDS",
    "session_pk", "pair_sk", "seq_sk", "usecase_counter_key", "device_counter_key",
    "pairlock_key", "by_usecase_pk", "by_pair_pk", "gsi_sort", "device_of_pair_sk",
    "can_transition", "is_live", "creation_actions", "shadow_result_actions", "stop_actions", "seq_item",
    "serialize_actions", "deserialize_item", "cancellation_to_error",
]

PENDING = "pending"
ACTIVE = "active"
STOPPED = "stopped"
EXPIRED = "expired"
LIVE_STATES = (PENDING, ACTIVE)
TERMINAL_STATES = (STOPPED, EXPIRED)
TRANSITIONS = {
    PENDING: (ACTIVE, STOPPED, EXPIRED),
    ACTIVE: (STOPPED, EXPIRED),
    STOPPED: (),
    EXPIRED: (),
}

META_SK = "META"
ACTIVE_SK = "ACTIVE"
LOCK_SK = "LOCK"

MAX_TRANSACTION_ACTIONS = 100
MAX_DEVICES_PER_SESSION = 32          # 3 x 32 + 2 = 98 <= 100
DEVICE_SESSION_CAP = 2                 # pending-or-active sessions per device
HISTORY_DAYS = 30                      # META/PAIR# TTL after expiresAt
SEQ_TTL_SECONDS = 10 * 60
PAIRLOCK_GRACE_SECONDS = 24 * 60 * 60  # TTL safety net on a marker (the stop/expire delete it)

_serializer = TypeSerializer()
_deserializer = TypeDeserializer()


# ----------------------------------------------------------------------------- keys
def session_pk(session_id: str) -> str:
    return "SESSION#{0}".format(session_id)


def pair_sk(device_id: str) -> str:
    return "PAIR#{0}".format(device_id)


def device_of_pair_sk(sk: str) -> str:
    return sk[len("PAIR#"):] if isinstance(sk, str) and sk.startswith("PAIR#") else ""


def seq_sk(instance: str, seq: int) -> str:
    return "SEQ#{0}#{1}".format(instance, int(seq))


def usecase_counter_key(usecase_id: str) -> Dict[str, str]:
    return {"pk": "UC#{0}".format(usecase_id), "sk": ACTIVE_SK}


def device_counter_key(device_id: str) -> Dict[str, str]:
    return {"pk": "DEV#{0}".format(device_id), "sk": ACTIVE_SK}


def pairlock_key(usecase_id: str, device_id: str, workflow_id: str) -> Dict[str, str]:
    return {"pk": "PAIRLOCK#{0}#{1}#{2}".format(usecase_id, device_id, workflow_id), "sk": LOCK_SK}


def by_usecase_pk(usecase_id: str) -> str:
    return "UC#{0}".format(usecase_id)


def by_pair_pk(usecase_id: str, device_id: str, workflow_id: str) -> str:
    return "P#{0}#{1}#{2}".format(usecase_id, device_id, workflow_id)


def gsi_sort(expires_at_ms: int, session_id: str) -> str:
    return "{0:013d}#{1}".format(int(expires_at_ms), session_id)


# ----------------------------------------------------------------------------- states
def can_transition(current: str, target: str) -> bool:
    return target in TRANSITIONS.get(current, ())


def is_live(state: Any) -> bool:
    return state in LIVE_STATES


# ----------------------------------------------------------------------------- items
def _ttl_seconds(expires_at_ms: int, extra_seconds: int) -> int:
    return int(expires_at_ms // 1000) + int(extra_seconds)


def meta_item(*, session_id: str, usecase_id: str, workflow_id: str, workflow_version: Optional[str],
              devices: List[str], all_devices: bool, enabled_at: int, expires_at: int, actor: str,
              options: Dict[str, Any], log_group: str,
              media: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """``media`` (``{bucket, region, region_source}``) records what the API
    resolved for the shadow entries, so the maintenance function can rebuild
    an identical entry when it retries a failed shadow write."""
    return {
        "pk": session_pk(session_id), "sk": META_SK,
        "session_id": session_id, "usecase_id": usecase_id, "workflow_id": workflow_id,
        "workflow_version": workflow_version, "device_ids": list(devices), "all_devices": bool(all_devices),
        "state": PENDING, "enabledAt": int(enabled_at), "expiresAt": int(expires_at), "actor": actor,
        "options": dict(options), "counters": {"records": 0, "bytes": 0}, "logGroup": log_group,
        "media": dict(media or {}),
        "gsi_usecase": by_usecase_pk(usecase_id), "gsi_sort": gsi_sort(expires_at, session_id),
        "expires_at": _ttl_seconds(expires_at, HISTORY_DAYS * 24 * 60 * 60),
    }


def pair_item(*, session_id: str, usecase_id: str, workflow_id: str, device_id: str,
              expires_at: int) -> Dict[str, Any]:
    return {
        "pk": session_pk(session_id), "sk": pair_sk(device_id),
        "usecase_id": usecase_id, "workflow_id": workflow_id, "device_id": device_id,
        "sessionId": session_id, "state": PENDING, "expiresAt": int(expires_at),
        "shadow": {"result": "pending"}, "reported": None,
        "logStream": logs_writer.stream_name(device_id, workflow_id),
        "gsi_pair": by_pair_pk(usecase_id, device_id, workflow_id),
        "gsi_sort": gsi_sort(expires_at, session_id),
        "expires_at": _ttl_seconds(expires_at, HISTORY_DAYS * 24 * 60 * 60),
    }


def seq_item(session_id: str, instance: str, seq: int, now_ms: int) -> Dict[str, Any]:
    """The dedup mark written only after a successful ``PutLogEvents``."""
    return {"pk": session_pk(session_id), "sk": seq_sk(instance, seq),
            "writtenAt": int(now_ms), "expires_at": int(now_ms // 1000) + SEQ_TTL_SECONDS}


# ----------------------------------------------------------------------------- transactions
def creation_actions(table_name: str, *, session_id: str, usecase_id: str, workflow_id: str,
                     workflow_version: Optional[str], devices: Iterable[str], all_devices: bool,
                     enabled_at: int, expires_at: int, actor: str, options: Dict[str, Any],
                     log_group: str, max_sessions_per_usecase: int,
                     media: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """The ``3N + 2`` plain-Python actions of the creation transaction, in
    the order META, the PAIR#s, the use-case counter, the device counters,
    the PAIRLOCK markers. ``serialize_actions`` turns them into the
    ``TransactWriteItems`` request."""
    device_list = [str(d) for d in devices]
    if not device_list:
        raise ValueError("a session needs at least one device")
    if len(device_list) != len(set(device_list)):
        raise ValueError("duplicate device in the target set")
    if len(device_list) > MAX_DEVICES_PER_SESSION:
        raise ValueError("at most {0} devices per session".format(MAX_DEVICES_PER_SESSION))
    actions: List[Dict[str, Any]] = [{
        "Put": {
            "TableName": table_name,
            "Item": meta_item(session_id=session_id, usecase_id=usecase_id, workflow_id=workflow_id,
                              workflow_version=workflow_version, devices=device_list,
                              all_devices=all_devices, enabled_at=enabled_at, expires_at=expires_at,
                              actor=actor, options=options, log_group=log_group, media=media),
            "ConditionExpression": "attribute_not_exists(pk)",
        },
    }]
    for device_id in device_list:
        actions.append({"Put": {
            "TableName": table_name,
            "Item": pair_item(session_id=session_id, usecase_id=usecase_id, workflow_id=workflow_id,
                              device_id=device_id, expires_at=expires_at),
        }})
    actions.append({"Update": {
        "TableName": table_name,
        "Key": usecase_counter_key(usecase_id),
        "UpdateExpression": "ADD #count :one",
        "ConditionExpression": "attribute_not_exists(#count) OR #count < :max",
        "ExpressionAttributeNames": {"#count": "count"},
        "ExpressionAttributeValues": {":one": 1, ":max": int(max_sessions_per_usecase)},
        "ReturnValuesOnConditionCheckFailure": "ALL_OLD",
    }})
    for device_id in device_list:
        actions.append({"Update": {
            "TableName": table_name,
            "Key": device_counter_key(device_id),
            "UpdateExpression": "ADD #count :one",
            "ConditionExpression": "attribute_not_exists(#count) OR #count < :cap",
            "ExpressionAttributeNames": {"#count": "count"},
            "ExpressionAttributeValues": {":one": 1, ":cap": DEVICE_SESSION_CAP},
            "ReturnValuesOnConditionCheckFailure": "ALL_OLD",
        }})
    for device_id in device_list:
        key = pairlock_key(usecase_id, device_id, workflow_id)
        actions.append({"Put": {
            "TableName": table_name,
            "Item": dict(key, session_id=session_id, usecase_id=usecase_id, device_id=device_id,
                         workflow_id=workflow_id,
                         expires_at=_ttl_seconds(expires_at, PAIRLOCK_GRACE_SECONDS)),
            "ConditionExpression": "attribute_not_exists(pk)",
            "ReturnValuesOnConditionCheckFailure": "ALL_OLD",
        }})
    assert len(actions) == 3 * len(device_list) + 2
    assert len(actions) <= MAX_TRANSACTION_ACTIONS
    return actions


def shadow_result_actions(table_name: str, session_id: str, results: Dict[str, Dict[str, Any]], *,
                          activate: bool) -> List[Dict[str, Any]]:
    """Record the shadow write results on the ``PAIR#`` items and, when every
    write is ``ok`` (``activate``), move META and every PAIR# to ``active`` in
    the same transaction (META conditioned on ``pending``, so a session
    stopped meanwhile is never reactivated)."""
    actions: List[Dict[str, Any]] = []
    if activate:
        actions.append({"Update": {
            "TableName": table_name,
            "Key": {"pk": session_pk(session_id), "sk": META_SK},
            "UpdateExpression": "SET #state = :active",
            "ConditionExpression": "#state = :pending",
            "ExpressionAttributeNames": {"#state": "state"},
            "ExpressionAttributeValues": {":active": ACTIVE, ":pending": PENDING},
        }})
    for device_id, result in results.items():
        expression = "SET shadow = :shadow"
        values: Dict[str, Any] = {":shadow": dict(result)}
        names = {}
        if activate:
            expression += ", #state = :active"
            values[":active"] = ACTIVE
            names["#state"] = "state"
        action: Dict[str, Any] = {
            "TableName": table_name,
            "Key": {"pk": session_pk(session_id), "sk": pair_sk(device_id)},
            "UpdateExpression": expression,
            "ExpressionAttributeValues": values,
        }
        if names:
            action["ExpressionAttributeNames"] = names
        actions.append({"Update": action})
    assert len(actions) <= MAX_TRANSACTION_ACTIONS
    return actions


def stop_actions(table_name: str, meta: Dict[str, Any], pairs: Iterable[Dict[str, Any]], *,
                 new_state: str, now_ms: int) -> List[Dict[str, Any]]:
    """The stop (``stopped``) or expiry (``expired``) transaction: the state
    on META (conditioned on a live state, so a second stop fails cleanly and
    the counters are decremented once) and every PAIR#, the PAIRLOCK deletes
    and the counter decrements."""
    if new_state not in TERMINAL_STATES:
        raise ValueError("new_state must be stopped or expired")
    session_id = str(meta["session_id"])
    usecase_id = str(meta["usecase_id"])
    workflow_id = str(meta["workflow_id"])
    pair_list = list(pairs)
    devices = [str(p.get("device_id") or device_of_pair_sk(p.get("sk"))) for p in pair_list]
    actions: List[Dict[str, Any]] = [{"Update": {
        "TableName": table_name,
        "Key": {"pk": session_pk(session_id), "sk": META_SK},
        "UpdateExpression": "SET #state = :state, stoppedAt = :now",
        "ConditionExpression": "#state IN (:pending, :active)",
        "ExpressionAttributeNames": {"#state": "state"},
        "ExpressionAttributeValues": {":state": new_state, ":now": int(now_ms),
                                      ":pending": PENDING, ":active": ACTIVE},
    }}]
    for device_id in devices:
        actions.append({"Update": {
            "TableName": table_name,
            "Key": {"pk": session_pk(session_id), "sk": pair_sk(device_id)},
            "UpdateExpression": "SET #state = :state",
            "ExpressionAttributeNames": {"#state": "state"},
            "ExpressionAttributeValues": {":state": new_state},
        }})
    for device_id in devices:
        actions.append({"Delete": {
            "TableName": table_name,
            "Key": pairlock_key(usecase_id, device_id, workflow_id),
        }})
    actions.append({"Update": {
        "TableName": table_name,
        "Key": usecase_counter_key(usecase_id),
        "UpdateExpression": "ADD #count :minus",
        "ExpressionAttributeNames": {"#count": "count"},
        "ExpressionAttributeValues": {":minus": -1},
    }})
    for device_id in devices:
        actions.append({"Update": {
            "TableName": table_name,
            "Key": device_counter_key(device_id),
            "UpdateExpression": "ADD #count :minus",
            "ExpressionAttributeNames": {"#count": "count"},
            "ExpressionAttributeValues": {":minus": -1},
        }})
    assert len(actions) <= MAX_TRANSACTION_ACTIONS
    return actions


# ----------------------------------------------------------------------------- serialization
def _serialize_value(value: Any) -> Dict[str, Any]:
    return _serializer.serialize(_plain(value))


def _plain(value: Any) -> Any:
    """Floats are not accepted by the DynamoDB serializer; the session items
    carry none, but a caller's options might."""
    if isinstance(value, float):
        return int(value) if value.is_integer() else str(value)
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


def serialize_actions(actions: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Plain-Python actions -> the typed ``TransactItems`` of the client API."""
    serialized = []
    for action in actions:
        (kind, body), = action.items()
        typed = dict(body)
        if "Item" in typed:
            typed["Item"] = {k: _serialize_value(v) for k, v in typed["Item"].items()}
        if "Key" in typed:
            typed["Key"] = {k: _serialize_value(v) for k, v in typed["Key"].items()}
        if "ExpressionAttributeValues" in typed:
            typed["ExpressionAttributeValues"] = {
                k: _serialize_value(v) for k, v in typed["ExpressionAttributeValues"].items()}
        serialized.append({kind: typed})
    return serialized


def deserialize_item(item: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not item:
        return {}
    return {k: _deserializer.deserialize(v) for k, v in item.items()}


# ----------------------------------------------------------------------------- cancellation
def cancellation_to_error(reasons: Iterable[Any], actions: List[Dict[str, Any]],
                          devices: Iterable[str]) -> Tuple[str, Dict[str, Any]]:
    """Map a ``TransactionCanceledException``'s ``CancellationReasons`` (one
    per action, in order) to the API's refusal: ``('debug_session_limit',
    {limit: 'usecase' | 'device', active, device?})`` for a counter condition,
    ``('debug_session_exists', {session_id, device})`` for a ``PAIRLOCK`` (or
    META) put whose key already exists. The first failed action decides."""
    device_list = [str(d) for d in devices]
    n = len(device_list)
    for index, reason in enumerate(list(reasons)):
        code = reason.get("Code") if isinstance(reason, dict) else getattr(reason, "Code", None)
        if code in (None, "None"):
            continue
        if code != "ConditionalCheckFailed":
            continue
        existing = deserialize_item(reason.get("Item") if isinstance(reason, dict) else None)
        if index == 0:
            return "debug_session_exists", {"session_id": str(existing.get("session_id") or ""), "device": None}
        if index == n + 1:
            return "debug_session_limit", {"limit": "usecase", "active": _count(existing)}
        if n + 2 <= index < 2 * n + 2:
            device = device_list[index - (n + 2)]
            return "debug_session_limit", {"limit": "device", "active": _count(existing), "device": device}
        if 2 * n + 2 <= index < 3 * n + 2:
            device = device_list[index - (2 * n + 2)]
            return "debug_session_exists", {"session_id": str(existing.get("session_id") or ""), "device": device}
    return "debug_session_limit", {"limit": "usecase", "active": None}


def _count(item: Dict[str, Any]) -> Optional[int]:
    value = item.get("count")
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def dumps(value: Any) -> str:
    """Compact JSON for the audit rows."""
    return json.dumps(value, sort_keys=True, default=str)
