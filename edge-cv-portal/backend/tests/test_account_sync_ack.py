"""
Unit tests for the account_sync.py ack ingest and timeout sweep
(spec: portal-user-manager, task 3.5).

Covers: SQS ack ingest (partial-batch-failure pattern) - a reported
ackSyncId matching the row's current syncId marks it success with
lastSyncAt = appliedAt and clears pendingChanges (Reqs 7.4, 7.5); a
reported error marks failed with the device's reason, pending retained;
stale acks are discarded without state change; malformed records are
dead-lettered (or reported for redelivery when no DLQ is configured),
never silently lost. Timeout sweep on the 5-minute schedule -
in_progress rows with attemptAt older than 60 s and no ack are marked
failed / device unreachable with pending changes retained (Reqs 7.6,
7.9).

DynamoDB and SQS run against real moto (aws_stack conftest fixture);
the iot-data client is a recording fake installed over the module's
iot_data_client factory.

_Requirements: 7.4, 7.5, 7.6, 7.9_
"""
import json
import os
import sys
import time

import pytest

REGION = "us-east-1"
ACCOUNT_SYNC_TABLE = "test-account-sync"


class FakeIotDataClient:
    """Records update_thing_shadow writes (schedule-path tests)."""

    def __init__(self):
        self.updates = []

    def update_thing_shadow(self, thingName, shadowName, payload):
        self.updates.append({
            "thing_name": thingName,
            "shadow_name": shadowName,
            "payload": json.loads(payload),
        })
        return {"payload": b"{}"}


# --------------------------------------------------------------- fixtures

