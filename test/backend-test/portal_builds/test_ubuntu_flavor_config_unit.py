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
Unit tests for the ``ubuntu_flavor`` build-config plumbing
(ubuntu-pro-build-servers task 1.6).

**Validates: Requirements 6.4**

Covers:

- ``ubuntu_flavor`` is present in ``build_config.KNOWN_PARAMETERS`` via
  ``build_domain.DEFAULT_BUILD_CONFIG`` with no ``build_config.py``
  change (the ``test_default_repository_config.py`` one-parameter-table
  pattern), and the documented default is ``standard``.
- ``GET /build-config`` returns ``ubuntu_flavor: 'standard'`` when the
  configuration has never been written (no default stored is treated as
  ``standard``, Req 6.4).

The PortalSettings table is moto-mocked; the real rbac_middleware
decorators and build_domain functions run unstubbed. Only the RBAC role
lookup, ``get_user_from_event``, and ``log_audit_event`` are patched per
test (the sibling pattern in test_default_repository_config.py).
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

_SETTINGS_TABLE = "portal-settings-ubuntu-flavor"
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
# the sibling test_default_repository_config.py).
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
for _module in ("build_config", "build_domain", "build_source",
                "rbac_middleware", "shared_utils"):
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


# ---------------------------------------------------------------------------
# Helpers (sibling pattern: test_default_repository_config.py)
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
# One parameter-table definition (the default_repository precedent)
# ---------------------------------------------------------------------------

class TestUbuntuFlavorParameterTable:
    """ubuntu_flavor enters KNOWN_PARAMETERS via DEFAULT_BUILD_CONFIG
    with no build_config.py change, defaulting to 'standard'."""

    def test_known_parameters_carry_ubuntu_flavor(self):
        assert "ubuntu_flavor" in build_config.KNOWN_PARAMETERS
        assert set(build_config.KNOWN_PARAMETERS) == \
            set(build_domain.DEFAULT_BUILD_CONFIG)

    def test_default_table_documents_standard(self):
        assert build_domain.DEFAULT_BUILD_CONFIG["ubuntu_flavor"] == \
            build_domain.UBUNTU_FLAVOR_STANDARD == "standard"

    def test_effective_config_defaults_to_standard(self):
        # No default stored is treated as standard (Req 6.4): never
        # written, field absent, and field stored as None all read back
        # as 'standard'.
        for stored in (None, {}, {"volume_size_gb": 250},
                       {"ubuntu_flavor": None}):
            config = build_domain.effective_build_config(stored)
            assert config["ubuntu_flavor"] == "standard", stored


# ---------------------------------------------------------------------------
# GET /build-config returns 'standard' when unconfigured (Req 6.4)
# ---------------------------------------------------------------------------

class TestGetBuildConfigUbuntuFlavor:
    """GET /build-config reports the effective ubuntu_flavor 'standard'
    when the configuration has never been written."""

    def setup_method(self):
        _clear_settings()

    def test_get_returns_standard_when_never_configured(self):
        with _AsAdmin():
            response = build_config.handler(_event("GET"), None)

        assert response["statusCode"] == 200, response["body"]
        config = json.loads(response["body"])["config"]
        assert config["ubuntu_flavor"] == "standard"
