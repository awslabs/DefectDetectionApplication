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
Unit tests for the build_config.py handler
(task 7.8 of portal-build-fleet-and-workflow-gates).

Validates: Requirements 9.1, 9.4, 9.6

Covers:
- Config read wiring for the dispatch/launch parameters (Req 9.1):
  GET /build-config returns every parameter the Build_Manager reads when
  dispatching a Build_Job or launching a Dedicated_Build_Server (the
  instance type per CPU architecture, the volume size, the AWS region,
  and the maximum Build_Job runtime), with the documented defaults for
  absent fields and stored PortalSettings values (key
  `build_infrastructure_config`, nested {setting_key, value} shape)
  merged over them per field.
- Audit entry content on change (Req 9.4): PUT /build-config records one
  `build_config_changed` Audit_Log entry per applied change carrying the
  changed parameter, the prior value, the new value, the acting user,
  and the time of the change; a supplied parameter whose effective value
  did not change records no entry.
- Non-PortalAdmin denial with audit (Req 9.6): PUT /build-config from a
  Build_Operator without the PortalAdmin role is rejected with the
  authorization error, records a denied-access Audit_Log entry, and
  leaves the stored configuration unchanged.

The PortalSettings table is moto-mocked; the real rbac_middleware
decorators and build_domain functions run unstubbed. Only the RBAC role
lookup, ``get_user_from_event``, and ``log_audit_event`` are patched per
test (the sibling pattern in test_build_jobs_rbac_audit.py /
test_build_fleet_lifecycle_and_audit.py).
"""
import json
import os
import sys
import types
from unittest import mock

# ---------------------------------------------------------------------------
# Environment BEFORE any import: shared_utils and build_config bind their
# boto3 resources/clients and table names at import time.
# ---------------------------------------------------------------------------
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ["AWS_ACCESS_KEY_ID"] = "testing"
os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
os.environ["AWS_SECURITY_TOKEN"] = "testing"
os.environ["AWS_SESSION_TOKEN"] = "testing"

_SETTINGS_TABLE = "portal-settings-t78"
os.environ["SETTINGS_TABLE"] = _SETTINGS_TABLE

# Import boto3 (and thus botocore/urllib3) from the test environment BEFORE
# the Lambda layer directory joins sys.path: the layer vendors its own
# urllib3 build targeting the Lambda Python runtime, which must not shadow
# the environment's copy.
import boto3  # noqa: E402

# The flask-app verification container's python3.9 is built without the
# _bz2 C extension, and moto's request path imports moto.s3 -> bz2 on
# every call. bz2 is only used for S3-Select payload decompression, which
# this DynamoDB-only suite never exercises, so a minimal stdlib-shaped
# stub keeps the import chain intact where _bz2 is absent (same shim as
# the sibling test_build_jobs_rbac_audit.py).
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

# Fresh modules so build_config's module-level boto3 handles are created
# under the moto mock started below (sibling pattern).
for _module in ("build_config", "build_domain", "rbac_middleware",
                "shared_utils"):
    sys.modules.pop(_module, None)

# Module-scope moto: active for every import below and for the whole run.
_MOCK = mock_aws()
_MOCK.start()

_DDB = boto3.resource("dynamodb", region_name="us-east-1")
_DDB.create_table(
    TableName=_SETTINGS_TABLE,
    KeySchema=[{"AttributeName": "setting_key", "KeyType": "HASH"}],
    AttributeDefinitions=[{"AttributeName": "setting_key",
                           "AttributeType": "S"}],
    BillingMode="PAY_PER_REQUEST",
)
_SETTINGS = _DDB.Table(_SETTINGS_TABLE)

import build_config  # noqa: E402
import build_domain  # noqa: E402
import rbac_middleware  # noqa: E402
from shared_utils import RBACManager, Role  # noqa: E402


_ADMIN = {"user_id": "portal-admin", "email": "admin@example.com",
          "username": "portal-admin"}
_OPERATOR = {"user_id": "build-operator", "email": "op@example.com",
             "username": "build-operator"}

#: The configuration parameters Req 9.1 requires the Build_Manager to
#: read from portal configuration when dispatching a Build_Job or
#: initiating a Dedicated_Build_Server launch: the instance type per CPU
#: architecture and the volume size (launches), the AWS region and the
#: ephemeral sizing (dispatch), and the maximum Build_Job runtime.
_DISPATCH_LAUNCH_PARAMETERS = (
    "arm64_instance_type",
    "x86_64_instance_type",
    "volume_size_gb",
    "region",
    "max_runtime_hours",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _event(method, body=None):
    """Minimal API Gateway event for build_config.handler routing."""
    event = {
        "resource": "/build-config",
        "httpMethod": method,
        "path": "/build-config",
    }
    if body is not None:
        event["body"] = json.dumps(body)
    return event


def _clear_settings():
    for item in _SETTINGS.scan().get("Items", []):
        _SETTINGS.delete_item(Key={"setting_key": item["setting_key"]})


def _seed_stored_config(value, updated_by="seed", updated_at=1):
    """Write the stored configuration in the nested {setting_key, value}
    item shape the handler persists (design §7)."""
    _SETTINGS.put_item(Item={
        "setting_key": build_config.BUILD_CONFIG_SETTING_KEY,
        "value": value,
        "updated_by": updated_by,
        "updated_at": updated_at,
    })


def _get_stored_item():
    return _SETTINGS.get_item(
        Key={"setting_key": build_config.BUILD_CONFIG_SETTING_KEY}
    ).get("Item")


class _RolePatches:
    """Run build_config.handler as a given portal role, capturing both
    the handler's Audit_Log calls and the RBAC middleware's denied-access
    Audit_Log calls."""

    def __init__(self, role, user):
        self._patches = [
            mock.patch.object(rbac_middleware, "get_user_from_event",
                              return_value=dict(user)),
            mock.patch.object(RBACManager, "get_user_role",
                              return_value=role),
            mock.patch.object(build_config, "get_user_from_event",
                              return_value=dict(user)),
            mock.patch.object(rbac_middleware, "log_audit_event"),
            mock.patch.object(build_config, "log_audit_event"),
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
    """PortalAdmin: allowed to change the configuration (Req 9.6)."""
    return _RolePatches(Role.PORTAL_ADMIN, _ADMIN)


def _as_operator():
    """DataScientist: a Build_Operator (builds:read) but NOT PortalAdmin,
    so configuration changes must be denied (Req 9.6)."""
    return _RolePatches(Role.DATA_SCIENTIST, _OPERATOR)


def _get_config(patches_factory=_as_operator):
    """GET /build-config and return the effective config object."""
    with patches_factory():
        response = build_config.handler(_event("GET"), None)
    assert response["statusCode"] == 200, response["body"]
    return json.loads(response["body"])["config"]


# ---------------------------------------------------------------------------
# Requirement 9.1 — config read wiring for dispatch/launch parameters
# ---------------------------------------------------------------------------

class TestConfigReadWiring:
    """GET /build-config surfaces every parameter the Build_Manager reads
    at Build_Job dispatch and Dedicated_Build_Server launch, from the
    PortalSettings key `build_infrastructure_config`, with the documented
    defaults applied per field on read (Req 9.1)."""

    def setup_method(self):
        _clear_settings()

    def test_read_returns_every_dispatch_launch_parameter_by_default(self):
        # Nothing stored: the read is wired to the documented defaults
        # for every dispatch/launch parameter (Req 9.1 read + Req 9.2
        # defaults: m6g.4xlarge / m6i.4xlarge / 200 GB / us-east-1 / 4 h;
        # the volume default was raised 100 -> 200 by the
        # build-fleet-execution-failures storage amendment, Req 2.20).
        config = _get_config()

        for parameter in _DISPATCH_LAUNCH_PARAMETERS:
            assert parameter in config, \
                f"the config read must supply {parameter}"
        assert config["arm64_instance_type"] == "m6g.4xlarge"
        assert config["x86_64_instance_type"] == "m6i.4xlarge"
        assert config["volume_size_gb"] == 200
        assert config["region"] == "us-east-1"
        assert config["max_runtime_hours"] == 4
        # The full documented parameter table is returned.
        assert config == build_domain.DEFAULT_BUILD_CONFIG

    def test_read_wires_stored_values_over_defaults_per_field(self):
        # A partially stored configuration (the persisted nested item
        # shape under key `build_infrastructure_config`): stored fields
        # are read back, absent fields keep their documented default.
        _seed_stored_config({
            "arm64_instance_type": "m7g.8xlarge",
            "volume_size_gb": 250,
        })

        config = _get_config()

        assert config["arm64_instance_type"] == "m7g.8xlarge"
        assert config["volume_size_gb"] == 250
        # Absent fields fall back to the documented defaults.
        assert config["x86_64_instance_type"] == "m6i.4xlarge"
        assert config["region"] == "us-east-1"
        assert config["max_runtime_hours"] == 4

    def test_read_reflects_an_applied_update(self):
        # Round trip: what PUT stores is exactly what the dispatch/launch
        # read path gets back (Req 9.1).
        with _as_admin():
            response = build_config.handler(
                _event("PUT", body={"region": "eu-west-1",
                                    "max_runtime_hours": 6}), None)
        assert response["statusCode"] == 200, response["body"]

        config = _get_config()
        assert config["region"] == "eu-west-1"
        assert config["max_runtime_hours"] == 6

        # The write landed under the PortalSettings key
        # `build_infrastructure_config` in the nested item shape.
        item = _get_stored_item()
        assert item is not None
        assert item["setting_key"] == "build_infrastructure_config"
        assert item["value"]["region"] == "eu-west-1"


# ---------------------------------------------------------------------------
# Requirement 9.4 — audit entry content on change
# ---------------------------------------------------------------------------

class TestConfigChangeAudit:
    """PUT /build-config records one build_config_changed Audit_Log entry
    per applied change with the changed parameter, the prior value, the
    new value, the acting user, and the time of the change (Req 9.4)."""

    def setup_method(self):
        _clear_settings()

    def test_one_audit_entry_per_applied_change_with_full_content(self):
        _seed_stored_config({"volume_size_gb": 100})

        with _as_admin() as patches:
            response = build_config.handler(
                _event("PUT", body={"volume_size_gb": 250,
                                    "arm64_instance_type": "m7g.4xlarge"}),
                None)

        assert response["statusCode"] == 200, response["body"]
        body = json.loads(response["body"])
        changes = {c["parameter"]: c for c in body["changes"]}
        assert set(changes) == {"volume_size_gb", "arm64_instance_type"}

        # One entry per applied change (Req 9.4).
        assert patches.handler_audit.call_count == 2
        audited = {}
        for call in patches.handler_audit.call_args_list:
            kwargs = call.kwargs
            assert kwargs["action"] == "build_config_changed"
            assert kwargs["resource_type"] == "build_config"
            assert kwargs["resource_id"] == "build_infrastructure_config"
            assert kwargs["result"] == "success"
            # The acting user (Req 9.4).
            assert kwargs["user_id"] == _ADMIN["user_id"]
            audited[kwargs["details"]["parameter"]] = kwargs["details"]

        assert set(audited) == {"volume_size_gb", "arm64_instance_type"}

        # The changed parameter, prior value, and new value (Req 9.4).
        assert audited["volume_size_gb"]["prior_value"] == 100
        assert audited["volume_size_gb"]["new_value"] == 250
        assert audited["arm64_instance_type"]["prior_value"] == \
            "m6g.4xlarge"
        assert audited["arm64_instance_type"]["new_value"] == \
            "m7g.4xlarge"

        # The time of the change (Req 9.4): the audited change time is
        # the stored update time, shared by every entry of the request.
        item = _get_stored_item()
        for details in audited.values():
            assert details["changed_at"] == int(item["updated_at"])

        # The response changes mirror the audited entries.
        for parameter, details in audited.items():
            assert changes[parameter] == details

        # The authorized path never records a denied-access entry.
        patches.denied_audit.assert_not_called()

    def test_supplied_but_unchanged_parameter_records_no_entry(self):
        # volume_size_gb 200 equals the documented default (raised from
        # 100 by the storage amendment, Req 2.20): supplying it changes
        # no effective value, so no change is audited (Req 9.4 audits
        # changes, not writes).
        with _as_admin() as patches:
            response = build_config.handler(
                _event("PUT", body={"volume_size_gb": 200}), None)

        assert response["statusCode"] == 200, response["body"]
        assert json.loads(response["body"])["changes"] == []
        patches.handler_audit.assert_not_called()


# ---------------------------------------------------------------------------
# Requirement 9.6 — non-PortalAdmin denial with audit
# ---------------------------------------------------------------------------

class TestNonAdminDenied:
    """PUT /build-config from a user without the PortalAdmin role is
    rejected without applying the change, returns an authorization
    error, and records a denied-access Audit_Log entry (Req 9.6)."""

    def setup_method(self):
        _clear_settings()

    def test_update_denied_audited_and_config_unchanged(self):
        _seed_stored_config({"volume_size_gb": 100})
        before = _get_stored_item()

        with _as_operator() as patches:
            response = build_config.handler(
                _event("PUT", body={"volume_size_gb": 999,
                                    "region": "eu-central-1"}), None)

        # The authorization error (Req 9.6).
        assert response["statusCode"] == 403
        body = json.loads(response["body"])
        assert body["error"] == "Super user access required"
        assert body["required_role"] == "PortalAdmin"

        # The denied-access Audit_Log entry (Req 9.6).
        patches.denied_audit.assert_called_once()
        kwargs = patches.denied_audit.call_args.kwargs
        assert kwargs["user_id"] == _OPERATOR["user_id"]
        assert kwargs["action"] == "unauthorized_super_user_access"
        assert kwargs["resource_type"] == "api_endpoint"
        assert kwargs["resource_id"] == "/build-config"
        assert kwargs["result"] == "denied"

        # The change was not applied: the stored item is byte-identical
        # and the effective read is unchanged.
        assert _get_stored_item() == before
        config = _get_config()
        assert config["volume_size_gb"] == 100
        assert config["region"] == "us-east-1"

        # No build_config_changed entry is recorded on a denial.
        patches.handler_audit.assert_not_called()
