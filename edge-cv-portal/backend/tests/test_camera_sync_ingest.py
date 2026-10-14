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
  - rtsp-rtmp-stream-cameras task 29.5: a stream create acknowledged under
    the device's own id links the created entry to the create's secret
    record and marks the mirror (``alias_of``), idempotently and for the
    stream types only, with the partial-processing fallback; and the
    stale-failure matrix of Requirement 5.12

Requirements: 3.2, 3.5 (rtsp-rtmp-stream-cameras: 5.8, 5.12, 18.3)
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


def aravis_discovered(version, camera_id="Aravis-Fake-GV01"):
    """An AravisDiscovered Camera_Source exactly as the Edge_Sync_Agent
    reports it (aravis-camera-input: build_inventory discovered-only
    entry shape)."""
    return {
        "version": version,
        "name": "Aravis Fake GV",
        "type": "AravisDiscovered",
        "origin": "edge-discovered",
        "params": {
            "cameraId": camera_id,
            "serial": "SN-0001",
            "protocol": "GigEVision",
            "address": "192.168.1.20",
        },
        "capabilities": {"aravis": {
            "model": "Fake GV",
            "address": "192.168.1.20",
            "physicalId": "eth0",
            "protocol": "GigEVision",
            "serial": "SN-0001",
            "vendor": "Aravis",
        }},
    }


class TestAravisDiscoveredIngestion:
    """Registry ingestion of AravisDiscovered reports (aravis-camera-input
    Requirement 7.3): the existing reduce_report/handler path stores the
    new type verbatim without changes to existing Camera_Source types."""

    def test_aravis_discovered_entry_stored_verbatim(self, ingest_env, env):
        """An AravisDiscovered entry flows through the ingest handler as
        one more opaque type string: every declared field lands in the
        registry exactly as reported (Req 7.3)."""
        usecase_id = env.create_usecase()
        thing_name = register_device(ingest_env, usecase_id)
        csid = "arv-3fe9c0d21ab4"
        incoming = aravis_discovered(1)
        record = make_record(thing_name, report(
            {csid: incoming}, reported_at=1730000000000))

        result = ingest_env.module.handler({"Records": [record]}, None)

        assert result == {"batchItemFailures": []}
        entry = camera_item(device_items(ingest_env, thing_name), csid)
        assert entry is not None
        assert entry["camera_source_id"] == csid
        assert entry["name"] == incoming["name"]
        assert entry["type"] == "AravisDiscovered"
        assert entry["origin"] == "edge-discovered"
        assert entry["params"] == incoming["params"]
        assert entry["capabilities"] == incoming["capabilities"]
        assert entry["version"] == 1
        assert entry["sync_status"] == "synced"
        assert entry["usecase_id"] == usecase_id
        assert entry["last_reported_at"] == 1730000000000

    def test_existing_types_unaffected_by_aravis_entries(
            self, ingest_env, env):
        """A report carrying AravisDiscovered entries alongside existing
        types stores the existing-type entries exactly as a report without
        the Aravis entries does (Req 7.3)."""
        usecase_id = env.create_usecase()
        with_aravis = register_device(ingest_env, usecase_id)
        without_aravis = register_device(ingest_env, usecase_id)
        classic = {
            "cfg-a": camera(2, "line-1"),
            "disc-9a1b2c3d4e5f": camera(
                1, "usb-cam", "/dev/video2",
                origin="edge-discovered", type="V4L2Discovered"),
        }
        mixed = dict(classic)
        mixed["arv-3fe9c0d21ab4"] = aravis_discovered(1)

        ingest_env.module.handler({"Records": [
            make_record(with_aravis, report(mixed,
                                            reported_at=1730000000000)),
            make_record(without_aravis, report(classic,
                                               reported_at=1730000000000)),
        ]}, None)

        items_with = device_items(ingest_env, with_aravis)
        items_without = device_items(ingest_env, without_aravis)

        def strip_device(item):
            return {k: v for k, v in item.items() if k != "device_id"}

        # The Aravis entry is stored; every existing-type entry is
        # byte-identical to the Aravis-free ingestion of the same report.
        assert camera_item(items_with, "arv-3fe9c0d21ab4") is not None
        assert camera_item(items_without, "arv-3fe9c0d21ab4") is None
        for csid in classic:
            assert strip_device(camera_item(items_with, csid)) == \
                strip_device(camera_item(items_without, csid))


# ---------------------------------------------------------------------------
# rtsp-rtmp-stream-cameras task 29.5: create links and stale failures
# ---------------------------------------------------------------------------

