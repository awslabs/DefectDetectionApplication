"""get_deployment's additive model_readiness field (F6).

Spec triton-model-cleanup-wedge, D11 (task 6.2; Requirements 2.10, 3.8;
Property 14): the Portal's view of the LATEST deployment of a target lists
the deployment's model components that devices currently report as not
READY in their dda-model-status shadow (modelReadiness.notReady, selfHeal;
design.md D8). The field is additive, appears only when non-empty, and any
error means no field.

Harness: deployments.py imported inside the moto-backed aws_stack session
fixture (the established deployment-test pattern); the use-case context
and the use-case client factory are replaced by deterministic fakes.
"""
from __future__ import annotations

import io
import json
import sys
import time as real_time

import pytest
from botocore.exceptions import ClientError, ReadTimeoutError

USECASE_ID = "uc-model-readiness"
ACCOUNT_ID = "123456789012"
REGION = "us-east-1"
DEPLOYMENT_ID = "dep-latest"
GROUP_ARN = f"arn:aws:iot:{REGION}:{ACCOUNT_ID}:thinggroup/line-a"
LOCAL_SERVER = "aws.edgeml.dda.LocalServer.arm64JP5"

EXISTING_KEYS = {
    "deployment_id", "deployment_name", "target_arn", "revision_id",
    "deployment_status", "iot_job_id", "iot_job_arn", "is_latest_for_target",
    "creation_timestamp", "components", "deployment_policies", "tags",
    "usecase_id", "effective_deployments", "error_messages",
}


@pytest.fixture(scope="module")
def deployments(aws_stack):
    for module_name in ("deployments", "workflow_guards"):
        sys.modules.pop(module_name, None)
    import deployments

    return deployments


def _device(i):
    return f"line-a-camera-{i:02d}"


def _not_ready_shadow(models, updated_at="2026-10-16T10:00:00Z", self_heal=None):
    reported = {
        "gpuDegraded": False,
        "modelReadiness": {
            "updatedAt": updated_at,
            "readyModels": 0,
            "coldModels": 0,
            "models": [{"model": m, "state": "LOADING", "stalled": True} for m in models],
            "notReady": [{"model": m, "status": "LOADING", "stalled": True,
                          "since": "2026-10-16T09:00:00Z", "reason": "no load progress",
                          "loadOutcome": {"state": "LOADING"}} for m in models],
            "truncated": False,
        },
        "selfHeal": self_heal if self_heal is not None else {
            "enabled": True, "disabledBy": None, "state": "idle", "deferredBy": None,
            "attemptsIn24h": 0, "maxPer24h": 3, "minIntervalSeconds": 1800,
            "rearmsAt": None, "last": None,
        },
    }
    return reported


class FakeGreengrass:
    def __init__(self, devices, components, latest=True, statuses=None):
        self.devices = devices
        self.components = components
        self.latest = latest
        self.statuses = statuses or {}

    def get_deployment(self, deploymentId):
        assert deploymentId == DEPLOYMENT_ID
        return {
            "deploymentId": DEPLOYMENT_ID,
            "deploymentName": "line-a",
            "targetArn": GROUP_ARN,
            "revisionId": "3",
            "deploymentStatus": "ACTIVE",
            "iotJobId": "job-1",
            "iotJobArn": f"arn:aws:iot:{REGION}:{ACCOUNT_ID}:job/job-1",
            "isLatestForTarget": self.latest,
            "components": {name: {"componentVersion": "1.0.0"} for name in self.components},
            "deploymentPolicies": {},
            "tags": {},
        }

    def list_core_devices(self, thingGroupArn, maxResults):
        return {"coreDevices": [{"coreDeviceThingName": d} for d in self.devices]}

    def list_effective_deployments(self, coreDeviceThingName):
        return {"effectiveDeployments": [{
            "deploymentId": DEPLOYMENT_ID,
            "coreDeviceExecutionStatus": self.statuses.get(coreDeviceThingName, "SUCCEEDED"),
            "reason": "SUCCESSFUL",
        }]}


