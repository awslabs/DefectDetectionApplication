"""
Sync reducer: stream Camera_Sources and reported Device_Stream_Capabilities
(rtsp-rtmp-stream-cameras tasks 10.1 and 10.2).

- ``reported.deviceCapabilities.streamIngest`` is stored, sanitized, as
  ``stream_capabilities`` on the device META item and returned by
  ``GET /devices/{id}/cameras`` (Req 16.5).
- A capabilities section that cannot be processed never affects the camera
  reduction or the META stamp (design component 8, the
  ``_process_pin_section`` isolation pattern).
- ``RTSP`` / ``RTMP`` entries reduce like every other type, and a pending
  stream change the device applied converges to synced: the device echoes
  ``credentialRef`` / ``credentialsUpdatedAt`` verbatim and reports its
  default for every setting the Portal left unset (Reqs 5.5, 18.3).

Requirements: 5.5, 16.5, 18.3
"""
import json
import os
import sys
import uuid
from decimal import Decimal
from types import SimpleNamespace

import pytest

from conftest import REGION, TEST_ENV

CAMERA_REGISTRY_TABLE_NAME = "test-camera-registry-stream-capabilities"
SETTINGS_TABLE_NAME = "test-settings-camera-stream-capabilities"
DLQ_NAME = "test-camera-shadow-report-dlq-stream"
NOW = 1_790_000_000_000

CAPABILITIES = {
    "rtsp": True, "rtmp": True, "tls": True,
    "codecs": {
        "h264": {"hardware": "nvv4l2decoder", "software": "avdec_h264"},
        "h265": {"hardware": "nvv4l2decoder", "software": "avdec_h265"},
    },
    "gstreamer": "1.20.3", "pyav": "14.2.0", "ffmpeg": "7.1",
    "probedAtMs": NOW,
}

REFERENCE = {"secretArn": "arn:aws:secretsmanager:us-east-1:123456789012:"
                          "secret:dda-portal/stream-camera-credentials/t/c-AbCdEf",
             "versionId": "v-1"}


