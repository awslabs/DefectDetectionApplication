"""
Pin_Request lifecycle core (cloud-static-camera-provisioning task 1.1).

Pure/AWS-light Pin_Request logic bundled into the Camera_Registry Lambda
asset (the camera_sync.py precedent): the confirmation reducer, the
condition-guarded persistence helpers enforcing the single
``pending -> {applied, failed, superseded}`` transition (Req 4.1), the
supersede helper (Req 5.3), the desired-document builder for the
``desired.staticImagePin`` Sync_Channel slot, and the provisioning status
view builder served by the Portal_Pin_API status route.

Storage (design "Data Models"): ``PIN_REQUEST#`` items in the
``dda-portal-camera-registry`` table — PK ``device_id``, SK
``PIN_REQUEST#{createdAtMs:014d}#{uuid8}`` — zero-padded so lexicographic
SK order is creation order and "most recent" = highest SK (Req 4.4). The
``pin_request_id`` is the SK suffix and doubles as the shadow
``requestId``, so a device confirmation resolves its item with a single
GetItem (no scan).

Lifecycle rules:
  - exactly one Sync_Status at a time; the only transition is out of
    ``pending``, to exactly one of ``applied`` / ``failed`` /
    ``superseded``, enforced with a DynamoDB ``ConditionExpression`` so
    racing confirmations and supersedes cannot double-transition (Req 4.1)
  - confirmations referencing an unknown or non-``pending`` Pin_Request
    change nothing (Reqs 4.8, 5.6); duplicate delivery is idempotent
  - an ``applied`` confirmation records the device-reported metadata and
    the confirmation timestamp (Req 4.2); a failure report records the
    device-reported reason and timestamp (Req 4.3)
  - on any terminal transition the canonical Image_Transport object is
    deleted best-effort (Open Decision 4); nothing here expires by time
    (Reqs 2.6, 5.1)

Every helper takes its table / S3 client as a parameter so tests inject
fakes; boto3 condition builders are imported lazily so importing this
module never touches AWS.
"""
import json
import logging
import time
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Sync_Status values (Req 4.1).
STATUS_PENDING = "pending"
STATUS_APPLIED = "applied"
STATUS_FAILED = "failed"
STATUS_SUPERSEDED = "superseded"
TERMINAL_STATUSES = (STATUS_APPLIED, STATUS_FAILED, STATUS_SUPERSEDED)

# Pin_Request operation types.
OP_PIN = "pin"
OP_REMOVE = "remove"

# Item-type SK prefix (dda-portal-camera-registry layout).
SK_PIN_REQUEST_PREFIX = "PIN_REQUEST#"

# The fixed Static_Image_Camera identifier and its registry entry SK —
# the device-report-driven record backing ``deviceReported`` (Req 4.6).
STATIC_IMAGE_CAMERA_ID = "static-image-camera"
SK_STATIC_IMAGE_CAMERA = f"CAMERA#{STATIC_IMAGE_CAMERA_ID}"

# Portal-enforced bound on the serialized desired.staticImagePin section
# (design Decision 1's 8 KB shadow budget; Reqs 2.3, 2.4).
MAX_DESIRED_SECTION_BYTES = 1024
# fileName is truncated for the shadow echo; the original full name stays
# on the Pin_Request item.
MAX_FILE_NAME_CHARS = 128

# Status-response connectivity values (Req 4.5).
CONNECTIVITY_CONNECTED = "connected"
CONNECTIVITY_DISCONNECTED = "disconnected"

# Confirmation reduction actions.
ACTION_TRANSITION = "transition"
ACTION_NOOP = "noop"


@dataclass(frozen=True)
class PinOutcome:
    """Result of reducing one ``reported.staticImagePin`` document.

    action: transition | noop
    to_status / device_metadata / failure_reason / completed_at are set
    exactly when action == transition.
    """

    action: str
    to_status: Optional[str] = None
    device_metadata: Optional[Dict[str, Any]] = None
    failure_reason: Optional[str] = None
    completed_at: Optional[int] = None


# ---------------------------------------------------------------------------
# Identifiers and item shapes
# ---------------------------------------------------------------------------