class FakeIotData:
    """get_thing_shadow from a per-device map: a dict is the reported
    document, an Exception instance is raised, a missing device is
    ResourceNotFoundException."""

    def __init__(self, shadows, on_read=None):
        self.shadows = shadows
        self.reads = []
        self.on_read = on_read

    def get_thing_shadow(self, thingName, shadowName):
        assert shadowName == "dda-model-status"
        self.reads.append(thingName)
        if self.on_read:
            self.on_read()
        value = self.shadows.get(thingName)
        if value is None:
            raise ClientError({"Error": {"Code": "ResourceNotFoundException",
                                         "Message": "no shadow"}}, "GetThingShadow")
        if isinstance(value, Exception):
            raise value
        body = json.dumps({"state": {"reported": value}}).encode()
        return {"payload": io.BytesIO(body)}


class Env:
    def __init__(self, deployments, monkeypatch):
        self.d = deployments
        self.monkeypatch = monkeypatch
        self.client_requests = []
        self.greengrass = None
        self.iot_data = None
        monkeypatch.setattr(deployments, "get_usecase", lambda usecase_id: {
            "usecase_id": usecase_id, "account_id": ACCOUNT_ID,
            "cross_account_role_arn": f"arn:aws:iam::{ACCOUNT_ID}:role/portal-cross-account",
            "external_id": "ext-1", "region": REGION,
        })
        monkeypatch.setattr(deployments, "is_super_user", lambda user_id: True)
        monkeypatch.setattr(deployments, "log_audit_event", lambda *a, **k: None)
        monkeypatch.setattr(deployments, "get_usecase_client", self._client)

    def _client(self, service, usecase, session_name=None, region=None, **kwargs):
        self.client_requests.append((service, session_name, region, kwargs))
        if service == "greengrassv2":
            return self.greengrass
        if service == "iot-data":
            if isinstance(self.iot_data, Exception):
                raise self.iot_data
            return self.iot_data
        raise AssertionError(f"unexpected client {service}")

    def get(self, devices, components, shadows, latest=True, statuses=None, on_read=None):
        self.greengrass = FakeGreengrass(devices, components, latest=latest, statuses=statuses)
        if not isinstance(self.iot_data, Exception):
            self.iot_data = FakeIotData(shadows, on_read=on_read)
        response = self.d.get_deployment(DEPLOYMENT_ID, {"user_id": "user-1"},
                                         {"usecase_id": USECASE_ID})
        assert response["statusCode"] == 200, response
        return json.loads(response["body"])["deployment"]

    def iot_data_requests(self):
        return [r for r in self.client_requests if r[0] == "iot-data"]


@pytest.fixture
def env(deployments, monkeypatch):
    return Env(deployments, monkeypatch)


COMPONENTS = [LOCAL_SERVER, "model-a", "model-b"]


# ---------------------------------------------------------------------------
# Present / absent / error / malformed
# ---------------------------------------------------------------------------

def test_shadow_present_lists_not_ready_models_as_current_state(env):
    shadows = {_device(1): _not_ready_shadow(["model-a", "model-other"], self_heal={
        "enabled": True, "disabledBy": None, "state": "exhausted", "deferredBy": None,
        "attemptsIn24h": 3, "maxPer24h": 3, "minIntervalSeconds": 1800,
        "rearmsAt": "2026-10-17T09:00:00Z",
        "last": {"attempt": 3, "requestedAt": "2026-10-16T08:00:00Z",
                 "restartedAt": "2026-10-16T08:00:05Z", "models": [], "evidence": [],
                 "vllmModelsDropped": []},
    })}
    deployment = env.get([_device(1)], COMPONENTS, shadows)

    assert deployment["model_readiness"] == {
        "warning": "Current device state: 1 model(s) of this deployment are not READY on 1 device(s)",
        "devices": [{
            "core_device": _device(1),
            "shadow_updated_at": "2026-10-16T10:00:00Z",
            # model-other is not one of the deployment's components
            "not_ready": [{"model": "model-a", "status": "LOADING",
                           "since": "2026-10-16T09:00:00Z", "reason": "no load progress",
                           "stalled": True}],
            "self_heal": {"state": "exhausted", "enabled": True, "attempts_in_24h": 3,
                          "last_requested_at": "2026-10-16T08:00:00Z",
                          "rearms_at": "2026-10-17T09:00:00Z"},
        }],
        "checked_devices": 1,
        "truncated": False,
    }


def test_two_models_on_one_device_counts_both(env):
    deployment = env.get([_device(1)], COMPONENTS,
                         {_device(1): _not_ready_shadow(["model-a", "model-b"])})
    readiness = deployment["model_readiness"]
    assert readiness["warning"] == (
        "Current device state: 2 model(s) of this deployment are not READY on 1 device(s)")
    assert [i["model"] for i in readiness["devices"][0]["not_ready"]] == ["model-a", "model-b"]
    # idle self-heal with no last attempt is not shown
    assert "self_heal" not in readiness["devices"][0]


