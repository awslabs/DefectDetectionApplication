"""Portal absence-leg baseline for model-gpu-fallback-visibility (Task 2).

Property 2: Preservation — portal leg (bugfix.md 2.5 absence clause + 3.4).

Observation-first baseline captured from the UNFIXED ``devices.py``
(2026-08-16): the single-device ``GET /api/v1/devices/{id}`` response shape
for a device that reports NO ``dda-model-status`` shadow. Requirement 2.5:
when a device reports no signal (older device software, or the signal has
not yet propagated), the portal renders that device EXACTLY as it does
today — so the fixed handler's response, minus the optional additive
``model_status`` key, must deep-equal this baseline.

Written absence-tolerant so it PASSES ON BOTH TREES:
- unfixed tree: no ``model_status`` key exists → the popped-key comparison
  is against the response as-is (this run RECORDS the baseline);
- fixed tree (binds at tasks 3.9/4.4): the additive ``model_status`` key is
  popped before comparison, and when present for a no-shadow device it must
  be ``None`` (Decision 6: absence means "no information").

The DeviceDetail absence-DOM leg is host-testable only via vitest and lands
with task 4.4 (deferred there, not silently skipped).

Harness: the established devices.py pattern (test_secure_tunneling_jp5_guard)
— module imported inside the moto-backed ``aws_stack`` session fixture,
use-case context / cross-account role / boto3 client factory monkeypatched
to deterministic fakes. Any client the (future) shadow-read leg requests
(e.g. ``iot-data``) raises ``ResourceNotFoundException``, which per design
must degrade to ``model_status: None``, never an error.

# Validates: Requirements 2.5, 3.4
"""
from __future__ import annotations

import json
import sys
from datetime import datetime

import pytest
from botocore.exceptions import ClientError

DEVICE_ID = "jetson-thor1"
USECASE_ID = "uc-model-status"
ACCOUNT_ID = "123456789012"
REGION = "us-east-1"


@pytest.fixture(scope="module")
def devices(aws_stack):
    """Import devices inside the moto mock so its module-level boto3
    resources are intercepted (the established device-test pattern)."""
    for module_name in ("devices", "deployments", "workflow_guards"):
        sys.modules.pop(module_name, None)
    import devices

    return devices


# ---------------------------------------------------------------------------
# Deterministic AWS-client fakes for the get_device read path
# ---------------------------------------------------------------------------

class _FakeIot:
    def describe_thing(self, thingName):
        assert thingName == DEVICE_ID
        return {
            "thingArn": f"arn:aws:iot:{REGION}:{ACCOUNT_ID}:thing/{DEVICE_ID}",
            "thingTypeName": "DDA-Device",
            "attributes": {"fleet": "production"},
            "version": 3,
        }


class _FakeGreengrass:
    def list_tags_for_resource(self, resourceArn):
        return {"tags": {"dda-portal:managed": "true"}}

    def get_core_device(self, coreDeviceThingName):
        return {
            "status": "HEALTHY",
            "lastStatusUpdateTimestamp": datetime(2026, 8, 15, 20, 23, 13),
            "coreVersion": "2.12.5",
            "platform": "linux",
            "architecture": "aarch64",
            "tags": {},
        }

    def get_paginator(self, name):
        assert name == "list_installed_components"
        pages = [{
            "installedComponents": [{
                "componentName": "aws.edgeml.dda.LocalServer.arm64JP7",
                "componentVersion": "1.0.34",
                "lifecycleState": "RUNNING",
                "lifecycleStateDetails": None,
                "isRoot": True,
                "lastStatusChangeTimestamp": datetime(2026, 8, 15, 20, 0, 0),
                "lastInstallationSource": "dep-1",
                "lastReportedTimestamp": datetime(2026, 8, 15, 20, 1, 0),
            }],
        }]

        class _P:
            def paginate(self, **kwargs):
                return iter(pages)

        return _P()

    def list_effective_deployments(self, coreDeviceThingName, maxResults):
        return {"effectiveDeployments": [{
            "deploymentId": "dep-1",
            "deploymentName": "Deployment for jetson-thor1",
            "iotJobId": "job-1",
            "iotJobArn": f"arn:aws:iot:{REGION}:{ACCOUNT_ID}:job/job-1",
            "targetArn": f"arn:aws:iot:{REGION}:{ACCOUNT_ID}:thing/{DEVICE_ID}",
            "coreDeviceExecutionStatus": "SUCCEEDED",
            "reason": "SUCCESSFUL",
            "creationTimestamp": datetime(2026, 8, 15, 19, 0, 0),
            "modifiedTimestamp": datetime(2026, 8, 15, 19, 30, 0),
        }]}