def new_pin_request_id(created_at_ms: int) -> str:
    """A fresh Pin_Request id: ``{createdAtMs:014d}#{uuid8}``.

    Zero-padded so lexicographic order is creation order (Req 4.4); the
    id is also the shadow ``requestId``.
    """
    return f"{int(created_at_ms):014d}#{uuid.uuid4().hex[:8]}"


def pin_request_sk(pin_request_id: str) -> str:
    return f"{SK_PIN_REQUEST_PREFIX}{pin_request_id}"


def build_pin_request_item(
    device_id: str,
    usecase_id: str,
    op: str,
    created_at_ms: int,
    *,
    pin_request_id: Optional[str] = None,
    s3_bucket: Optional[str] = None,
    s3_key: Optional[str] = None,
    sha256: Optional[str] = None,
    size_bytes: Optional[int] = None,
    image_format: Optional[str] = None,
    file_name: Optional[str] = None,
) -> Dict[str, Any]:
    """A new ``pending`` Pin_Request item (design "Data Models")."""
    if op not in (OP_PIN, OP_REMOVE):
        raise ValueError(f"unknown Pin_Request op: {op!r}")
    pin_request_id = pin_request_id or new_pin_request_id(created_at_ms)
    item: Dict[str, Any] = {
        "device_id": device_id,
        "sk": pin_request_sk(pin_request_id),
        "pin_request_id": pin_request_id,
        "usecase_id": usecase_id,
        "op": op,
        "status": STATUS_PENDING,
        "created_at": int(created_at_ms),
    }
    if op == OP_PIN:
        item.update({
            "s3_bucket": s3_bucket,
            "s3_key": s3_key,
            "sha256": sha256,
            "size_bytes": None if size_bytes is None else int(size_bytes),
            "format": image_format,
            "file_name": file_name,
        })
    return item


def build_desired_document(
    item: Dict[str, Any], *, max_file_name_chars: int = MAX_FILE_NAME_CHARS
) -> Dict[str, Any]:
    """The ``desired.staticImagePin`` document for a Pin_Request item.

    Carries the Image_Transport reference, the Content_Checksum, and the
    size/format metadata — never image bytes (Req 2.2). ``op: remove``
    documents carry only requestId / op / requestedAtEpochMs. fileName is
    truncated to 128 characters (the original stays on the item).
    """
    document: Dict[str, Any] = {
        "requestId": item["pin_request_id"],
        "op": item.get("op"),
        "requestedAtEpochMs": _to_int(item.get("created_at")),
    }
    if item.get("op") == OP_PIN:
        document.update({
            "bucket": item.get("s3_bucket"),
            "key": item.get("s3_key"),
            "sha256": item.get("sha256"),
            "sizeBytes": _to_int(item.get("size_bytes")),
            "format": item.get("format"),
            "fileName": str(item.get("file_name") or "")[:max_file_name_chars],
        })
    return document


def desired_document_size_bytes(document: Dict[str, Any]) -> int:
    """Serialized size of a desired document, for the ≤ 1024 B bound
    enforced before any Sync_Channel write (Reqs 2.3, 2.4)."""
    return len(json.dumps(document, separators=(",", ":"),
                          default=str).encode("utf-8"))


# ---------------------------------------------------------------------------
# Confirmation reduction (pure)
# ---------------------------------------------------------------------------

