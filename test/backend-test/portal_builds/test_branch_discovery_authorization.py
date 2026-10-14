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
Branch discovery authorization at the real `build_jobs.handler` /
`@require_builds_read()` boundary (build-source-selection, task 10.2).

**Property 11: Expected Behavior** - Discovery authorization.

**Validates: Requirements 3.4** (plus the route-level halves of 3.1, 3.3,
3.5 exercised on the way through).

The rule, restated independently of the implementation: _for any_ role,
`GET /build-branches` succeeds (200 with the branch list) if and only if
the role holds the builds read permission; otherwise it returns the
EXISTING 403 authorization envelope and records EXACTLY ONE denial in the
existing audit structure — and no outbound discovery call is ever made on
a denial.

Nothing about authorization is mocked (same discipline as
`test_build_authorization_preservation.py`): the real `build_jobs.handler`
dispatch routes `/build-branches` to the real imported
`list_build_branches`, which is decorated with the real
`@require_builds_read()`; the real `shared_utils.RBACManager`, the real
role-permission matrix, the real `log_audit_event` writes, and real
DynamoDB (moto) UserRoles/AuditLog tables are used. The decorator is never
unwrapped, bypassed, or redecorated.

The ONLY substitution is the network transport: `discover_branches` runs
with its injected-fetch seam pointed at a scripted in-process GitHub
(the `vllm_fit_check._default_hf_fetch` pattern, as in
`test_branch_discovery_property.py`), so no real call to GitHub is ever
made.