class _NoShadowClient:
    """Any other client the handler asks for (the fixed tree's shadow-read
    leg, e.g. ``iot-data``): every call raises ResourceNotFoundException —
    the no-shadow device. Per design Decision 6 the fixed handler must
    degrade this to ``model_status: None``, never an error."""

    def __init__(self, service):
        self._service = service

    def __getattr__(self, name):
        def _raise(*args, **kwargs):
            raise ClientError(
                {"Error": {"Code": "ResourceNotFoundException",
                           "Message": f"no shadow ({self._service}.{name})"}},
                name)

        return _raise


def _client_factory(service, credentials=None, region=None, *a, **k):
    if service == "iot":
        return _FakeIot()
    if service == "greengrassv2":
        return _FakeGreengrass()
    return _NoShadowClient(service)


@pytest.fixture
def wired_devices(devices, monkeypatch):
    monkeypatch.setattr(devices, "get_usecase", lambda usecase_id: {
        "usecase_id": usecase_id,
        "cross_account_role_arn":
            f"arn:aws:iam::{ACCOUNT_ID}:role/portal-cross-account",
        "external_id": "ext-1",
        "region": REGION,
        "account_id": ACCOUNT_ID,
    })
    monkeypatch.setattr(devices, "assume_cross_account_role",
                        lambda *a, **k: {})
    monkeypatch.setattr(devices, "create_boto3_client", _client_factory)
    monkeypatch.setattr(devices, "is_super_user", lambda user_id: True)
    monkeypatch.setattr(devices, "log_audit_event", lambda *a, **k: None)
    # The fixed read leg may use a dedicated use-case client helper (the
    # camera_registry refresh pattern); route it to the same fakes if it
    # appears on the module. Absence-tolerant: nothing to patch today.
    if hasattr(devices, "get_usecase_client"):
        monkeypatch.setattr(
            devices, "get_usecase_client",
            lambda service, *a, **k: _client_factory(service))
    return devices


# ---------------------------------------------------------------------------
# The UNFIXED baseline, recorded 2026-08-16 (this test's first green run IS
# the observation). Every key mirrors devices.get_device on the unfixed
# tree with the deterministic fakes above; datetimes ISO-serialized by the
# handler itself.
# ---------------------------------------------------------------------------

