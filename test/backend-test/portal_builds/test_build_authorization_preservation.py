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
Build authorization PRESERVATION baselines at the real
`build_jobs.handler` / RBAC decorator boundary
(build-fleet-rbac-visibility, task 6 — Property 6: Preservation).

**Validates: Requirements 2.7, 3.1, 3.2, 3.3, 3.4, 3.5**

Observation-first: every assertion in this file records behavior observed
on the CURRENT (unfixed) code for inputs OUTSIDE the newly reproduced
PortalAdmin submit bug condition (task 5,
`test_jwt_admin_build_submit_authorization.py`). These baselines MUST
PASS before the task 7.2 fix and MUST remain unchanged after it (task
7.4 re-runs this file verbatim).

What is pinned here:

* `Role × build operation` matrix (property-based, hypothesis): exactly
  PortalAdmin, DataScientist and UseCaseAdmin hold
  `builds:submit`/`builds:read`/`builds:cancel`; Viewer and Operator hold
  none of them (Req 3.4).
* Viewer/Operator `POST /builds`: the standard 403 `Insufficient
  permissions` envelope (`error`, `required_permissions`, `usecase_id`),
  zero Build_Jobs, exactly one denied `unauthorized_access` audit record
  carrying `builds:submit` and `usecase_id='global'` — and NEVER the
  generic `Authorization check failed` 500 (Req 2.7, 3.2).
* Denied `require_builds_read()` / `require_builds_cancel()` /
  `require_builds_submit()` routes: same 403 envelope, same denial audit
  structure, and no Build_Job mutation (Req 2.7, 3.2, 3.5).
* Authorized GET/list and detail paths: response shape and
  most-recent-first ordering, plus the `rbac_context` key set (Req 3.3,
  3.5).
* Cancel semantics: queued → cancelled + `build_cancelled` audit;
  terminal → 409 `CANCELLATION_REJECTED` with the job unchanged (Req
  3.5).
* The existing NON-authorization error envelope
  (`{"error": {"code", "message", "details"}}`) for 404/400 build API
  errors (Req 3.2, 3.5).
* Role resolution: DynamoDB-row precedence and JWT-only resolution
  outside the reproduced submit failure, including the documented
  residual global-scope behavior for non-PortalAdmin global rows
  (Req 3.1, 3.7).

Nothing about authorization is mocked: the real `build_jobs.handler`
dispatch, the real `@require_builds_*()` decorators, the real
`shared_utils.RBACManager`, the real role-permission matrix, the real
`log_audit_event` writes and the real DynamoDB (moto) tables are used.
The BuildJobs table is created with the EXACT deployed schema INCLUDING
the `status-index` / `server-index` / `request-index` GSIs, matching the
task 5 fixture — the sibling suites omit the GSIs, which is why they miss
the submit defect.

Safety: `invoke_dispatcher` is a no-op (its function-name env var is
unset and it is additionally patched for submit cases), so no EC2/SSM
build can ever start from this suite.