def test_shadow_absent_gives_no_field(env):
    deployment = env.get([_device(1), _device(2)], COMPONENTS, {})
    assert "model_readiness" not in deployment
    assert set(deployment) == EXISTING_KEYS


def test_all_models_ready_gives_no_field(env):
    deployment = env.get([_device(1)], COMPONENTS, {_device(1): _not_ready_shadow([])})
    assert "model_readiness" not in deployment


def test_shadow_read_error_gives_no_field(env):
    shadows = {_device(1): ClientError({"Error": {"Code": "AccessDeniedException",
                                                  "Message": "denied"}}, "GetThingShadow")}
    deployment = env.get([_device(1)], COMPONENTS, shadows)
    assert "model_readiness" not in deployment
    assert set(deployment) == EXISTING_KEYS


def test_client_creation_error_gives_no_field(env):
    env.iot_data = RuntimeError("assume role failed")
    deployment = env.get([_device(1)], COMPONENTS, {})
    assert "model_readiness" not in deployment
    assert set(deployment) == EXISTING_KEYS


def test_summary_exception_gives_no_field(env, monkeypatch):
    def _boom(*a, **k):
        raise ValueError("unexpected")

    monkeypatch.setattr(env.d, "_model_readiness_summary", _boom)
    deployment = env.get([_device(1)], COMPONENTS, {_device(1): _not_ready_shadow(["model-a"])})
    assert "model_readiness" not in deployment
    assert set(deployment) == EXISTING_KEYS


def test_malformed_not_ready_map_is_skipped(env):
    reported = _not_ready_shadow(["model-a"])
    reported["modelReadiness"]["notReady"] = {"model-a": {"status": "LOADING"}}
    deployment = env.get([_device(1)], COMPONENTS, {_device(1): reported})
    assert "model_readiness" not in deployment


def test_malformed_entries_and_types_are_skipped_and_strings_cut(env):
    reported = _not_ready_shadow(["model-a"])
    reported["modelReadiness"]["notReady"] = [
        "model-b",                               # not an object
        {"model": 7, "status": "LOADING"},       # model not a string
        {"model": "model-a", "status": "LOADING", "stalled": "yes",
         "since": 12, "reason": "x" * 500},
    ]
    reported["selfHeal"] = ["not", "an", "object"]
    deployment = env.get([_device(1)], COMPONENTS, {_device(1): reported})
    device = deployment["model_readiness"]["devices"][0]
    assert device["not_ready"] == [{"model": "model-a", "status": "LOADING", "reason": "x" * 200}]
    assert "self_heal" not in device


def test_model_readiness_not_an_object_with_active_self_heal(env):
    reported = {"modelReadiness": "broken", "selfHeal": {
        "enabled": True, "state": "cooldown", "attemptsIn24h": 1, "rearmsAt": None,
        "last": {"requestedAt": "2026-10-16T08:00:00Z"}}}
    deployment = env.get([_device(1)], COMPONENTS, {_device(1): reported})
    readiness = deployment["model_readiness"]
    assert readiness["devices"] == [{
        "core_device": _device(1), "shadow_updated_at": None, "not_ready": [],
        "self_heal": {"state": "cooldown", "enabled": True, "attempts_in_24h": 1,
                      "last_requested_at": "2026-10-16T08:00:00Z", "rearms_at": None},
    }]
    assert readiness["warning"].startswith("Current device state: 0 model(s)")


# ---------------------------------------------------------------------------
# Scope: model deployments, latest only, eligible devices, the cap
# ---------------------------------------------------------------------------

def test_non_model_deployment_gives_no_field_and_reads_nothing(env):
    deployment = env.get([_device(1)], [LOCAL_SERVER, "aws.greengrass.ShadowManager"],
                         {_device(1): _not_ready_shadow(["model-a"])})
    assert "model_readiness" not in deployment
    assert env.iot_data_requests() == []


def test_not_latest_for_target_gives_no_field_and_reads_nothing(env):
    deployment = env.get([_device(1)], COMPONENTS, {_device(1): _not_ready_shadow(["model-a"])},
                         latest=False)
    assert "model_readiness" not in deployment
    assert set(deployment) == EXISTING_KEYS
    assert env.iot_data_requests() == []