def reduce_pin_confirmation(
    pin_item: Optional[Dict[str, Any]],
    reported: Dict[str, Any],
    now_ms: int,
) -> PinOutcome:
    """Reduce one ``reported.staticImagePin`` document against its item.

    - unknown requestId or non-``pending`` item -> no-op (Reqs 4.1, 4.8,
      5.6); duplicate delivery of an already-reduced confirmation is
      therefore idempotent
    - ``applied`` on a ``pending`` item -> applied + device-reported
      metadata + confirmation timestamp (Req 4.2)
    - ``failed`` on a ``pending`` item -> failed + device-reported reason
      + timestamp (Req 4.3)
    """
    if not isinstance(reported, dict) or pin_item is None:
        return PinOutcome(ACTION_NOOP)
    if pin_item.get("status") != STATUS_PENDING:
        return PinOutcome(ACTION_NOOP)
    if reported.get("requestId") != pin_item.get("pin_request_id"):
        return PinOutcome(ACTION_NOOP)

    reported_status = reported.get("status")
    completed_at = _to_int(reported.get("completedAtEpochMs"))
    if completed_at is None:
        completed_at = int(now_ms)

    if reported_status == STATUS_APPLIED:
        metadata = reported.get("metadata")
        return PinOutcome(
            ACTION_TRANSITION,
            to_status=STATUS_APPLIED,
            device_metadata=metadata if isinstance(metadata, dict) else None,
            completed_at=completed_at,
        )
    if reported_status == STATUS_FAILED:
        reason = reported.get("reason")
        return PinOutcome(
            ACTION_TRANSITION,
            to_status=STATUS_FAILED,
            failure_reason=None if reason is None else str(reason),
            completed_at=completed_at,
        )
    return PinOutcome(ACTION_NOOP)


# ---------------------------------------------------------------------------
# Persistence helpers (table / s3 clients injected by the caller)
# ---------------------------------------------------------------------------

def query_pin_request_items(
    table, device_id: str, limit: Optional[int] = None
) -> List[Dict[str, Any]]:
    """The device's Pin_Request items, newest first (highest SK first)."""
    from boto3.dynamodb.conditions import Key

    kwargs: Dict[str, Any] = {
        "KeyConditionExpression": (
            Key("device_id").eq(device_id)
            & Key("sk").begins_with(SK_PIN_REQUEST_PREFIX)
        ),
        "ScanIndexForward": False,
    }
    if limit is not None:
        kwargs["Limit"] = limit
    items: List[Dict[str, Any]] = []
    while True:
        response = table.query(**kwargs)
        items.extend(response.get("Items", []))
        if limit is not None and len(items) >= limit:
            return items[:limit]
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            return items
        kwargs["ExclusiveStartKey"] = last_key


def get_pin_request_item(
    table, device_id: str, pin_request_id: str
) -> Optional[Dict[str, Any]]:
    """One Pin_Request item by its id (SK derivable — no scan)."""
    response = table.get_item(
        Key={"device_id": device_id, "sk": pin_request_sk(pin_request_id)}
    )
    return response.get("Item")


def insert_pin_request(table, item: Dict[str, Any]) -> None:
    """Write a freshly built ``pending`` Pin_Request item."""
    table.put_item(Item=_strip_none(item))