One deliberate FIX CHECK lives here: permitted-role `POST /builds` in
`ephemeral` mode was the task 5 bug condition and was therefore marked
xfail while unfixed; task 7.4 confirmed it now passes and removed the
obsolete marker (no assertion changed). The dedicated-mode counterpart
is an observed-passing baseline and is asserted normally.
"""
import json
import os
import sys
import types
import uuid
from unittest import mock

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

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

_SUFFIX = "authz-preserve"
_JOBS_TABLE = f"dda-portal-build-jobs-{_SUFFIX}"
_SERVERS_TABLE = f"dda-portal-build-servers-{_SUFFIX}"
_SETTINGS_TABLE = f"dda-portal-settings-{_SUFFIX}"
_USER_ROLES_TABLE = f"dda-portal-user-roles-{_SUFFIX}"
_AUDIT_TABLE = f"dda-portal-audit-log-{_SUFFIX}"

os.environ["BUILD_JOBS_TABLE"] = _JOBS_TABLE
os.environ["BUILD_SERVERS_TABLE"] = _SERVERS_TABLE
os.environ["SETTINGS_TABLE"] = _SETTINGS_TABLE
os.environ["USER_ROLES_TABLE"] = _USER_ROLES_TABLE
os.environ["AUDIT_LOG_TABLE"] = _AUDIT_TABLE
# Unset so invoke_dispatcher is a logged no-op: nothing can be dispatched.
os.environ.pop("BUILD_DISPATCHER_FUNCTION_NAME", None)

# Import boto3 (and thus botocore/urllib3) from the test environment BEFORE
# the Lambda layer directory joins sys.path: the layer vendors its own
# urllib3 build targeting the Lambda Python runtime, which must not shadow
# the environment's copy.
import boto3  # noqa: E402

# Some verification containers ship a python build without the _bz2 C
# extension while moto's request path imports moto.s3 -> bz2 (sibling
# shim in test_build_jobs_rbac_audit.py).
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

#: Artifact assembly under test: the current source tree by default, or an
#: unpacked deployed function artifact + attached layer contents (same
#: switches as the task 5 exploration fixture).
FUNCTIONS_DIR = os.environ.get(
    "DDA_BUILD_FN_DIR", os.path.join(_BACKEND, "functions"))
LAYER_DIR = os.environ.get(
    "DDA_BUILD_LAYER_DIR",
    os.path.join(_BACKEND, "layers", "shared", "python"))

for _p in (LAYER_DIR, FUNCTIONS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# Fresh modules so the handler's module-level boto3 handles are created
# under the moto mock started below, bound to this file's table names.
for _module in ("build_jobs", "build_domain", "rbac_middleware",
                "shared_utils"):
    sys.modules.pop(_module, None)

# Module-scope moto: active for every import below and for the whole run.
_MOCK = mock_aws()
_MOCK.start()

_DDB = boto3.resource("dynamodb", region_name="us-east-1")

# BuildJobs with the deployed schema, GSIs included (verified against
# `aws dynamodb describe-table --table-name dda-portal-build-jobs`).
_DDB.create_table(
    TableName=_JOBS_TABLE,
    KeySchema=[{"AttributeName": "build_job_id", "KeyType": "HASH"}],
    AttributeDefinitions=[
        {"AttributeName": "build_job_id", "AttributeType": "S"},
        {"AttributeName": "status", "AttributeType": "S"},
        {"AttributeName": "created_at", "AttributeType": "N"},
        {"AttributeName": "server_id", "AttributeType": "S"},
        {"AttributeName": "request_id", "AttributeType": "S"},
        {"AttributeName": "request_order", "AttributeType": "N"},
    ],
    GlobalSecondaryIndexes=[
        {
            "IndexName": "status-index",
            "KeySchema": [
                {"AttributeName": "status", "KeyType": "HASH"},
                {"AttributeName": "created_at", "KeyType": "RANGE"},
            ],
            "Projection": {"ProjectionType": "ALL"},
        },
        {
            "IndexName": "server-index",
            "KeySchema": [
                {"AttributeName": "server_id", "KeyType": "HASH"},
                {"AttributeName": "created_at", "KeyType": "RANGE"},
            ],
            "Projection": {"ProjectionType": "ALL"},
        },
        {
            "IndexName": "request-index",
            "KeySchema": [
                {"AttributeName": "request_id", "KeyType": "HASH"},
                {"AttributeName": "request_order", "KeyType": "RANGE"},
            ],
            "Projection": {"ProjectionType": "ALL"},
        },
    ],
    BillingMode="PAY_PER_REQUEST",
)
for _name, _key in ((_SERVERS_TABLE, "server_id"),
                    (_SETTINGS_TABLE, "setting_key")):
    _DDB.create_table(
        TableName=_name,
        KeySchema=[{"AttributeName": _key, "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": _key, "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
# UserRoles / AuditLog with the deployed key schema, so real role
# resolution and real audit writes run unmocked.
_DDB.create_table(
    TableName=_USER_ROLES_TABLE,
    KeySchema=[
        {"AttributeName": "user_id", "KeyType": "HASH"},
        {"AttributeName": "usecase_id", "KeyType": "RANGE"},
    ],
    AttributeDefinitions=[
        {"AttributeName": "user_id", "AttributeType": "S"},
        {"AttributeName": "usecase_id", "AttributeType": "S"},
    ],
    BillingMode="PAY_PER_REQUEST",
)
_DDB.create_table(
    TableName=_AUDIT_TABLE,
    KeySchema=[
        {"AttributeName": "event_id", "KeyType": "HASH"},
        {"AttributeName": "timestamp", "KeyType": "RANGE"},
    ],
    AttributeDefinitions=[
        {"AttributeName": "event_id", "AttributeType": "S"},
        {"AttributeName": "timestamp", "AttributeType": "N"},
    ],
    BillingMode="PAY_PER_REQUEST",
)

_JOBS = _DDB.Table(_JOBS_TABLE)
_SERVERS = _DDB.Table(_SERVERS_TABLE)
_USER_ROLES = _DDB.Table(_USER_ROLES_TABLE)
_AUDIT = _DDB.Table(_AUDIT_TABLE)

import build_domain  # noqa: E402
import build_jobs  # noqa: E402
import rbac_middleware  # noqa: E402
import shared_utils  # noqa: E402
from shared_utils import Permission, Role, rbac_manager  # noqa: E402


# ---------------------------------------------------------------------------
# Domains
# ---------------------------------------------------------------------------

#: The merged role matrix (shared_utils.RBACManager): exactly these roles
#: hold builds:submit / builds:read / builds:cancel — the Build_Operator
#: capability (Req 3.4).
BUILD_ROLES = ("PortalAdmin", "DataScientist", "UseCaseAdmin")
#: Roles that hold NO builds:* permission (Req 3.4).
NON_BUILD_ROLES = ("Viewer", "Operator")
ALL_ROLES = tuple(role.value for role in Role)

BUILDS_PERMISSIONS = (
    Permission.BUILDS_SUBMIT,
    Permission.BUILDS_CANCEL,
    Permission.BUILDS_READ,
)

TARGETS = (build_domain.TARGET_JP5, build_domain.TARGET_JP6,
           build_domain.TARGET_AMD64, build_domain.TARGET_AMD64_NVIDIA)
MODES = (build_domain.EXECUTION_MODE_EPHEMERAL,
         build_domain.EXECUTION_MODE_DEDICATED)

#: Guarded build operations and the permission each one requires, exactly
#: as build_jobs.py registers them today.
#: (resource, method, permission, needs_job_id)
BUILD_OPERATIONS = (
    ("/builds", "POST", "builds:submit", False),
    ("/builds", "GET", "builds:read", False),
    ("/builds/{id}", "GET", "builds:read", True),
    ("/builds/{id}/logs", "GET", "builds:read", True),
    ("/builds/{id}/cancel", "POST", "builds:cancel", True),
    ("/builds/{id}/retry", "POST", "builds:submit", True),
)

#: The exact rbac_context key set rbac_check populates (Req 3.3).
RBAC_CONTEXT_KEYS = {"user_id", "usecase_id", "user_role", "permissions",
                     "is_super_user"}

#: The catch-all body that must never answer a legitimate denial (Req 2.7).
GENERIC_AUTH_ERROR = "Authorization check failed"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _jwt_user(role_value):
    """A JWT-only caller: fresh Cognito `sub` and `custom:role`, with no
    dda-portal-user-roles row of any kind."""
    user_id = str(uuid.uuid4())
    return {
        "user_id": user_id,
        "email": f"{role_value.lower()}-{user_id[:8]}@example.com",
        "username": f"{role_value.lower()}-{user_id[:8]}",
        "role": role_value,
    }


def _event(resource, method, user, body=None, job_id=None, query=None):
    """API Gateway REST event with Cognito claims for build_jobs.handler."""
    event = {
        "resource": resource,
        "httpMethod": method,
        "path": resource.replace("{id}", job_id or ""),
        "pathParameters": {"id": job_id} if job_id is not None else None,
        "queryStringParameters": query,
        "requestContext": {
            "requestId": str(uuid.uuid4()),
            "authorizer": {
                "claims": {
                    "sub": user["user_id"],
                    "email": user["email"],
                    "cognito:username": user["username"],
                    "custom:role": user["role"],
                }
            },
        },
    }
    if body is not None:
        event["body"] = json.dumps(body)
    return event


def _scan_jobs():
    items, kwargs = [], {}
    while True:
        page = _JOBS.scan(**kwargs)
        items.extend(page.get("Items", []))
        if "LastEvaluatedKey" not in page:
            return {item["build_job_id"]: item for item in items}
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def _audit_records():
    return _AUDIT.scan().get("Items", [])


def _clear_tables():
    for job_id in _scan_jobs():
        _JOBS.delete_item(Key={"build_job_id": job_id})
    for item in _SERVERS.scan().get("Items", []):
        _SERVERS.delete_item(Key={"server_id": item["server_id"]})
    for item in _USER_ROLES.scan().get("Items", []):
        _USER_ROLES.delete_item(Key={"user_id": item["user_id"],
                                     "usecase_id": item["usecase_id"]})
    for item in _audit_records():
        _AUDIT.delete_item(Key={"event_id": item["event_id"],
                                "timestamp": item["timestamp"]})


def _seed_running_arm64_server():
    """A valid, running, arm64 Dedicated_Build_Server (JP5 needs arm64)."""
    server_id = f"srv-{uuid.uuid4()}"
    _SERVERS.put_item(Item={
        "server_id": server_id,
        "name": "arm64-preserve-server",
        "instance_id": "i-0123456789abcdef0",
        "lifecycle_state": "running",
        "cpu_architecture": build_domain.ARCH_ARM64,
    })
    return server_id


def _seed_job(job_id, status, created_at, execution_mode="ephemeral",
              server_id=None, requested_by="seed-user"):
    item = {
        "build_job_id": job_id,
        "build_target": build_domain.TARGET_JP5,
        "execution_mode": execution_mode,
        "status": status,
        "requested_by": requested_by,
        "created_at": created_at,
    }
    if server_id:
        item["server_id"] = server_id
    _JOBS.put_item(Item=item)
    return item


def _artifact_fingerprint():
    """Which handler/layer assembly produced this observation."""
    return {
        "build_jobs": getattr(build_jobs, "__file__", "?"),
        "rbac_middleware": getattr(rbac_middleware, "__file__", "?"),
        "shared_utils": getattr(shared_utils, "__file__", "?"),
        "code_version": os.environ.get("CODE_VERSION", "<unset locally>"),
    }


def _observation(**fields):
    fields["artifact"] = _artifact_fingerprint()
    return json.dumps(fields, indent=2, default=str)


def _assert_standard_denial(response, permission, user, event,
                           expected_resource):
    """The standard 403 authorization envelope + the denied
    `unauthorized_access` audit record, exactly as they exist today
    (Req 2.7, 3.2)."""
    body = json.loads(response["body"])
    observed = _observation(status=response["statusCode"], body=body,
                            permission=permission, role=user["role"],
                            resource=expected_resource)

    # A legitimate denial is never the generic catch-all (Req 2.7).
    assert body.get("error") != GENERIC_AUTH_ERROR, (
        f"denial fell through to the generic authorization error\n{observed}")
    assert response["statusCode"] == 403, f"expected 403\n{observed}"

    # Exactly the existing 403 fields.
    assert set(body) == {"error", "required_permissions", "usecase_id"}, (
        f"403 envelope fields changed\n{observed}")
    assert body["error"] == "Insufficient permissions", observed
    assert body["required_permissions"] == [permission], observed
    assert body["usecase_id"] == "global", observed

    # Exactly one denial audit record with the existing structure.
    records = [r for r in _audit_records()
               if r["user_id"] == user["user_id"]]
    assert len(records) == 1, (
        f"expected exactly one denial audit record, got {len(records)}"
        f"\n{observed}")
    record = records[0]
    assert record["action"] == "unauthorized_access", observed
    assert record["result"] == "denied", observed
    assert record["resource_type"] == "api_endpoint", observed
    assert record["resource_id"] == expected_resource, observed
    details = record["details"]
    assert details["required_permissions"] == [permission], observed
    assert details["usecase_id"] == "global", observed
    assert details["user_role"] == user["role"], observed
    assert details["method"] == event["httpMethod"], observed
    assert details["path"] == event["path"], observed


def _get_job_item(job_id):
    return _JOBS.get_item(Key={"build_job_id": job_id}).get("Item")


def _user_audit_records(user):
    return [r for r in _audit_records() if r["user_id"] == user["user_id"]]


def _assert_error_envelope(response, status, code):
    """The existing NON-authorization build API error envelope
    `{"error": {"code", "message", "details"}}` (Req 3.2, 3.5)."""
    body = json.loads(response["body"])
    observed = _observation(status=response["statusCode"], body=body)
    assert response["statusCode"] == status, observed
    assert set(body) == {"error"}, observed
    assert set(body["error"]) == {"code", "message", "details"}, observed
    assert body["error"]["code"] == code, observed
    assert isinstance(body["error"]["message"], str) and body["error"]["message"]
    assert isinstance(body["error"]["details"], dict), observed
    return body


class _NoDispatch:
    """Belt-and-braces: `invoke_dispatcher` is already a no-op (its env
    var is unset), and this patch guarantees it for submit cases."""

    def __enter__(self):
        self._patch = mock.patch.object(build_jobs, "invoke_dispatcher",
                                        side_effect=lambda ids: None)
        self._patch.start()
        return self

    def __exit__(self, *exc):
        self._patch.stop()
        return False


# ---------------------------------------------------------------------------
# Property 6 (a): the Role × build-operation matrix is exactly the merged
# matrix — PortalAdmin/DataScientist/UseCaseAdmin hold
# builds:submit/read/cancel, Viewer/Operator hold none (Req 3.4).
# ---------------------------------------------------------------------------

class TestBuildRoleMatrixPreservation:
    """Property-based over `Role × build operation`, both against the
    declared matrix and against real JWT-only role resolution at the
    'global' scope every builds route uses."""

    @given(role=st.sampled_from(ALL_ROLES),
           permission=st.sampled_from(BUILDS_PERMISSIONS))
    @settings(max_examples=150, deadline=None,
              suppress_health_check=[HealthCheck.too_slow])
    def test_matrix_grants_builds_permissions_to_exactly_the_build_roles(
            self, role, permission):
        expected = role in BUILD_ROLES
        granted = permission in rbac_manager.role_permissions[Role(role)]
        assert granted is expected, _observation(
            role=role, permission=permission.value, granted=granted,
            expected=expected, note="role-permission matrix changed")

    @given(role=st.sampled_from(ALL_ROLES),
           operation=st.sampled_from(BUILD_OPERATIONS))
    @settings(max_examples=150, deadline=None,
              suppress_health_check=[HealthCheck.too_slow])
    def test_jwt_only_resolution_matches_the_matrix_per_operation(
            self, role, operation):
        """A JWT-only caller (no UserRoles row at all) resolves to their
        JWT role at 'global' scope, and holds the permission each build
        operation requires iff the matrix says so (Req 3.1, 3.4)."""
        _resource, _method, permission, _needs_job = operation
        user = _jwt_user(role)
        expected = role in BUILD_ROLES

        resolved = rbac_manager.get_user_role(user["user_id"], "global", user)
        granted = rbac_manager.has_permission(
            user["user_id"], "global", Permission(permission), user_info=user)

        observed = _observation(role=role, operation=f"{_method} {_resource}",
                                permission=permission,
                                resolved_role=resolved.value if resolved
                                else None, granted=granted)
        assert resolved == Role(role), observed
        assert granted is expected, observed


# ---------------------------------------------------------------------------
# Property 6 (b): every denied build operation returns the standard 403
# envelope with the denial audit record and mutates nothing — never the
# generic authorization 500 (Req 2.7, 3.2, 3.5).
# ---------------------------------------------------------------------------

class TestNonBuildRoleDenialAtRealBoundary:
    """Property-based over `non-build Role × build operation` through the
    real `build_jobs.handler` dispatch and the real
    `@require_builds_*()` decorators."""

    @given(role=st.sampled_from(NON_BUILD_ROLES),
           operation=st.sampled_from(BUILD_OPERATIONS),
           target=st.sampled_from(TARGETS),
           execution_mode=st.sampled_from(MODES),
           job_status=st.sampled_from(sorted(build_domain.ALL_STATUSES)))
    @settings(max_examples=120, deadline=None,
              suppress_health_check=[HealthCheck.too_slow])
    def test_denied_with_standard_envelope_and_no_mutation(
            self, role, operation, target, execution_mode, job_status):
        """The denial is independent of the requested Build_Target, the
        execution mode, and the target job's status."""
        resource, method, permission, needs_job = operation
        user = _jwt_user(role)

        job_id, before = None, None
        if needs_job:
            job_id = f"bj-{uuid.uuid4()}"
            before = _seed_job(job_id, job_status, build_jobs.now_ms(),
                               execution_mode=execution_mode)
        jobs_before = set(_scan_jobs())

        body = ({"targets": [target], "execution_mode": execution_mode}
                if resource == "/builds" and method == "POST" else None)
        event = _event(resource, method, user, body=body, job_id=job_id)

        with _NoDispatch():
            response = build_jobs.handler(event, None)

        _assert_standard_denial(response, permission, user, event, resource)

        # No Build_Job mutation of any kind on denial (Req 3.5).
        assert set(_scan_jobs()) == jobs_before, _observation(
            role=role, operation=f"{method} {resource}",
            note="denied request changed the Build_Jobs table")
        if needs_job:
            after = _get_job_item(job_id)
            assert after == before, _observation(
                before=before, after=after,
                note="denied request mutated the Build_Job")

        # Denied requests never populate rbac_context (observed today).
        assert "rbac_context" not in event