def test_only_succeeded_or_completed_devices_are_read(env):
    devices = [_device(1), _device(2), _device(3)]
    statuses = {_device(1): "FAILED", _device(2): "COMPLETED", _device(3): "IN_PROGRESS"}
    shadows = {d: _not_ready_shadow(["model-a"]) for d in devices}
    deployment = env.get(devices, COMPONENTS, shadows, statuses=statuses)
    assert env.iot_data.reads == [_device(2)]
    assert [d["core_device"] for d in deployment["model_readiness"]["devices"]] == [_device(2)]


def test_cap_of_twenty_devices_sets_truncated(env):
    devices = [_device(i) for i in range(25)]
    shadows = {d: _not_ready_shadow(["model-a"]) for d in devices}
    deployment = env.get(devices, COMPONENTS, shadows)
    readiness = deployment["model_readiness"]
    assert env.iot_data.reads == devices[:20]
    assert readiness["checked_devices"] == 20
    assert readiness["truncated"] is True
    assert len(readiness["devices"]) == 20
    assert readiness["warning"] == (
        "Current device state: 1 model(s) of this deployment are not READY on 20 device(s)")


# ---------------------------------------------------------------------------
# Property 14: additive; every existing field unchanged
# ---------------------------------------------------------------------------

def test_existing_fields_unchanged(env):
    devices = [_device(1), _device(2)]
    without = env.get(devices, COMPONENTS, {})
    with_field = env.get(devices, COMPONENTS, {_device(1): _not_ready_shadow(["model-a"])})
    assert "model_readiness" in with_field
    with_field.pop("model_readiness")
    for d in (without, with_field):
        d.pop("creation_timestamp", None)
    assert with_field == without
    assert set(with_field) | {"creation_timestamp"} == EXISTING_KEYS


# ---------------------------------------------------------------------------
# Bounded reads: one client, the config, the budget, per-device timeouts
# ---------------------------------------------------------------------------

def test_one_iot_data_client_per_summary_with_the_read_config(env):
    devices = [_device(i) for i in range(3)]
    env.get(devices, COMPONENTS, {d: _not_ready_shadow(["model-a"]) for d in devices})
    requests = env.iot_data_requests()
    assert len(requests) == 1
    _service, session_name, region, kwargs = requests[0]
    assert region == REGION
    assert session_name and len(session_name) <= 64
    cfg = kwargs["config"]
    assert cfg is env.d.SHADOW_READ_CONFIG
    assert cfg.connect_timeout == 2
    assert cfg.read_timeout == 2
    assert cfg.retries == {"total_max_attempts": 1}
    assert env.d.MODEL_READINESS_MAX_DEVICES == 20
    assert env.d.MODEL_READINESS_BUDGET_S == 5


class _FakeTime:
    """deployments.time with a fake monotonic clock; everything else real."""

    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now

    def __getattr__(self, name):
        return getattr(real_time, name)


def test_budget_stops_after_five_seconds_with_truncated(env, monkeypatch):
    clock = _FakeTime()
    monkeypatch.setattr(env.d, "time", clock)

    def _advance():
        clock.now += 2

    devices = [_device(i) for i in range(6)]
    shadows = {d: _not_ready_shadow(["model-a"]) for d in devices}
    deployment = env.get(devices, COMPONENTS, shadows, on_read=_advance)
    readiness = deployment["model_readiness"]
    # checks before each read at 0 s, 2 s, 4 s; the 4th check sees 6 s
    assert env.iot_data.reads == devices[:3]
    assert [d["core_device"] for d in readiness["devices"]] == devices[:3]
    assert readiness["checked_devices"] == 3
    assert readiness["truncated"] is True


def test_read_timeout_skips_only_that_device(env):
    devices = [_device(1), _device(2), _device(3)]
    shadows = {
        _device(1): _not_ready_shadow(["model-a"]),
        _device(2): ReadTimeoutError(endpoint_url="https://example.com"),
        _device(3): _not_ready_shadow(["model-b"]),
    }
    deployment = env.get(devices, COMPONENTS, shadows)
    readiness = deployment["model_readiness"]
    assert env.iot_data.reads == devices
    assert [d["core_device"] for d in readiness["devices"]] == [_device(1), _device(3)]
    assert readiness["checked_devices"] == 3
    assert readiness["truncated"] is False
    assert readiness["warning"] == (
        "Current device state: 2 model(s) of this deployment are not READY on 2 device(s)")