@pytest.fixture(scope="module")
def sync(aws_stack):
    """Registry + settings tables, a DLQ, and freshly bound camera_sync and
    camera_registry modules."""
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
    client.create_table(
        TableName=SETTINGS_TABLE_NAME,
        KeySchema=[{"AttributeName": "setting_key", "KeyType": "HASH"}],
        AttributeDefinitions=[
            {"AttributeName": "setting_key", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    sqs = boto3.client("sqs", region_name=REGION)
    dlq_url = sqs.create_queue(QueueName=DLQ_NAME)["QueueUrl"]
    os.environ["CAMERA_REGISTRY_TABLE"] = CAMERA_REGISTRY_TABLE_NAME
    os.environ["SETTINGS_TABLE"] = SETTINGS_TABLE_NAME
    os.environ["CAMERA_SHADOW_REPORT_DLQ_URL"] = dlq_url
    sys.modules.pop("camera_sync", None)
    sys.modules.pop("camera_registry", None)
    import camera_registry
    import camera_sync

    resource = boto3.resource("dynamodb", region_name=REGION)
    yield SimpleNamespace(
        module=camera_sync,
        registry_module=camera_registry,
        registry=resource.Table(CAMERA_REGISTRY_TABLE_NAME),
        devices=resource.Table(TEST_ENV["DEVICES_TABLE"]),
        sqs=sqs,
        dlq_url=dlq_url,
    )


@pytest.fixture
def device(sync, env):
    """A device registered to a fresh Use_Case, with an Operator."""
    usecase_id = env.create_usecase()
    thing_name = f"thing-{uuid.uuid4().hex[:12]}"
    sync.devices.put_item(Item={"device_id": thing_name,
                                "usecase_id": usecase_id})
    return SimpleNamespace(thing_name=thing_name, usecase_id=usecase_id,
                           operator=env.make_user(role="Operator"))


def ingest(sync, thing_name, reported):
    """Run one shadow documents event through the SQS handler."""
    body = json.dumps({"thing_name": thing_name,
                       "current": {"state": {"reported": reported}}})
    result = sync.module.handler(
        {"Records": [{"messageId": str(uuid.uuid4()), "body": body}]}, None)
    assert result == {"batchItemFailures": []}


def meta_of(sync, thing_name):
    return sync.registry.get_item(
        Key={"device_id": thing_name, "sk": "META"}).get("Item")


def camera_item(sync, thing_name, csid):
    return sync.registry.get_item(
        Key={"device_id": thing_name, "sk": f"CAMERA#{csid}"}).get("Item")


def dlq_depth(sync):
    response = sync.sqs.get_queue_attributes(
        QueueUrl=sync.dlq_url, AttributeNames=["ApproximateNumberOfMessages"])
    return int(response["Attributes"]["ApproximateNumberOfMessages"])


def usb_camera(version=1):
    return {"version": version, "name": "USB", "type": "Camera",
            "origin": "edge-configured",
            "params": {"devicePath": "/dev/video0"}, "capabilities": {}}


def rtsp_camera(version=1, **params):
    """A stream entry as the Edge_Sync_Agent reports it (design component
    12): every setting present, the device default where none was set."""
    reported_params = {
        "url": "rtsp://10.0.4.21:554/Streaming/Channels/101",
        "transport": "tcp", "latencyMs": 200, "decoder": "auto",
        "maxFrameDimension": 1920, "stallTimeoutS": 10,
        "credentialsConfigured": False,
    }
    reported_params.update(params)
    return {
        "version": version, "name": "Dock 3", "type": "RTSP",
        "origin": "edge-configured", "params": reported_params,
        "capabilities": {"stream": {"state": "streaming", "codec": "h265",
                                    "width": 1920, "height": 1080,
                                    "decoder": "hardware"}},
    }


# ---------------------------------------------------------------------------
# sanitize_stream_capabilities
# ---------------------------------------------------------------------------

class TestSanitizeStreamCapabilities:

    def test_documented_shape_is_kept(self, sync):
        assert sync.module.sanitize_stream_capabilities(CAPABILITIES) == \
            CAPABILITIES

    def test_unknown_keys_and_wrong_types_are_dropped(self, sync):
        section = dict(CAPABILITIES, rtsp="yes", tls=1, gstreamer=1.2,
                       ffmpeg="x" * 65, probedAtMs=-5, shell="rm -rf /",
                       extra={"nested": True})
        assert sync.module.sanitize_stream_capabilities(section) == {
            "rtmp": True,
            "codecs": CAPABILITIES["codecs"],
            "pyav": "14.2.0",
        }

    def test_codecs_are_bounded_and_unavailable_paths_absent(self, sync):
        codecs = {"h264": {"hardware": None, "software": "avdec_h264"},
                  "H265": {"hardware": "nvv4l2decoder"},       # not lowercase
                  "vp9": "avdec_vp9",                           # not an object
                  "av1": {"software": 7}}                       # not a string
        # A flood of extra codec names (sorting after the real ones) is cut
        # at the bound.
        codecs.update({f"zz{i:02d}": {"software": "d"} for i in range(30)})
        kept = sync.module.sanitize_stream_capabilities(
            {"codecs": codecs})["codecs"]
        assert kept["h264"] == {"software": "avdec_h264"}
        assert kept["av1"] == {}
        assert "H265" not in kept and "vp9" not in kept
        assert len(kept) == sync.module._MAX_CAPABILITY_CODECS

    def test_decimal_probe_time_from_the_json_parse_is_an_int(self, sync):
        assert sync.module.sanitize_stream_capabilities(
            {"probedAtMs": Decimal("1790000000000")}) == {
                "probedAtMs": 1790000000000}

    @pytest.mark.parametrize("section", [[], "caps", 7, True])
    def test_a_non_object_section_is_rejected(self, sync, section):
        with pytest.raises(ValueError):
            sync.module.sanitize_stream_capabilities(section)


# ---------------------------------------------------------------------------
# Req 16.5: capabilities on the META item and in GET /devices/{id}/cameras
# ---------------------------------------------------------------------------

class TestCapabilitiesSection:

    def test_reported_capabilities_are_stored_on_meta(self, sync, device):
        ingest(sync, device.thing_name, {
            "reportedAt": NOW,
            "cameras": {"usb-1": usb_camera()},
            "deviceCapabilities": {"streamIngest": CAPABILITIES},
        })
        meta = meta_of(sync, device.thing_name)
        assert meta["stream_capabilities"]["codecs"] == CAPABILITIES["codecs"]
        assert meta["stream_capabilities"]["rtsp"] is True
        assert meta["last_report_at"] == NOW
        assert camera_item(sync, device.thing_name, "usb-1") is not None

    @pytest.mark.parametrize("section", [
        ["not", "an", "object"],
        {"streamIngest": "caps"},
        {"streamIngest": ["caps"]},
    ])
    def test_a_malformed_section_never_affects_the_camera_reduction(
            self, sync, device, section):
        ingest(sync, device.thing_name, {
            "reportedAt": NOW,
            "cameras": {"usb-1": usb_camera(), "rtsp-1": rtsp_camera()},
            "deviceCapabilities": section,
        })
        assert camera_item(sync, device.thing_name, "usb-1")[
            "sync_status"] == "synced"
        assert camera_item(sync, device.thing_name, "rtsp-1")[
            "sync_status"] == "synced"
        meta = meta_of(sync, device.thing_name)
        assert meta["last_report_at"] == NOW
        assert meta["never_synced"] is False
        assert "stream_capabilities" not in meta
        assert dlq_depth(sync) == 0

    def test_an_unexpected_failure_is_isolated_too(self, sync, device,
                                                   monkeypatch):
        def explode(section):
            raise RuntimeError("boom")
        monkeypatch.setattr(sync.module, "sanitize_stream_capabilities",
                            explode)
        ingest(sync, device.thing_name, {
            "reportedAt": NOW, "cameras": {"usb-1": usb_camera()},
            "deviceCapabilities": {"streamIngest": CAPABILITIES},
        })
        assert camera_item(sync, device.thing_name, "usb-1") is not None
        assert meta_of(sync, device.thing_name)["last_report_at"] == NOW

    def test_a_report_without_the_section_keeps_the_stored_value(
            self, sync, device):
        ingest(sync, device.thing_name, {
            "reportedAt": NOW, "cameras": {},
            "deviceCapabilities": {"streamIngest": CAPABILITIES},
        })
        ingest(sync, device.thing_name, {"reportedAt": NOW + 1000,
                                         "cameras": {}})
        meta = meta_of(sync, device.thing_name)
        assert meta["last_report_at"] == NOW + 1000
        assert meta["stream_capabilities"]["gstreamer"] == "1.20.3"

    def test_a_new_report_replaces_the_stored_value(self, sync, device):
        ingest(sync, device.thing_name, {
            "reportedAt": NOW, "cameras": {},
            "deviceCapabilities": {"streamIngest": CAPABILITIES},
        })
        ingest(sync, device.thing_name, {
            "reportedAt": NOW + 1000, "cameras": {},
            "deviceCapabilities": {"streamIngest": {"rtsp": True,
                                                    "rtmp": False}},
        })
        assert meta_of(sync, device.thing_name)["stream_capabilities"] == {
            "rtsp": True, "rtmp": False}

    def _get(self, sync, device):
        event = {
            "httpMethod": "GET",
            "path": f"/devices/{device.thing_name}/cameras",
            "pathParameters": {"id": device.thing_name},
            "queryStringParameters": None,
            "body": None,
            "requestContext": {"authorizer": {"claims": {
                "sub": device.operator["user_id"],
                "email": device.operator["email"],
                "cognito:username": device.operator["username"],
                "custom:role": device.operator["role"],
            }}},
        }
        response = sync.registry_module.handler(event, None)
        assert response["statusCode"] == 200, response["body"]
        return json.loads(response["body"])

    def test_the_camera_list_returns_the_stored_capabilities(self, sync,
                                                            device):
        ingest(sync, device.thing_name, {
            "reportedAt": NOW, "cameras": {"rtsp-1": rtsp_camera()},
            "deviceCapabilities": {"streamIngest": CAPABILITIES},
        })
        body = self._get(sync, device)
        assert body["stream_capabilities"] == CAPABILITIES
        (camera,) = body["cameras"]
        assert camera["capabilities"]["stream"]["codec"] == "h265"

    def test_a_device_without_capabilities_gets_the_unchanged_response(
            self, sync, device):
        ingest(sync, device.thing_name, {"reportedAt": NOW,
                                         "cameras": {"usb-1": usb_camera()}})
        assert "stream_capabilities" not in self._get(sync, device)


# ---------------------------------------------------------------------------
# Reqs 5.5, 18.3: stream entries reduce like every other type
# ---------------------------------------------------------------------------

def pending_entry(csid, content, change_id="pc-1", version=1, **extra):
    entry = {
        "camera_source_id": csid, "usecase_id": "uc-1", "device_id": "t",
        "name": content.get("name"), "type": content.get("type"),
        "params": content.get("params"), "capabilities": {},
        "origin": "portal-created", "version": version,
        "sync_status": "pending", "portal_change_id": change_id,
        "pending_content": {"op": "update", **content},
    }
    entry.update(extra)
    return entry


class TestStreamReduction:

    def test_a_new_stream_entry_upserts_synced(self, sync):
        outcome = sync.module.reduce_report(None, rtsp_camera(), NOW)
        assert outcome.action == "upsert"
        assert outcome.entry["type"] == "RTSP"
        assert outcome.entry["sync_status"] == "synced"
        assert outcome.entry["capabilities"]["stream"]["state"] == \
            "streaming"

    def test_an_older_stream_report_is_discarded(self, sync):
        current = dict(rtsp_camera(version=5), sync_status="synced")
        outcome = sync.module.reduce_report(current, rtsp_camera(version=4),
                                            NOW)
        assert outcome.action == "discard_stale"

    def test_an_acknowledged_stream_change_becomes_synced(self, sync):
        entry = pending_entry("c", {"name": "Dock 3", "type": "RTSP",
                                    "params": {"url": "rtsp://a/b"}})
        incoming = dict(rtsp_camera(version=2, url="rtsp://other/x"),
                        ack="pc-1")
        outcome = sync.module.reduce_report(entry, incoming, NOW)
        assert (outcome.action, outcome.entry["sync_status"]) == (
            "upsert", "synced")
        assert outcome.conflict_event is None

    def test_a_converged_pending_entry_with_an_echoed_reference_is_synced(
            self, sync):
        """No ack: the device applied the change and reports it back with
        credentialRef / credentialsUpdatedAt echoed and its defaults for
        every setting the Portal left unset."""
        pending_params = {"url": "rtsp://10.0.4.21:554/Streaming/Channels/101",
                          "latencyMs": Decimal(300),
                          "credentialRef": REFERENCE,
                          "credentialsConfigured": True,
                          "credentialsUpdatedAt": Decimal(NOW - 5000)}
        entry = pending_entry("c", {"name": "Dock 3", "type": "RTSP",
                                    "params": pending_params})
        incoming = rtsp_camera(version=2, latencyMs=300,
                               credentialRef=REFERENCE,
                               credentialsConfigured=True,
                               credentialsUpdatedAt=NOW - 5000)
        outcome = sync.module.reduce_report(entry, incoming, NOW)
        assert outcome.action == "upsert"
        assert outcome.conflict_event is None
        assert outcome.entry["sync_status"] == "synced"
        assert outcome.entry["params"]["credentialRef"] == REFERENCE

    def test_a_different_reference_is_still_a_conflict(self, sync):
        entry = pending_entry("c", {"name": "Dock 3", "type": "RTSP",
                                    "params": {"url": rtsp_camera()[
                                        "params"]["url"],
                                        "credentialRef": REFERENCE,
                                        "credentialsConfigured": True}})
        incoming = rtsp_camera(version=2, credentialsConfigured=True,
                               credentialRef=dict(REFERENCE,
                                                  versionId="v-0"))
        outcome = sync.module.reduce_report(entry, incoming, NOW)
        assert outcome.action == "conflict"
        assert outcome.conflict_event.resolution == "edge-retained"

    def test_a_changed_setting_is_still_a_conflict(self, sync):
        entry = pending_entry("c", {"name": "Dock 3", "type": "RTSP",
                                    "params": {"url": rtsp_camera()[
                                        "params"]["url"],
                                        "decoder": "software"}})
        outcome = sync.module.reduce_report(entry, rtsp_camera(version=2),
                                            NOW)
        assert outcome.action == "conflict"
        # The recorded edge version is the device's report, not the
        # default-completed comparison form.
        assert outcome.conflict_event.edge_version["params"] == \
            rtsp_camera()["params"]

    def test_rtmp_defaults_do_not_include_rtsp_only_settings(self, sync):
        entry = pending_entry("c", {"name": "Ingest", "type": "RTMP",
                                    "params": {"url": "rtmp://m/live"}})
        incoming = {"version": 2, "name": "Ingest", "type": "RTMP",
                    "origin": "portal-created",
                    "params": {"url": "rtmp://m/live", "decoder": "auto",
                               "maxFrameDimension": 1920,
                               "stallTimeoutS": 10,
                               "credentialsConfigured": False},
                    "capabilities": {}}
        assert sync.module.reduce_report(entry, incoming, NOW).action == \
            "upsert"
        incoming["params"]["transport"] = "tcp"
        assert sync.module.reduce_report(entry, incoming, NOW).action == \
            "conflict"

    def test_other_types_are_compared_exactly_as_before(self, sync):
        """The default completion is for the stream types only (Req 18.3):
        a Camera entry reporting a key its pending content lacks is a
        conflict exactly as it was before this feature."""
        entry = pending_entry("c", {"name": "USB", "type": "Camera",
                                    "params": {"devicePath": "/dev/video0"}})
        incoming = dict(usb_camera(version=2))
        incoming["params"] = {"devicePath": "/dev/video0",
                              "decoder": "auto"}
        assert sync.module.reduce_report(entry, incoming, NOW).action == \
            "conflict"

    def test_a_failed_stream_change_is_marked_failed(self, sync):
        entry = pending_entry("c", {"name": "Dock 3", "type": "RTSP",
                                    "params": {"url": "rtsp://a/b"}})
        outcome = sync.module.reduce_report(
            entry, {"status": "failed", "portalChangeId": "pc-1",
                    "reason": "credential retrieval failed"}, NOW)
        assert outcome.entry["sync_status"] == "failed"
        assert outcome.entry["failure_reason"] == \
            "credential retrieval failed"

    def test_a_stream_entry_deleted_on_the_device_while_pending(self, sync):
        entry = pending_entry("c", {"name": "Dock 3", "type": "RTSP",
                                    "params": {"url": "rtsp://a/b"}})
        outcome = sync.module.reduce_report(entry, None, NOW)
        assert outcome.action == "conflict"
        assert outcome.conflict_event.resolution == "deletion-retained"

    def test_a_converged_stream_report_ingests_synced(self, sync, device):
        """End to end through the SQS handler and DynamoDB (Decimal
        numbers on the stored side)."""
        sync.registry.put_item(Item={
            **pending_entry("rtsp-1", {"name": "Dock 3", "type": "RTSP",
                                       "params": {
                                           "url": rtsp_camera()["params"][
                                               "url"],
                                           "credentialRef": REFERENCE,
                                           "credentialsConfigured": True,
                                           "credentialsUpdatedAt": NOW}}),
            "device_id": device.thing_name, "sk": "CAMERA#rtsp-1",
            "usecase_id": device.usecase_id,
        })
        ingest(sync, device.thing_name, {
            "reportedAt": NOW + 1000,
            "cameras": {"rtsp-1": rtsp_camera(
                version=2, credentialRef=REFERENCE,
                credentialsConfigured=True, credentialsUpdatedAt=NOW)},
        })
        item = camera_item(sync, device.thing_name, "rtsp-1")
        assert item["sync_status"] == "synced"
        conflicts = [i for i in sync.registry.query(
            KeyConditionExpression="device_id = :d",
            ExpressionAttributeValues={":d": device.thing_name})["Items"]
            if i["sk"].startswith("CONFLICT#")]
        assert conflicts == []
