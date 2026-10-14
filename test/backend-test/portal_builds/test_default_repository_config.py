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
Unit tests for the operator-controlled ``default_repository`` build-config
parameter (task 8 of build-source-selection).

**Validates: Requirements 1.5, 7.5**

Covers:

- ``build_domain.effective_build_config`` returns the stored
  ``default_repository`` when present and the documented default
  (``https://github.com/awslabs/DefectDetectionApplication``) when the
  field is absent or stored as ``None`` (Req 1.5).
- ``build_domain.validate_build_config`` gains
  ``RULE_CONFIG_REPOSITORY_INVALID``, delegating to
  ``build_source.normalize_repository_url``: a valid HTTPS GitHub remote
  is accepted, an invalid value is rejected naming the rule and the
  parameter, and a rejected update is discarded in full (atomic reject).
- ``PUT /build-config`` accepts a valid repository and rejects an invalid
  one atomically with the existing ``CONFIG_INVALID`` envelope, recording
  one ``build_config_changed`` Audit_Log entry per applied change and
  none on rejection.
- The parameter table has ONE definition:
  ``build_jobs.DEFAULT_BUILD_CONFIG`` and
  ``build_fleet.DEFAULT_BUILD_CONFIG`` are the same object as
  ``build_domain.DEFAULT_BUILD_CONFIG``, and
  ``build_config.KNOWN_PARAMETERS`` therefore carries
  ``default_repository`` with no ``build_config.py`` change (Req 7.5:
  the snapshot/config table is extended, not restructured).

The PortalSettings table is moto-mocked; the real rbac_middleware
decorators and build_domain functions run unstubbed. Only the RBAC role
lookup, ``get_user_from_event``, and ``log_audit_event`` are patched per
test (the sibling pattern in test_build_config_rbac_and_audit.py).
"""
import json
import os
import sys
import types
from unittest import mock

# ---------------------------------------------------------------------------
# Environment BEFORE any import: shared_utils and the handler modules bind
# their boto3 resources/clients and table names at import time.
# ---------------------------------------------------------------------------
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ["AWS_ACCESS_KEY_ID"] = "testing"
os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
os.environ["AWS_SECURITY_TOKEN"] = "testing"
os.environ["AWS_SESSION_TOKEN"] = "testing"

_SETTINGS_TABLE = "portal-settings-t8-default-repo"
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
# the sibling test_build_config_rbac_and_audit.py).
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

# Fresh modules so the handlers' module-level boto3 handles are created
# under the moto mock started below (sibling pattern).
for _module in ("build_config", "build_jobs", "build_fleet", "build_domain",
                "build_source", "rbac_middleware", "shared_utils"):
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
import build_fleet  # noqa: E402
import build_jobs  # noqa: E402
import build_source  # noqa: E402
import rbac_middleware  # noqa: E402
from shared_utils import RBACManager, Role  # noqa: E402


#: The documented default (Req 1.5), restated independently of the module.
_DOCUMENTED_DEFAULT_REPOSITORY = \
    "https://github.com/awslabs/DefectDetectionApplication"

_ADMIN = {"user_id": "portal-admin", "email": "admin@example.com",
          "username": "portal-admin"}


# ---------------------------------------------------------------------------
# Helpers (sibling pattern: test_build_config_rbac_and_audit.py)
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


def _get_stored_item():
    return _SETTINGS.get_item(
        Key={"setting_key": build_config.BUILD_CONFIG_SETTING_KEY}
    ).get("Item")


class _AsAdmin:
    """Run build_config.handler as PortalAdmin, capturing the handler's
    Audit_Log calls."""

    def __init__(self):
        self._patches = [
            mock.patch.object(rbac_middleware, "get_user_from_event",
                              return_value=dict(_ADMIN)),
            mock.patch.object(RBACManager, "get_user_role",
                              return_value=Role.PORTAL_ADMIN),
            mock.patch.object(build_config, "get_user_from_event",
                              return_value=dict(_ADMIN)),
            mock.patch.object(rbac_middleware, "log_audit_event"),
            mock.patch.object(build_config, "log_audit_event"),
        ]

    def __enter__(self):
        started = [p.start() for p in self._patches]
        self.handler_audit = started[4]
        return self

    def __exit__(self, *exc):
        for p in self._patches:
            p.stop()
        return False


# ---------------------------------------------------------------------------
# effective_build_config: stored value / documented default (Req 1.5)
# ---------------------------------------------------------------------------

class TestEffectiveDefaultRepository:
    """effective_build_config returns the stored default_repository when
    present and the documented default when absent or None."""

    def test_default_when_nothing_stored(self):
        config = build_domain.effective_build_config(None)
        assert config["default_repository"] == \
            _DOCUMENTED_DEFAULT_REPOSITORY

    def test_default_when_field_absent(self):
        config = build_domain.effective_build_config(
            {"volume_size_gb": 250})
        assert config["default_repository"] == \
            _DOCUMENTED_DEFAULT_REPOSITORY

    def test_default_when_field_stored_as_none(self):
        config = build_domain.effective_build_config(
            {"default_repository": None})
        assert config["default_repository"] == \
            _DOCUMENTED_DEFAULT_REPOSITORY

    def test_stored_value_when_present(self):
        stored = {"default_repository": "https://github.com/someone/fork"}
        config = build_domain.effective_build_config(stored)
        assert config["default_repository"] == \
            "https://github.com/someone/fork"

    def test_default_table_carries_the_parameter(self):
        assert build_domain.DEFAULT_BUILD_CONFIG["default_repository"] == \
            _DOCUMENTED_DEFAULT_REPOSITORY


# ---------------------------------------------------------------------------
# validate_build_config: RULE_CONFIG_REPOSITORY_INVALID (Req 1.5)
# ---------------------------------------------------------------------------

class TestValidateDefaultRepository:
    """validate_build_config delegates default_repository to
    build_source.normalize_repository_url, naming the rule and the
    parameter on rejection; a rejected update is discarded in full."""

    def test_valid_repository_accepted(self):
        for value in (
            "https://github.com/awslabs/DefectDetectionApplication",
            "https://github.com/awslabs/DefectDetectionApplication.git",
            "https://github.com/someone/fork/",
        ):
            result = build_domain.validate_build_config(
                {"default_repository": value})
            assert result.valid, (value, result.errors)

    def test_none_reverts_to_default_and_is_not_validated(self):
        result = build_domain.validate_build_config(
            {"default_repository": None})
        assert result.valid, result.errors

    def test_invalid_repository_rejected_naming_rule_and_parameter(self):
        for value in (
            "http://github.com/owner/repo",          # not https
            "git@github.com:owner/repo.git",          # scp-style remote
            "https://gitlab.com/owner/repo",          # non-allowlisted host
            "https://github.com/owner/repo/tree/main",  # extra segments
            "https://github.com/owner",               # missing repository
            "not a url",
            42,
        ):
            result = build_domain.validate_build_config(
                {"default_repository": value})
            assert not result.valid, value
            assert len(result.errors) == 1, (value, result.errors)
            error = result.errors[0]
            assert error["rule"] == \
                build_domain.RULE_CONFIG_REPOSITORY_INVALID, error
            assert error["parameter"] == "default_repository", error
            assert error["message"], error

    def test_rejection_delegates_to_normalize_repository_url(self):
        # The acceptance decision is exactly build_source's: any value the
        # normalizer rejects is rejected here, any value it accepts is
        # accepted here.
        for value in ("https://github.com/a/b", "ftp://github.com/a/b",
                      "https://github.com/a/b?x=1", ""):
            _, source_error = build_source.normalize_repository_url(value)
            result = build_domain.validate_build_config(
                {"default_repository": value})
            assert result.valid == (source_error is None), (value,
                                                            result.errors)

    def test_rejected_update_is_discarded_in_full(self):
        # Atomic reject: the individually valid volume_size_gb is NOT
        # applied when default_repository is rejected.
        stored = {"volume_size_gb": 100}
        new_stored, result = build_domain.apply_config_update(
            stored, {"volume_size_gb": 250,
                     "default_repository": "http://github.com/a/b"})
        assert not result.valid
        assert new_stored == {"volume_size_gb": 100}


# ---------------------------------------------------------------------------
# One parameter-table definition (design B1 / decisions item 5, Req 7.5)
# ---------------------------------------------------------------------------

class TestOneParameterTable:
    """build_jobs and build_fleet point at build_domain's table, and
    build_config.KNOWN_PARAMETERS picks the new parameter up with no
    build_config.py change."""

    def test_build_jobs_table_is_the_domain_table(self):
        assert build_jobs.DEFAULT_BUILD_CONFIG is \
            build_domain.DEFAULT_BUILD_CONFIG

    def test_build_fleet_table_is_the_domain_table(self):
        assert build_fleet.DEFAULT_BUILD_CONFIG is \
            build_domain.DEFAULT_BUILD_CONFIG

    def test_known_parameters_carry_default_repository(self):
        assert "default_repository" in build_config.KNOWN_PARAMETERS
        assert set(build_config.KNOWN_PARAMETERS) == \
            set(build_domain.DEFAULT_BUILD_CONFIG)


# ---------------------------------------------------------------------------
# PUT /build-config: accept valid, reject invalid atomically (Req 1.5)
# ---------------------------------------------------------------------------

class TestPutBuildConfigRepository:
    """PUT /build-config accepts a valid repository (one audit entry per
    applied change) and rejects an invalid one atomically with the
    existing CONFIG_INVALID envelope and no audit entry."""

    def setup_method(self):
        _clear_settings()

    def test_valid_repository_accepted_with_one_audit_entry(self):
        fork = "https://github.com/someone/fork"
        with _AsAdmin() as patches:
            response = build_config.handler(
                _event("PUT", body={"default_repository": fork}), None)

        assert response["statusCode"] == 200, response["body"]
        body = json.loads(response["body"])
        assert body["config"]["default_repository"] == fork

        # One build_config_changed Audit_Log entry for the one applied
        # change, carrying the parameter, prior value, and new value.
        assert patches.handler_audit.call_count == 1
        kwargs = patches.handler_audit.call_args.kwargs
        assert kwargs["action"] == "build_config_changed"
        assert kwargs["user_id"] == _ADMIN["user_id"]
        assert kwargs["details"]["parameter"] == "default_repository"
        assert kwargs["details"]["prior_value"] == \
            _DOCUMENTED_DEFAULT_REPOSITORY
        assert kwargs["details"]["new_value"] == fork

        # The value landed in PortalSettings and reads back effectively.
        item = _get_stored_item()
        assert item["value"]["default_repository"] == fork
        with _AsAdmin():
            get_response = build_config.handler(_event("GET"), None)
        assert json.loads(get_response["body"])["config"][
            "default_repository"] == fork

    def test_invalid_repository_rejected_atomically_with_envelope(self):
        # Seed a stored configuration; the rejected update must leave it
        # byte-identical, including the individually valid volume_size_gb.
        with _AsAdmin():
            seed = build_config.handler(
                _event("PUT", body={"volume_size_gb": 150}), None)
        assert seed["statusCode"] == 200, seed["body"]
        before = _get_stored_item()

        with _AsAdmin() as patches:
            response = build_config.handler(
                _event("PUT", body={
                    "default_repository": "git@github.com:owner/repo.git",
                    "volume_size_gb": 250,
                }), None)

        # The existing error envelope: {error: {code, message, details}}.
        assert response["statusCode"] == 400, response["body"]
        body = json.loads(response["body"])
        assert body["error"]["code"] == "CONFIG_INVALID"
        errors = body["error"]["details"]["errors"]
        assert len(errors) == 1, errors
        assert errors[0]["rule"] == \
            build_domain.RULE_CONFIG_REPOSITORY_INVALID
        assert errors[0]["parameter"] == "default_repository"

        # Atomic reject: nothing applied, nothing audited.
        assert _get_stored_item() == before
        patches.handler_audit.assert_not_called()
        with _AsAdmin():
            get_response = build_config.handler(_event("GET"), None)
        config = json.loads(get_response["body"])["config"]
        assert config["default_repository"] == \
            _DOCUMENTED_DEFAULT_REPOSITORY
        assert config["volume_size_gb"] == 150
