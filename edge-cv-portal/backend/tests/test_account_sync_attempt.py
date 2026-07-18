"""
Unit tests for the account_sync.py sync-attempt entry
(spec: portal-user-manager, task 3.4).

Covers: direct invoke ({action: 'sync_attempt', device_id, syncId})
building the desired document from the staged set and writing the
dda-user-accounts named shadow, the row stamped in_progress with
attemptAt; shadow-write failure and size-limit violation marked failed
with the reason and pending changes retained (Req 7.6); the EventBridge
5-minute schedule attempting delivery for every device with pending
changes (Req 7.7); SQS-shaped events routed away from the attempt path
(ack ingest is task 3.5).

DynamoDB runs against real moto (aws_stack conftest fixture); the
iot-data client is a recording fake installed over the module's
iot_data_client factory.

_Requirements: 7.6, 7.7_
"""
import json
import os
import sys

import pytest

REGION = "us-east-1"
ACCOUNT_SYNC_TABLE = "test-account-sync"
SHADOW_NAME = "dda-user-accounts"


# ------------------------------------------------- fake iot-data client

class FakeIotDataClient:
    """Records update_thing_shadow writes; optionally fails per thing."""

    def __init__(self, fail_for=None, error=None):
        self.updates = []
        self.fail_for = set(fail_for or [])
        self.error = error or RuntimeError("shadow service unavailable")

    def update_thing_shadow(self, thingName, shadowName, payload):
        if thingName in self.fail_for:
            raise self.error
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
    def _install(fail_for=None, error=None):
        fake = FakeIotDataClient(fail_for=fail_for, error=error)
        monkeypatch.setattr(account_sync, "iot_data_client",
                            lambda device_id: fake)
        return fake
    return _install


# ---------------------------------------------------------------- helpers

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


def direct_invoke(account_sync, device_id, sync_id="sync-1"):
    return account_sync.handler(
        {"action": "sync_attempt", "device_id": device_id,
         "syncId": sync_id}, None)


def schedule_event():
    return {"source": "aws.events", "detail-type": "Scheduled Event",
            "detail": {}}


# ----------------------------------------------------- direct sync attempt

class TestDirectInvoke:
    def test_writes_desired_shadow_and_stamps_in_progress(
            self, account_sync, sync_table, install_iot):
        """The desired document is built from the staged set and written
        to the dda-user-accounts named shadow; the row is stamped
        in_progress with attemptAt, pending changes retained until the
        ack (task 3.5)."""
        iot = install_iot()
        accounts = {
            "op1": {"email": "op1@example.com", "role": "Operator",
                    "enabled": True,
                    "verifier": {"algorithm": "pbkdf2-sha256",
                                 "iterations": 10, "salt": "c2FsdA==",
                                 "hash": "aGFzaA=="}},
            "disabled-viewer": {"email": "v@example.com", "role": "Viewer",
                                "enabled": False},
        }
        sync_table.put_item(Item=staged_row(
            "edge-1", sync_id="sync-42", accounts=accounts))

        result = direct_invoke(account_sync, "edge-1", "sync-42")

        assert result["status"] == "in_progress"
        assert result["syncId"] == "sync-42"

        assert len(iot.updates) == 1
        update = iot.updates[0]
        assert update["thing_name"] == "edge-1"
        assert update["shadow_name"] == SHADOW_NAME
        desired = update["payload"]["state"]["desired"]
        assert desired["syncId"] == "sync-42"
        assert desired["version"] == 1
        assert set(desired["accounts"]) == {"op1", "disabled-viewer"}
        assert desired["accounts"]["op1"]["verifier"]["iterations"] == 10
        # Disabled account carried marked enabled=false, never dropped
        assert desired["accounts"]["disabled-viewer"]["enabled"] is False

        row = sync_table.get_item(Key={"device_id": "edge-1"})["Item"]
        assert row["status"] == "in_progress"
        assert row["attemptAt"] > 0
        assert row["pendingChanges"] is True
        assert row["syncId"] == "sync-42"

    def test_uses_the_rows_current_sync_id_over_a_stale_invoke(
            self, account_sync, sync_table, install_iot):
        """An attribute-change hook may refresh the staged set between
        staging and the async invoke: the row's current syncId and
        content are what get delivered."""
        iot = install_iot()
        sync_table.put_item(Item=staged_row("edge-1", sync_id="fresh-sync"))

        result = direct_invoke(account_sync, "edge-1", "stale-sync")

        assert result["syncId"] == "fresh-sync"
        assert iot.updates[0]["payload"]["state"]["desired"]["syncId"] == \
            "fresh-sync"

    def test_no_staged_row_is_skipped_without_shadow_write(
            self, account_sync, sync_table, install_iot):
        iot = install_iot()
        result = direct_invoke(account_sync, "ghost-device")
        assert result["status"] == "skipped"
        assert iot.updates == []

    def test_no_pending_changes_is_skipped_without_shadow_write(
            self, account_sync, sync_table, install_iot):
        """A row already delivered (ack cleared pendingChanges) is not
        re-attempted by a late direct invoke."""
        iot = install_iot()
        sync_table.put_item(Item=staged_row(
            "edge-1", pending=False, status="success"))
        result = direct_invoke(account_sync, "edge-1")
        assert result["status"] == "skipped"
        assert iot.updates == []
        row = sync_table.get_item(Key={"device_id": "edge-1"})["Item"]
        assert row["status"] == "success"


# ------------------------------------------------- failure paths (Req 7.6)

