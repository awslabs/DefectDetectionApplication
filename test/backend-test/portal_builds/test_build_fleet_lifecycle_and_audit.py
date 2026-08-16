# Copyright 2025 Amazon Web Services, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""
Unit and integration tests for the build_fleet.py handler
(task 7.6 of portal-build-fleet-and-workflow-gates).

Validates: Requirements 6.2, 6.3, 6.5, 6.6, 6.7, 6.8, 6.12

Covers:
- Terminate confirmation flow: a missing or wrong ``confirm`` body echo
  performs no termination and leaves the Dedicated_Build_Server (record
  and EC2 instance) unchanged (Req 6.6, 6.12); the exact server-name
  echo terminates the instance (Req 6.6).
- Non-PortalAdmin fleet management requests (launch, start, stop,
  terminate): rejected with an authorization error, a denied-access
  Audit_Log entry, and no action performed (Req 6.7).
- Action-outcome Audit_Log entries: every accepted action records a
  success entry with the action, acting user, target server, and
  outcome; a rejected action records a failure entry (Req 6.8).
- moto-based lifecycle integration: start of a stopped server reaches
  running (Req 6.2), stop of a running server with no running Build_Job
  reaches stopped (Req 6.3), and launch provisions a tagged EC2 instance
  of the configured type and registers it in the fleet list (Req 6.5).
  The GET /build-servers reconciliation observes each transition and
  clears the pending_action marker when the expected state is reached.