EXPECTED_DEVICE_BASELINE = {
    "device_id": DEVICE_ID,
    "thing_name": DEVICE_ID,
    "thing_arn": f"arn:aws:iot:{REGION}:{ACCOUNT_ID}:thing/{DEVICE_ID}",
    "thing_type": "DDA-Device",
    "attributes": {"fleet": "production"},
    "version": 3,
    "tags": {"dda-portal:managed": "true"},
    "status": "HEALTHY",
    "last_status_update": "2026-08-15T20:23:13",
    "greengrass_version": "2.12.5",
    "platform": "linux",
    "architecture": "aarch64",
    "test_device": False,
    "target_architecture": None,
    "installed_components": [{
        "componentName": "aws.edgeml.dda.LocalServer.arm64JP7",
        "componentVersion": "1.0.34",
        "lifecycleState": "RUNNING",
        "lifecycleStateDetails": None,
        "isRoot": True,
        "lastStatusChangeTimestamp": "2026-08-15T20:00:00",
        "lastInstallationSource": "dep-1",
        "lastReportedTimestamp": "2026-08-15T20:01:00",
    }],
    "deployments": [{
        "deploymentId": "dep-1",
        "deploymentName": "Deployment for jetson-thor1",
        "iotJobId": "job-1",
        "iotJobArn": f"arn:aws:iot:{REGION}:{ACCOUNT_ID}:job/job-1",
        "targetArn": f"arn:aws:iot:{REGION}:{ACCOUNT_ID}:thing/{DEVICE_ID}",
        "coreDeviceExecutionStatus": "SUCCEEDED",
        "reason": "SUCCESSFUL",
        "creationTimestamp": "2026-08-15T19:00:00",
        "modifiedTimestamp": "2026-08-15T19:30:00",
    }],
    "usecase_id": USECASE_ID,
}


def test_get_device_without_shadow_matches_unfixed_baseline(wired_devices):
    """Single-device GET for a device with NO dda-model-status shadow:
    the response, minus the optional additive ``model_status`` key, must
    deep-equal the recorded unfixed baseline — byte-for-byte today's
    rendering (2.5 absence clause; 3.4 additive-only). Passes on BOTH
    trees; the deep-equal-minus-model_status binding is exercised for real
    at tasks 3.9/4.4.

    # Validates: Requirements 2.5, 3.4
    """
    response = wired_devices.get_device(
        DEVICE_ID, {"user_id": "user-1"}, {"usecase_id": USECASE_ID})

    assert response["statusCode"] == 200, response
    device = json.loads(response["body"])["device"]

    # The additive key (fixed tree only). For a no-shadow device it must be
    # None — absence means "no information" (Decision 6) — never an error
    # and never a value fabricated without a device-reported signal.
    if "model_status" in device:
        assert device["model_status"] is None, (
            "a device with no dda-model-status shadow must read as "
            f"model_status: None, got {device['model_status']!r}")
        device.pop("model_status")

    assert device == EXPECTED_DEVICE_BASELINE, (
        "single-device GET response (minus the optional additive "
        "model_status key) diverged from the recorded unfixed baseline — "
        "the portal must render a no-signal device exactly as today (2.5)")


# ===========================================================================
# Task 4.4 — fix-checking legs (Property 4 portal leg; design fix-check
# case 8). The baseline test above stays as-is; the fixed handler now HAS
# the shadow-read leg, so these bind the additive behavior:
#   shadow present            -> model_status carries the reported document
#   ResourceNotFoundException -> model_status: None, rest unchanged (the
#                                task-2 baseline, bound explicitly)
#   any other iot-data error  -> model_status: None, never a 500
# ===========================================================================

import io

# The reported document a healthy-ish device would publish to the
# dda-model-status shadow (design Decision 4 shape).
REPORTED_MODEL_STATUS = {
    "models": {
        "yolo_test": {
            "status": "READY",
            "runtime": "onnx",
            "gpuRequested": True,
            "gpuActive": False,
        },
    },
    "gpuDegraded": True,
    "gpuChainModels": 1,
    "gpuActiveModels": 0,
    "updatedAt": "2026-08-16T12:00:00Z",
}


class _ShadowIotData:
    """iot-data fake with the dda-model-status shadow PRESENT. Returns the
    full GetThingShadow document (state + metadata + version + timestamp)
    so the test proves the handler attaches ONLY state.reported."""

    def get_thing_shadow(self, thingName, shadowName):
        assert thingName == DEVICE_ID
        assert shadowName == "dda-model-status"
        document = {
            "state": {
                "reported": REPORTED_MODEL_STATUS,
                # A desired section must NOT leak into model_status (the
                # shadow is reported-only by design, but the read side
                # must still select state.reported).
                "desired": {"should": "never-appear"},
            },
            "metadata": {"reported": {}},
            "version": 7,
            "timestamp": 1786968000,
        }
        return {"payload": io.BytesIO(json.dumps(document).encode())}


