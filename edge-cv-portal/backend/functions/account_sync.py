"""
Account_Sync_Service - portal-side sync Lambda.

Feature: portal-user-manager (task 3.4: sync-attempt entry).

One Lambda with three entry paths, routed by event shape (mirroring
camera_sync.py's style):

  - Sync attempt (this task): invoked directly by user_admin.py
    ({action: 'sync_attempt', device_id, syncId}) after staging, and by
    the EventBridge rate(5 minutes) schedule for every device with
    pending changes (Req 7.7). Builds the device's full desired account
    document from the staged set in `dda-portal-account-sync`, writes
    the `dda-user-accounts` named-shadow desired state via
    update_thing_shadow, and stamps the row `in_progress` with
    `attemptAt`. A shadow-write failure or size-limit violation marks
    the attempt `failed` with the reason; pending changes are retained
    (Req 7.6).

  - Ack ingest (task 3.5): SQS records from the IoT topic rule on
    $aws/things/+/shadow/name/dda-user-accounts/update/documents
    (partial-batch-failure pattern, camera_sync.py conventions). A
    reported ackSyncId equal to the row's current syncId marks it
    `success` with lastSyncAt = appliedAt and clears pendingChanges
    (Reqs 7.4, 7.5); a reported error marks `failed` with the device's
    reason, pending retained; stale acks (ackSyncId != current syncId)
    are discarded without any state change. Malformed records are
    logged and dead-lettered, never endlessly redelivered.

  - Timeout sweep (task 3.5): piggybacked on the 5-minute schedule.
    in_progress rows whose attemptAt is older than 60 s without an ack
    are marked `failed` / `device unreachable` (Req 7.9); the staged
    set and pendingChanges are retained for the next scheduled retry
    (Req 7.6).
"""
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# The per-thing named shadow carrying the desired account set.
USER_ACCOUNTS_SHADOW_NAME = "dda-user-accounts"

# Sync-state row statuses (dda-portal-account-sync data model).
STATUS_PENDING = "pending"
STATUS_IN_PROGRESS = "in_progress"
STATUS_SUCCESS = "success"
STATUS_FAILED = "failed"

# An in_progress attempt without an ack within this window is recorded
# failed with REASON_DEVICE_UNREACHABLE (Req 7.9).
ACK_TIMEOUT_MS = 60 * 1000
REASON_DEVICE_UNREACHABLE = "device unreachable"

# --- Pure sync-document builder --------------------------------------------
#
# Own copy of the builder consistent with user_admin.build_sync_document
# (separate Lambda packages cannot import each other's function modules;
# the camera-registry-sync feature duplicates shared pure logic the same
# way).

# Shadow dda-user-accounts document schema version (design data model).
SYNC_DOCUMENT_VERSION = 1

# AWS IoT named-shadow document size limit the rendered desired state
# must fit within (design: validate against the 8 KB shadow limit).
SHADOW_SIZE_LIMIT_BYTES = 8 * 1024


class SyncDocumentTooLarge(ValueError):
    """The rendered sync document exceeds the 8 KB shadow size limit."""


def build_sync_document(accounts: Dict[str, Dict[str, Any]],
                        sync_id: str) -> Dict[str, Any]:
    """
    Build the complete desired sync document for one device from a staged
    account set (pure function, design data model for the
    dda-user-accounts shadow).

    Each record carries only {email, role, enabled, deleted?, verifier?}
    - the fields are copied by an explicit whitelist so plaintext
    passwords can never appear in a sync payload no matter what the
    input carries (Req 7.3). Disabled or deleted accounts are marked
    `enabled: false` and are never dropped from the document (Req 7.8).

    Raises SyncDocumentTooLarge when the rendered desired state exceeds
    the 8 KB shadow limit, with an explicit reason.
    """
    doc_accounts = {}
    for username, record in (accounts or {}).items():
        record = record or {}
        deleted = bool(record.get("deleted", False))
        enabled = bool(record.get("enabled", False)) and not deleted

        entry: Dict[str, Any] = {
            "email": record.get("email", ""),
            "role": record.get("role") or "Viewer",
            "enabled": enabled,
        }
        if deleted:
            entry["deleted"] = True

        verifier = record.get("verifier")
        if verifier:
            entry["verifier"] = {
                "algorithm": verifier.get("algorithm"),
                "iterations": int(verifier.get("iterations", 0)),
                "salt": verifier.get("salt"),
                "hash": verifier.get("hash"),
            }

        doc_accounts[username] = entry

    document = {
        "syncId": sync_id,
        "version": SYNC_DOCUMENT_VERSION,
        "accounts": doc_accounts,
    }

    rendered = json.dumps({"state": {"desired": document}},
                          separators=(",", ":"))
    size = len(rendered.encode("utf-8"))
    if size > SHADOW_SIZE_LIMIT_BYTES:
        raise SyncDocumentTooLarge(
            f"The rendered sync document is {size} bytes, exceeding the "
            f"{SHADOW_SIZE_LIMIT_BYTES}-byte (8 KB) IoT shadow limit; "
            f"reduce the number of selected accounts"
        )
    return document