The BuildServers / PortalSettings tables and the EC2/SSM/IAM control
plane are moto-mocked; the real rbac_middleware decorators and
build_domain validation run unstubbed. Only the RBAC role lookup,
``get_user_from_event``, and ``log_audit_event`` are patched per test
(the sibling pattern in test_build_jobs_rbac_audit.py).
"""
import json
import os
import sys
import types
from unittest import mock

# ---------------------------------------------------------------------------
# Environment BEFORE any import: shared_utils and build_fleet bind their
# boto3 resources/clients and table names at import time.
# ---------------------------------------------------------------------------
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ["AWS_ACCESS_KEY_ID"] = "testing"
os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
os.environ["AWS_SECURITY_TOKEN"] = "testing"
os.environ["AWS_SESSION_TOKEN"] = "testing"

_SERVERS_TABLE = "build-servers-t76"
_SETTINGS_TABLE = "portal-settings-t76"
os.environ["BUILD_SERVERS_TABLE"] = _SERVERS_TABLE
os.environ["SETTINGS_TABLE"] = _SETTINGS_TABLE
# No security group / subnet pin: RunInstances uses the moto defaults.
os.environ.pop("BUILD_SECURITY_GROUP_ID", None)
os.environ.pop("BUILD_SUBNET_ID", None)

# Import boto3 (and thus botocore/urllib3) from the test environment BEFORE
# the Lambda layer directory joins sys.path: the layer vendors its own
# urllib3 build targeting the Lambda Python runtime, which must not shadow
# the environment's copy.
import boto3  # noqa: E402

# The flask-app verification container's python3.9 is built without the
# _bz2 C extension, and moto's request path imports moto.s3 -> bz2 on
# every call. bz2 is only used for S3-Select payload decompression, which
# this suite never exercises, so a minimal stdlib-shaped stub keeps the
# import chain intact where _bz2 is absent (same shim as the sibling
# test_build_jobs_rbac_audit.py).
try:
    import bz2  # noqa: F401
except ImportError:  # pragma: no cover - depends on the runner's build
    _bz2_stub = types.ModuleType("_bz2")

    class _Bz2Unavailable:
        def __init__(self, *args, **kwargs):
            raise RuntimeError("bz2 is unavailable in this environment")

    _bz2_stub.BZ2Compressor = _Bz2Unavailable
    _bz2_stub.BZ2Decompressor = _Bz2Unavailable
    sys.modules["_bz2"] = _bz2_stub

from moto import mock_aws  # noqa: E402

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.normpath(os.path.join(_HERE, "..", "..", ".."))
_BACKEND = os.path.join(_REPO_ROOT, "edge-cv-portal", "backend")
_LAYER_DIR = os.path.join(_BACKEND, "layers", "shared", "python")
_FUNCTIONS_DIR = os.path.join(_BACKEND, "functions")
for _p in (_LAYER_DIR, _FUNCTIONS_DIR):
    if _p not in sys.path:
        sys.path.append(_p)

# Fresh modules so build_fleet's module-level boto3 handles are created
# under the moto mock started below (sibling pattern).
for _module in ("build_fleet", "build_planner", "build_domain",
                "rbac_middleware", "shared_utils"):
    sys.modules.pop(_module, None)

# Module-scope moto: active for every import below and for the whole run.
_MOCK = mock_aws()
_MOCK.start()

_DDB = boto3.resource("dynamodb", region_name="us-east-1")
for _name, _key in ((_SERVERS_TABLE, "server_id"),
                    (_SETTINGS_TABLE, "setting_key")):
    _DDB.create_table(
        TableName=_name,
        KeySchema=[{"AttributeName": _key, "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": _key, "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
_SERVERS = _DDB.Table(_SERVERS_TABLE)

_EC2 = boto3.client("ec2", region_name="us-east-1")
_IAM = boto3.client("iam", region_name="us-east-1")
# The hardened launch profile referenced by RunInstances (design §2).
_IAM.create_instance_profile(InstanceProfileName="dda-build-role")

import build_domain  # noqa: E402
import build_fleet  # noqa: E402
import rbac_middleware  # noqa: E402
from shared_utils import RBACManager, Role  # noqa: E402


_ADMIN = {"user_id": "portal-admin", "email": "admin@example.com",
          "username": "portal-admin"}
_OPERATOR = {"user_id": "build-operator", "email": "op@example.com",
             "username": "build-operator"}

_TEN_MINUTES_MS = 10 * 60 * 1000


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _event(resource, method, body=None, server_id=None):
    """Minimal API Gateway event for build_fleet.handler routing."""
    event = {
        "resource": resource,
        "httpMethod": method,
        "path": resource.replace("{id}", server_id or ""),
    }
    if body is not None:
        event["body"] = json.dumps(body)
    if server_id is not None:
        event["pathParameters"] = {"id": server_id}
    return event


def _moto_ami():
    """Any AMI id known to the moto EC2 backend."""
    return _EC2.describe_images()["Images"][0]["ImageId"]


def _run_real_instance(name, instance_type="m6g.4xlarge"):
    """Launch a real moto EC2 instance for lifecycle tests."""
    response = _EC2.run_instances(
        ImageId=_moto_ami(), MinCount=1, MaxCount=1,
        InstanceType=instance_type,
        TagSpecifications=[{
            "ResourceType": "instance",
            "Tags": [{"Key": "Name", "Value": name}],
        }],
    )
    return response["Instances"][0]["InstanceId"]


def _describe_instance(instance_id):
    response = _EC2.describe_instances(InstanceIds=[instance_id])
    return response["Reservations"][0]["Instances"][0]


def _instance_state(instance_id):
    return _describe_instance(instance_id)["State"]["Name"]


def _seed_server(server_id, name, instance_id, state, running_job=None):
    item = {
        "server_id": server_id,
        "name": name,
        "instance_id": instance_id,
        "instance_type": "m6g.4xlarge",
        "cpu_architecture": build_domain.ARCH_ARM64,
        "lifecycle_state": state,
        "last_state_change_at": 1_700_000_000_000,
        "created_at": 1_700_000_000_000,
    }
    if running_job:
        item["running_build_job_id"] = running_job
    _SERVERS.put_item(Item=item)
    return item


def _get_record(server_id):
    return _SERVERS.get_item(Key={"server_id": server_id}).get("Item")


def _clear_servers():
    for item in _SERVERS.scan().get("Items", []):
        _SERVERS.delete_item(Key={"server_id": item["server_id"]})


class _RolePatches:
    """Run build_fleet.handler as a given portal role, capturing both the
    handler's Audit_Log calls and the RBAC middleware's denied-access
    Audit_Log calls."""

    def __init__(self, role, user):
        self._patches = [
            mock.patch.object(rbac_middleware, "get_user_from_event",
                              return_value=dict(user)),
            mock.patch.object(RBACManager, "get_user_role",
                              return_value=role),
            mock.patch.object(build_fleet, "get_user_from_event",
                              return_value=dict(user)),
            mock.patch.object(rbac_middleware, "log_audit_event"),
            mock.patch.object(build_fleet, "log_audit_event"),
        ]

    def __enter__(self):
        started = [p.start() for p in self._patches]
        self.denied_audit = started[3]
        self.handler_audit = started[4]
        return self

    def __exit__(self, *exc):
        for p in self._patches:
            p.stop()
        return False


def _as_admin():
    """PortalAdmin: allowed every fleet management action (Req 6.7)."""
    return _RolePatches(Role.PORTAL_ADMIN, _ADMIN)


def _as_operator():
    """DataScientist: a Build_Operator (builds:read) but NOT PortalAdmin,
    so fleet management actions must be denied (Req 6.7)."""
    return _RolePatches(Role.DATA_SCIENTIST, _OPERATOR)


def _assert_success_audit(patches, action, server_id, user_id):
    """The accepted action records exactly one success Audit_Log entry
    with the action, the acting user, the target server, and the outcome
    (Req 6.8)."""
    patches.handler_audit.assert_called_once()
    kwargs = patches.handler_audit.call_args.kwargs
    assert kwargs["user_id"] == user_id
    assert kwargs["action"] == f"fleet_server_{action}"
    assert kwargs["resource_type"] == "build_server"
    assert kwargs["resource_id"] == server_id
    assert kwargs["result"] == "success"
    return kwargs


# ---------------------------------------------------------------------------
# Requirements 6.6, 6.12 — terminate confirmation flow
# ---------------------------------------------------------------------------

class TestTerminateConfirmation:
    """DELETE /build-servers/{id} requires the request body to echo the
    exact server name; a missing, empty, or wrong confirmation performs
    no termination and leaves the server unchanged (Req 6.6, 6.12)."""

    def setup_method(self):
        _clear_servers()

    def test_missing_confirmation_terminates_nothing(self):
        instance_id = _run_real_instance("arm-server-a")
        before = _seed_server("srv-a", "arm-server-a", instance_id,
                              build_domain.SERVER_STATE_RUNNING)

        with _as_admin() as patches:
            response = build_fleet.handler(
                _event("/build-servers/{id}", "DELETE", server_id="srv-a"),
                None)

        assert response["statusCode"] == 400, response["body"]
        body = json.loads(response["body"])
        assert body["error"]["code"] == "CONFIRMATION_REQUIRED"
        assert body["error"]["details"]["expected_confirmation"] == \
            "arm-server-a"

        # No termination was performed and the server is unchanged
        # (Req 6.12): the EC2 instance is still running and the
        # BuildServers record is byte-identical.
        assert _instance_state(instance_id) == "running"
        assert _get_record("srv-a") == before
        patches.handler_audit.assert_not_called()

    def test_wrong_confirmation_terminates_nothing(self):
        instance_id = _run_real_instance("arm-server-b")
        before = _seed_server("srv-b", "arm-server-b", instance_id,
                              build_domain.SERVER_STATE_RUNNING)

        with _as_admin() as patches:
            response = build_fleet.handler(
                _event("/build-servers/{id}", "DELETE",
                       body={"confirm": "arm-server-B"},  # wrong case
                       server_id="srv-b"),
                None)

        assert response["statusCode"] == 400, response["body"]
        assert json.loads(response["body"])["error"]["code"] == \
            "CONFIRMATION_REQUIRED"
        assert _instance_state(instance_id) == "running"
        assert _get_record("srv-b") == before
        patches.handler_audit.assert_not_called()

    def test_exact_confirmation_terminates_the_instance(self):
        instance_id = _run_real_instance("arm-server-c")
        _seed_server("srv-c", "arm-server-c", instance_id,
                     build_domain.SERVER_STATE_RUNNING)

        with _as_admin() as patches:
            response = build_fleet.handler(
                _event("/build-servers/{id}", "DELETE",
                       body={"confirm": "arm-server-c"},
                       server_id="srv-c"),
                None)

        assert response["statusCode"] == 200, response["body"]
        # The EC2 instance is actually terminating/terminated (Req 6.6).
        assert _instance_state(instance_id) in ("shutting-down",
                                                "terminated")

        # The accepted action recorded its pending_action marker with the
        # 10-minute deadline and the expected terminated state.
        record = _get_record("srv-c")
        pending = record["pending_action"]
        assert pending["action"] == build_domain.FLEET_ACTION_TERMINATE
        assert pending["expected_state"] == \
            build_domain.SERVER_STATE_TERMINATED
        assert int(pending["deadline"]) == \
            int(pending["initiated_at"]) + _TEN_MINUTES_MS

        kwargs = _assert_success_audit(patches, "terminate", "srv-c",
                                       _ADMIN["user_id"])
        assert kwargs["details"]["instance_id"] == instance_id


# ---------------------------------------------------------------------------
# Requirement 6.7 — non-PortalAdmin denial with audit
# ---------------------------------------------------------------------------

class TestNonAdminDenied:
    """Every fleet management action (launch, start, stop, terminate)
    from a non-PortalAdmin is rejected without performing the action,
    returns an authorization error, and records a denied-access
    Audit_Log entry (Req 6.7)."""

    def setup_method(self):
        _clear_servers()

    def _assert_denied(self, response, patches):
        assert response["statusCode"] == 403
        body = json.loads(response["body"])
        assert body["error"] == "Super user access required"
        assert body["required_role"] == "PortalAdmin"

        patches.denied_audit.assert_called_once()
        kwargs = patches.denied_audit.call_args.kwargs
        assert kwargs["user_id"] == _OPERATOR["user_id"]
        assert kwargs["action"] == "unauthorized_super_user_access"
        assert kwargs["result"] == "denied"
        # The denial never produces a handler-side (outcome) audit entry.
        patches.handler_audit.assert_not_called()

    def test_launch_denied_creates_nothing(self):
        reservations_before = len(
            _EC2.describe_instances()["Reservations"])

        with _as_operator() as patches:
            response = build_fleet.handler(
                _event("/build-servers", "POST",
                       body={"name": "srv", "architecture": "arm64"}),
                None)

        self._assert_denied(response, patches)
        assert _SERVERS.scan().get("Items", []) == [], \
            "a denied launch must not register any Build_Server"
        assert len(_EC2.describe_instances()["Reservations"]) == \
            reservations_before, \
            "a denied launch must not run any instance"

    def test_start_stop_terminate_denied_leave_server_unchanged(self):
        instance_id = _run_real_instance("arm-server-d")
        before = _seed_server("srv-d", "arm-server-d", instance_id,
                              build_domain.SERVER_STATE_RUNNING)

        denials = [
            _event("/build-servers/{id}/start", "POST", server_id="srv-d"),
            _event("/build-servers/{id}/stop", "POST", server_id="srv-d"),
            _event("/build-servers/{id}", "DELETE",
                   body={"confirm": "arm-server-d"}, server_id="srv-d"),
        ]
        for event in denials:
            with _as_operator() as patches:
                response = build_fleet.handler(event, None)
            self._assert_denied(response, patches)

        # The server is untouched: same record, instance still running.
        assert _get_record("srv-d") == before
        assert _instance_state(instance_id) == "running"


# ---------------------------------------------------------------------------
# Requirement 6.8 — action-outcome audit entries
# ---------------------------------------------------------------------------

class TestActionOutcomeAudit:
    """Fleet management actions record their outcome in the Audit_Log:
    success entries carry the action, acting user, target server, and
    outcome; a rejected action records a failure entry naming the
    lifecycle state that blocked it (Req 6.8)."""

    def setup_method(self):
        _clear_servers()

    def test_rejected_start_records_failure_audit(self):
        # start is only valid from stopped (Req 6.10): starting a running
        # server is rejected and the failed action is audited.
        instance_id = _run_real_instance("arm-server-e")
        _seed_server("srv-e", "arm-server-e", instance_id,
                     build_domain.SERVER_STATE_RUNNING)

        with _as_admin() as patches:
            response = build_fleet.handler(
                _event("/build-servers/{id}/start", "POST",
                       server_id="srv-e"),
                None)

        assert response["statusCode"] == 409, response["body"]
        body = json.loads(response["body"])
        assert body["error"]["code"] == "FLEET_ACTION_REJECTED"
        assert body["error"]["details"]["lifecycle_state"] == "running"

        patches.handler_audit.assert_called_once()
        kwargs = patches.handler_audit.call_args.kwargs
        assert kwargs["user_id"] == _ADMIN["user_id"]
        assert kwargs["action"] == "fleet_server_start"
        assert kwargs["resource_type"] == "build_server"
        assert kwargs["resource_id"] == "srv-e"
        assert kwargs["result"] == "failure"
        assert kwargs["details"]["lifecycle_state"] == "running"
        assert kwargs["details"]["errors"], \
            "the failure audit entry must carry the rejection errors"
        # The rejected action changed nothing (Req 6.10).
        assert _instance_state(instance_id) == "running"

    def test_stop_with_running_job_rejected_and_audited(self):
        # stop with a running Build_Job is rejected (Req 6.4) and the
        # failed action outcome names the job (Req 6.8).
        instance_id = _run_real_instance("arm-server-f")
        _seed_server("srv-f", "arm-server-f", instance_id,
                     build_domain.SERVER_STATE_RUNNING,
                     running_job="job-busy")

        with _as_admin() as patches:
            response = build_fleet.handler(
                _event("/build-servers/{id}/stop", "POST",
                       server_id="srv-f"),
                None)

        assert response["statusCode"] == 409, response["body"]
        patches.handler_audit.assert_called_once()
        kwargs = patches.handler_audit.call_args.kwargs
        assert kwargs["action"] == "fleet_server_stop"
        assert kwargs["result"] == "failure"
        assert kwargs["details"]["running_build_job_id"] == "job-busy"
        assert _instance_state(instance_id) == "running"


# ---------------------------------------------------------------------------
# Requirements 6.2, 6.3, 6.5 — moto lifecycle integration
# ---------------------------------------------------------------------------

class TestLifecycleIntegration:
    """Start, stop, and launch drive the real (moto) EC2 control plane
    and the BuildServers registry, and the GET /build-servers
    reconciliation observes each transition (Req 6.2, 6.3, 6.5)."""

    def setup_method(self):
        _clear_servers()

    def _list_servers(self):
        with _as_admin():
            response = build_fleet.handler(
                _event("/build-servers", "GET"), None)
        assert response["statusCode"] == 200, response["body"]
        return {s["server_id"]: s
                for s in json.loads(response["body"])["servers"]}

    def test_start_stopped_server_reaches_running(self):
        instance_id = _run_real_instance("arm-server-g")
        _EC2.stop_instances(InstanceIds=[instance_id])
        assert _instance_state(instance_id) == "stopped"
        _seed_server("srv-g", "arm-server-g", instance_id,
                     build_domain.SERVER_STATE_STOPPED)

        with _as_admin() as patches:
            response = build_fleet.handler(
                _event("/build-servers/{id}/start", "POST",
                       server_id="srv-g"),
                None)

        assert response["statusCode"] == 200, response["body"]
        # StartInstances actually ran: the instance left stopped.
        assert _instance_state(instance_id) in ("pending", "running")

        record = _get_record("srv-g")
        pending = record["pending_action"]
        assert pending["action"] == build_domain.FLEET_ACTION_START
        assert pending["expected_state"] == \
            build_domain.SERVER_STATE_RUNNING
        assert int(pending["deadline"]) == \
            int(pending["initiated_at"]) + _TEN_MINUTES_MS

        _assert_success_audit(patches, "start", "srv-g", _ADMIN["user_id"])

        # The fleet list reconciliation observes the transition to
        # running and clears the reached pending_action (Req 6.2).
        server = self._list_servers()["srv-g"]
        assert server["lifecycle_state"] == \
            build_domain.SERVER_STATE_RUNNING
        assert "pending_action" not in _get_record("srv-g")

    def test_stop_running_server_reaches_stopped(self):
        instance_id = _run_real_instance("arm-server-h")
        assert _instance_state(instance_id) == "running"
        _seed_server("srv-h", "arm-server-h", instance_id,
                     build_domain.SERVER_STATE_RUNNING)

        with _as_admin() as patches:
            response = build_fleet.handler(
                _event("/build-servers/{id}/stop", "POST",
                       server_id="srv-h"),
                None)

        assert response["statusCode"] == 200, response["body"]
        # StopInstances actually ran: the instance left running.
        assert _instance_state(instance_id) in ("stopping", "stopped")

        record = _get_record("srv-h")
        pending = record["pending_action"]
        assert pending["action"] == build_domain.FLEET_ACTION_STOP
        assert pending["expected_state"] == \
            build_domain.SERVER_STATE_STOPPED
        assert int(pending["deadline"]) == \
            int(pending["initiated_at"]) + _TEN_MINUTES_MS

        _assert_success_audit(patches, "stop", "srv-h", _ADMIN["user_id"])

        # The fleet list reconciliation observes the transition to
        # stopped and clears the reached pending_action (Req 6.3).
        server = self._list_servers()["srv-h"]
        assert server["lifecycle_state"] == \
            build_domain.SERVER_STATE_STOPPED
        assert "pending_action" not in _get_record("srv-h")

    def test_launch_provisions_and_registers_the_server(self):
        # The Ubuntu SSM public parameter / Canonical DescribeImages
        # catalog is not part of the moto backend, so AMI resolution is
        # pinned to a moto-known AMI; RunInstances and the registration
        # run for real against moto (Req 6.5).
        ami_id = _moto_ami()
        with _as_admin() as patches, \
                mock.patch.object(build_fleet, "resolve_ubuntu_ami",
                                  return_value=ami_id) as resolve:
            response = build_fleet.handler(
                _event("/build-servers", "POST",
                       body={"name": "arm-server-new",
                             "architecture": build_domain.ARCH_ARM64}),
                None)

        assert response["statusCode"] == 201, response["body"]
        # No ubuntu_version in the request: 22.04, the pre-JP7 behavior.
        resolve.assert_called_once_with(build_domain.ARCH_ARM64, "22.04")
        server = json.loads(response["body"])["server"]
        server_id = server["server_id"]

        # Registered in the fleet list under the provided name with its
        # CPU architecture and the configured (default) instance type.
        record = _get_record(server_id)
        assert record["name"] == "arm-server-new"
        assert record["cpu_architecture"] == build_domain.ARCH_ARM64
        assert record["instance_type"] == "m6g.4xlarge"
        assert record["ubuntu_version"] == "22.04"
        assert record["created_by"] == _ADMIN["user_id"]
        pending = record["pending_action"]
        assert pending["action"] == "launch"
        assert pending["expected_state"] == \
            build_domain.SERVER_STATE_RUNNING
        assert int(pending["deadline"]) == \
            int(pending["initiated_at"]) + _TEN_MINUTES_MS

        # The EC2 instance really exists with the configured type, the
        # fleet scoping tags, and no key pair (hardened profile).
        instance = _describe_instance(record["instance_id"])
        assert instance["InstanceType"] == "m6g.4xlarge"
        assert instance["State"]["Name"] in ("pending", "running")
        tags = {t["Key"]: t["Value"] for t in instance.get("Tags", [])}
        assert tags["Name"] == "arm-server-new"
        assert tags["dda-build:fleet"] == "true"
        assert tags["dda-build:server-id"] == server_id
        assert not instance.get("KeyName"), \
            "fleet instances must launch without a key pair"

        kwargs = _assert_success_audit(patches, "launch", server_id,
                                       _ADMIN["user_id"])
        assert kwargs["details"]["name"] == "arm-server-new"
        assert kwargs["details"]["architecture"] == build_domain.ARCH_ARM64
        assert kwargs["details"]["ubuntu_version"] == "22.04"
        assert kwargs["details"]["instance_id"] == record["instance_id"]
        assert kwargs["details"]["instance_type"] == "m6g.4xlarge"
        assert kwargs["details"]["ami_id"] == ami_id

        # The fleet list shows the launched server (Req 6.5).
        assert server_id in self._list_servers()

    def test_launch_2404_arm64_registers_the_jp7_host(self):
        # Ubuntu 24.04 (noble) is the JP7 build host (jetpack7-support
        # design §10): an arm64 launch with ubuntu_version 24.04 resolves
        # the noble AMI and records the release on the server.
        ami_id = _moto_ami()
        with _as_admin() as patches, \
                mock.patch.object(build_fleet, "resolve_ubuntu_ami",
                                  return_value=ami_id) as resolve:
            response = build_fleet.handler(
                _event("/build-servers", "POST",
                       body={"name": "jp7-server",
                             "architecture": build_domain.ARCH_ARM64,
                             "ubuntu_version": "24.04"}),
                None)

        assert response["statusCode"] == 201, response["body"]
        resolve.assert_called_once_with(build_domain.ARCH_ARM64, "24.04")
        server = json.loads(response["body"])["server"]
        server_id = server["server_id"]

        record = _get_record(server_id)
        assert record["cpu_architecture"] == build_domain.ARCH_ARM64
        assert record["ubuntu_version"] == "24.04"

        kwargs = _assert_success_audit(patches, "launch", server_id,
                                       _ADMIN["user_id"])
        assert kwargs["details"]["ubuntu_version"] == "24.04"

    def test_launch_2404_x86_64_rejected_without_launching(self):
        # 24.04 exists to host JP7 builds, which require an arm64 host:
        # requesting it for x86_64 is rejected as an invalid request and
        # launches nothing (fail closed).
        reservations_before = len(
            _EC2.describe_instances()["Reservations"])

        with _as_admin() as patches:
            response = build_fleet.handler(
                _event("/build-servers", "POST",
                       body={"name": "bad-server",
                             "architecture": build_domain.ARCH_X86_64,
                             "ubuntu_version": "24.04"}),
                None)

        assert response["statusCode"] == 400, response["body"]
        body = json.loads(response["body"])
        assert body["error"]["code"] == "LAUNCH_REQUEST_INVALID"
        rules = [e["rule"] for e in body["error"]["details"]["errors"]]
        assert "ubuntu_version_arch_unsupported" in rules

        assert _SERVERS.scan().get("Items", []) == []
        assert len(_EC2.describe_instances()["Reservations"]) == \
            reservations_before
        patches.handler_audit.assert_not_called()

    def test_launch_unknown_ubuntu_version_rejected(self):
        # Only the supported releases are accepted; anything else is an
        # invalid request that launches nothing.
        with _as_admin():
            response = build_fleet.handler(
                _event("/build-servers", "POST",
                       body={"name": "bad-server",
                             "architecture": build_domain.ARCH_ARM64,
                             "ubuntu_version": "18.04"}),
                None)

        assert response["statusCode"] == 400, response["body"]
        body = json.loads(response["body"])
        assert body["error"]["code"] == "LAUNCH_REQUEST_INVALID"
        rules = [e["rule"] for e in body["error"]["details"]["errors"]]
        assert "ubuntu_version_invalid" in rules
        assert _SERVERS.scan().get("Items", []) == []


# ---------------------------------------------------------------------------
# Ubuntu AMI resolution oracles (jetpack7-support design §10)
# ---------------------------------------------------------------------------

class TestUbuntuAmiResolution:
    """resolve_ubuntu_ami consults the exact Canonical SSM public
    parameter for the requested release/architecture, falls back to the
    release's DescribeImages name filter, and fails closed on an
    unmapped pairing. The 22.04 paths are the pre-JP7 oracles
    byte-preserved; 24.04 (noble) uses the ebs-gp3 / hvm-ssd-gp3
    canonical path shape."""

    #: (arch, ubuntu_version) -> expected canonical SSM parameter path.
    SSM_ORACLES = {
        (build_domain.ARCH_ARM64, "22.04"):
            "/aws/service/canonical/ubuntu/server/22.04/stable/current/"
            "arm64/hvm/ebs-gp2/ami-id",
        (build_domain.ARCH_X86_64, "22.04"):
            "/aws/service/canonical/ubuntu/server/22.04/stable/current/"
            "amd64/hvm/ebs-gp2/ami-id",
        (build_domain.ARCH_ARM64, "24.04"):
            "/aws/service/canonical/ubuntu/server/24.04/stable/current/"
            "arm64/hvm/ebs-gp3/ami-id",
    }

    def test_ssm_parameter_paths(self):
        for (arch, version), expected in self.SSM_ORACLES.items():
            with mock.patch.object(build_fleet.ssm, "get_parameter",
                                   return_value={
                                       "Parameter": {"Value": "ami-oracle"},
                                   }) as get_parameter:
                assert build_fleet.resolve_ubuntu_ami(arch, version) == \
                    "ami-oracle"
            get_parameter.assert_called_once_with(Name=expected)

    def test_default_version_is_2204(self):
        # No version argument: the 22.04 parameter, i.e. the pre-JP7
        # behavior byte-preserved.
        with mock.patch.object(build_fleet.ssm, "get_parameter",
                               return_value={
                                   "Parameter": {"Value": "ami-default"},
                               }) as get_parameter:
            assert build_fleet.resolve_ubuntu_ami(
                build_domain.ARCH_ARM64) == "ami-default"
        get_parameter.assert_called_once_with(
            Name=self.SSM_ORACLES[(build_domain.ARCH_ARM64, "22.04")])

    def test_noble_describe_images_fallback(self):
        # SSM lookup failure: the noble arm64 fallback filters on the
        # hvm-ssd-gp3 noble name pattern and returns the newest image.
        ssm_error = build_fleet.ClientError(
            {"Error": {"Code": "ParameterNotFound", "Message": "missing"}},
            "GetParameter")
        images = {"Images": [
            {"ImageId": "ami-older", "CreationDate": "2025-01-01T00:00:00Z"},
            {"ImageId": "ami-newest", "CreationDate": "2025-06-01T00:00:00Z"},
        ]}
        with mock.patch.object(build_fleet.ssm, "get_parameter",
                               side_effect=ssm_error), \
                mock.patch.object(build_fleet.ec2, "describe_images",
                                  return_value=images) as describe:
            assert build_fleet.resolve_ubuntu_ami(
                build_domain.ARCH_ARM64, "24.04") == "ami-newest"
        filters = {f["Name"]: f["Values"]
                   for f in describe.call_args.kwargs["Filters"]}
        assert filters["name"] == \
            ["ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-arm64-server-*"]
        assert filters["architecture"] == ["arm64"]

    def test_unmapped_pairing_fails_closed_before_any_aws_call(self):
        # 24.04 x86_64 (and unknown releases) have no AMI mapping: the
        # resolver raises without consulting SSM or EC2.
        with mock.patch.object(build_fleet.ssm, "get_parameter") as ssm_call, \
                mock.patch.object(build_fleet.ec2,
                                  "describe_images") as ec2_call:
            for arch, version in ((build_domain.ARCH_X86_64, "24.04"),
                                  (build_domain.ARCH_ARM64, "18.04")):
                try:
                    build_fleet.resolve_ubuntu_ami(arch, version)
                    raise AssertionError(
                        f"resolve_ubuntu_ami must fail closed for "
                        f"({arch}, {version})")
                except RuntimeError as e:
                    assert "AMI mapping" in str(e)
        ssm_call.assert_not_called()
        ec2_call.assert_not_called()
