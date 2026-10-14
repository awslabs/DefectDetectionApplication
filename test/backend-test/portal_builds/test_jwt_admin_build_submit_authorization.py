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
POST /builds authorization boundary for JWT-only build operators
(build-fleet-rbac-visibility, task 5 — Property 5: Bug Condition;
re-run and extended as the fix check in task 7.3 — Property 5: Expected
Behavior).

**Validates: Requirements 1.3, 2.1, 2.3, 2.7, 3.1, 3.4, 3.5**

Bug condition (the newly reported failure, distinct from the already
fixed `user_info` gap of commit 22a27eb): the caller is authenticated by
Cognito with JWT `custom:role=PortalAdmin`, has NO `dda-portal-user-roles`
row for `global`, submits target `JP5`, selects either `ephemeral` or
`dedicated`, and receives the generic catch-all
`{"error": "Authorization check failed"}` (HTTP 500) while no Build_Job is
created.

Task 7.3 keeps this exact boundary and fixture and only widens the input
domain: the same submission is now exercised for every JWT-only build
operator role in the merged matrix (`PortalAdmin`, `DataScientist`,
`UseCaseAdmin`, each with no `global` role row) across both execution
modes, and one dedicated case proves that the screenshot-selected
`ephemeral` path and the user-described `dedicated` path pass one and the
same `@require_builds_submit()` authorization decision.

What this test exercises (deliberately end to end, no authorization
shortcuts):

* the real API dispatch `build_jobs.handler` routing `POST /builds` to the
  real imported `submit_build`, which is decorated with the real
  `@require_builds_submit()`;
* the real `shared_utils.get_user_from_event`, `rbac_middleware.rbac_check`,
  `shared_utils.RBACManager`, `Permission.BUILDS_SUBMIT`, and the real
  role-permission matrix — nothing about authorization, role resolution,
  enum lookup, or the decorator is mocked, unwrapped, or redecorated;
* DynamoDB tables created with the EXACT schema of the deployed tables,
  including the BuildJobs GSIs (`status-index`, `server-index`,
  `request-index`). This fidelity is essential: the sibling suites create
  the table without GSIs, which is precisely why they never reproduced
  this failure.

Safety (no real build can launch): `invoke_dispatcher` is replaced with a
no-op recorder, so nothing is ever dispatched, and every AWS call is
moto-backed. `put_new_job` is wrapped by a RECORDING fake that records the
would-be Build_Job and then delegates to the real implementation — the
real persistence step must stay in place, since replacing it outright
would mask the very failure this test exists to reproduce.

