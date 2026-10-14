"""
Ingest routing unit tests for reported.staticImagePin
(cloud-static-camera-provisioning task 3.2).

Example-based tests for the camera_sync.py SQS ingest extension
(task 3.1): a device pin confirmation delivered through the documents
event routes to the pin_requests reducer; a missing section is a no-op;
a malformed section is logged and skipped without affecting the camera
reduction of the same record (section isolation); confirmations
referencing a superseded or already-terminal Pin_Request change nothing
(condition-guarded no-op — duplicate documents-event re-reduction is
idempotent).

_Requirements: 4.1, 4.8, 5.6_
"""
import json
import os
import sys
import uuid
from types import SimpleNamespace

import pytest

from conftest import REGION

CAMERA_REGISTRY_TABLE_NAME = "test-camera-registry-pin-ingest"
DLQ_NAME = "test-camera-pin-ingest-dlq"
BUCKET_NAME = "test-dda-component-pin-ingest"


@pytest.fixture(scope="module")
def ingest_env(aws_stack):
    """Registry table + DLQ + component bucket + freshly bound portal
    camera_sync / pin_requests modules (the shadow-sync suite pattern)."""
    import boto3

    boto3.client("dynamodb", region_name=REGION).create_table(
        TableName=CAMERA_REGISTRY_TABLE_NAME,
        KeySchema=[
            {"AttributeName": "device_id", "KeyType": "HASH"},
            {"AttributeName": "sk", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "device_id", "AttributeType": "S"},
            {"AttributeName": "sk", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    sqs = boto3.client("sqs", region_name=REGION)
    dlq_url = sqs.create_queue(QueueName=DLQ_NAME)["QueueUrl"]
    s3 = boto3.client("s3", region_name=REGION)
    s3.create_bucket(Bucket=BUCKET_NAME)

    os.environ["CAMERA_REGISTRY_TABLE"] = CAMERA_REGISTRY_TABLE_NAME
    os.environ["CAMERA_SHADOW_REPORT_DLQ_URL"] = dlq_url

    for module_name in ("camera_sync", "pin_requests"):
        sys.modules.pop(module_name, None)
    import camera_sync
    import pin_requests

    resource = boto3.resource("dynamodb", region_name=REGION)
    yield SimpleNamespace(
        camera_sync=camera_sync,
        pin_requests=pin_requests,
        registry=resource.Table(CAMERA_REGISTRY_TABLE_NAME),
        devices=aws_stack.tables.devices,
        s3=s3,
        sqs=sqs,
        dlq_url=dlq_url,
    )


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def register_device(ingest_env):
    """A fresh device with a devices-table record (fresh ids isolate
    tests without table truncation)."""
    device_id = f"thing-pin-ingest-{uuid.uuid4().hex[:12]}"
    usecase_id = f"uc-{uuid.uuid4()}"
    ingest_env.devices.put_item(Item={
        "device_id": device_id, "usecase_id": usecase_id,
    })
    return device_id, usecase_id


def rule_record(thing_name, reported):
    """One SQS record exactly as the IoT documents topic rule produces it."""
    return {
        "messageId": str(uuid.uuid4()),
        "body": json.dumps({
            "thing_name": thing_name,
            "current": {"state": {"reported": reported}, "version": 1},
        }),
    }


def ingest(ingest_env, thing_name, reported):
    result = ingest_env.camera_sync.handler(
        {"Records": [rule_record(thing_name, reported)]}, None)
    assert result == {"batchItemFailures": []}


def put_pending_pin_item(ingest_env, device_id, usecase_id, *,
                         op=None, with_object=False):
    """A pending Pin_Request item; optionally with a canonical S3 object."""
    pr = ingest_env.pin_requests
    created_at = 1_730_000_000_000
    pin_request_id = pr.new_pin_request_id(created_at)
    kwargs = {}
    if (op or pr.OP_PIN) == pr.OP_PIN:
        kwargs = {
            "s3_bucket": BUCKET_NAME,
            "s3_key": f"static-image-pins/{device_id}/{pin_request_id}",
            "sha256": "ab" * 32,
            "size_bytes": 1234,
            "image_format": "PNG",
            "file_name": "sample.png",
        }
    item = pr.build_pin_request_item(
        device_id, usecase_id, op or pr.OP_PIN, created_at,
        pin_request_id=pin_request_id, **kwargs)
    pr.insert_pin_request(ingest_env.registry, item)
    if with_object:
        ingest_env.s3.put_object(
            Bucket=BUCKET_NAME, Key=kwargs["s3_key"], Body=b"img-bytes")
    return item


def get_item(ingest_env, device_id, pin_request_id):
    return ingest_env.pin_requests.get_pin_request_item(
        ingest_env.registry, device_id, pin_request_id)


def confirmation(item, status="applied", **extra):
    """A device reported.staticImagePin echo for one Pin_Request item."""
    document = {
        "requestId": item["pin_request_id"],
        "op": item["op"],
        "status": status,
        "completedAtEpochMs": 1_730_000_100_000,
    }
    document.update(extra)
    return document


APPLIED_METADATA = {"width": 640, "height": 480, "format": "PNG",
                    "fileName": "sample.png"}

CAMERA_REPORT = {
    "name": "Line camera", "type": "Camera", "origin": "edge-configured",
    "params": {"devicePath": "/dev/video0"}, "capabilities": {},
    "version": 1,
}

STATIC_CAMERA_SK = "CAMERA#static-image-camera"


def static_camera_report(version=1):
    """The device's static-image-camera inventory entry as reported
    present (the stale shape shadow merge keeps re-delivering)."""
    return {
        "name": "Static Image Camera", "type": "StaticImage",
        "origin": "edge-discovered", "params": {},
        "capabilities": {"staticImage": {"id": "static-image-camera"}},
        "discovered": True, "absent": False, "version": version,
    }


def put_static_camera_entry(ingest_env, device_id, usecase_id, *,
                            absent=False, absent_since=None, version=1):
    """Seed a CAMERA#static-image-camera registry entry."""
    item = {
        "device_id": device_id, "sk": STATIC_CAMERA_SK,
        "camera_source_id": "static-image-camera",
        "usecase_id": usecase_id,
        "name": "Static Image Camera", "type": "StaticImage",
        "origin": "edge-discovered", "params": {},
        "capabilities": {"staticImage": {"id": "static-image-camera"}},
        "version": version, "sync_status": "synced", "absent": absent,
    }
    if absent_since is not None:
        item["absent_since"] = absent_since
    ingest_env.registry.put_item(Item=item)


def get_static_camera_entry(ingest_env, device_id):
    return ingest_env.registry.get_item(
        Key={"device_id": device_id, "sk": STATIC_CAMERA_SK}).get("Item")


class RecordingIotClient:
    """iot-data client double recording update_thing_shadow calls (or
    failing them, for the best-effort path)."""

    def __init__(self, fail=False):
        self.fail = fail
        self.writes = []

    def update_thing_shadow(self, thingName, shadowName, payload):
        if self.fail:
            raise RuntimeError("injected shadow write failure")
        self.writes.append(
            (thingName, shadowName, json.loads(payload)))
        return {}


@pytest.fixture
def recording_iot(ingest_env, monkeypatch):
    """Route camera_sync's use-case iot-data seam to a recording fake."""
    client = RecordingIotClient()
    usecases = []

    def fake_iot_data_client(usecase_id):
        usecases.append(usecase_id)
        return client

    monkeypatch.setattr(ingest_env.camera_sync, "iot_data_client",
                        fake_iot_data_client)
    client.usecases = usecases
    return client


# ==========================================================================
# Confirmations route to the pin reducer (Req 4.1)
# ==========================================================================

class TestConfirmationRouting:
    def test_applied_confirmation_transitions_pending_item(self, ingest_env):
        device_id, usecase_id = register_device(ingest_env)
        item = put_pending_pin_item(ingest_env, device_id, usecase_id,
                                    with_object=True)

        ingest(ingest_env, device_id, {
            "staticImagePin": confirmation(item, metadata=APPLIED_METADATA),
        })

        stored = get_item(ingest_env, device_id, item["pin_request_id"])
        assert stored["status"] == "applied"
        assert stored["device_metadata"]["width"] == 640
        assert stored["device_metadata"]["fileName"] == "sample.png"
        assert int(stored["completed_at"]) == 1_730_000_100_000
        # Canonical object deleted on the terminal transition.
        objects = ingest_env.s3.list_objects_v2(
            Bucket=BUCKET_NAME, Prefix=item["s3_key"])
        assert objects.get("KeyCount", 0) == 0

    def test_failed_confirmation_records_reason(self, ingest_env):
        device_id, usecase_id = register_device(ingest_env)
        item = put_pending_pin_item(ingest_env, device_id, usecase_id)

        ingest(ingest_env, device_id, {
            "staticImagePin": confirmation(
                item, status="failed", reason="checksum mismatch"),
        })

        stored = get_item(ingest_env, device_id, item["pin_request_id"])
        assert stored["status"] == "failed"
        assert stored["failure_reason"] == "checksum mismatch"

    def test_duplicate_delivery_is_idempotent(self, ingest_env):
        """Re-reducing an already-applied confirmation (the documents
        event always carries the full reported state) is a no-op."""
        device_id, usecase_id = register_device(ingest_env)
        item = put_pending_pin_item(ingest_env, device_id, usecase_id)
        document = confirmation(item, metadata=APPLIED_METADATA)

        ingest(ingest_env, device_id, {"staticImagePin": document})
        first = get_item(ingest_env, device_id, item["pin_request_id"])
        ingest(ingest_env, device_id, {"staticImagePin": document})
        second = get_item(ingest_env, device_id, item["pin_request_id"])

        assert first == second
        assert second["status"] == "applied"


# ==========================================================================
# Missing section is a no-op
# ==========================================================================

class TestMissingSection:
    def test_report_without_pin_section_changes_no_pin_item(self, ingest_env):
        device_id, usecase_id = register_device(ingest_env)
        item = put_pending_pin_item(ingest_env, device_id, usecase_id)

        ingest(ingest_env, device_id, {"cameras": {"cfg-1": CAMERA_REPORT}})

        stored = get_item(ingest_env, device_id, item["pin_request_id"])
        assert stored["status"] == "pending"
        # The camera path processed normally.
        camera = ingest_env.registry.get_item(
            Key={"device_id": device_id, "sk": "CAMERA#cfg-1"}).get("Item")
        assert camera["sync_status"] == "synced"


# ==========================================================================
# Malformed section isolation (task 3.1 section isolation)
# ==========================================================================

class TestMalformedSectionIsolation:
    @pytest.mark.parametrize("malformed", [
        "garbage", 17, ["not", "a", "document"], {"op": "pin"},
        {"requestId": None}, {"requestId": {"nested": True}},
    ])
    def test_malformed_pin_section_leaves_camera_path_untouched(
            self, ingest_env, malformed):
        device_id, usecase_id = register_device(ingest_env)
        item = put_pending_pin_item(ingest_env, device_id, usecase_id)

        ingest(ingest_env, device_id, {
            "cameras": {"cfg-1": CAMERA_REPORT},
            "staticImagePin": malformed,
        })

        # Camera reduction unaffected: entry upserted, META stamped.
        camera = ingest_env.registry.get_item(
            Key={"device_id": device_id, "sk": "CAMERA#cfg-1"}).get("Item")
        assert camera["sync_status"] == "synced"
        meta = ingest_env.registry.get_item(
            Key={"device_id": device_id, "sk": "META"}).get("Item")
        assert meta["never_synced"] is False
        # Pin item untouched by the malformed section.
        stored = get_item(ingest_env, device_id, item["pin_request_id"])
        assert stored["status"] == "pending"

    def test_pin_reducer_exception_is_isolated_and_not_a_batch_failure(
            self, ingest_env, monkeypatch):
        """Even an unexpected reducer crash is logged and skipped: the
        camera reduction persists and the record is not retried."""
        device_id, usecase_id = register_device(ingest_env)

        def boom(*args, **kwargs):
            raise RuntimeError("injected pin reducer crash")

        monkeypatch.setattr(
            ingest_env.pin_requests, "apply_pin_confirmation", boom)
        ingest(ingest_env, device_id, {
            "cameras": {"cfg-1": CAMERA_REPORT},
            "staticImagePin": {"requestId": "whatever", "status": "applied"},
        })

        camera = ingest_env.registry.get_item(
            Key={"device_id": device_id, "sk": "CAMERA#cfg-1"}).get("Item")
        assert camera["sync_status"] == "synced"


# ==========================================================================
# Superseded / terminal request ids change nothing (Reqs 4.8, 5.6)
# ==========================================================================

class TestNonPendingConfirmations:
    def test_confirmation_for_superseded_request_changes_nothing(
            self, ingest_env):
        device_id, usecase_id = register_device(ingest_env)
        pr = ingest_env.pin_requests
        old = put_pending_pin_item(ingest_env, device_id, usecase_id)
        pr.supersede_pending_requests(
            ingest_env.registry, device_id, 1_730_000_050_000)
        newest = put_pending_pin_item(ingest_env, device_id, usecase_id)

        # Late confirmation for the superseded request id (Req 5.6).
        ingest(ingest_env, device_id, {
            "staticImagePin": confirmation(old, metadata=APPLIED_METADATA),
        })

        stored_old = get_item(ingest_env, device_id, old["pin_request_id"])
        assert stored_old["status"] == "superseded"
        assert "device_metadata" not in stored_old
        stored_newest = get_item(ingest_env, device_id,
                                 newest["pin_request_id"])
        assert stored_newest["status"] == "pending"

    def test_confirmation_for_unknown_request_changes_nothing(
            self, ingest_env):
        device_id, usecase_id = register_device(ingest_env)
        item = put_pending_pin_item(ingest_env, device_id, usecase_id)

        ingest(ingest_env, device_id, {
            "staticImagePin": {
                "requestId": "00000000000001#deadbeef",
                "op": "pin", "status": "applied",
                "metadata": APPLIED_METADATA,
            },
        })

        stored = get_item(ingest_env, device_id, item["pin_request_id"])
        assert stored["status"] == "pending"

    def test_confirmation_for_terminal_request_never_retransitions(
            self, ingest_env):
        """A failed item never transitions again (single transition out
        of pending, Req 4.1)."""
        device_id, usecase_id = register_device(ingest_env)
        item = put_pending_pin_item(ingest_env, device_id, usecase_id)
        ingest(ingest_env, device_id, {
            "staticImagePin": confirmation(
                item, status="failed", reason="retrieval failure"),
        })

        ingest(ingest_env, device_id, {
            "staticImagePin": confirmation(item, metadata=APPLIED_METADATA),
        })

        stored = get_item(ingest_env, device_id, item["pin_request_id"])
        assert stored["status"] == "failed"
        assert stored["failure_reason"] == "retrieval failure"
        assert "device_metadata" not in stored


# ==========================================================================
# Applied-remove convergence (Req 6.2 mitigation — second hardware
# finding, jetson-thor1 / LocalServer.arm64JP7 1.0.23): deployed device
# builds omit the static camera from the post-unpin report, but shadow
# updates MERGE nested maps, so the stale key keeps re-upserting the
# registry entry as present. On an applied remove the ingest must (a)
# mark the registry entry absent AFTER the camera reduction and (b)
# best-effort null the stale shadow key.
# ==========================================================================

REMOVE_COMPLETED_AT = 1_730_000_100_000


class TestAppliedRemoveConvergence:
    def _applied_remove(self, ingest_env, device_id, usecase_id,
                        reported_extra=None, stale_cameras=True):
        """One documents event carrying the applied-remove confirmation
        PLUS (by default) the stale present cameras map — exactly what
        shadow merge semantics deliver: the device's post-unpin report
        omitted the key, so the merged reported state still carries it
        present."""
        item = put_pending_pin_item(ingest_env, device_id, usecase_id,
                                    op=ingest_env.pin_requests.OP_REMOVE)
        reported = {"staticImagePin": confirmation(item)}
        if stale_cameras:
            reported["cameras"] = {
                "static-image-camera": static_camera_report(version=2)}
        reported.update(reported_extra or {})
        ingest(ingest_env, device_id, reported)
        return item

    def test_applied_remove_marks_registry_entry_absent(
            self, ingest_env, recording_iot):
        device_id, usecase_id = register_device(ingest_env)
        put_static_camera_entry(ingest_env, device_id, usecase_id)

        item = self._applied_remove(ingest_env, device_id, usecase_id)

        stored = get_item(ingest_env, device_id, item["pin_request_id"])
        assert stored["status"] == "applied"
        entry = get_static_camera_entry(ingest_env, device_id)
        assert entry["absent"] is True
        assert int(entry["absent_since"]) == REMOVE_COMPLETED_AT

    def test_applied_remove_writes_shadow_null_for_stale_key(
            self, ingest_env, recording_iot):
        device_id, usecase_id = register_device(ingest_env)
        put_static_camera_entry(ingest_env, device_id, usecase_id)

        self._applied_remove(ingest_env, device_id, usecase_id)

        assert recording_iot.usecases == [usecase_id]
        assert recording_iot.writes == [(
            device_id, "dda-camera-registry",
            {"state": {"reported": {"cameras": {
                "static-image-camera": None}}}},
        )]

    def test_absent_marking_wins_over_stale_entry_in_same_event(
            self, ingest_env, recording_iot):
        """The documents event carrying the remove confirmation ALSO
        carries the stale (shadow-merged) present cameras map; the camera
        reducer upserts it first (as a fresh, higher-versioned present
        entry), and the pin convergence — running after — must win
        within the event."""
        device_id, usecase_id = register_device(ingest_env)
        put_static_camera_entry(ingest_env, device_id, usecase_id)

        self._applied_remove(
            ingest_env, device_id, usecase_id,
            reported_extra={"cameras": {
                "static-image-camera": static_camera_report(version=5)}})

        entry = get_static_camera_entry(ingest_env, device_id)
        assert int(entry["version"]) == 5  # the camera reducer DID upsert
        assert entry["absent"] is True     # ...and the convergence won
        assert int(entry["absent_since"]) == REMOVE_COMPLETED_AT

    def test_shadow_null_failure_is_logged_not_fatal(
            self, ingest_env, monkeypatch, caplog):
        """A failing shadow write never fails the record: the transition
        and the absent-marking persist and no batch failure is reported
        (ingest() asserts an empty batchItemFailures)."""
        device_id, usecase_id = register_device(ingest_env)
        put_static_camera_entry(ingest_env, device_id, usecase_id)
        failing = RecordingIotClient(fail=True)
        monkeypatch.setattr(ingest_env.camera_sync, "iot_data_client",
                            lambda usecase_id: failing)

        item = self._applied_remove(ingest_env, device_id, usecase_id)

        stored = get_item(ingest_env, device_id, item["pin_request_id"])
        assert stored["status"] == "applied"
        entry = get_static_camera_entry(ingest_env, device_id)
        assert entry["absent"] is True
        assert any("static-image camera shadow key" in record.getMessage()
                   for record in caplog.records)

    def test_no_registry_entry_means_nothing_created(
            self, ingest_env, recording_iot):
        """Absent-marking is condition-guarded: no CAMERA# item and no
        stale key in the report — nothing is created (the shadow-key
        cleanup still runs, harmlessly)."""
        device_id, usecase_id = register_device(ingest_env)

        self._applied_remove(ingest_env, device_id, usecase_id,
                             stale_cameras=False)

        assert get_static_camera_entry(ingest_env, device_id) is None
        assert len(recording_iot.writes) == 1

    def test_already_absent_entry_keeps_original_timestamp(
            self, ingest_env, recording_iot):
        """A remove confirmed after the stale key was already cleared
        (the event's cameras map no longer carries the entry): the
        recorded absence keeps its original timestamp."""
        device_id, usecase_id = register_device(ingest_env)
        original_since = 1_729_000_000_000
        put_static_camera_entry(ingest_env, device_id, usecase_id,
                                absent=True, absent_since=original_since)

        self._applied_remove(ingest_env, device_id, usecase_id,
                             reported_extra={"cameras": {}},
                             stale_cameras=False)

        entry = get_static_camera_entry(ingest_env, device_id)
        assert entry["absent"] is True
        assert int(entry["absent_since"]) == original_since

    def test_applied_pin_confirmation_triggers_no_convergence(
            self, ingest_env, recording_iot):
        """Only applied REMOVES converge: an applied pin neither
        absence-marks the entry nor writes to the shadow."""
        device_id, usecase_id = register_device(ingest_env)
        put_static_camera_entry(ingest_env, device_id, usecase_id)
        item = put_pending_pin_item(ingest_env, device_id, usecase_id,
                                    with_object=True)

        # A real applied-pin event reports the entry present.
        ingest(ingest_env, device_id, {
            "staticImagePin": confirmation(item, metadata=APPLIED_METADATA),
            "cameras": {"static-image-camera": static_camera_report(
                version=2)},
        })

        entry = get_static_camera_entry(ingest_env, device_id)
        assert entry["absent"] is False
        assert recording_iot.writes == []

    def test_duplicate_remove_delivery_converges_once(
            self, ingest_env, recording_iot):
        """The convergence is keyed to the condition-guarded pending ->
        applied transition, so duplicate documents-event re-reduction
        writes the shadow null exactly once."""
        device_id, usecase_id = register_device(ingest_env)
        put_static_camera_entry(ingest_env, device_id, usecase_id)

        item = self._applied_remove(ingest_env, device_id, usecase_id)
        ingest(ingest_env, device_id,
               {"staticImagePin": confirmation(item)})

        assert len(recording_iot.writes) == 1


# ==========================================================================
# Reported deletion of the static camera (post-null events / builds not
# reporting the entry): absence-tracked, never deleted (Req 6.2)
# ==========================================================================

class TestStaticCameraReportedDeletion:
    def test_report_omitting_static_entry_marks_absent_not_deleted(
            self, ingest_env):
        """Once the shadow null lands, subsequent full reports omit the
        static camera entirely; the recorded entry must go (stay) absent
        through the deletion path — not be deleted like a physical
        configured source."""
        device_id, usecase_id = register_device(ingest_env)
        put_static_camera_entry(ingest_env, device_id, usecase_id)

        ingest(ingest_env, device_id, {
            "reportedAt": 1_730_000_400_000,
            "cameras": {"cfg-1": CAMERA_REPORT},
        })

        entry = get_static_camera_entry(ingest_env, device_id)
        assert entry is not None, "entry must not be deleted (Req 6.2)"
        assert entry["absent"] is True
        assert int(entry["absent_since"]) == 1_730_000_400_000

    def test_already_absent_entry_is_untouched_by_omitting_reports(
            self, ingest_env):
        device_id, usecase_id = register_device(ingest_env)
        original_since = 1_729_500_000_000
        put_static_camera_entry(ingest_env, device_id, usecase_id,
                                absent=True, absent_since=original_since)

        ingest(ingest_env, device_id,
               {"reportedAt": 1_730_000_500_000, "cameras": {}})

        entry = get_static_camera_entry(ingest_env, device_id)
        assert entry["absent"] is True
        assert int(entry["absent_since"]) == original_since

    def test_physical_camera_deletion_path_is_unchanged(self, ingest_env):
        """The special case is scoped to the static camera id: a physical
        source missing from the full report still reduces to deletion."""
        device_id, usecase_id = register_device(ingest_env)
        ingest(ingest_env, device_id, {
            "cameras": {"cfg-1": CAMERA_REPORT},
        })
        assert ingest_env.registry.get_item(
            Key={"device_id": device_id, "sk": "CAMERA#cfg-1"}).get("Item")

        ingest(ingest_env, device_id, {"cameras": {}})

        assert ingest_env.registry.get_item(
            Key={"device_id": device_id, "sk": "CAMERA#cfg-1"}).get(
                "Item") is None