class TestNonBuildRoleSubmitDenial:
    """The explicit Viewer/Operator `POST /builds` baseline: standard 403
    `Insufficient permissions`, zero Build_Jobs, exactly one denied
    `unauthorized_access` audit record carrying `builds:submit` and
    `usecase_id='global'` (Req 2.7, 3.2, 3.4)."""

    def setup_method(self):
        _clear_tables()

    @pytest.mark.parametrize("role", NON_BUILD_ROLES)
    @pytest.mark.parametrize("execution_mode", MODES)
    def test_post_builds_denied_in_both_modes(self, role, execution_mode):
        user = _jwt_user(role)
        request = {"targets": [build_domain.TARGET_JP5],
                   "execution_mode": execution_mode}
        if execution_mode == build_domain.EXECUTION_MODE_DEDICATED:
            request["server_id"] = _seed_running_arm64_server()

        event = _event("/builds", "POST", user, body=request)
        with _NoDispatch():
            response = build_jobs.handler(event, None)

        _assert_standard_denial(response, "builds:submit", user, event,
                                "/builds")
        assert _scan_jobs() == {}, "a denied submit created a Build_Job"
        assert len(_audit_records()) == 1, (
            "a denied submit wrote more than the one denial audit record")


# ---------------------------------------------------------------------------
# Property 6 (c): authorized require_builds_read() paths — response shape,
# most-recent-first ordering, pagination, rbac_context keys (Req 3.3, 3.5).
# ---------------------------------------------------------------------------