Artifact selection: by default the test loads the current source tree. Set
`DDA_BUILD_FN_DIR` / `DDA_BUILD_LAYER_DIR` to run the identical fixture
against an unpacked deployed function artifact plus its attached layer
contents (task 5's source-vs-deployed requirement).

Expected outcome BEFORE the fix: at least one mode reproduces the generic
catch-all 500 with zero Build_Jobs created, and the captured RBAC logger
traceback names the swallowed exception. The SAME assertions are the
accepted-response check in task 7.3 after the fix: every permitted
`role x mode` case returns 201 with exactly one queued JP5 Build_Job and
no `Authorization check failed` anywhere.
"""
import json
import logging
import os
import sys
import types
import uuid
from unittest import mock

import pytest

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

_SUFFIX = "authz-explore"
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
# Unset so invoke_dispatcher is a logged no-op even before it is wrapped.
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
#: unpacked deployed function artifact + attached layer contents.
FUNCTIONS_DIR = os.environ.get(
    "DDA_BUILD_FN_DIR", os.path.join(_BACKEND, "functions"))
LAYER_DIR = os.environ.get(
    "DDA_BUILD_LAYER_DIR",
    os.path.join(_BACKEND, "layers", "shared", "python"))

for _p in (LAYER_DIR, FUNCTIONS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# Fresh modules so the handler's module-level boto3 handles are created
# under the moto mock started below, and so the modules come from the
# artifact assembly selected above.
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
# resolution and real denial audit writes run unmocked.
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

import build_domain  # noqa: E402
import build_jobs  # noqa: E402
import rbac_middleware  # noqa: E402
import shared_utils  # noqa: E402


#: The JWT roles the merged role-permission matrix grants `builds:submit`
#: to (the Build_Operator capability). Viewer/Operator denial stays in the
#: task 6 preservation suite.
BUILD_OPERATOR_ROLES = ("PortalAdmin", "DataScientist", "UseCaseAdmin")


def _jwt_only_user(role="PortalAdmin"):
    """The real deployed Cognito caller shape: a `sub` uuid and a
    `custom:role` claim, with no UserRoles rows at all."""
    user_id = str(uuid.uuid4())
    username = "admin" if role == "PortalAdmin" else role.lower()
    return {
        "user_id": user_id,
        "email": f"{username}-{user_id[:8]}@example.com",
        "username": username,
        "role": role,
    }


def _jwt_only_portal_admin():
    """The real Cognito `admin` from the screenshot/user report."""
    return _jwt_only_user("PortalAdmin")


def _post_builds_event(user, body):
    """API Gateway REST event for POST /builds with Cognito claims."""
    return {
        "resource": "/builds",
        "httpMethod": "POST",
        "path": "/builds",
        "pathParameters": None,
        "queryStringParameters": None,
        "body": json.dumps(body),
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
    for item in _USER_ROLES.scan().get("Items", []):
        _USER_ROLES.delete_item(Key={"user_id": item["user_id"],
                                     "usecase_id": item["usecase_id"]})


def _seed_running_arm64_server():
    """A valid, running, arm64 Dedicated_Build_Server for the
    user-described dedicated mode case (JP5 requires arm64)."""
    server_id = f"srv-{uuid.uuid4()}"
    _SERVERS.put_item(Item={
        "server_id": server_id,
        "name": "arm64-test-server",
        "instance_id": "i-0123456789abcdef0",
        "lifecycle_state": "running",
        "cpu_architecture": build_domain.ARCH_ARM64,
    })
    return server_id


def _assert_no_global_role_row(user):
    """The bug condition requires a JWT-only caller: no UserRoles row for
    'global' (and in fact no row at all for this user)."""
    global_row = _USER_ROLES.get_item(
        Key={"user_id": user["user_id"], "usecase_id": "global"}
    ).get("Item")
    assert global_row is None, (
        "precondition violated: the caller must have NO "
        f"dda-portal-user-roles row for 'global' (found {global_row})")
    rows = _USER_ROLES.query(
        KeyConditionExpression="user_id = :u",
        ExpressionAttributeValues={":u": user["user_id"]},
    ).get("Items", [])
    assert rows == [], (
        f"precondition violated: the caller must be JWT-only (rows: {rows})")


def _artifact_fingerprint():
    """Which handler/layer assembly produced this result (task 5 requires
    the artifact identity in the counterexample)."""
    return {
        "build_jobs": getattr(build_jobs, "__file__", "?"),
        "rbac_middleware": getattr(rbac_middleware, "__file__", "?"),
        "shared_utils": getattr(shared_utils, "__file__", "?"),
        "code_version": os.environ.get("CODE_VERSION", "<unset locally>"),
        "deployed_function_version": os.environ.get(
            "DDA_DEPLOYED_FN_VERSION", "<not supplied>"),
        "deployed_layer_version": os.environ.get(
            "DDA_DEPLOYED_LAYER_VERSION", "<not supplied>"),
    }


class _RecordingSideEffects:
    """Records the would-be Build_Jobs and suppresses dispatch, while
    keeping the real persistence call (the failure under investigation
    happens inside it, so it must not be stubbed away)."""

    def __init__(self):
        self.recorded_jobs = []
        self.dispatched = []
        self._real_put_new_job = build_jobs.put_new_job
        self._patches = []

    def __enter__(self):
        def recording_put_new_job(job):
            self.recorded_jobs.append(dict(job))
            return self._real_put_new_job(job)

        def noop_invoke_dispatcher(build_job_ids):
            self.dispatched.append(list(build_job_ids))

        self._patches = [
            mock.patch.object(build_jobs, "put_new_job",
                              side_effect=recording_put_new_job),
            mock.patch.object(build_jobs, "invoke_dispatcher",
                              side_effect=noop_invoke_dispatcher),
        ]
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in self._patches:
            p.stop()
        return False


# ---------------------------------------------------------------------------
# Property 5 — a JWT-only build operator reaches POST /builds through the
# real @require_builds_submit() boundary in BOTH execution modes
# (Requirements 1.3, 2.1, 2.3, 2.7, 3.1, 3.4, 3.5).
#
# Task 5 ran this as the Bug Condition (ephemeral reproduced the generic
# catch-all 500). Task 7.3 re-runs the identical boundary as the Expected
# Behavior check, widened to every permitted JWT role.
# ---------------------------------------------------------------------------

MODE_CASES = [
    pytest.param("ephemeral", False, id="ephemeral"),
    pytest.param("dedicated", True, id="dedicated"),
]

#: role x mode: the full permitted submission matrix for task 7.3.
ROLE_MODE_CASES = [
    pytest.param(role, mode.values[0], mode.values[1],
                 id=f"{role}-{mode.id}")
    for role in BUILD_OPERATOR_ROLES
    for mode in MODE_CASES
]


class _SubmitResult:
    """Everything one real POST /builds round trip produced."""

    def __init__(self, user, request, response, body, fakes, persisted,
                 swallowed, event):
        self.user = user
        self.request = request
        self.response = response
        self.body = body
        self.fakes = fakes
        self.persisted = persisted
        self.swallowed = swallowed
        self.event = event

    @property
    def status(self):
        return self.response["statusCode"]

    @property
    def rbac_context(self):
        """The authorization decision the real decorator injected."""
        return self.event.get("rbac_context")

    @property
    def counterexample(self):
        return json.dumps({
            "role": self.user["role"],
            "mode": self.request["execution_mode"],
            "request": self.request,
            "status": self.status,
            "body": self.body,
            "recorded_jobs": len(self.fakes.recorded_jobs),
            "persisted_jobs": len(self.persisted),
            "rbac_context": self.rbac_context,
            "artifact": _artifact_fingerprint(),
        }, indent=2, default=str) + (
            "\nSwallowed by rbac_check:\n" + self.swallowed)


class TestJwtBuildOperatorSubmission:
    """The screenshot-selected ephemeral request and the user-described
    dedicated request must both pass the same real builds-submit
    authorization decision and be accepted, for every JWT role the matrix
    grants `builds:submit`."""

    def setup_method(self):
        _clear_tables()

    @staticmethod
    def _submit_jp5(user, execution_mode, needs_server, caplog):
        """Drive the REAL API dispatch: build_jobs.handler -> the imported
        submit_build decorated with the real @require_builds_submit()."""
        request = {"targets": [build_domain.TARGET_JP5],
                   "execution_mode": execution_mode}
        if needs_server:
            request["server_id"] = _seed_running_arm64_server()

        event = _post_builds_event(user, request)

        caplog.clear()
        with caplog.at_level(logging.ERROR), _RecordingSideEffects() as fakes:
            response = build_jobs.handler(event, None)

        swallowed = "\n".join(
            record.getMessage() + "\n" + (
                logging.Formatter().formatException(record.exc_info)
                if record.exc_info else "")
            for record in caplog.records)

        return _SubmitResult(user, request, response,
                             json.loads(response["body"]), fakes,
                             _scan_jobs(), swallowed, event)

    @staticmethod
    def _assert_accepted_single_queued_job(result):
        """Accepted status, exactly one recorded queued Build_Job that
        preserves target/mode/requesting user, no generic authorization
        response, and nothing dispatched."""
        ce = result.counterexample

        # The generic catch-all must never be the answer for a caller who
        # holds builds:submit (Req 2.1, 2.3, 2.7).
        assert result.body.get("error") != "Authorization check failed", (
            "POST /builds returned the generic catch-all authorization "
            f"failure for a JWT-only {result.user['role']}.\n{ce}")
        assert "Authorization check failed" not in json.dumps(result.body), (
            f"generic authorization text leaked into the response.\n{ce}")

        # Accepted, with exactly one queued JP5 Build_Job that preserves
        # target, mode, and requesting user (Req 1.3, 2.1, 3.4, 3.5).
        assert result.status == 201, (
            f"POST /builds was not accepted.\n{ce}")
        assert len(result.fakes.recorded_jobs) == 1, (
            f"expected exactly one would-be Build_Job.\n{ce}")
        job = result.fakes.recorded_jobs[0]
        assert job["build_target"] == build_domain.TARGET_JP5, ce
        assert job["execution_mode"] == result.request["execution_mode"], ce
        assert job["requested_by"] == result.user["user_id"], ce
        assert job["status"] == build_domain.STATUS_QUEUED, ce
        assert len(result.persisted) == 1, (
            f"the accepted Build_Job was not persisted.\n{ce}")

        # Nothing was dispatched: no EC2/SSM build can start from this test.
        assert result.fakes.dispatched == [[job["build_job_id"]]], ce
        return job

    @pytest.mark.parametrize("role,execution_mode,needs_server",
                             ROLE_MODE_CASES)
    def test_jwt_build_operator_submits_jp5(self, role, execution_mode,
                                            needs_server, caplog):
        user = _jwt_only_user(role)
        _assert_no_global_role_row(user)

        # The role-permission matrix really does grant builds:submit to
        # the caller's JWT role: any denial/exception below is a defect,
        # not a legitimate authorization outcome.
        assert shared_utils.rbac_manager.has_permission(
            user["user_id"], "global", shared_utils.Permission.BUILDS_SUBMIT,
            user_info=user), (
            f"matrix precondition: JWT {role} must hold builds:submit")

        result = self._submit_jp5(user, execution_mode, needs_server, caplog)
        self._assert_accepted_single_queued_job(result)

        # The real decorator authorized at global scope with the JWT role.
        context = result.rbac_context
        assert context is not None, result.counterexample
        assert context["usecase_id"] == "global", result.counterexample
        assert context["user_role"].value == role, result.counterexample
        assert shared_utils.Permission.BUILDS_SUBMIT in context["permissions"], \
            result.counterexample

    def test_portal_admin_ephemeral_and_dedicated_share_one_decision(
            self, caplog):
        """The screenshot-selected ephemeral path and the user-described
        dedicated path pass the SAME @require_builds_submit() decision:
        authorization is independent of execution mode."""
        admin = _jwt_only_portal_admin()
        _assert_no_global_role_row(admin)

        decisions = {}
        for execution_mode, needs_server in (("ephemeral", False),
                                             ("dedicated", True)):
            _clear_tables()
            result = self._submit_jp5(admin, execution_mode, needs_server,
                                      caplog)
            self._assert_accepted_single_queued_job(result)

            context = result.rbac_context
            assert context is not None, result.counterexample
            decisions[execution_mode] = {
                "user_id": context["user_id"],
                "usecase_id": context["usecase_id"],
                "user_role": context["user_role"],
                "permissions": sorted(p.value for p in context["permissions"]),
                "is_super_user": context["is_super_user"],
            }

        assert decisions["ephemeral"] == decisions["dedicated"], (
            "the same PortalAdmin got different authorization decisions for "
            f"the two execution modes: {decisions}")
        assert decisions["ephemeral"]["usecase_id"] == "global"
        assert decisions["ephemeral"]["user_role"].value == "PortalAdmin"
        assert decisions["ephemeral"]["is_super_user"] is True
        assert (shared_utils.Permission.BUILDS_SUBMIT.value
                in decisions["ephemeral"]["permissions"])