def transition_pin_request(
    table,
    item: Dict[str, Any],
    to_status: str,
    *,
    completed_at_ms: Optional[int] = None,
    device_metadata: Optional[Dict[str, Any]] = None,
    failure_reason: Optional[str] = None,
    s3_client=None,
) -> bool:
    """The single condition-guarded ``pending -> terminal`` transition.

    Returns False (leaving the item untouched) when the item is no longer
    ``pending`` — racing confirmations and supersedes cannot
    double-transition (Req 4.1). On success the canonical Image_Transport
    object is deleted best-effort (the request left ``pending``).
    """
    if to_status not in TERMINAL_STATUSES:
        raise ValueError(f"not a terminal Sync_Status: {to_status!r}")
    from botocore.exceptions import ClientError

    set_clauses = ["#status = :status"]
    values: Dict[str, Any] = {
        ":pending": STATUS_PENDING,
        ":status": to_status,
    }
    if completed_at_ms is not None:
        set_clauses.append("completed_at = :completed_at")
        values[":completed_at"] = int(completed_at_ms)
    if device_metadata is not None:
        set_clauses.append("device_metadata = :metadata")
        values[":metadata"] = _dynamo_safe(device_metadata)
    if failure_reason is not None:
        set_clauses.append("failure_reason = :reason")
        values[":reason"] = failure_reason
    try:
        table.update_item(
            Key={"device_id": item["device_id"], "sk": item["sk"]},
            UpdateExpression="SET " + ", ".join(set_clauses),
            ConditionExpression="#status = :pending",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues=values,
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == \
                "ConditionalCheckFailedException":
            return False
        raise
    _delete_canonical_object_best_effort(s3_client, item)
    return True


def supersede_pending_requests(
    table, device_id: str, now_ms: int, s3_client=None
) -> List[str]:
    """Transition the device's ``pending`` Pin_Request (if any) to
    ``superseded`` — called before a new submission's item is written, so
    at most one Pin_Request is ever ``pending`` (Req 5.3).

    Returns the superseded Pin_Request ids (defensively handles more than
    one pending item, though the invariant permits at most one).
    """
    superseded: List[str] = []
    for item in query_pin_request_items(table, device_id):
        if item.get("status") != STATUS_PENDING:
            continue
        if transition_pin_request(table, item, STATUS_SUPERSEDED,
                                  completed_at_ms=now_ms,
                                  s3_client=s3_client):
            superseded.append(item["pin_request_id"])
    return superseded


def apply_pin_confirmation(
    table,
    device_id: str,
    reported: Dict[str, Any],
    now_ms: Optional[int] = None,
    s3_client=None,
) -> Optional[str]:
    """Ingest entrypoint: reduce a ``reported.staticImagePin`` document
    and persist the transition it implies.

    Returns the new Sync_Status when a transition was applied, None when
    the confirmation changed nothing (unknown / non-pending request,
    malformed document, or a racing transition won).
    """
    if not isinstance(reported, dict):
        return None
    request_id = reported.get("requestId")
    if not request_id or not isinstance(request_id, str):
        return None
    now = int(now_ms) if now_ms is not None else int(time.time() * 1000)
    item = get_pin_request_item(table, device_id, request_id)
    outcome = reduce_pin_confirmation(item, reported, now)
    if outcome.action != ACTION_TRANSITION:
        return None
    transitioned = transition_pin_request(
        table, item, outcome.to_status,
        completed_at_ms=outcome.completed_at,
        device_metadata=outcome.device_metadata,
        failure_reason=outcome.failure_reason,
        s3_client=s3_client,
    )
    return outcome.to_status if transitioned else None


def _delete_canonical_object_best_effort(s3_client, item: Dict[str, Any]):
    """Delete a pin-type request's canonical Image_Transport object.

    Best-effort by design (Open Decision 4): a delete failure never fails
    the lifecycle transition. Removal-type requests carry no object.
    """
    if s3_client is None:
        return
    bucket = item.get("s3_bucket")
    key = item.get("s3_key")
    if not bucket or not key:
        return
    try:
        s3_client.delete_object(Bucket=bucket, Key=key)
    except Exception:  # noqa: BLE001 — best-effort cleanup only
        logger.warning(
            "Best-effort delete of canonical pin object s3://%s/%s failed",
            bucket, key, exc_info=True)


# ---------------------------------------------------------------------------
# Status view (Reqs 1.7, 1.10, 4.4–4.8, 5.7)
# ---------------------------------------------------------------------------

def map_connectivity(raw_status: Optional[str]) -> str:
    """Greengrass core-device status -> exactly ``connected`` (HEALTHY)
    or ``disconnected`` (anything else) (Req 4.5)."""
    return (CONNECTIVITY_CONNECTED if raw_status == "HEALTHY"
            else CONNECTIVITY_DISCONNECTED)


def build_status_view(
    device_id: str,
    items: List[Dict[str, Any]],
    *,
    usecase_id: Optional[str] = None,
    camera_entry: Optional[Dict[str, Any]] = None,
    connectivity_provider: Optional[Callable[[], Optional[str]]] = None,
    history_limit: Optional[int] = 50,
) -> Dict[str, Any]:
    """The provisioning status response for one Target_Device.

    - ``latest`` is the most recent non-``superseded`` Pin_Request
      (identifier, Sync_Status, operation type, creation timestamp —
      Req 4.4); superseded requests are excluded from the current state
      but retained in ``history`` (Req 5.7). ``latest`` is null and
      ``noPinRequest`` true for a device with zero Pin_Requests
      (Reqs 1.10, 4.7 — a response, not an error).
    - ``deviceMetadata`` is included exactly when the most recent
      pin-type Pin_Request is ``applied`` (Req 1.7).
    - ``connectivity`` (``connected`` | ``disconnected``) is included
      while ``latest`` is ``pending``; the provider is only invoked then
      (Req 4.5).
    - ``deviceReported`` is derived from the ``CAMERA#static-image-camera``
      registry entry — the device-report-driven record — and is presented
      as the current state even when it disagrees with the recorded
      outcome (Reqs 4.6, 4.8).

    ``items`` may be the raw pin-request query result or a full device
    item list; non-Pin_Request items are ignored.
    """
    requests = sorted(
        (item for item in items
         if str(item.get("sk", "")).startswith(SK_PIN_REQUEST_PREFIX)
         and item.get("pin_request_id")),
        key=lambda item: str(item["pin_request_id"]),
        reverse=True,
    )
    if usecase_id is None:
        usecase_id = next(
            (item["usecase_id"] for item in requests
             if item.get("usecase_id")), None)

    latest_item = next(
        (item for item in requests
         if item.get("status") != STATUS_SUPERSEDED), None)

    view: Dict[str, Any] = {
        "deviceId": device_id,
        "usecaseId": usecase_id,
        "latest": _latest_view(latest_item),
        "noPinRequest": len(requests) == 0,
        "deviceReported": _device_reported_view(camera_entry),
        "history": [_history_view(item)
                    for item in requests[:history_limit]],
    }

    if (latest_item is not None
            and latest_item.get("status") == STATUS_PENDING
            and connectivity_provider is not None):
        view["connectivity"] = map_connectivity(connectivity_provider())

    latest_pin = next(
        (item for item in requests if item.get("op") == OP_PIN), None)
    if (latest_pin is not None
            and latest_pin.get("status") == STATUS_APPLIED
            and latest_pin.get("device_metadata")):
        view["deviceMetadata"] = _clean(latest_pin["device_metadata"])

    return view


def _latest_view(item: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if item is None:
        return None
    latest: Dict[str, Any] = {
        "pinRequestId": item["pin_request_id"],
        "op": item.get("op"),
        "status": item.get("status"),
        "createdAt": _to_int(item.get("created_at")),
    }
    if item.get("completed_at") is not None:
        latest["completedAt"] = _to_int(item["completed_at"])
    if item.get("failure_reason") is not None:
        latest["failureReason"] = item["failure_reason"]
    if item.get("device_metadata"):
        latest["deviceMetadata"] = _clean(item["device_metadata"])
    return latest


def _history_view(item: Dict[str, Any]) -> Dict[str, Any]:
    """One history record (Req 5.7: id, op, creation timestamp, status)."""
    return {
        "pinRequestId": item["pin_request_id"],
        "op": item.get("op"),
        "status": item.get("status"),
        "createdAt": _to_int(item.get("created_at")),
    }


def _device_reported_view(
    camera_entry: Optional[Dict[str, Any]]
) -> Optional[Dict[str, Any]]:
    """Device-reported pinned state from the static-camera registry entry."""
    if camera_entry is None:
        return None
    absent = bool(camera_entry.get("absent", False))
    reported: Dict[str, Any] = {"present": not absent, "absent": absent}
    if camera_entry.get("absent_since") is not None:
        reported["absentSince"] = _to_int(camera_entry["absent_since"])
    return reported


# ---------------------------------------------------------------------------
# Small conversions
# ---------------------------------------------------------------------------

def _to_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _clean(value: Any) -> Any:
    """DynamoDB Decimals -> plain JSON numbers, recursively."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() \
            else float(value)
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_clean(v) for v in value]
    return value


def _dynamo_safe(value: Any) -> Any:
    """Python floats -> Decimal, recursively (DynamoDB rejects floats)."""
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: _dynamo_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_dynamo_safe(v) for v in value]
    return value


def _strip_none(value: Any) -> Any:
    """Drop None values (DynamoDB stores them as NULL noise) recursively."""
    if isinstance(value, dict):
        return {k: _strip_none(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_strip_none(v) for v in value]
    return value