class TestAuthorizedReadPathsPreservation:

    def setup_method(self):
        _clear_tables()
        now = build_jobs.now_ms()
        # Deterministic most-recent-first expectation: created_at desc.
        self.newest = _seed_job("bj-newest", build_domain.STATUS_QUEUED,
                                now)
        self.middle = _seed_job("bj-middle", build_domain.STATUS_BUILDING,
                                now - 60_000)
        self.oldest = _seed_job("bj-oldest", build_domain.STATUS_SUCCEEDED,
                                now - 120_000)
        self.expected_order = ["bj-newest", "bj-middle", "bj-oldest"]

    def _assert_rbac_context(self, event, role):
        context = event["rbac_context"]
        observed = _observation(role=role, context=context)
        assert set(context) == RBAC_CONTEXT_KEYS, observed
        assert context["usecase_id"] == "global", observed
        assert context["user_role"] == Role(role), observed
        assert context["is_super_user"] is (role == "PortalAdmin"), observed
        assert Permission.BUILDS_READ in context["permissions"], observed

    @pytest.mark.parametrize("role", BUILD_ROLES)
    def test_list_builds_shape_and_ordering(self, role):
        user = _jwt_user(role)
        event = _event("/builds", "GET", user)
        response = build_jobs.handler(event, None)
        body = json.loads(response["body"])
        observed = _observation(role=role, status=response["statusCode"],
                                body=body)

        assert response["statusCode"] == 200, observed
        assert set(body) == {"jobs", "nextToken", "total"}, observed
        assert [job["build_job_id"] for job in body["jobs"]] == \
            self.expected_order, observed
        assert body["total"] == 3, observed
        assert body["nextToken"] is None, observed
        self._assert_rbac_context(event, role)
        # Authorized reads write no audit record (observed today).
        assert _user_audit_records(user) == []

    def test_list_builds_pagination_tokens(self):
        user = _jwt_user("PortalAdmin")
        first = build_jobs.handler(
            _event("/builds", "GET", user, query={"limit": "2"}), None)
        first_body = json.loads(first["body"])
        assert first["statusCode"] == 200
        assert [j["build_job_id"] for j in first_body["jobs"]] == \
            self.expected_order[:2]
        assert first_body["nextToken"], "page token missing on a full page"

        second = build_jobs.handler(
            _event("/builds", "GET", user,
                   query={"limit": "2",
                          "nextToken": first_body["nextToken"]}), None)
        second_body = json.loads(second["body"])
        assert second["statusCode"] == 200
        assert [j["build_job_id"] for j in second_body["jobs"]] == \
            self.expected_order[2:]
        assert second_body["nextToken"] is None

    @pytest.mark.parametrize("role", BUILD_ROLES)
    def test_get_build_detail(self, role):
        user = _jwt_user(role)
        event = _event("/builds/{id}", "GET", user, job_id="bj-middle")
        response = build_jobs.handler(event, None)
        body = json.loads(response["body"])
        observed = _observation(role=role, status=response["statusCode"],
                                body=body)

        assert response["statusCode"] == 200, observed
        assert set(body) == {"job"}, observed
        assert body["job"]["build_job_id"] == "bj-middle", observed
        assert body["job"]["build_target"] == build_domain.TARGET_JP5, observed
        assert body["job"]["status"] == build_domain.STATUS_BUILDING, observed
        self._assert_rbac_context(event, role)

    @pytest.mark.parametrize("role", BUILD_ROLES)
    def test_get_build_logs_empty_page_before_output(self, role):
        """A job with no log stream yet yields an empty page, not an
        error (authorized require_builds_read() path)."""
        user = _jwt_user(role)
        event = _event("/builds/{id}/logs", "GET", user, job_id="bj-newest")
        response = build_jobs.handler(event, None)
        body = json.loads(response["body"])
        observed = _observation(role=role, status=response["statusCode"],
                                body=body)
        assert response["statusCode"] == 200, observed
        assert body == {"events": [], "nextToken": None}, observed
        self._assert_rbac_context(event, role)

    def test_detail_404_uses_the_non_authorization_error_envelope(self):
        user = _jwt_user("DataScientist")
        response = build_jobs.handler(
            _event("/builds/{id}", "GET", user, job_id="bj-missing"), None)
        _assert_error_envelope(response, 404, "BUILD_JOB_NOT_FOUND")

    def test_list_invalid_page_token_uses_the_same_error_envelope(self):
        user = _jwt_user("UseCaseAdmin")
        response = build_jobs.handler(
            _event("/builds", "GET", user,
                   query={"nextToken": "not-a-token"}), None)
        _assert_error_envelope(response, 400, "INVALID_PARAMETER")


