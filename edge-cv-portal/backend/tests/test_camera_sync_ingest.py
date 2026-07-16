"""
Portal_Sync_Service SQS ingest behavior (camera-registry-sync task 5.5).

Unit tests for the `handler` in functions/camera_sync.py against the
moto-backed conftest stack (registry table + devices table + SQS DLQ):

  - duplicate SQS delivery idempotency: replaying a report reproduces the
    identical registry state and never emits a second conflict event
  - out-of-order delivery: an older-version report arriving after a newer
    one is discarded, leaving the registry unchanged (Req 3.5)
  - malformed / unparseable reports are explicitly dead-lettered with a
    reason and are NOT reported as batch item failures
  - a device unknown to the devices table (no usecase_id) dead-letters
  - transient persistence failures produce a partial batch response
    (batchItemFailures) so only the affected record retries
  - every processed report stamps the device META item: last_report_at
    set, never_synced cleared (Req 3.2)

Requirements: 3.2, 3.5
"""
import json
import os
import sys
import uuid
from types import SimpleNamespace

import pytest

from conftest import REGION, TEST_ENV

CAMERA_REGISTRY_TABLE_NAME = "test-camera-registry-ingest"
DLQ_NAME = "test-camera-shadow-report-dlq"


@pytest.fixture(scope="module")
def ingest_env(aws_stack):
    """Registry table + DLQ and a freshly bound camera_sync module."""
    import boto3

    client = boto3.client("dynamodb", region_name=REGION)
    client.create_table(
        TableName=CAMERA_REGISTRY_TABLE_NAME,
        KeySchema=[
            {"AttributeName": "device_id", "KeyType": "HASH"},
            {"AttributeName": "sk", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "device_id", "AttributeType": "S"},
            {"AttributeName": "sk", "AttributeType": "S"},
            {"AttributeName": "usecase_id", "AttributeType": "S"},
        ],
        GlobalSecondaryIndexes=[{
            "IndexName": "usecase-index",
            "KeySchema": [{"AttributeName": "usecase_id", "KeyType": "HASH"}],
            "Projection": {"ProjectionType": "ALL"},
        }],
        BillingMode="PAY_PER_REQUEST",
    )

    sqs = boto3.client("sqs", region_name=REGION)
    dlq_url = sqs.create_queue(QueueName=DLQ_NAME)["QueueUrl"]

    os.environ["CAMERA_REGISTRY_TABLE"] = CAMERA_REGISTRY_TABLE_NAME
    os.environ["CAMERA_SHADOW_REPORT_DLQ_URL"] = dlq_url

    # Re-import so the module binds inside the active moto mock
    # (conftest pattern).
    sys.modules.pop("camera_sync", None)
    import camera_sync

    resource = boto3.resource("dynamodb", region_name=REGION)
    yield SimpleNamespace(
        module=camera_sync,
        registry=resource.Table(CAMERA_REGISTRY_TABLE_NAME),
        devices=resource.Table(TEST_ENV["DEVICES_TABLE"]),
        sqs=sqs,
        dlq_url=dlq_url,
    )


def drain_dlq(ingest_env):
    """Receive-and-delete every message currently on the DLQ."""
    messages = []
    while True:
        response = ingest_env.sqs.receive_message(
            QueueUrl=ingest_env.dlq_url,
            MaxNumberOfMessages=10,
            WaitTimeSeconds=0,
            MessageAttributeNames=["All"],
        )
        batch = response.get("Messages", [])
        if not batch:
            return messages
        for message in batch:
            ingest_env.sqs.delete_message(
                QueueUrl=ingest_env.dlq_url,
                ReceiptHandle=message["ReceiptHandle"])
        messages.extend(batch)


@pytest.fixture(autouse=True)
def clean_dlq(ingest_env):
    """Each test starts from an empty DLQ."""
    drain_dlq(ingest_env)
    yield


def register_device(ingest_env, usecase_id):
    """A device known to the portal devices table; returns its thing name."""
    thing_name = f"thing-{uuid.uuid4()}"
    ingest_env.devices.put_item(Item={
        "device_id": thing_name, "usecase_id": usecase_id,
    })
    return thing_name


def make_record(thing_name=None, reported=None, body=None,
                message_id=None):
    """One SQS record shaped like the IoT rule output (shadow documents
    payload plus the rule-added thing_name)."""
    if body is None:
        payload = {"thing_name": thing_name,
                   "current": {"state": {"reported": reported}}}
        body = json.dumps(payload)
    return {"messageId": message_id or str(uuid.uuid4()), "body": body}


def camera(version, name, device_path="/dev/video0", **extra):
    source = {
        "version": version,
        "name": name,
        "type": "Camera",
        "origin": "edge-configured",
        "params": {"devicePath": device_path},
        "capabilities": {"formats": [
            {"pixelFormat": "YUYV", "resolutions": [[1920, 1080]]}]},
    }
    source.update(extra)
    return source