# --- Lazy AWS accessors -----------------------------------------------------
# Created lazily (camera_sync.py style) so test mocks are honored.

def _dynamodb():
    import boto3

    return boto3.resource("dynamodb")


def _sync_table():
    table_name = os.environ.get(
        "ACCOUNT_SYNC_TABLE", "dda-portal-account-sync")
    return _dynamodb().Table(table_name)


def _resolve_usecase_id(device_id: str) -> Optional[str]:
    """The device's usecase_id from the portal devices table, or None."""
    devices_table = os.environ.get("DEVICES_TABLE")
    if not devices_table:
        return None
    try:
        response = _dynamodb().Table(devices_table).get_item(
            Key={"device_id": device_id})
    except Exception as exc:  # noqa: BLE001 - fall back to the local client
        logger.warning(
            "Could not resolve usecase for device %s: %s", device_id, exc)
        return None
    usecase_id = (response.get("Item") or {}).get("usecase_id")
    return str(usecase_id) if usecase_id else None


def iot_data_client(device_id: str):
    """iot-data client for the device's Use_Case account.

    Assumed-role (or single-account) client when the device resolves to
    a Use_Case (camera_registry.py pattern); the Lambda's own iot-data
    client otherwise.
    """
    usecase_id = _resolve_usecase_id(device_id)
    if usecase_id:
        from shared_utils import (get_usecase, get_usecase_client,
                                  get_usecase_region)

        usecase = get_usecase(usecase_id)
        return get_usecase_client("iot-data", usecase,
                                  region=get_usecase_region(usecase))
    import boto3

    return boto3.client("iot-data")


# --- Sync-state row stamping ------------------------------------------------

def _stamp_in_progress(device_id: str, now_ms: int) -> None:
    """The shadow desired write succeeded: the attempt is in flight,
    awaiting the device ack (ingested by task 3.5). pendingChanges is
    deliberately untouched - only a matching ack clears it."""
    _sync_table().update_item(
        Key={"device_id": device_id},
        UpdateExpression=(
            "SET #st = :in_progress, attemptAt = :now "
            "REMOVE failureReason"),
        ExpressionAttributeNames={"#st": "status"},
        ExpressionAttributeValues={
            ":in_progress": STATUS_IN_PROGRESS,
            ":now": now_ms,
        },
    )


def _stamp_failed(device_id: str, reason: str, now_ms: int) -> None:
    """The attempt failed (shadow write error or size-limit violation):
    record the reason. pendingChanges is retained so the staged set is
    retried by the 5-minute schedule (Req 7.6)."""
    _sync_table().update_item(
        Key={"device_id": device_id},
        UpdateExpression=(
            "SET #st = :failed, failureReason = :reason, attemptAt = :now"),
        ExpressionAttributeNames={"#st": "status"},
        ExpressionAttributeValues={
            ":failed": STATUS_FAILED,
            ":reason": reason,
            ":now": now_ms,
        },
    )


# --- Sync attempt ------------------------------------------------------------