# ---------------------------------------------------------------------------
# Property 6 (d): require_builds_cancel() semantics (Req 3.5).
# ---------------------------------------------------------------------------

class TestCancelSemanticsPreservation:

    def setup_method(self):
        _clear_tables()

    @pytest.mark.parametrize("role", BUILD_ROLES)
    def test_queued_job_cancels_immediately_with_audit(self, role):
        user = _jwt_user(role)
        job_id = "bj-queued-cancel"
        _seed_job(job_id, build_domain.STATUS_QUEUED, build_jobs.now_ms())

        event = _event("/builds/{id}/cancel", "POST", user, job_id=job_id)
        response = build_jobs.handler(event, None)
        body = json.loads(response["body"])
        observed = _observation(role=role, status=response["statusCode"],
                                body=body)

        assert response["statusCode"] == 200, observed
        assert set(body) == {"job"}, observed
        assert body["job"]["status"] == build_domain.STATUS_CANCELLED, observed
        assert body["job"].get("ended_at"), observed

        records = _user_audit_records(user)
        assert len(records) == 1, observed
        record = records[0]
        assert record["action"] == "build_cancelled"
        assert record["result"] == "success"
        assert record["resource_type"] == "build_job"
        assert record["resource_id"] == job_id
        assert record["details"]["status_at_request"] == \
            build_domain.STATUS_QUEUED
        assert record["details"]["removed_from_queue"] is True

    @pytest.mark.parametrize("status", (build_domain.STATUS_SUCCEEDED,
                                        build_domain.STATUS_FAILED,
                                        build_domain.STATUS_CANCELLED))
    def test_terminal_job_rejected_409_unchanged(self, status):
        user = _jwt_user("PortalAdmin")
        job_id = f"bj-terminal-{status}"
        before = _seed_job(job_id, status, build_jobs.now_ms())

        response = build_jobs.handler(
            _event("/builds/{id}/cancel", "POST", user, job_id=job_id), None)
        body = _assert_error_envelope(response, 409, "CANCELLATION_REJECTED")
        assert body["error"]["details"]["status"] == status
        assert body["error"]["details"]["errors"], "rejection names no rule"

        after = _get_job_item(job_id)
        assert after == before, _observation(before=before, after=after,
                                            note="rejected cancel mutated "
                                                 "the Build_Job")
        assert _user_audit_records(user) == []

    def test_cancel_missing_job_404_envelope(self):
        user = _jwt_user("DataScientist")
        response = build_jobs.handler(
            _event("/builds/{id}/cancel", "POST", user,
                   job_id="bj-missing"), None)
        _assert_error_envelope(response, 404, "BUILD_JOB_NOT_FOUND")