def report(cameras, reported_at, failures=None):
    doc = {"schemaVersion": 1, "reportedAt": reported_at, "cameras": cameras}
    if failures is not None:
        doc["failures"] = failures
    return doc


def device_items(ingest_env, thing_name):
    from boto3.dynamodb.conditions import Key

    response = ingest_env.registry.query(
        KeyConditionExpression=Key("device_id").eq(thing_name))
    return {item["sk"]: item for item in response["Items"]}


def camera_item(items, csid):
    return items.get(f"CAMERA#{csid}")


def conflict_items(items):
    return [item for sk, item in items.items() if sk.startswith("CONFLICT#")]


class TestDuplicateDeliveryIdempotency:
    def test_duplicate_report_reproduces_identical_state(
            self, ingest_env, env):
        """Delivering the same report twice yields the identical registry
        state - reduction is version-guarded and idempotent (Req 3.5)."""
        usecase_id = env.create_usecase()
        thing_name = register_device(ingest_env, usecase_id)
        record = make_record(thing_name, report(
            {"cfg-a": camera(3, "line-1"),
             "disc-3fe9c0d21ab4": camera(
                 1, "usb-cam", "/dev/video2", origin="edge-discovered")},
            reported_at=1730000000000))

        first = ingest_env.module.handler({"Records": [record]}, None)
        after_first = device_items(ingest_env, thing_name)
        second = ingest_env.module.handler({"Records": [record]}, None)
        after_second = device_items(ingest_env, thing_name)

        assert first == {"batchItemFailures": []}
        assert second == {"batchItemFailures": []}
        assert after_second == after_first
        assert camera_item(after_first, "cfg-a")["version"] == 3
        assert camera_item(after_first, "cfg-a")["sync_status"] == "synced"
        assert conflict_items(after_first) == []

    def test_duplicate_conflicting_report_emits_one_conflict_event(
            self, ingest_env, env):
        """A report conflicting with a pending portal change records
        exactly one conflict event; replaying the same report reduces to
        a plain upsert without a second event (Reqs 3.5, 6.1)."""
        usecase_id = env.create_usecase()
        thing_name = register_device(ingest_env, usecase_id)
        ingest_env.registry.put_item(Item={
            "device_id": thing_name, "sk": "CAMERA#cfg-a",
            "camera_source_id": "cfg-a", "usecase_id": usecase_id,
            "name": "portal-name", "type": "Camera",
            "params": {"devicePath": "/dev/video0"},
            "origin": "edge-configured", "version": 3,
            "sync_status": "pending", "portal_change_id": "pc-1",
            "pending_content": {
                "op": "update", "name": "portal-name", "type": "Camera",
                "params": {"devicePath": "/dev/video9"},
            },
        })
        # Unacknowledged edge state diverging from the pending content.
        record = make_record(thing_name, report(
            {"cfg-a": camera(4, "edge-name")}, reported_at=1730000001000))

        ingest_env.module.handler({"Records": [record]}, None)
        after_first = device_items(ingest_env, thing_name)
        ingest_env.module.handler({"Records": [record]}, None)
        after_second = device_items(ingest_env, thing_name)

        # Edge wins, exactly one conflict event survives the replay.
        assert camera_item(after_first, "cfg-a")["name"] == "edge-name"
        assert camera_item(after_first, "cfg-a")["sync_status"] == "synced"
        assert len(conflict_items(after_first)) == 1
        assert len(conflict_items(after_second)) == 1
        assert after_second == after_first
        conflict = conflict_items(after_first)[0]
        assert conflict["resolution"] == "edge-retained"
        assert conflict["camera_source_id"] == "cfg-a"


class TestOutOfOrderDelivery:
    def test_older_version_report_is_discarded(self, ingest_env, env):
        """An older-version report arriving after a newer one leaves the
        newer registry entry untouched (Req 3.5)."""
        usecase_id = env.create_usecase()
        thing_name = register_device(ingest_env, usecase_id)
        newer = make_record(thing_name, report(
            {"cfg-a": camera(5, "newer", "/dev/video5")},
            reported_at=1730000005000))
        older = make_record(thing_name, report(
            {"cfg-a": camera(3, "older", "/dev/video3")},
            reported_at=1730000003000))

        ingest_env.module.handler({"Records": [newer]}, None)
        after_newer = device_items(ingest_env, thing_name)
        result = ingest_env.module.handler({"Records": [older]}, None)
        after_older = device_items(ingest_env, thing_name)

        assert result == {"batchItemFailures": []}
        entry = camera_item(after_older, "cfg-a")
        assert entry["version"] == 5
        assert entry["name"] == "newer"
        assert entry["params"]["devicePath"] == "/dev/video5"
        assert entry["last_reported_at"] == 1730000005000
        assert camera_item(after_newer, "cfg-a") == entry
        assert conflict_items(after_older) == []