LINK_ARN = ("arn:aws:secretsmanager:us-east-1:123456789012:secret:"
            "dda-portal/stream-camera-credentials/{device}/portal-x-AbCdEf")


def seed_pending_create(ingest_env, thing_name, usecase_id, source_type,
                        record=None, csid="portal-x", change_id="pc-create"):
    """A Portal create as mark_pending leaves it."""
    params = ({"devicePath": "/dev/video0"} if source_type == "Camera"
              else {"url": "rtsp://10.0.0.5/live"})
    if record is not None:
        params = {**params, "credentialRef": {"secretArn": record,
                                              "versionId": "v1"},
                  "credentialsConfigured": True}
    item = {
        "device_id": thing_name, "sk": f"CAMERA#{csid}",
        "camera_source_id": csid, "usecase_id": usecase_id,
        "name": "Dock", "type": source_type, "params": params,
        "capabilities": {}, "origin": "portal-created", "version": 0,
        "absent": False, "sync_status": "pending",
        "portal_change_id": change_id,
        "pending_content": {"op": "create", "name": "Dock",
                            "type": source_type, "params": params},
    }
    if record is not None:
        item["credential_secret_arn"] = record
    ingest_env.registry.put_item(Item=item)
    return item


def created_camera(source_type, params, ack="pc-create", version=1):
    return {"version": version, "name": "Dock", "type": source_type,
            "origin": "edge-configured", "params": params,
            "capabilities": {}, "ack": ack}


class TestCreateLinks:
    def test_the_link_a_replay_before_retirement_and_the_retirement(
            self, ingest_env, env):
        usecase_id = env.create_usecase()
        thing_name = register_device(ingest_env, usecase_id)
        record = LINK_ARN.format(device=thing_name)
        create = seed_pending_create(ingest_env, thing_name, usecase_id,
                                     "RTSP", record)
        camera = created_camera("RTSP", create["params"])
        alias_event = make_record(thing_name, report(
            {"cfg-y": camera, "portal-x": dict(camera)},
            reported_at=1730000001000))

        ingest_env.module.handler({"Records": [alias_event]}, None)
        linked = device_items(ingest_env, thing_name)
        created = camera_item(linked, "cfg-y")
        mirror = camera_item(linked, "portal-x")
        assert created["credential_secret_arn"] == record
        assert "alias_of" not in created
        assert mirror["alias_of"] == "cfg-y"
        assert mirror["sync_status"] == "synced"
        assert mirror["credential_secret_arn"] == record

        # Replaying the event before the device retires the mirror changes
        # nothing: both entries already hold their link fields.
        ingest_env.module.handler({"Records": [alias_event]}, None)
        assert device_items(ingest_env, thing_name) == linked

        # The retirement deletes the mirror and leaves the created entry.
        ingest_env.module.handler({"Records": [make_record(
            thing_name, report({"cfg-y": camera},
                               reported_at=1730000002000))]}, None)
        retired = device_items(ingest_env, thing_name)
        assert camera_item(retired, "portal-x") is None
        assert camera_item(retired, "cfg-y")["credential_secret_arn"] == \
            record

    def test_a_non_stream_create_links_nothing(self, ingest_env, env):
        usecase_id = env.create_usecase()
        thing_name = register_device(ingest_env, usecase_id)
        create = seed_pending_create(ingest_env, thing_name, usecase_id,
                                     "Camera")
        camera = created_camera("Camera", create["params"])
        ingest_env.module.handler({"Records": [make_record(
            thing_name, report({"cfg-c": camera, "portal-x": dict(camera)},
                               reported_at=1730000001000))]}, None)
        items = device_items(ingest_env, thing_name)
        # Stored exactly as at 2ae4645: the reported content, synced, and
        # no Portal-owned key on either entry.
        base_keys = {"device_id", "sk", "camera_source_id", "usecase_id",
                     "name", "type", "params", "capabilities", "origin",
                     "version", "last_reported_at", "absent", "sync_status"}
        for csid in ("cfg-c", "portal-x"):
            entry = camera_item(items, csid)
            assert set(entry) == base_keys, csid
            assert entry["sync_status"] == "synced"
            assert entry["params"] == create["params"]
            assert entry["last_reported_at"] == 1730000001000

    def test_the_partial_processing_fallback(self, ingest_env, env):
        """A retried, partly processed event: the create's entry was
        already rebuilt from the mirror, so the link is missed and the
        created entry has no record. 29.4's resolution still finds the
        secret through its reported Credential_Reference."""
        usecase_id = env.create_usecase()
        thing_name = register_device(ingest_env, usecase_id)
        record = LINK_ARN.format(device=thing_name)
        create = seed_pending_create(ingest_env, thing_name, usecase_id,
                                     "RTSP", record)
        camera = created_camera("RTSP", create["params"])
        # The first, partly failed attempt persisted the mirror only.
        mirror = dict(create)
        for key in ("pending_content", "portal_change_id"):
            mirror.pop(key)
        mirror.update(sync_status="synced", alias_of="cfg-y", version=1,
                      origin="edge-configured", last_reported_at=1730000001000)
        ingest_env.registry.put_item(Item=mirror)

        ingest_env.module.handler({"Records": [make_record(
            thing_name, report({"cfg-y": camera, "portal-x": dict(camera)},
                               reported_at=1730000001000))]}, None)
        created = camera_item(device_items(ingest_env, thing_name), "cfg-y")
        assert "credential_secret_arn" not in created
        assert created["params"]["credentialRef"]["secretArn"] == record

        import camera_registry
        resolved = camera_registry.credential_secret_ids(
            created, thing_name, "cfg-y", "123456789012", "us-east-1")
        assert resolved[0][0] == record