# ---------------------------------------------------------------------------
# Property 6 (e): role resolution outside the reproduced submit failure —
# DynamoDB-row precedence and JWT-only resolution (Req 3.1, 3.7).
# ---------------------------------------------------------------------------

class TestRoleResolutionPreservation:

    def setup_method(self):
        _clear_tables()

    def _assign(self, user, usecase_id, role_value):
        _USER_ROLES.put_item(Item={"user_id": user["user_id"],
                                   "usecase_id": usecase_id,
                                   "role": role_value,
                                   "assigned_by": "test"})

    def test_dynamodb_global_portal_admin_row_outranks_a_jwt_viewer(self):
        user = _jwt_user("Viewer")
        self._assign(user, "global", "PortalAdmin")

        assert rbac_manager.get_user_role(
            user["user_id"], "global", user) == Role.PORTAL_ADMIN
        event = _event("/builds", "GET", user)
        response = build_jobs.handler(event, None)
        assert response["statusCode"] == 200, response["body"]
        assert event["rbac_context"]["user_role"] == Role.PORTAL_ADMIN
        assert event["rbac_context"]["is_super_user"] is True

    @pytest.mark.parametrize("row_role", ("Viewer", "Operator",
                                          "DataScientist", "UseCaseAdmin"))
    def test_per_usecase_row_outranks_the_jwt_role(self, row_role):
        user = _jwt_user("DataScientist")
        self._assign(user, "uc-preserve", row_role)

        assert rbac_manager.get_user_role(
            user["user_id"], "uc-preserve", user) == Role(row_role)
        # And the JWT role still resolves for other scopes.
        assert rbac_manager.get_user_role(
            user["user_id"], "uc-other", user) == Role.DATA_SCIENTIST

    def test_non_portal_admin_global_row_does_not_grant_at_global_scope(self):
        """Documented residual behavior (bugfix Req 3.7): at 'global'
        scope only a PortalAdmin row is consulted, so a Viewer JWT with a
        DataScientist global row stays denied on builds routes."""
        user = _jwt_user("Viewer")
        self._assign(user, "global", "DataScientist")

        assert rbac_manager.get_user_role(
            user["user_id"], "global", user) == Role.VIEWER
        event = _event("/builds", "GET", user)
        response = build_jobs.handler(event, None)
        _assert_standard_denial(response, "builds:read", user, event,
                                "/builds")

    @pytest.mark.parametrize("role", ALL_ROLES)
    def test_jwt_only_user_resolves_to_the_jwt_role(self, role):
        user = _jwt_user(role)
        assert rbac_manager.get_user_role(
            user["user_id"], "global", user) == Role(role)
        assert rbac_manager.is_portal_admin(
            user["user_id"], user_info=user) is (role == "PortalAdmin")