def attempt_sync(device_id: str) -> Dict[str, Any]:
    """Attempt delivery of a device's staged account set (Reqs 7.6, 7.7).

    Builds the desired document from the row's *current* staged set and
    syncId (a direct invoke's syncId may already be superseded by an
    attribute-change hook), writes the dda-user-accounts shadow desired
    state, and stamps the row in_progress with attemptAt. Any failure
    marks the row failed with the reason; pending changes are retained.
    """
    row = _sync_table().get_item(
        Key={"device_id": device_id}).get("Item")
    if not row:
        logger.info("No staged account sync for device %s; nothing to "
                    "attempt", device_id)
        return {"device_id": device_id, "status": "skipped",
                "reason": "no staged sync"}
    if not row.get("pendingChanges"):
        logger.info("Device %s has no pending account changes; nothing "
                    "to attempt", device_id)
        return {"device_id": device_id, "status": "skipped",
                "reason": "no pending changes"}

    sync_id = row.get("syncId") or ""
    now_ms = int(time.time() * 1000)

    try:
        document = build_sync_document(row.get("accounts") or {}, sync_id)
    except SyncDocumentTooLarge as exc:
        logger.error("Account sync for %s failed: %s", device_id, exc)
        _stamp_failed(device_id, str(exc), now_ms)
        return {"device_id": device_id, "syncId": sync_id,
                "status": STATUS_FAILED, "reason": str(exc)}

    try:
        client = iot_data_client(device_id)
        client.update_thing_shadow(
            thingName=device_id,
            shadowName=USER_ACCOUNTS_SHADOW_NAME,
            payload=json.dumps({"state": {"desired": document}},
                               separators=(",", ":")),
        )
    except Exception as exc:  # noqa: BLE001 - any shadow-path failure
        reason = f"shadow write failed: {exc}"
        logger.error("Account sync for %s failed: %s", device_id, reason)
        _stamp_failed(device_id, reason, now_ms)
        return {"device_id": device_id, "syncId": sync_id,
                "status": STATUS_FAILED, "reason": reason}

    _stamp_in_progress(device_id, now_ms)
    logger.info("Account sync attempt for %s in progress (syncId %s, "
                "%d account(s))", device_id, sync_id,
                len(document["accounts"]))
    return {"device_id": device_id, "syncId": sync_id,
            "status": STATUS_IN_PROGRESS}


def _pending_device_ids() -> List[str]:
    """Every device with undelivered pending account changes (Req 7.7)."""
    from boto3.dynamodb.conditions import Attr

    device_ids: List[str] = []
    scan_kwargs: Dict[str, Any] = {
        "FilterExpression": Attr("pendingChanges").eq(True),
        "ProjectionExpression": "device_id",
    }
    table = _sync_table()
    while True:
        page = table.scan(**scan_kwargs)
        device_ids.extend(
            item["device_id"] for item in page.get("Items", []))
        last_key = page.get("LastEvaluatedKey")
        if not last_key:
            return device_ids
        scan_kwargs["ExclusiveStartKey"] = last_key


# --- Entry paths --------------------------------------------------------------

def _handle_direct_invoke(event: Dict[str, Any]) -> Dict[str, Any]:
    """user_admin.py invoke: {action: 'sync_attempt', device_id, syncId}."""
    device_id = event.get("device_id")
    if not device_id:
        logger.error("sync_attempt invoke without a device_id: %s", event)
        return {"status": "skipped", "reason": "no device_id"}
    invoked_sync_id = event.get("syncId")
    result = attempt_sync(device_id)
    if invoked_sync_id and result.get("syncId") not in (None,
                                                        invoked_sync_id):
        # The staged set was refreshed between staging and this invoke;
        # the row's current syncId was delivered, which supersedes the
        # invoke's.
        logger.info(
            "Invoked syncId %s superseded by %s for device %s",
            invoked_sync_id, result.get("syncId"), device_id)
    return result


def _handle_schedule() -> Dict[str, Any]:
    """EventBridge rate(5 minutes): sweep timeouts, then attempt delivery
    for every device with pending changes (Req 7.7)."""
    _sweep_timeouts()
    results = [attempt_sync(device_id)
               for device_id in _pending_device_ids()]
    logger.info("Scheduled account sync pass attempted %d device(s)",
                len(results))
    return {"attempted": len(results), "results": results}