class TestStaleFailureMatrix:
    """Requirement 5.12: a reported failure keeps its entry, except an
    entry pending a Portal delete whose id is neither cfg- nor
    discovery-managed and whose failure belongs to an earlier change."""

    IDS = ("portal-0123456789ab", "cam-1", "cfg", "CFG-1",
           "static-image-camera-2", "cfg-1", "disc-1", "arv-1",
           "static-image-camera", "static-video-camera")
    LISTED = {"cfg-1", "disc-1", "arv-1", "static-image-camera",
              "static-video-camera"}
    STATES = ("pending_delete", "pending_update", "pending_create",
              "synced", "failed")
    FAILURES = ("earlier", "current", "without_change_id")

    @staticmethod
    def entry(thing_name, usecase_id, csid, state):
        item = {
            "device_id": thing_name, "sk": f"CAMERA#{csid}",
            "camera_source_id": csid, "usecase_id": usecase_id,
            "name": csid, "type": "Camera",
            "params": {"devicePath": "/dev/video0"}, "capabilities": {},
            "origin": "portal-created", "version": 1, "absent": False,
        }
        op = {"pending_delete": "delete", "pending_update": "update",
              "pending_create": "create"}.get(state)
        if op is not None:
            item.update(sync_status="pending", portal_change_id="pc-now",
                        pending_content={"op": op} if op == "delete" else {
                            "op": op, "name": csid, "type": "Camera",
                            "params": {"devicePath": "/dev/video1"}})
        elif state == "failed":
            item.update(sync_status="failed", portal_change_id="pc-now",
                        failure_reason="it failed")
        else:
            item.update(sync_status="synced")
        return item

    @pytest.mark.parametrize("state", STATES)
    @pytest.mark.parametrize("failure_kind", FAILURES)
    def test_matrix(self, ingest_env, env, state, failure_kind):
        usecase_id = env.create_usecase()
        thing_name = register_device(ingest_env, usecase_id)
        failure = {"reason": "an earlier change failed"}
        if failure_kind == "earlier":
            failure["portalChangeId"] = "pc-earlier"
        elif failure_kind == "current":
            failure["portalChangeId"] = "pc-now"
        if state == "synced" and failure_kind == "without_change_id":
            # A synced entry has no change id, so this failure would be
            # its own; the matrix keeps the failure foreign.
            failure["portalChangeId"] = "pc-earlier"
        for csid in self.IDS:
            ingest_env.registry.put_item(
                Item=self.entry(thing_name, usecase_id, csid, state))

        ingest_env.module.handler({"Records": [make_record(
            thing_name, report({}, reported_at=1730000001000,
                               failures={csid: dict(failure)
                                         for csid in self.IDS}))]}, None)

        items = device_items(ingest_env, thing_name)
        for csid in self.IDS:
            entry = camera_item(items, csid)
            removed = (csid not in self.LISTED and state == "pending_delete"
                       and failure_kind == "earlier")
            if removed:
                assert entry is None, csid
                continue
            assert entry is not None, (csid, state, failure_kind)
            if failure_kind == "current" and state != "synced":
                assert entry["sync_status"] == "failed", csid
                assert entry["failure_reason"] == failure["reason"]
            else:
                expected = self.entry(thing_name, usecase_id, csid,
                                      state)["sync_status"]
                assert entry["sync_status"] == expected, csid
        assert conflict_items(items) == []