# ---------------------------------------------------------------------------
# FIX CHECKS (task 7.3 verifies these): permitted-role POST /builds.
# The ephemeral case was the task 5 bug condition (xfail while unfixed,
# now asserted plainly); the dedicated case is an observed-passing
# baseline and is asserted.
# ---------------------------------------------------------------------------

class TestPermittedRoleSubmitFixChecks:

    def setup_method(self):
        _clear_tables()

    def _submit(self, role, execution_mode):
        user = _jwt_user(role)
        request = {"targets": [build_domain.TARGET_JP5],
                   "execution_mode": execution_mode}
        if execution_mode == build_domain.EXECUTION_MODE_DEDICATED:
            request["server_id"] = _seed_running_arm64_server()
        event = _event("/builds", "POST", user, body=request)
        with _NoDispatch():
            response = build_jobs.handler(event, None)
        return user, response

    def _assert_accepted(self, user, response, execution_mode):
        body = json.loads(response["body"])
        jobs = _scan_jobs()
        observed = _observation(role=user["role"], mode=execution_mode,
                                status=response["statusCode"], body=body,
                                persisted=len(jobs))

        assert body.get("error") != GENERIC_AUTH_ERROR, (
            f"permitted role fell through to the generic authorization "
            f"failure\n{observed}")
        assert response["statusCode"] == 201, observed
        assert len(jobs) == 1, observed
        job = next(iter(jobs.values()))
        assert job["build_target"] == build_domain.TARGET_JP5, observed
        assert job["execution_mode"] == execution_mode, observed
        assert job["requested_by"] == user["user_id"], observed
        assert job["status"] == build_domain.STATUS_QUEUED, observed

    @pytest.mark.parametrize("role", BUILD_ROLES)
    def test_ephemeral_submit_accepted(self, role):
        # Was xfail for the task 5 bug condition (an ephemeral submit
        # persisted server_id=None, which the deployed BuildJobs
        # server-index GSI rejected, and rbac_check swallowed the
        # ClientError into the generic 'Authorization check failed' 500).
        # The task 7.2 fix makes this pass, so the marker was removed in
        # task 7.4 without changing a single assertion.
        user, response = self._submit(
            role, build_domain.EXECUTION_MODE_EPHEMERAL)
        self._assert_accepted(user, response,
                              build_domain.EXECUTION_MODE_EPHEMERAL)

    @pytest.mark.parametrize("role", BUILD_ROLES)
    def test_dedicated_submit_accepted_baseline(self, role):
        user, response = self._submit(
            role, build_domain.EXECUTION_MODE_DEDICATED)
        self._assert_accepted(user, response,
                              build_domain.EXECUTION_MODE_DEDICATED)
        records = [r for r in _user_audit_records(user)
                   if r["action"] == "build_requested"]
        assert len(records) == 1, "expected one build_requested audit record"
        assert records[0]["result"] == "success"