@pytest.fixture(scope="module")
def account_sync(aws_stack):
    """The real account_sync module imported inside the moto mock, with
    the account-sync table created."""
    import boto3

    os.environ["ACCOUNT_SYNC_TABLE"] = ACCOUNT_SYNC_TABLE

    ddb = boto3.client("dynamodb", region_name=REGION)
    if ACCOUNT_SYNC_TABLE not in ddb.list_tables()["TableNames"]:
        ddb.create_table(
            TableName=ACCOUNT_SYNC_TABLE,
            KeySchema=[{"AttributeName": "device_id", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "device_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )

    sys.modules.pop("account_sync", None)
    import account_sync as module
    return module


@pytest.fixture
def sync_table(account_sync):
    """The moto-backed sync-state table, emptied per test."""
    import boto3

    table = boto3.resource(
        "dynamodb", region_name=REGION).Table(ACCOUNT_SYNC_TABLE)
    for item in table.scan()["Items"]:
        table.delete_item(Key={"device_id": item["device_id"]})
    return table


@pytest.fixture
def install_iot(account_sync, monkeypatch):
    def _install():
        fake = FakeIotDataClient()
        monkeypatch.setattr(account_sync, "iot_data_client",
                            lambda device_id: fake)
        return fake
    return _install


# ---------------------------------------------------------------- helpers

def now_ms():
    return int(time.time() * 1000)


def staged_row(device_id, sync_id="sync-1", accounts=None,
               pending=True, status="pending", **extra):
    row = {
        "device_id": device_id,
        "syncId": sync_id,
        "accounts": accounts if accounts is not None else {
            "op1": {"email": "op1@example.com", "role": "Operator",
                    "enabled": True},
        },
        "status": status,
        "pendingChanges": pending,
        "stagedAt": 1700000000000,
    }
    row.update(extra)
    return row


def ack_record(thing_name, reported, message_id="m-1"):
    """One SQS record shaped like the shadow-documents topic rule output."""
    body = {
        "thing_name": thing_name,
        "current": {"state": {"reported": reported}},
    }
    return {"eventSource": "aws:sqs", "messageId": message_id,
            "body": json.dumps(body)}


def sqs_event(*records):
    return {"Records": list(records)}


def schedule_event():
    return {"source": "aws.events", "detail-type": "Scheduled Event",
            "detail": {}}


# --------------------------------------------- matching acks (Reqs 7.4, 7.5)

class TestAckSuccess:
    def test_matching_ack_marks_success_and_clears_pending(
            self, account_sync, sync_table):
        """Req 7.4: an ackSyncId equal to the row's current syncId marks
        the row success with lastSyncAt = appliedAt and clears
        pendingChanges."""
        sync_table.put_item(Item=staged_row(
            "edge-1", sync_id="sync-42", status="in_progress",
            attemptAt=now_ms()))

        result = account_sync.handler(sqs_event(ack_record(
            "edge-1", {"ackSyncId": "sync-42",
                       "appliedAt": 1700000123456,
                       "accountCount": 1})), None)

        assert result["batchItemFailures"] == []
        row = sync_table.get_item(Key={"device_id": "edge-1"})["Item"]
        assert row["status"] == "success"
        assert row["lastSyncAt"] == 1700000123456
        assert row["pendingChanges"] is False
        assert row["syncId"] == "sync-42"

    def test_matching_ack_clears_a_previous_failure_reason(
            self, account_sync, sync_table):
        """A retried sync that finally acks leaves no stale failure
        reason on the row."""
        sync_table.put_item(Item=staged_row(
            "edge-1", sync_id="sync-9", status="in_progress",
            attemptAt=now_ms(), failureReason="device unreachable"))

        account_sync.handler(sqs_event(ack_record(
            "edge-1", {"ackSyncId": "sync-9", "appliedAt": 1700000000001,
                       "accountCount": 1})), None)

        row = sync_table.get_item(Key={"device_id": "edge-1"})["Item"]
        assert row["status"] == "success"
        assert "failureReason" not in row

    def test_zero_change_sync_ack_reports_success(
            self, account_sync, sync_table):
        """Req 7.5: a sync with zero account changes acks the same way
        and is reported successful."""
        sync_table.put_item(Item=staged_row(
            "edge-1", sync_id="sync-0", accounts={},
            status="in_progress", attemptAt=now_ms()))

        result = account_sync.handler(sqs_event(ack_record(
            "edge-1", {"ackSyncId": "sync-0", "appliedAt": 1700000000002,
                       "accountCount": 0})), None)

        assert result["batchItemFailures"] == []
        row = sync_table.get_item(Key={"device_id": "edge-1"})["Item"]
        assert row["status"] == "success"
        assert row["pendingChanges"] is False

    def test_duplicate_ack_delivery_is_idempotent(
            self, account_sync, sync_table):
        """SQS may deliver the same documents event more than once."""
        sync_table.put_item(Item=staged_row(
            "edge-1", sync_id="sync-42", status="in_progress",
            attemptAt=now_ms()))
        reported = {"ackSyncId": "sync-42", "appliedAt": 1700000123456,
                    "accountCount": 1}

        account_sync.handler(sqs_event(ack_record("edge-1", reported)), None)
        result = account_sync.handler(
            sqs_event(ack_record("edge-1", reported, "m-2")), None)

        assert result["batchItemFailures"] == []
        row = sync_table.get_item(Key={"device_id": "edge-1"})["Item"]
        assert row["status"] == "success"
        assert row["lastSyncAt"] == 1700000123456


# ------------------------------------------------- error acks (Req 7.6)

class TestAckError:
    def test_reported_error_marks_failed_with_the_devices_reason(
            self, account_sync, sync_table):
        """A device validation failure marks the row failed with the
        device's reason; the staged set and pendingChanges are retained
        for retry (Req 7.6)."""
        sync_table.put_item(Item=staged_row(
            "edge-1", sync_id="sync-7", status="in_progress",
            attemptAt=now_ms()))

        result = account_sync.handler(sqs_event(ack_record(
            "edge-1", {"ackSyncId": "sync-7",
                       "error": "unsupported document version 99"})), None)

        assert result["batchItemFailures"] == []
        row = sync_table.get_item(Key={"device_id": "edge-1"})["Item"]
        assert row["status"] == "failed"
        assert row["failureReason"] == "unsupported document version 99"
        assert row["pendingChanges"] is True
        assert set(row["accounts"]) == {"op1"}
        assert "lastSyncAt" not in row


# ------------------------------------------------------ stale acks

class TestStaleAcks:
    def test_stale_ack_is_discarded_without_state_change(
            self, account_sync, sync_table):
        """An ack for a superseded syncId never touches the row: the
        newer staged sync stays pending/in flight."""
        before = staged_row(
            "edge-1", sync_id="sync-new", status="in_progress",
            attemptAt=1700000000000)
        sync_table.put_item(Item=before)

        result = account_sync.handler(sqs_event(ack_record(
            "edge-1", {"ackSyncId": "sync-old",
                       "appliedAt": 1700000123456, "accountCount": 1})),
            None)

        assert result["batchItemFailures"] == []
        row = sync_table.get_item(Key={"device_id": "edge-1"})["Item"]
        assert row["status"] == "in_progress"
        assert row["pendingChanges"] is True
        assert "lastSyncAt" not in row

    def test_stale_error_ack_is_discarded_too(
            self, account_sync, sync_table):
        sync_table.put_item(Item=staged_row(
            "edge-1", sync_id="sync-new", status="in_progress",
            attemptAt=1700000000000))

        account_sync.handler(sqs_event(ack_record(
            "edge-1", {"ackSyncId": "sync-old", "error": "bad doc"})),
            None)

        row = sync_table.get_item(Key={"device_id": "edge-1"})["Item"]
        assert row["status"] == "in_progress"
        assert "failureReason" not in row

    def test_ack_for_an_unknown_device_is_discarded(
            self, account_sync, sync_table):
        result = account_sync.handler(sqs_event(ack_record(
            "ghost-device", {"ackSyncId": "sync-1",
                             "appliedAt": 1, "accountCount": 0})), None)

        assert result["batchItemFailures"] == []
        assert "Item" not in sync_table.get_item(
            Key={"device_id": "ghost-device"})


# --------------------------------------- non-ack and malformed records

class TestRecordHygiene:
    def test_desired_only_documents_event_is_ignored(
            self, account_sync, sync_table):
        """Our own desired writes fire the documents topic too; events
        without a reported state carry nothing to ingest."""
        sync_table.put_item(Item=staged_row(
            "edge-1", sync_id="sync-1", status="in_progress",
            attemptAt=now_ms()))
        body = {"thing_name": "edge-1",
                "current": {"state": {"desired": {"syncId": "sync-1"}}}}

        result = account_sync.handler(sqs_event(
            {"eventSource": "aws:sqs", "messageId": "m-1",
             "body": json.dumps(body)}), None)

        assert result["batchItemFailures"] == []
        row = sync_table.get_item(Key={"device_id": "edge-1"})["Item"]
        assert row["status"] == "in_progress"

    def test_reported_state_without_ack_sync_id_is_ignored(
            self, account_sync, sync_table):
        sync_table.put_item(Item=staged_row(
            "edge-1", sync_id="sync-1", status="in_progress",
            attemptAt=now_ms()))

        result = account_sync.handler(sqs_event(ack_record(
            "edge-1", {"somethingElse": True})), None)

        assert result["batchItemFailures"] == []
        row = sync_table.get_item(Key={"device_id": "edge-1"})["Item"]
        assert row["status"] == "in_progress"

    def test_malformed_record_is_dead_lettered_not_redelivered(
            self, account_sync, sync_table, monkeypatch):
        """Malformed records are logged and dead-lettered, never
        reported as batch failures (endless redelivery)."""
        import boto3

        sqs = boto3.client("sqs", region_name=REGION)
        dlq_url = sqs.create_queue(
            QueueName="test-account-sync-ack-dlq")["QueueUrl"]
        monkeypatch.setenv("ACCOUNT_SYNC_ACK_DLQ_URL", dlq_url)

        result = account_sync.handler(sqs_event(
            {"eventSource": "aws:sqs", "messageId": "m-bad",
             "body": "not json at all"}), None)

        assert result["batchItemFailures"] == []
        messages = sqs.receive_message(
            QueueUrl=dlq_url, MaxNumberOfMessages=10).get("Messages", [])
        assert len(messages) == 1
        assert messages[0]["Body"] == "not json at all"

    def test_malformed_record_without_dlq_is_kept_for_redelivery(
            self, account_sync, sync_table, monkeypatch):
        """When no DLQ is configured the record must not be lost: it is
        reported as a batch item failure instead."""
        monkeypatch.delenv("ACCOUNT_SYNC_ACK_DLQ_URL", raising=False)

        result = account_sync.handler(sqs_event(
            {"eventSource": "aws:sqs", "messageId": "m-bad",
             "body": json.dumps({"current": {}})}), None)

        assert result["batchItemFailures"] == [
            {"itemIdentifier": "m-bad"}]

    def test_transient_failure_reports_only_the_affected_record(
            self, account_sync, sync_table, monkeypatch):
        """A persistence error on one record retries that record only;
        the rest of the batch still processes (partial batch
        response)."""
        sync_table.put_item(Item=staged_row(
            "edge-ok", sync_id="s-ok", status="in_progress",
            attemptAt=now_ms()))

        original = account_sync._ingest_ack

        def flaky(device_id, reported):
            if device_id == "edge-down":
                raise RuntimeError("dynamodb unavailable")
            return original(device_id, reported)

        monkeypatch.setattr(account_sync, "_ingest_ack", flaky)

        result = account_sync.handler(sqs_event(
            ack_record("edge-down",
                       {"ackSyncId": "s-x", "appliedAt": 1,
                        "accountCount": 0}, "m-down"),
            ack_record("edge-ok",
                       {"ackSyncId": "s-ok", "appliedAt": 2,
                        "accountCount": 1}, "m-ok"),
        ), None)

        assert result["batchItemFailures"] == [
            {"itemIdentifier": "m-down"}]
        row = sync_table.get_item(Key={"device_id": "edge-ok"})["Item"]
        assert row["status"] == "success"


# ----------------------------------------- timeout sweep (Reqs 7.6, 7.9)

class TestTimeoutSweep:
    def test_in_progress_older_than_60s_is_marked_device_unreachable(
            self, account_sync, sync_table):
        """Req 7.9: no ack within 60 s of the attempt -> failed with
        reason 'device unreachable'; Req 7.6: the staged set and
        pendingChanges are retained."""
        sync_table.put_item(Item=staged_row(
            "edge-1", sync_id="sync-1", status="in_progress",
            attemptAt=now_ms() - 120_000))

        account_sync._sweep_timeouts()

        row = sync_table.get_item(Key={"device_id": "edge-1"})["Item"]
        assert row["status"] == "failed"
        assert row["failureReason"] == "device unreachable"
        assert row["pendingChanges"] is True
        assert set(row["accounts"]) == {"op1"}
        assert row["syncId"] == "sync-1"

    def test_recent_in_progress_rows_are_left_alone(
            self, account_sync, sync_table):
        """An attempt still inside the 60 s ack window is not a
        timeout."""
        sync_table.put_item(Item=staged_row(
            "edge-1", sync_id="sync-1", status="in_progress",
            attemptAt=now_ms() - 5_000))

        account_sync._sweep_timeouts()

        row = sync_table.get_item(Key={"device_id": "edge-1"})["Item"]
        assert row["status"] == "in_progress"
        assert "failureReason" not in row

    def test_non_in_progress_rows_are_never_swept(
            self, account_sync, sync_table):
        """Acked (success) and already-failed rows are untouched no
        matter how old their attemptAt is."""
        sync_table.put_item(Item=staged_row(
            "edge-ok", status="success", pending=False,
            attemptAt=now_ms() - 900_000, lastSyncAt=1700000000000))
        sync_table.put_item(Item=staged_row(
            "edge-failed", status="failed",
            failureReason="shadow write failed: down",
            attemptAt=now_ms() - 900_000))

        account_sync._sweep_timeouts()

        ok = sync_table.get_item(Key={"device_id": "edge-ok"})["Item"]
        assert ok["status"] == "success"
        failed = sync_table.get_item(
            Key={"device_id": "edge-failed"})["Item"]
        assert failed["failureReason"] == "shadow write failed: down"

    def test_schedule_sweeps_timeouts_before_attempting_delivery(
            self, account_sync, sync_table, install_iot):
        """The 5-minute schedule runs the sweep, then retries the (still
        pending) timed-out device in the same pass (Reqs 7.7, 7.9)."""
        iot = install_iot()
        old_attempt = now_ms() - 120_000
        sync_table.put_item(Item=staged_row(
            "edge-1", sync_id="sync-1", status="in_progress",
            attemptAt=old_attempt))
        # A timed-out row that the attempt pass will skip (no pending
        # changes) exposes the sweep's marking through the schedule
        # entry itself.
        sync_table.put_item(Item=staged_row(
            "edge-2", sync_id="sync-2", status="in_progress",
            pending=False, attemptAt=old_attempt))

        result = account_sync.handler(schedule_event(), None)

        # edge-1: swept to failed/unreachable, then re-attempted because
        # pending changes were retained - it ends in_progress with a
        # fresh attemptAt and the shadow written again (Req 7.7).
        assert result["attempted"] == 1
        assert len(iot.updates) == 1
        assert iot.updates[0]["thing_name"] == "edge-1"
        row = sync_table.get_item(Key={"device_id": "edge-1"})["Item"]
        assert row["status"] == "in_progress"
        assert row["attemptAt"] > old_attempt

        # edge-2: swept to failed/unreachable and not re-attempted.
        swept = sync_table.get_item(Key={"device_id": "edge-2"})["Item"]
        assert swept["status"] == "failed"
        assert swept["failureReason"] == "device unreachable"
