"""
Portal_Sync_Service - camera registry sync reduction core.

Feature: camera-registry-sync (task 5.1).

Pure, side-effect-free reduction of incoming edge camera reports against
the current Camera_Registry state, plus the SQS ingest handler (task 5.4)
that applies `reduce_report` per camera source and `stamp_meta` per
report, persisting the outcomes to the `dda-portal-camera-registry`
table scoped to the device's `usecase_id` (Req 1.4).

Reduction rules (design "Portal_Sync_Service" section):
  - discard_stale: incoming.version < registry_entry.version (Req 3.5)
  - ack matching the entry's portal_change_id -> upsert, synced (Req 5.3)
  - failure entry for the entry's portal_change_id -> failed + reason (Req 5.4)
  - conflict exactly when a pending portal change is unacknowledged and the
    edge content differs from the pending content: edge wins, and a
    ConflictEvent carries both versions, the resolution, and the
    timestamp (Reqs 6.1, 6.2, 6.3)
  - a reported deletion while a portal update is pending resolves as
    deletion-retained with a ConflictEvent (Req 6.5)
  - every processed report stamps the device META item: last_report_at,
    never_synced cleared (Req 3.2)

The reducer is idempotent under duplicate and out-of-order delivery:
re-applying an already-applied report reproduces the same registry entry
(version-guarded), and replaying a report whose conflict was already
resolved reduces to a plain upsert without emitting a second event.
"""
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# SyncOutcome actions
ACTION_UPSERT = "upsert"
ACTION_DISCARD_STALE = "discard_stale"
ACTION_CONFLICT = "conflict"

# Registry entry sync statuses
SYNC_STATUS_SYNCED = "synced"
SYNC_STATUS_PENDING = "pending"
SYNC_STATUS_FAILED = "failed"

# Conflict resolutions
RESOLUTION_EDGE_RETAINED = "edge-retained"
RESOLUTION_DELETION_RETAINED = "deletion-retained"

# Fields that constitute a Camera_Source's comparable "content" for
# conflict classification (Req 6.1). Sync metadata, capability metadata,
# and version counters are deliberately excluded.
_CONTENT_FIELDS = ("name", "type", "params")

# Portal-originated change operation carried in pending_content.
_OP_DELETE = "delete"


@dataclass(frozen=True)
class ConflictEvent:
    """Record of a detected Conflict (Req 6.3).

    Carries both conflicting versions, the resolution applied, and the
    timestamp. `edge_version` is None when the edge state is a deletion
    (deletion-retained, Req 6.5).
    """

    camera_source_id: Optional[str]
    edge_version: Optional[Dict[str, Any]]
    portal_version: Optional[Dict[str, Any]]
    resolution: str
    created_at: int


@dataclass(frozen=True)
class SyncOutcome:
    """Result of reducing one incoming camera-source report.

    action: upsert | discard_stale | conflict
    entry:  the resulting registry entry to persist; None means the
            entry is deleted (or, for discard_stale with no prior
            entry, that there is nothing to persist)
    conflict_event: present exactly when action == conflict
    """

    action: str
    entry: Optional[Dict[str, Any]]
    conflict_event: Optional[ConflictEvent] = None