def _sweep_timeouts() -> None:
    """in_progress rows whose attemptAt is older than 60 s without an
    ack are marked failed / device unreachable (Req 7.9).

    An ack would have moved the row off in_progress, so status alone
    identifies "no ack". The update is conditional on the row still
    being in_progress past the threshold so a racing ack or fresh
    attempt is never clobbered. pendingChanges and the staged set are
    retained, so the next scheduled pass retries (Reqs 7.6, 7.7).
    """
    from boto3.dynamodb.conditions import Attr
    from botocore.exceptions import ClientError

    threshold = int(time.time() * 1000) - ACK_TIMEOUT_MS
    table = _sync_table()

    timed_out: List[str] = []
    scan_kwargs: Dict[str, Any] = {
        "FilterExpression": (Attr("status").eq(STATUS_IN_PROGRESS)
                             & Attr("attemptAt").lt(threshold)),
        "ProjectionExpression": "device_id",
    }
    while True:
        page = table.scan(**scan_kwargs)
        timed_out.extend(
            item["device_id"] for item in page.get("Items", []))
        last_key = page.get("LastEvaluatedKey")
        if not last_key:
            break
        scan_kwargs["ExclusiveStartKey"] = last_key

    for device_id in timed_out:
        try:
            table.update_item(
                Key={"device_id": device_id},
                UpdateExpression=(
                    "SET #st = :failed, failureReason = :reason"),
                ConditionExpression=(
                    "#st = :in_progress AND attemptAt < :threshold"),
                ExpressionAttributeNames={"#st": "status"},
                ExpressionAttributeValues={
                    ":failed": STATUS_FAILED,
                    ":reason": REASON_DEVICE_UNREACHABLE,
                    ":in_progress": STATUS_IN_PROGRESS,
                    ":threshold": threshold,
                },
            )
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            if code == "ConditionalCheckFailedException":
                # An ack or a fresh attempt landed between the scan and
                # this update; the newer state wins.
                continue
            logger.exception(
                "Failed to record sync timeout for device %s", device_id)
            continue
        logger.info(
            "Account sync for %s timed out without an ack within %d s; "
            "recorded failed (%s), pending changes retained",
            device_id, ACK_TIMEOUT_MS // 1000, REASON_DEVICE_UNREACHABLE)


# --- Ack ingest ---------------------------------------------------------------
#
# The IoT topic rule
#   SELECT *, topic(3) AS thing_name
#   FROM '$aws/things/+/shadow/name/dda-user-accounts/update/documents'
# forwards every shadow documents event to the ack queue. Each SQS record
# body is the shadow documents payload plus the rule-added thing_name; the
# device's ack lives in current.state.reported as
# {ackSyncId, appliedAt, accountCount} (success) or {ackSyncId, error}
# (validation failure).
#
# Batch semantics (partial batch responses, camera_sync.py conventions):
#   - transient persistence errors -> the record's messageId is returned
#     in batchItemFailures so only that record is retried
#   - malformed / unparseable records -> logged and dead-lettered via an
#     explicit SendMessage to the DLQ, NOT reported as batch failures, so
#     a bad record is never endlessly redelivered


class MalformedAck(Exception):
    """An ack record that can never be processed (dead-letter it)."""


def _parse_ack_record(
        record: Dict[str, Any]) -> Tuple[str, Optional[Dict[str, Any]]]:
    """Extract (thing_name, reported state | None) from one SQS record.

    Returns None for the reported state when the shadow documents event
    carries none (e.g. our own desired write on a never-reported
    shadow) - nothing to ingest, not an error.

    Raises MalformedAck for anything that can never be processed.
    """
    try:
        body = json.loads(record.get("body") or "")
    except (json.JSONDecodeError, ValueError) as exc:
        raise MalformedAck(f"unparseable message body: {exc}") from exc
    if not isinstance(body, dict):
        raise MalformedAck("message body is not a JSON object")

    thing_name = body.get("thing_name")
    if not thing_name or not isinstance(thing_name, str):
        raise MalformedAck("missing thing_name (topic rule SELECT)")

    state = ((body.get("current") or {}).get("state")) or {}
    if not isinstance(state, dict):
        raise MalformedAck("current.state is not an object")
    reported = state.get("reported")
    if reported is None:
        return thing_name, None
    if not isinstance(reported, dict):
        raise MalformedAck("current.state.reported is not an object")
    return thing_name, reported


def _ingest_ack(device_id: str, reported: Dict[str, Any]) -> None:
    """Reduce one device ack into the sync-state row (Reqs 7.4, 7.5).

    Matching ackSyncId + no error -> success, lastSyncAt = appliedAt,
    pendingChanges cleared (a zero-change sync acks the same way, 7.5).
    Matching ackSyncId + error -> failed with the device's reason,
    pending retained. Stale acks (ackSyncId != current syncId, or no
    row) are discarded without any state change. Updates are guarded on
    the row still carrying the acked syncId so a concurrent re-stage is
    never clobbered.
    """
    from botocore.exceptions import ClientError

    ack_sync_id = reported.get("ackSyncId")
    if not ack_sync_id:
        logger.info(
            "Shadow documents event for %s carries no ackSyncId; "
            "nothing to ingest", device_id)
        return

    table = _sync_table()
    row = table.get_item(Key={"device_id": device_id}).get("Item")
    if not row or row.get("syncId") != ack_sync_id:
        logger.info(
            "Discarding stale ack for %s (ackSyncId %s, current syncId "
            "%s)", device_id, ack_sync_id,
            (row or {}).get("syncId"))
        return

    error = reported.get("error")
    try:
        if error:
            # The device rejected the document (validation failure):
            # failed with the device's reason, pending retained (7.6).
            table.update_item(
                Key={"device_id": device_id},
                UpdateExpression=(
                    "SET #st = :failed, failureReason = :reason"),
                ConditionExpression="syncId = :ack",
                ExpressionAttributeNames={"#st": "status"},
                ExpressionAttributeValues={
                    ":failed": STATUS_FAILED,
                    ":reason": str(error),
                    ":ack": ack_sync_id,
                },
            )
            logger.info(
                "Account sync %s for %s failed on the device: %s",
                ack_sync_id, device_id, error)
        else:
            applied_at = reported.get("appliedAt")
            last_sync_at = (int(applied_at) if applied_at is not None
                            else int(time.time() * 1000))
            table.update_item(
                Key={"device_id": device_id},
                UpdateExpression=(
                    "SET #st = :success, lastSyncAt = :applied, "
                    "pendingChanges = :false REMOVE failureReason"),
                ConditionExpression="syncId = :ack",
                ExpressionAttributeNames={"#st": "status"},
                ExpressionAttributeValues={
                    ":success": STATUS_SUCCESS,
                    ":applied": last_sync_at,
                    ":false": False,
                    ":ack": ack_sync_id,
                },
            )
            logger.info(
                "Account sync %s for %s acknowledged (appliedAt %s); "
                "pending changes cleared", ack_sync_id, device_id,
                applied_at)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code")
        if code == "ConditionalCheckFailedException":
            # The row was re-staged with a fresh syncId between the read
            # and this update: the ack is now stale; discard it.
            logger.info(
                "Discarding ack for %s superseded by a newer staged "
                "sync (ackSyncId %s)", device_id, ack_sync_id)
            return
        raise


def _dead_letter(record: Dict[str, Any], reason: str) -> bool:
    """Send a malformed record to the DLQ; True when the send succeeded."""
    import boto3

    dlq_url = os.environ.get("ACCOUNT_SYNC_ACK_DLQ_URL")
    if not dlq_url:
        logger.error("ACCOUNT_SYNC_ACK_DLQ_URL not configured; cannot "
                     "dead-letter malformed account-sync ack")
        return False
    try:
        boto3.client("sqs").send_message(
            QueueUrl=dlq_url,
            MessageBody=record.get("body") or "",
            MessageAttributes={
                "deadLetterReason": {
                    "DataType": "String",
                    "StringValue": reason[:256] or "unknown",
                },
            },
        )
        return True
    except Exception:  # noqa: BLE001 - DLQ send is best-effort
        logger.exception("Failed to dead-letter malformed account-sync ack")
        return False


def _handle_ack_records(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    """SQS ack ingest from the shadow update/documents topic rule.

    Returns a partial batch response ({"batchItemFailures": [...]}) so
    that transient persistence errors retry only the affected record.
    Malformed records are logged and dead-lettered explicitly and are
    NOT reported as batch failures (they would never succeed on retry).
    """
    batch_item_failures = []
    for record in records:
        message_id = record.get("messageId")
        try:
            thing_name, reported = _parse_ack_record(record)
            if reported is None:
                logger.info(
                    "Shadow documents event for '%s' carries no reported "
                    "state; nothing to ingest", thing_name)
                continue
            _ingest_ack(thing_name, reported)
        except MalformedAck as exc:
            logger.error(
                "Malformed account-sync ack (message %s): %s",
                message_id, exc)
            if not _dead_letter(record, str(exc)) and message_id:
                # Could not preserve the message; let SQS retry/redrive it.
                batch_item_failures.append({"itemIdentifier": message_id})
        except Exception:  # noqa: BLE001 - transient failure: retry record
            logger.exception(
                "Transient failure processing account-sync ack "
                "(message %s); reporting batch item failure", message_id)
            if message_id:
                batch_item_failures.append({"itemIdentifier": message_id})
    return {"batchItemFailures": batch_item_failures}


def handler(event, context):
    """Route by event shape: SQS records -> ack ingest; direct
    {action: 'sync_attempt'} -> single-device attempt; EventBridge
    scheduled event -> pending-changes sweep."""
    event = event or {}

    records = event.get("Records")
    if isinstance(records, list) and records and (
            records[0].get("eventSource") == "aws:sqs"):
        return _handle_ack_records(records)

    if event.get("action") == "sync_attempt":
        return _handle_direct_invoke(event)

    if event.get("source") == "aws.events" or (
            event.get("detail-type") == "Scheduled Event"):
        return _handle_schedule()

    logger.error("Unrecognized account_sync event shape: %s",
                 json.dumps(event, default=str)[:512])
    return {"status": "skipped", "reason": "unrecognized event shape"}
