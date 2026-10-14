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
Unit tests for the build_jobs.py handler's RBAC enforcement and
Audit_Log entries (task 7.4 of portal-build-fleet-and-workflow-gates).

Validates: Requirements 1.6, 1.7, 4.5, 4.6, 4.9, 4.10

Covers:
- Unauthorized POST /builds: rejected with the standard authorization
  error, no Build_Job created, denied-access Audit_Log entry (Req 1.6).
- Unauthorized POST /builds/{id}/cancel: rejected with the standard
  authorization error, Build_Job unchanged, denied-access Audit_Log
  entry (Req 4.10).
- ``build_requested`` Audit_Log content on create: one entry per created
  Build_Job with requesting user, Build_Target, execution mode, and
  submission time (Req 1.7).
- Cancellation Audit_Log entries: queued cancellation (removed from the
  queue, Req 4.5), running cancellation with a confirmed stop (Req 4.6),
  and the failed-cancellation path where the stop is NOT confirmed
  within the window — job keeps its status, the error and the audit
  entry name the affected Build_Server (Req 4.9).

The BuildJobs / BuildServers / PortalSettings tables are moto-mocked;
the RBAC role lookup, ``get_user_from_event``, ``log_audit_event``, and
the SSM stop/verification helpers are stubbed per test (the sibling
pattern in test_build_rbac_registration.py / test_persistence_iff_accept.py).
"""
import json
import os
import sys
import types
from unittest import mock

# ---------------------------------------------------------------------------
# Environment BEFORE any import: shared_utils and build_jobs bind their
# boto3 resources/clients and table names at import time.
# ---------------------------------------------------------------------------
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ["AWS_ACCESS_KEY_ID"] = "testing"
os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
os.environ["AWS_SECURITY_TOKEN"] = "testing"
os.environ["AWS_SESSION_TOKEN"] = "testing"

_JOBS_TABLE = "build-jobs-t74"
_SERVERS_TABLE = "build-servers-t74"
_SETTINGS_TABLE = "portal-settings-t74"
os.environ["BUILD_JOBS_TABLE"] = _JOBS_TABLE
os.environ["BUILD_SERVERS_TABLE"] = _SERVERS_TABLE
os.environ["SETTINGS_TABLE"] = _SETTINGS_TABLE
# BUILD_DISPATCHER_FUNCTION_NAME stays unset: invoke_dispatcher is then a
# logged no-op (the dispatcher schedule is the documented fallback).
os.environ.pop("BUILD_DISPATCHER_FUNCTION_NAME", None)

# Import boto3 (and thus botocore/urllib3) from the test environment BEFORE
# the Lambda layer directory joins sys.path: the layer vendors its own
# urllib3 build targeting the Lambda Python runtime, which must not shadow
# the environment's copy.
import boto3  # noqa: E402

# The flask-app verification container's python3.9 is built without the
# _bz2 C extension, and moto's request path imports moto.s3 -> bz2 on
# every call (moto.core.authorization -> moto.iam.access_control ->
# moto.s3.models). bz2 is only used for S3-Select payload decompression,
# which this DynamoDB-only suite never exercises, so a minimal
# stdlib-shaped stub keeps the import chain intact where _bz2 is absent
# (same shim as the sibling test_build_history_ordering.py).
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

# Fresh modules so build_jobs' module-level boto3 handles are created under
# the moto mock started below (sibling pattern).
for _module in ("build_jobs", "build_domain", "rbac_middleware",
                "shared_utils"):
    sys.modules.pop(_module, None)

# Module-scope moto: active for every import below and for the whole run.
_MOCK = mock_aws()
_MOCK.start()

_DDB = boto3.resource("dynamodb", region_name="us-east-1")
for _name, _key in ((_JOBS_TABLE, "build_job_id"),
                    (_SERVERS_TABLE, "server_id"),
                    (_SETTINGS_TABLE, "setting_key")):
    _DDB.create_table(
        TableName=_name,
        KeySchema=[{"AttributeName": _key, "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": _key, "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
_JOBS = _DDB.Table(_JOBS_TABLE)
_SERVERS = _DDB.Table(_SERVERS_TABLE)

import build_domain  # noqa: E402
import build_jobs  # noqa: E402
import rbac_middleware  # noqa: E402
from shared_utils import RBACManager, Role  # noqa: E402


_OPERATOR = {"user_id": "build-operator", "email": "op@example.com",
             "username": "build-operator"}
_INTRUDER = {"user_id": "intruder", "email": "intruder@example.com",
             "username": "intruder"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _event(resource, method, body=None, job_id=None):
    """Minimal API Gateway event for build_jobs.handler routing."""
    event = {
        "resource": resource,
        "httpMethod": method,
        "path": resource.replace("{id}", job_id or ""),
    }
    if body is not None:
        event["body"] = json.dumps(body)
    if job_id is not None:
        event["pathParameters"] = {"id": job_id}
    return event


def _scan_jobs():
    items, kwargs = [], {}
    while True:
        page = _JOBS.scan(**kwargs)
        items.extend(page.get("Items", []))
        if "LastEvaluatedKey" not in page:
            return {item["build_job_id"]: item for item in items}
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def _clear_tables():
    for job_id in _scan_jobs():
        _JOBS.delete_item(Key={"build_job_id": job_id})
    for item in _SERVERS.scan().get("Items", []):
        _SERVERS.delete_item(Key={"server_id": item["server_id"]})


def _seed_job(job_id, status, execution_mode="ephemeral", server_id=None):
    item = {
        "build_job_id": job_id,
        "build_target": build_domain.TARGET_JP5,
        "execution_mode": execution_mode,
        "server_id": server_id,
        "status": status,
        "requested_by": _OPERATOR["user_id"],
        "created_at": 1_700_000_000_000,
    }
    _JOBS.put_item(Item={k: v for k, v in item.items() if v is not None})
    return item


def _seed_server(server_id, name, instance_id):
    _SERVERS.put_item(Item={
        "server_id": server_id,
        "name": name,
        "instance_id": instance_id,
        "lifecycle_state": "running",
        "cpu_architecture": build_domain.ARCH_ARM64,
    })


class _RolePatches:
    """Run build_jobs.handler as a given portal role, capturing both the
    handler's Audit_Log calls and the RBAC middleware's denied-access
    Audit_Log calls."""

    def __init__(self, role, user):
        self._patches = [
            mock.patch.object(rbac_middleware, "get_user_from_event",
                              return_value=dict(user)),
            mock.patch.object(RBACManager, "get_user_role",
                              return_value=role),
            mock.patch.object(build_jobs, "get_user_from_event",
                              return_value=dict(user)),
            mock.patch.object(rbac_middleware, "log_audit_event"),
            mock.patch.object(build_jobs, "log_audit_event"),
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


def _as_operator():
    """DataScientist holds the Build_Operator permissions (design grant
    matrix)."""
    return _RolePatches(Role.DATA_SCIENTIST, _OPERATOR)


def _as_viewer():
    """Viewer holds no builds:* permission."""
    return _RolePatches(Role.VIEWER, _INTRUDER)


def _assert_denied(response, patches, permission):
    """Standard authorization error + denied-access Audit_Log entry."""
    assert response["statusCode"] == 403
    body = json.loads(response["body"])
    assert body["error"] == "Insufficient permissions"
    assert body["required_permissions"] == [permission]

    patches.denied_audit.assert_called_once()
    kwargs = patches.denied_audit.call_args.kwargs
    assert kwargs["user_id"] == _INTRUDER["user_id"]
    assert kwargs["action"] == "unauthorized_access"
    assert kwargs["result"] == "denied"
    assert kwargs["details"]["required_permissions"] == [permission]
    assert kwargs["details"]["usecase_id"] == "global"

    # The denial never produces a handler-side audit entry.
    patches.handler_audit.assert_not_called()


# ---------------------------------------------------------------------------
# Requirement 1.6 — unauthorized build submission
# ---------------------------------------------------------------------------

class TestUnauthorizedSubmit:
    """POST /builds without builds:submit: rejected, authorization error,
    no Build_Job created, denied-access Audit_Log entry (Req 1.6)."""

    def setup_method(self):
        _clear_tables()

    def test_submit_denied_creates_no_job_and_audits_denial(self):
        request = {"targets": [build_domain.TARGET_JP5],
                   "execution_mode": "ephemeral"}
        with _as_viewer() as patches:
            response = build_jobs.handler(
                _event("/builds", "POST", body=request), None)

        _assert_denied(response, patches, "builds:submit")
        assert _scan_jobs() == {}, \
            "an unauthorized submit must not create any Build_Job"


# ---------------------------------------------------------------------------
# Requirement 4.10 — unauthorized cancellation
# ---------------------------------------------------------------------------

class TestUnauthorizedCancel:
    """POST /builds/{id}/cancel without builds:cancel: rejected,
    authorization error, Build_Job unchanged, denied-access Audit_Log
    entry (Req 4.10)."""

    def setup_method(self):
        _clear_tables()

    def test_cancel_denied_leaves_job_unchanged_and_audits_denial(self):
        _seed_job("job-q", build_domain.STATUS_QUEUED)
        before = _scan_jobs()

        with _as_viewer() as patches:
            response = build_jobs.handler(
                _event("/builds/{id}/cancel", "POST", job_id="job-q"), None)

        _assert_denied(response, patches, "builds:cancel")
        assert _scan_jobs() == before, \
            "an unauthorized cancellation must not change the Build_Job"


# ---------------------------------------------------------------------------
# Requirement 1.7 — build_requested Audit_Log content on create
# ---------------------------------------------------------------------------

class TestBuildRequestedAudit:
    """Job creation records one build_requested Audit_Log entry per
    created Build_Job with the requesting user, Build_Target, execution
    mode, and submission time (Req 1.7)."""

    def setup_method(self):
        _clear_tables()

    def test_build_requested_audit_content_per_created_job(self):
        request = {
            "targets": [build_domain.TARGET_JP5, build_domain.TARGET_AMD64],
            "execution_mode": "ephemeral",
        }
        with _as_operator() as patches:
            response = build_jobs.handler(
                _event("/builds", "POST", body=request), None)

        assert response["statusCode"] == 201, response["body"]
        body = json.loads(response["body"])
        jobs = {job["build_job_id"]: job for job in body["jobs"]}
        assert len(jobs) == 2

        # One build_requested entry per created Build_Job.
        assert patches.handler_audit.call_count == 2
        audited = {}
        for call in patches.handler_audit.call_args_list:
            kwargs = call.kwargs
            assert kwargs["action"] == "build_requested"
            assert kwargs["resource_type"] == "build_job"
            assert kwargs["result"] == "success"
            audited[kwargs["resource_id"]] = kwargs

        assert set(audited) == set(jobs), \
            "every created Build_Job must have its own build_requested entry"
        for job_id, job in jobs.items():
            kwargs = audited[job_id]
            # Requesting user, Build_Target, execution mode, submission
            # time (Req 1.7).
            assert kwargs["user_id"] == _OPERATOR["user_id"]
            assert kwargs["details"]["build_target"] == job["build_target"]
            assert kwargs["details"]["execution_mode"] == "ephemeral"
            assert kwargs["details"]["submitted_at"] == job["created_at"]

        # Denied-access audit is not involved on the authorized path.
        patches.denied_audit.assert_not_called()


# ---------------------------------------------------------------------------
# Requirements 4.5, 4.6, 4.9 — cancellation Audit_Log entries
# ---------------------------------------------------------------------------

class TestCancellationAudit:
    """Cancellation audit entries: queued (Req 4.5), running with a
    confirmed stop (Req 4.6), and the failed-cancellation path (Req 4.9)."""

    def setup_method(self):
        _clear_tables()

    def test_queued_cancellation_audited_and_removed_from_queue(self):
        _seed_job("job-q", build_domain.STATUS_QUEUED)

        with _as_operator() as patches:
            response = build_jobs.handler(
                _event("/builds/{id}/cancel", "POST", job_id="job-q"), None)

        assert response["statusCode"] == 200, response["body"]
        assert _scan_jobs()["job-q"]["status"] == \
            build_domain.STATUS_CANCELLED

        patches.handler_audit.assert_called_once()
        kwargs = patches.handler_audit.call_args.kwargs
        assert kwargs["user_id"] == _OPERATOR["user_id"]
        assert kwargs["action"] == "build_cancelled"
        assert kwargs["resource_type"] == "build_job"
        assert kwargs["resource_id"] == "job-q"
        assert kwargs["result"] == "success"
        assert kwargs["details"]["status_at_request"] == \
            build_domain.STATUS_QUEUED
        assert kwargs["details"]["removed_from_queue"] is True

    def test_running_cancellation_confirmed_stop_audited(self):
        _seed_server("srv-1", "arm-server-1", "i-0123456789abcdef0")
        _seed_job("job-r", build_domain.STATUS_BUILDING,
                  execution_mode="dedicated", server_id="srv-1")

        with _as_operator() as patches, \
                mock.patch.object(build_jobs, "send_shell_command",
                                  return_value="cmd-1") as send, \
                mock.patch.object(build_jobs, "confirm_build_stopped",
                                  return_value=True):
            response = build_jobs.handler(
                _event("/builds/{id}/cancel", "POST", job_id="job-r"), None)

        assert response["statusCode"] == 200, response["body"]
        assert _scan_jobs()["job-r"]["status"] == \
            build_domain.STATUS_CANCELLED
        # The stop command was actually issued to the job's Build_Server.
        send.assert_called_once_with("i-0123456789abcdef0",
                                     build_jobs.STOP_BUILD_COMMANDS)

        patches.handler_audit.assert_called_once()
        kwargs = patches.handler_audit.call_args.kwargs
        assert kwargs["user_id"] == _OPERATOR["user_id"]
        assert kwargs["action"] == "build_cancelled"
        assert kwargs["resource_id"] == "job-r"
        assert kwargs["result"] == "success"
        assert kwargs["details"]["status_at_request"] == \
            build_domain.STATUS_BUILDING
        assert kwargs["details"]["server"] == "arm-server-1"
        assert kwargs["details"]["stop_confirmed"] is True

    def test_failed_cancellation_keeps_status_names_server_and_audits(self):
        _seed_server("srv-1", "arm-server-1", "i-0123456789abcdef0")
        _seed_job("job-r", build_domain.STATUS_BUILDING,
                  execution_mode="dedicated", server_id="srv-1")

        with _as_operator() as patches, \
                mock.patch.object(build_jobs, "send_shell_command",
                                  return_value="cmd-1"), \
                mock.patch.object(build_jobs, "confirm_build_stopped",
                                  return_value=False):
            response = build_jobs.handler(
                _event("/builds/{id}/cancel", "POST", job_id="job-r"), None)

        # The job keeps its current status rather than being cancelled,
        # and the error names the affected Build_Server (Req 4.9).
        assert response["statusCode"] == 409, response["body"]
        body = json.loads(response["body"])
        assert body["error"]["code"] == "CANCELLATION_FAILED"
        assert "arm-server-1" in body["error"]["message"]
        assert body["error"]["details"]["server"] == "arm-server-1"
        assert _scan_jobs()["job-r"]["status"] == \
            build_domain.STATUS_BUILDING

        # The failed cancellation is recorded in the Audit_Log.
        patches.handler_audit.assert_called_once()
        kwargs = patches.handler_audit.call_args.kwargs
        assert kwargs["user_id"] == _OPERATOR["user_id"]
        assert kwargs["action"] == "build_cancelled"
        assert kwargs["resource_id"] == "job-r"
        assert kwargs["result"] == "failure"
        assert kwargs["details"]["status_at_request"] == \
            build_domain.STATUS_BUILDING
        assert kwargs["details"]["server"] == "arm-server-1"
        assert kwargs["details"]["stop_confirmed"] is False
        assert kwargs["details"]["errors"], \
            "the failed-cancellation audit entry must carry the rejection"