def _content_of(source: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Project a camera-source-shaped dict onto its comparable content."""
    if not source:
        return {}
    return {field: source.get(field) for field in _CONTENT_FIELDS}


def _is_failure_entry(incoming: Dict[str, Any]) -> bool:
    """Failure entries carry a reason (and no camera content)."""
    return incoming.get("status") == "failed" or (
        "reason" in incoming and "version" not in incoming
    )


def _is_deletion(incoming: Optional[Dict[str, Any]]) -> bool:
    """A reported deletion: the source vanished from a full report.

    The ingest handler represents it as None or {"deleted": True}.
    """
    return incoming is None or incoming.get("deleted") is True


def _carry_identity(
    entry: Dict[str, Any], registry_entry: Optional[Dict[str, Any]]
) -> Dict[str, Any]:
    """Preserve identity/scoping attributes from the existing entry (Req 1.4)."""
    if registry_entry:
        for key in ("usecase_id", "camera_source_id", "device_id"):
            if key in registry_entry:
                entry.setdefault(key, registry_entry[key])
    return entry


def _entry_from_incoming(
    registry_entry: Optional[Dict[str, Any]],
    incoming: Dict[str, Any],
    now_ms: int,
) -> Dict[str, Any]:
    """Build the upserted registry entry from an edge camera report (Req 1.1)."""
    entry: Dict[str, Any] = {
        "name": incoming.get("name"),
        "type": incoming.get("type"),
        "params": incoming.get("params", {}),
        "capabilities": incoming.get("capabilities", {}),
        "origin": incoming.get("origin"),
        "version": incoming.get("version"),
        "last_reported_at": now_ms,
        "absent": bool(incoming.get("absent", False)),
        "sync_status": SYNC_STATUS_SYNCED,
    }
    if entry["absent"] and incoming.get("absentSince") is not None:
        entry["absent_since"] = incoming["absentSince"]
    return _carry_identity(entry, registry_entry)


def _reduce_failure(
    registry_entry: Optional[Dict[str, Any]], incoming: Dict[str, Any]
) -> SyncOutcome:
    """A portal-originated change failed to apply on the device (Req 5.4)."""
    if registry_entry is None:
        # No entry to mark; a failure for an unknown source is stale noise.
        return SyncOutcome(ACTION_DISCARD_STALE, None)
    change_id = incoming.get("portalChangeId")
    if change_id != registry_entry.get("portal_change_id"):
        # Failure report for a superseded change: keep the current entry.
        return SyncOutcome(ACTION_DISCARD_STALE, registry_entry)
    entry = dict(registry_entry)
    entry["sync_status"] = SYNC_STATUS_FAILED
    entry["failure_reason"] = incoming.get("reason")
    return SyncOutcome(ACTION_UPSERT, entry)


def _reduce_deletion(
    registry_entry: Optional[Dict[str, Any]], now_ms: int
) -> SyncOutcome:
    """The source disappeared from the device's full report."""
    if registry_entry is None:
        # Already gone; duplicate delivery is a no-op (idempotent).
        return SyncOutcome(ACTION_UPSERT, None)
    pending_content = registry_entry.get("pending_content") or {}
    if registry_entry.get("sync_status") == SYNC_STATUS_PENDING:
        if pending_content.get("op") == _OP_DELETE:
            # Portal wanted the deletion too: agreement, not a conflict.
            return SyncOutcome(ACTION_UPSERT, None)
        # Edge deletion vs pending portal modification: deletion wins (Req 6.5).
        return SyncOutcome(
            ACTION_CONFLICT,
            None,
            ConflictEvent(
                camera_source_id=registry_entry.get("camera_source_id"),
                edge_version=None,
                portal_version=pending_content or None,
                resolution=RESOLUTION_DELETION_RETAINED,
                created_at=now_ms,
            ),
        )
    return SyncOutcome(ACTION_UPSERT, None)


def reduce_report(
    registry_entry: Optional[Dict[str, Any]],
    incoming: Optional[Dict[str, Any]],
    now_ms: int,
) -> SyncOutcome:
    """Reduce one incoming camera-source state against the registry entry.

    Args:
        registry_entry: the current Camera_Registry entry for this source,
            or None when the source is unknown to the registry.
        incoming: the source's state from the edge report - a camera map
            (design reported-document shape), a failure entry
            ({reason, portalChangeId}), or a deletion marker
            (None / {"deleted": True}) for a source missing from a
            full report.
        now_ms: report processing timestamp (epoch milliseconds).

    Returns:
        SyncOutcome with the action, the resulting entry (None = deleted),
        and a ConflictEvent exactly when a Conflict was classified.
    """
    if incoming is None or _is_deletion(incoming):
        return _reduce_deletion(registry_entry, now_ms)
    if _is_failure_entry(incoming):
        return _reduce_failure(registry_entry, incoming)

    # Version-guarded staleness discard (Req 3.5).
    if registry_entry is not None and (
        (incoming.get("version") or 0) < (registry_entry.get("version") or 0)
    ):
        return SyncOutcome(ACTION_DISCARD_STALE, registry_entry)

    entry = _entry_from_incoming(registry_entry, incoming, now_ms)

    if (
        registry_entry is not None
        and registry_entry.get("sync_status") == SYNC_STATUS_PENDING
    ):
        if incoming.get("ack") == registry_entry.get("portal_change_id"):
            # Device acknowledged the pending portal change (Req 5.3).
            return SyncOutcome(ACTION_UPSERT, entry)
        pending_content = registry_entry.get("pending_content") or {}
        if _content_of(incoming) != _content_of(pending_content):
            # Unacknowledged pending change and diverging edge content:
            # Conflict (Req 6.1). Edge wins (Req 6.2); both versions are
            # preserved in the event (Req 6.3).
            return SyncOutcome(
                ACTION_CONFLICT,
                entry,
                ConflictEvent(
                    camera_source_id=registry_entry.get("camera_source_id"),
                    edge_version=_content_of(incoming),
                    portal_version=pending_content or None,
                    resolution=RESOLUTION_EDGE_RETAINED,
                    created_at=now_ms,
                ),
            )
        # Edge content equals the pending portal content without an ack:
        # the states converged, so the change is effectively applied.
        return SyncOutcome(ACTION_UPSERT, entry)

    return SyncOutcome(ACTION_UPSERT, entry)


def stamp_meta(
    meta_entry: Optional[Dict[str, Any]], now_ms: int
) -> Dict[str, Any]:
    """Stamp the device META item for a processed report (Reqs 1.6, 3.2).

    Sets last_report_at and clears never_synced; idempotent.
    """
    meta = dict(meta_entry) if meta_entry else {}
    meta["last_report_at"] = now_ms
    meta["never_synced"] = False
    return meta


# ---------------------------------------------------------------------------
# SQS ingest handler (task 5.4)
#
# The per-use-case IoT topic rule
#   SELECT *, topic(3) AS thing_name
#   FROM '$aws/things/+/shadow/name/dda-camera-registry/update/documents'
# forwards every shadow documents event to the shadow-report queue. Each SQS
# record body is the shadow documents payload plus the rule-added thing_name.
#
# Batch semantics (partial batch responses, ReportBatchItemFailures):
#   - transient persistence errors -> the record's messageId is returned in
#     batchItemFailures so only that record is retried
#   - malformed / unparseable reports -> logged and dead-lettered via an
#     explicit SendMessage to the DLQ, NOT reported as batch failures, so a
#     bad report never blocks or poisons the batch
# ---------------------------------------------------------------------------

# Sort-key conventions of the dda-portal-camera-registry table (design
# "Data Models"): PK device_id, SK item-type-prefixed.
SK_CAMERA_PREFIX = "CAMERA#"
SK_META = "META"
SK_CONFLICT_PREFIX = "CONFLICT#"


class MalformedReport(Exception):
    """A shadow-report record that can never be processed (dead-letter it)."""


def _dynamodb():
    """DynamoDB resource, created lazily so test mocks are honored."""
    import boto3

    return boto3.resource("dynamodb")


def _registry_table():
    table_name = os.environ.get("CAMERA_REGISTRY_TABLE")
    if not table_name:
        raise RuntimeError("CAMERA_REGISTRY_TABLE not configured")
    return _dynamodb().Table(table_name)


def _resolve_usecase_id(thing_name: str) -> Optional[str]:
    """The device's usecase_id from the portal devices table (Req 1.4)."""
    devices_table = os.environ.get("DEVICES_TABLE")
    if not devices_table:
        return None
    response = _dynamodb().Table(devices_table).get_item(
        Key={"device_id": thing_name}
    )
    usecase_id = (response.get("Item") or {}).get("usecase_id")
    return str(usecase_id) if usecase_id else None


def _parse_record(record: Dict[str, Any]) -> Tuple[str, Optional[Dict[str, Any]]]:
    """Extract (thing_name, reported document | None) from one SQS record.

    Returns None for the report when the shadow document carries no
    reported state (e.g. a desired-only update on a never-reported shadow)
    — nothing to ingest, not an error.

    Raises MalformedReport for anything that can never be processed.
    """
    try:
        # parse_float=Decimal: camera params may carry non-integral numbers
        # and DynamoDB rejects Python floats.
        body = json.loads(record.get("body") or "", parse_float=Decimal)
    except (json.JSONDecodeError, ValueError) as exc:
        raise MalformedReport(f"unparseable message body: {exc}") from exc
    if not isinstance(body, dict):
        raise MalformedReport("message body is not a JSON object")

    thing_name = body.get("thing_name")
    if not thing_name or not isinstance(thing_name, str):
        raise MalformedReport("missing thing_name (topic rule SELECT)")

    state = ((body.get("current") or {}).get("state")) or {}
    if not isinstance(state, dict):
        raise MalformedReport("current.state is not an object")
    reported = state.get("reported")
    if reported is None:
        return thing_name, None
    if not isinstance(reported, dict):
        raise MalformedReport("current.state.reported is not an object")

    cameras = reported.get("cameras", {})
    if not isinstance(cameras, dict) or any(
        not isinstance(value, dict) for value in cameras.values()
    ):
        raise MalformedReport("reported.cameras is not a map of objects")
    failures = reported.get("failures", {})
    if not isinstance(failures, dict) or any(
        not isinstance(value, dict) for value in failures.values()
    ):
        raise MalformedReport("reported.failures is not a map of objects")

    return thing_name, reported


def _load_registry_state(
    table, device_id: str
) -> Tuple[Dict[str, Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Current camera entries (keyed by camera_source_id) and META item."""
    from boto3.dynamodb.conditions import Key

    entries: Dict[str, Dict[str, Any]] = {}
    meta: Optional[Dict[str, Any]] = None
    kwargs: Dict[str, Any] = {
        "KeyConditionExpression": Key("device_id").eq(device_id)
    }
    while True:
        response = table.query(**kwargs)
        for item in response.get("Items", []):
            sk = item.get("sk", "")
            if sk == SK_META:
                meta = item
            elif sk.startswith(SK_CAMERA_PREFIX):
                entries[sk[len(SK_CAMERA_PREFIX):]] = item
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            break
        kwargs["ExclusiveStartKey"] = last_key
    return entries, meta


def _strip_empty(value: Any) -> Any:
    """Drop None values (DynamoDB stores them as NULL noise) recursively."""
    if isinstance(value, dict):
        return {k: _strip_empty(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_strip_empty(v) for v in value]
    return value


def _persist_outcome(
    table,
    device_id: str,
    usecase_id: str,
    csid: str,
    outcome: SyncOutcome,
) -> None:
    """Write one SyncOutcome: camera item upsert/delete + conflict event."""
    if outcome.conflict_event is not None:
        conflict = outcome.conflict_event
        table.put_item(Item=_strip_empty({
            "device_id": device_id,
            "sk": f"{SK_CONFLICT_PREFIX}{conflict.created_at}#{uuid.uuid4()}",
            "usecase_id": usecase_id,
            "camera_source_id": csid,
            "edge_version": conflict.edge_version,
            "portal_version": conflict.portal_version,
            "resolution": conflict.resolution,
            "created_at": conflict.created_at,
        }))

    if outcome.action == ACTION_DISCARD_STALE:
        return  # Req 3.5: the recorded (newer) entry stays untouched.

    camera_key = {"device_id": device_id, "sk": f"{SK_CAMERA_PREFIX}{csid}"}
    if outcome.entry is None:
        table.delete_item(Key=camera_key)
        return
    item = dict(outcome.entry)
    item.update(camera_key)
    item["camera_source_id"] = csid
    item["usecase_id"] = usecase_id  # Req 1.4: scoped to the device's use case
    table.put_item(Item=_strip_empty(item))


def _deletion_candidates(
    entries: Dict[str, Dict[str, Any]], reported: Dict[str, Any]
) -> list:
    """Registry sources missing from the full report -> reported deletions.

    Entries whose pending portal change is a `create` are excluded: the
    device never had the source, so its absence from a full report is
    expected delivery lag, not an edge deletion.
    """
    cameras = reported.get("cameras", {})
    failures = reported.get("failures", {})
    candidates = []
    for csid, entry in entries.items():
        if csid in cameras or csid in failures:
            continue
        pending_content = entry.get("pending_content") or {}
        if (
            entry.get("sync_status") == SYNC_STATUS_PENDING
            and pending_content.get("op") == "create"
        ):
            continue
        candidates.append(csid)
    return candidates


def _process_report(
    thing_name: str,
    reported: Dict[str, Any],
    usecase_id: Optional[str] = None,
) -> None:
    """Reduce one shadow report into the registry (Reqs 1.4, 3.2).

    `usecase_id` may be supplied by callers that already resolved and
    authorized the device's Use_Case (the camera_registry.py refresh
    route, which runs this same reduction over an on-demand
    GetThingShadow pull); the SQS ingest path resolves it from the
    devices table.
    """
    if usecase_id is None:
        usecase_id = _resolve_usecase_id(thing_name)
    if not usecase_id:
        raise MalformedReport(
            f"device '{thing_name}' has no usecase_id in the devices table"
        )

    table = _registry_table()
    entries, meta = _load_registry_state(table, thing_name)

    reported_at = reported.get("reportedAt")
    now_ms = int(reported_at) if reported_at is not None else int(time.time() * 1000)

    # Camera state entries from the report.
    for csid, incoming in reported.get("cameras", {}).items():
        outcome = reduce_report(entries.get(csid), incoming, now_ms)
        _persist_outcome(table, thing_name, usecase_id, csid, outcome)

    # Failure entries: portal-originated changes the device rejected (5.4).
    for csid, failure in reported.get("failures", {}).items():
        incoming = {"status": "failed", **failure}
        outcome = reduce_report(entries.get(csid), incoming, now_ms)
        _persist_outcome(table, thing_name, usecase_id, csid, outcome)

    # Sources missing from the full report: reported deletions.
    for csid in _deletion_candidates(entries, reported):
        outcome = reduce_report(entries.get(csid), None, now_ms)
        _persist_outcome(table, thing_name, usecase_id, csid, outcome)

    # Every processed report stamps the device META item (Reqs 1.6, 3.2).
    meta_item = stamp_meta(meta, now_ms)
    meta_item["device_id"] = thing_name
    meta_item["sk"] = SK_META
    meta_item["usecase_id"] = usecase_id
    table.put_item(Item=_strip_empty(meta_item))


def _dead_letter(record: Dict[str, Any], reason: str) -> bool:
    """Send a malformed record to the DLQ; True when the send succeeded."""
    import boto3

    dlq_url = os.environ.get("CAMERA_SHADOW_REPORT_DLQ_URL")
    if not dlq_url:
        logger.error("CAMERA_SHADOW_REPORT_DLQ_URL not configured; "
                      "cannot dead-letter malformed report")
        return False
    try:
        boto3.client("sqs").send_message(
            QueueUrl=dlq_url,
            MessageBody=record.get("body") or "",
            MessageAttributes={
                "deadLetterReason": {
                    "DataType": "String",
                    # SQS message attributes cap at 256 chars comfortably
                    "StringValue": reason[:256] or "unknown",
                },
            },
        )
        return True
    except Exception:  # noqa: BLE001 - DLQ send is best-effort
        logger.exception("Failed to dead-letter malformed shadow report")
        return False


def handler(event, context):
    """SQS ingest: reduce shadow documents events into the Camera_Registry.

    Returns a partial batch response ({"batchItemFailures": [...]}) so that
    transient persistence errors retry only the affected record. Malformed
    or unparseable reports are logged and dead-lettered explicitly and are
    NOT reported as batch failures (they would never succeed on retry).
    """
    batch_item_failures = []
    for record in (event or {}).get("Records", []):
        message_id = record.get("messageId")
        try:
            thing_name, reported = _parse_record(record)
            if reported is None:
                logger.info(
                    "Shadow documents event for '%s' carries no reported "
                    "state; nothing to ingest", thing_name)
                continue
            _process_report(thing_name, reported)
        except MalformedReport as exc:
            logger.error(
                "Malformed camera shadow report (message %s): %s",
                message_id, exc)
            if not _dead_letter(record, str(exc)) and message_id:
                # Could not preserve the message; let SQS retry/redrive it.
                batch_item_failures.append({"itemIdentifier": message_id})
        except Exception:  # noqa: BLE001 - transient failure: retry the record
            logger.exception(
                "Transient failure processing camera shadow report "
                "(message %s); reporting batch item failure", message_id)
            if message_id:
                batch_item_failures.append({"itemIdentifier": message_id})
    return {"batchItemFailures": batch_item_failures}