Run with ``--noconftest`` like the rest of the ``portal_builds`` suite.
"""
import json
import os
import sys
import types
import uuid
from unittest import mock
from urllib.parse import parse_qs, urlsplit

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

_SUFFIX = "branch-discovery-authz"
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
os.environ.pop("BUILD_DISPATCHER_FUNCTION_NAME", None)

# Import boto3 (and thus botocore/urllib3) from the test environment BEFORE
# the Lambda layer directory joins sys.path (the layer vendors its own
# urllib3 build targeting the Lambda runtime).
import boto3  # noqa: E402

# Sibling shim: some verification containers lack the _bz2 C extension
# while moto's import path reaches bz2.
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
FUNCTIONS_DIR = os.path.join(_BACKEND, "functions")
LAYER_DIR = os.path.join(_BACKEND, "layers", "shared", "python")

for _p in (LAYER_DIR, FUNCTIONS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# Fresh modules so the handler's module-level boto3 handles are created
# under the moto mock started below, bound to this file's table names.
for _module in ("build_jobs", "build_domain", "build_source",
                "rbac_middleware", "shared_utils"):
    sys.modules.pop(_module, None)

_MOCK = mock_aws()
_MOCK.start()

_DDB = boto3.resource("dynamodb", region_name="us-east-1")

# The discovery route touches no build table, but the module binds all of
# them at import; simple-key stand-ins keep every import-path invariant.
for _name, _key in ((_JOBS_TABLE, "build_job_id"),
                    (_SERVERS_TABLE, "server_id"),
                    (_SETTINGS_TABLE, "setting_key")):
    _DDB.create_table(
        TableName=_name,
        KeySchema=[{"AttributeName": _key, "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": _key, "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
# UserRoles / AuditLog with the deployed key schema, so real role
# resolution and real denial audit writes run unmocked (Req 3.4).
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

_AUDIT = _DDB.Table(_AUDIT_TABLE)

import build_jobs  # noqa: E402
import build_source  # noqa: E402
from shared_utils import Role  # noqa: E402


# ---------------------------------------------------------------------------
# Domains
# ---------------------------------------------------------------------------

#: Exactly these roles hold builds:read (the merged matrix pinned by
#: build-fleet-rbac-visibility); Viewer and Operator do not.
BUILD_READ_ROLES = ("PortalAdmin", "DataScientist", "UseCaseAdmin")
ALL_ROLES = tuple(role.value for role in Role)

DDA_URL = "https://github.com/awslabs/DefectDetectionApplication"
API_PREFIX = "https://api.github.com/repos/awslabs/DefectDetectionApplication"


# ---------------------------------------------------------------------------
# The scripted upstream: no real network call is ever made.
# ---------------------------------------------------------------------------

class FakeGitHub:
    """An injected fetch playing a healthy public repository and
    recording every outbound URL (containment check, Req 3.5)."""

    def __init__(self, branches=("develop", "main"), default="main"):
        self.branches = list(branches)
        self.default = default
        self.urls = []

    def __call__(self, url):
        self.urls.append(url)
        parts = urlsplit(url)
        if parts.path.endswith("/branches"):
            page = int(parse_qs(parts.query).get("page", ["1"])[0])
            return ([{"name": name} for name in self.branches]
                    if page == 1 else [])
        return {"default_branch": self.default}


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


def _event(user, query):
    """API Gateway REST event for GET /build-branches with Cognito claims."""
    return {
        "resource": "/build-branches",
        "httpMethod": "GET",
        "path": "/build-branches",
        "pathParameters": None,
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


def _user_audit_records(user):
    return [r for r in _AUDIT.scan().get("Items", [])
            if r["user_id"] == user["user_id"]]


def _call(user, query, fake=None):
    """Drive the REAL dispatch: build_jobs.handler routes /build-branches
    to the imported list_build_branches decorated with the real
    @require_builds_read(). Only the network transport is substituted."""
    fake = fake if fake is not None else FakeGitHub()
    event = _event(user, query)
    with mock.patch.object(build_source, "_default_github_fetch", fake):
        response = build_jobs.handler(event, None)
    return response, fake


# ---------------------------------------------------------------------------
# Property 11 — discovery succeeds IFF the role holds builds read; denial
# is the existing 403 envelope plus exactly one denial audit record
# (Req 3.4).
# ---------------------------------------------------------------------------

class TestDiscoveryAuthorizationProperty:

    @given(role=st.sampled_from(ALL_ROLES))
    @settings(max_examples=60, deadline=None,
              suppress_health_check=[HealthCheck.too_slow])
    def test_discovery_succeeds_iff_role_holds_builds_read(self, role):
        user = _jwt_user(role)
        response, fake = _call(user, {"repository": DDA_URL})
        body = json.loads(response["body"])
        observed = json.dumps({"role": role,
                               "status": response["statusCode"],
                               "body": body, "urls": fake.urls},
                              indent=2, default=str)

        if role in BUILD_READ_ROLES:
            # Authorized: 200 with the discovered branches (Req 3.1, 3.4).
            assert response["statusCode"] == 200, observed
            assert set(body) == {"branches", "default_branch",
                                 "truncated"}, observed
            assert body["default_branch"] == "main", observed
            assert "main" in body["branches"], observed
            assert body["truncated"] is False, observed
            # Containment: every outbound URL is built from the parsed
            # <owner>/<repo> against the fixed API host (Req 3.5).
            assert fake.urls, observed
            assert all(url.startswith(API_PREFIX) for url in fake.urls), \
                observed
            # Authorized reads write no denial audit record.
            assert _user_audit_records(user) == [], observed
        else:
            # Denied: the EXISTING 403 envelope, unchanged (Req 3.4).
            assert response["statusCode"] == 403, observed
            assert set(body) == {"error", "required_permissions",
                                 "usecase_id"}, observed
            assert body["error"] == "Insufficient permissions", observed
            assert body["required_permissions"] == ["builds:read"], observed
            assert body["usecase_id"] == "global", observed
            # No outbound discovery call is made on a denial (Req 3.4/3.5).
            assert fake.urls == [], observed
            # Exactly one denial record in the existing audit structure.
            records = _user_audit_records(user)
            assert len(records) == 1, observed
            record = records[0]
            assert record["action"] == "unauthorized_access", observed
            assert record["result"] == "denied", observed
            assert record["resource_type"] == "api_endpoint", observed
            assert record["resource_id"] == "/build-branches", observed
            details = record["details"]
            assert details["required_permissions"] == ["builds:read"], \
                observed
            assert details["usecase_id"] == "global", observed
            assert details["user_role"] == role, observed


# ---------------------------------------------------------------------------
# Route-level unit cases: the repository query parameter is validated and
# normalized BEFORE any outbound call (Req 3.5), and each discovery
# failure keeps its distinct code through the envelope (Req 3.3).
# ---------------------------------------------------------------------------

class TestDiscoveryRouteValidation:

    @pytest.mark.parametrize("query", [
        None,                                             # absent entirely
        {},                                               # no repository key
        {"repository": ""},                               # empty
        {"repository": "http://github.com/o/r"},          # not https
        {"repository": "https://evil.example.com/o/r"},   # other host
        {"repository": "https://github.com/o/r/tree/m"},  # extra segment
    ])
    def test_invalid_repository_rejected_before_any_outbound_call(
            self, query):
        user = _jwt_user("PortalAdmin")
        response, fake = _call(user, query)
        body = json.loads(response["body"])

        assert response["statusCode"] == 400, body
        assert set(body) == {"error"}, body
        assert body["error"]["code"] == "REPOSITORY_INVALID", body
        # The standard envelope names the offending field (Req 1.4, 3.5).
        assert body["error"]["details"]["field"] == "repository", body
        assert body["error"]["message"], body
        # Rejection happens BEFORE any outbound discovery call (Req 3.5).
        assert fake.urls == [], body

    def test_normalized_form_reaches_discovery(self):
        """`.git` suffix and trailing slash are dropped before the
        outbound URL is composed (Req 3.5)."""
        user = _jwt_user("DataScientist")
        response, fake = _call(user, {"repository": DDA_URL + ".git"})
        assert response["statusCode"] == 200, response["body"]
        assert all(url.startswith(API_PREFIX) for url in fake.urls), fake.urls

    def test_discovery_failure_keeps_its_distinct_code(self):
        """A classified discovery failure surfaces through the standard
        envelope with its own distinct code, never an empty-list success
        (Req 3.3)."""
        import email.message
        import io
        import urllib.error

        def not_found(url):
            raise urllib.error.HTTPError(
                url, 404, "HTTP 404", email.message.Message(),
                io.BytesIO(b"{}"))

        user = _jwt_user("UseCaseAdmin")
        event = _event(user, {"repository": DDA_URL})
        with mock.patch.object(build_source, "_default_github_fetch",
                               not_found):
            response = build_jobs.handler(event, None)
        body = json.loads(response["body"])
        assert response["statusCode"] == 404, body
        assert body["error"]["code"] == build_source.REPOSITORY_NOT_FOUND, \
            body
        assert "branches" not in body, body