class TestMalformedReportDeadLettering:
    def test_unparseable_body_is_dead_lettered_not_batch_failed(
            self, ingest_env):
        """An unparseable body goes to the DLQ with a reason and is NOT a
        batch item failure (it would never succeed on retry)."""
        record = make_record(body="{not json", message_id="mal-1")

        result = ingest_env.module.handler({"Records": [record]}, None)

        assert result == {"batchItemFailures": []}
        messages = drain_dlq(ingest_env)
        assert len(messages) == 1
        assert messages[0]["Body"] == "{not json"
        reason = messages[0]["MessageAttributes"]["deadLetterReason"][
            "StringValue"]
        assert "unparseable" in reason

    def test_missing_thing_name_is_dead_lettered(self, ingest_env):
        """A shadow document without the rule-added thing_name can never
        be attributed to a device: dead-letter it."""
        body = json.dumps(
            {"current": {"state": {"reported": {"cameras": {}}}}})
        record = make_record(body=body)

        result = ingest_env.module.handler({"Records": [record]}, None)

        assert result == {"batchItemFailures": []}
        messages = drain_dlq(ingest_env)
        assert len(messages) == 1
        reason = messages[0]["MessageAttributes"]["deadLetterReason"][
            "StringValue"]
        assert "thing_name" in reason

    def test_unknown_device_is_dead_lettered(self, ingest_env):
        """A report from a device with no usecase_id in the devices table
        cannot be scoped (Req 1.4): dead-letter, no registry write."""
        thing_name = f"thing-{uuid.uuid4()}"  # never registered
        record = make_record(thing_name, report(
            {"cfg-a": camera(1, "cam")}, reported_at=1730000000000))

        result = ingest_env.module.handler({"Records": [record]}, None)

        assert result == {"batchItemFailures": []}
        messages = drain_dlq(ingest_env)
        assert len(messages) == 1
        reason = messages[0]["MessageAttributes"]["deadLetterReason"][
            "StringValue"]
        assert "usecase_id" in reason
        assert device_items(ingest_env, thing_name) == {}


class TestTransientFailurePartialBatch:
    def test_persistence_failure_reports_batch_item_failure(
            self, ingest_env, env, monkeypatch):
        """A transient persistence error (registry table unavailable)
        returns the record in batchItemFailures for SQS retry - it is
        not dead-lettered."""
        usecase_id = env.create_usecase()
        thing_name = register_device(ingest_env, usecase_id)
        monkeypatch.setenv("CAMERA_REGISTRY_TABLE",
                           "test-camera-registry-missing")
        record = make_record(thing_name, report(
            {"cfg-a": camera(1, "cam")}, reported_at=1730000000000),
            message_id="transient-1")

        result = ingest_env.module.handler({"Records": [record]}, None)

        assert result == {"batchItemFailures": [
            {"itemIdentifier": "transient-1"}]}
        assert drain_dlq(ingest_env) == []

    def test_malformed_record_does_not_block_valid_records(
            self, ingest_env, env):
        """One malformed record in a batch is dead-lettered while the
        valid records in the same batch are processed normally."""
        usecase_id = env.create_usecase()
        thing_name = register_device(ingest_env, usecase_id)
        malformed = make_record(body="not even json")
        valid = make_record(thing_name, report(
            {"cfg-a": camera(2, "cam")}, reported_at=1730000000000))

        result = ingest_env.module.handler(
            {"Records": [malformed, valid]}, None)

        assert result == {"batchItemFailures": []}
        assert len(drain_dlq(ingest_env)) == 1
        assert camera_item(
            device_items(ingest_env, thing_name), "cfg-a")["version"] == 2


class TestMetaStamping:
    def test_processed_report_stamps_meta_and_clears_never_synced(
            self, ingest_env, env):
        """Every processed report sets META.last_report_at and clears
        never_synced (Req 3.2)."""
        usecase_id = env.create_usecase()
        thing_name = register_device(ingest_env, usecase_id)
        ingest_env.registry.put_item(Item={
            "device_id": thing_name, "sk": "META",
            "usecase_id": usecase_id, "never_synced": True,
        })
        record = make_record(thing_name, report(
            {"cfg-a": camera(1, "cam")}, reported_at=1730000042000))

        ingest_env.module.handler({"Records": [record]}, None)

        items = device_items(ingest_env, thing_name)
        meta = items["META"]
        assert meta["last_report_at"] == 1730000042000
        assert meta["never_synced"] is False
        assert meta["usecase_id"] == usecase_id
        entry = camera_item(items, "cfg-a")
        assert entry["usecase_id"] == usecase_id
        assert entry["last_reported_at"] == 1730000042000