class _ErrorIotData:
    """iot-data fake failing with a NON-ResourceNotFoundException error —
    per design any read error degrades to model_status: None, never a 500."""

    def get_thing_shadow(self, **kwargs):
        raise ClientError(
            {"Error": {"Code": "ThrottlingException",
                       "Message": "Rate exceeded"}},
            "GetThingShadow")


def _wire(devices, monkeypatch, iot_data_client):
    """The wired_devices patches with a controllable iot-data client."""
    monkeypatch.setattr(devices, "get_usecase", lambda usecase_id: {
        "usecase_id": usecase_id,
        "cross_account_role_arn":
            f"arn:aws:iam::{ACCOUNT_ID}:role/portal-cross-account",
        "external_id": "ext-1",
        "region": REGION,
        "account_id": ACCOUNT_ID,
    })
    monkeypatch.setattr(devices, "assume_cross_account_role",
                        lambda *a, **k: {})
    monkeypatch.setattr(devices, "create_boto3_client", _client_factory)
    monkeypatch.setattr(devices, "is_super_user", lambda user_id: True)
    monkeypatch.setattr(devices, "log_audit_event", lambda *a, **k: None)
    monkeypatch.setattr(
        devices, "get_usecase_client",
        lambda service, *a, **k: (
            iot_data_client if service == "iot-data"
            else _client_factory(service)))
    return devices


@pytest.fixture
def shadow_devices(devices, monkeypatch):
    return _wire(devices, monkeypatch, _ShadowIotData())


@pytest.fixture
def error_devices(devices, monkeypatch):
    return _wire(devices, monkeypatch, _ErrorIotData())


def _get_device(module):
    response = module.get_device(
        DEVICE_ID, {"user_id": "user-1"}, {"usecase_id": USECASE_ID})
    assert response["statusCode"] == 200, response
    return json.loads(response["body"])["device"]


def test_get_device_with_shadow_attaches_reported_document(shadow_devices):
    """Shadow present: the single-device GET carries the additive
    ``model_status`` field with EXACTLY the shadow's state.reported
    document (not the whole shadow envelope, not state.desired), and the
    rest of the response is byte-identical to the baseline — additive-only.

    # Validates: Requirements 2.5, 3.4
    """
    device = _get_device(shadow_devices)

    assert device["model_status"] == REPORTED_MODEL_STATUS

    device.pop("model_status")
    assert device == EXPECTED_DEVICE_BASELINE, (
        "attaching model_status must not change any other part of the "
        "single-device GET response (3.4 additive-only)")


def test_get_device_no_shadow_yields_model_status_none(wired_devices):
    """ResourceNotFoundException (no dda-model-status shadow — older
    device software): ``model_status`` is present and None, and the REST of
    the response is unchanged. This binds the task-2 absence baseline
    explicitly on the fixed tree: the key must EXIST (the handler always
    attaches it) and must be None (Decision 6 — absence means "no
    information", never an error and never a fabricated value).

    # Validates: Requirements 2.5, 3.4
    """
    device = _get_device(wired_devices)

    assert "model_status" in device
    assert device["model_status"] is None

    device.pop("model_status")
    assert device == EXPECTED_DEVICE_BASELINE


def test_get_device_iot_data_error_degrades_to_none(error_devices):
    """Any OTHER iot-data error (here ThrottlingException): the read leg
    degrades to ``model_status: None`` with the rest of the response
    unchanged — a shadow-read problem must never turn the device GET into
    a 500.

    # Validates: Requirements 2.5, 3.4
    """
    device = _get_device(error_devices)

    assert device["model_status"] is None

    device.pop("model_status")
    assert device == EXPECTED_DEVICE_BASELINE