class TestAttemptFailures:
    def test_shadow_write_failure_marks_failed_pending_retained(
            self, account_sync, sync_table, install_iot):
        """Req 7.6: a shadow-write failure marks the row failed with the
        reason; the staged set and pendingChanges are retained for the
        scheduled retry."""
        install_iot(fail_for={"edge-1"})
        sync_table.put_item(Item=staged_row("edge-1", sync_id="sync-9"))

        result = direct_invoke(account_sync, "edge-1", "sync-9")

        assert result["status"] == "failed"
        assert "shadow write failed" in result["reason"]

        row = sync_table.get_item(Key={"device_id": "edge-1"})["Item"]
        assert row["status"] == "failed"
        assert "shadow write failed" in row["failureReason"]
        assert row["pendingChanges"] is True
        assert row["syncId"] == "sync-9"
        assert set(row["accounts"]) == {"op1"}

    def test_size_limit_violation_marks_failed_without_shadow_write(
            self, account_sync, sync_table, install_iot):
        """Req 7.6: a staged set whose rendered document exceeds the
        8 KB shadow limit fails with the explicit reason; nothing is
        written to the shadow and pending changes are retained."""
        iot = install_iot()
        oversized = {
            f"user-{i:04d}": {"email": "x" * 60 + "@example.com",
                              "role": "Operator", "enabled": True}
            for i in range(100)
        }
        sync_table.put_item(Item=staged_row(
            "edge-1", accounts=oversized))

        result = direct_invoke(account_sync, "edge-1")

        assert result["status"] == "failed"
        assert "8 KB" in result["reason"]
        assert iot.updates == []

        row = sync_table.get_item(Key={"device_id": "edge-1"})["Item"]
        assert row["status"] == "failed"
        assert "8 KB" in row["failureReason"]
        assert row["pendingChanges"] is True

    def test_successful_retry_clears_a_previous_failure_reason(
            self, account_sync, sync_table, install_iot):
        install_iot()
        sync_table.put_item(Item=staged_row(
            "edge-1", status="failed",
            failureReason="shadow write failed: down"))

        result = direct_invoke(account_sync, "edge-1")

        assert result["status"] == "in_progress"
        row = sync_table.get_item(Key={"device_id": "edge-1"})["Item"]
        assert row["status"] == "in_progress"
        assert "failureReason" not in row


# --------------------------------------------- scheduled sweep (Req 7.7)

class TestScheduledSweep:
    def test_schedule_attempts_every_device_with_pending_changes(
            self, account_sync, sync_table, install_iot):
        """Req 7.7: the 5-minute schedule attempts delivery for every
        device with pendingChanges=true - and only those."""
        iot = install_iot()
        sync_table.put_item(Item=staged_row("edge-1", sync_id="s1"))
        sync_table.put_item(Item=staged_row(
            "edge-2", sync_id="s2", status="failed",
            failureReason="shadow write failed: down"))
        sync_table.put_item(Item=staged_row(
            "edge-3", sync_id="s3", pending=False, status="success"))

        result = account_sync.handler(schedule_event(), None)

        assert result["attempted"] == 2
        attempted = {u["thing_name"] for u in iot.updates}
        assert attempted == {"edge-1", "edge-2"}

        for device_id in ("edge-1", "edge-2"):
            row = sync_table.get_item(
                Key={"device_id": device_id})["Item"]
            assert row["status"] == "in_progress"
            assert row["pendingChanges"] is True
        untouched = sync_table.get_item(
            Key={"device_id": "edge-3"})["Item"]
        assert untouched["status"] == "success"

    def test_one_failing_device_never_blocks_the_others(
            self, account_sync, sync_table, install_iot):
        install_iot(fail_for={"edge-1"})
        sync_table.put_item(Item=staged_row("edge-1", sync_id="s1"))
        sync_table.put_item(Item=staged_row("edge-2", sync_id="s2"))

        result = account_sync.handler(schedule_event(), None)

        assert result["attempted"] == 2
        by_device = {r["device_id"]: r for r in result["results"]}
        assert by_device["edge-1"]["status"] == "failed"
        assert by_device["edge-2"]["status"] == "in_progress"

    def test_schedule_with_nothing_pending_is_a_noop(
            self, account_sync, sync_table, install_iot):
        iot = install_iot()
        result = account_sync.handler(schedule_event(), None)
        assert result["attempted"] == 0
        assert iot.updates == []


# ------------------------------------------------------- event routing

class TestEventRouting:
    def test_sqs_records_route_to_the_ack_path_not_the_attempt_path(
            self, account_sync, sync_table, install_iot):
        """SQS-shaped events (the ack ingest, task 3.5) never trigger
        shadow writes; unprocessed records are reported for redelivery."""
        iot = install_iot()
        sync_table.put_item(Item=staged_row("edge-1"))

        result = account_sync.handler({
            "Records": [{"eventSource": "aws:sqs", "messageId": "m-1",
                         "body": "{}"}],
        }, None)

        assert result["batchItemFailures"] == [{"itemIdentifier": "m-1"}]
        assert iot.updates == []
        row = sync_table.get_item(Key={"device_id": "edge-1"})["Item"]
        assert row["status"] == "pending"

    def test_unrecognized_event_shape_is_skipped(
            self, account_sync, sync_table, install_iot):
        iot = install_iot()
        result = account_sync.handler({"something": "else"}, None)
        assert result["status"] == "skipped"
        assert iot.updates == []
