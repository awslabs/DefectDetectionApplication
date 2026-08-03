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
Unit tests for the build fleet RBAC registration (task 7.1 of
portal-build-fleet-and-workflow-gates).

Validates: Requirements 1.6, 4.10, 6.7, 9.6

Covers:
- ``builds:submit`` / ``builds:cancel`` / ``builds:read`` are registered in
  the shared_utils ``Permission`` enum.
- Grant matrix: DataScientist, UseCaseAdmin, and PortalAdmin hold the
  Build_Operator permissions; Viewer and Operator hold none of them
  (``builds:read`` follows Build_Operator, per design).
- ``rbac_middleware`` exposes the build permission sets and global-scope
  decorators (``allow_global`` pattern).
- Denials return the standard authorization error (403) and record a
  denied-access Audit_Log entry.
"""
import json
import os
import sys
from unittest import mock

# shared_utils instantiates a boto3 resource at import time; a region must be
# configured before the module is imported.
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

# Import boto3 (and thus botocore/urllib3) from the test environment BEFORE
# the Lambda layer directory joins sys.path: the layer vendors its own
# urllib3 build targeting the Lambda Python runtime, which must not shadow
# the environment's copy.
import boto3  # noqa: E402,F401

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.normpath(os.path.join(_HERE, "..", "..", ".."))
_LAYER_DIR = os.path.join(
    _REPO_ROOT, "edge-cv-portal", "backend", "layers", "shared", "python"
)
_FUNCTIONS_DIR = os.path.join(_REPO_ROOT, "edge-cv-portal", "backend", "functions")
for _p in (_LAYER_DIR, _FUNCTIONS_DIR):
    if _p not in sys.path:
        sys.path.append(_p)

import shared_utils  # noqa: E402
import rbac_middleware  # noqa: E402
from shared_utils import Permission, RBACManager, Role  # noqa: E402


_BUILD_PERMISSIONS = {
    Permission.BUILDS_SUBMIT,
    Permission.BUILDS_CANCEL,
    Permission.BUILDS_READ,
}


class TestPermissionRegistration:
    """builds:* permissions are registered in the shared_utils Permission enum."""

    def test_permission_values(self):
        assert Permission.BUILDS_SUBMIT.value == "builds:submit"
        assert Permission.BUILDS_CANCEL.value == "builds:cancel"
        assert Permission.BUILDS_READ.value == "builds:read"


class TestGrantMatrix:
    """DataScientist, UseCaseAdmin, PortalAdmin -> Build_Operator (Req 1.6, 4.10)."""

    def setup_method(self):
        self.role_permissions = RBACManager().role_permissions

    def test_data_scientist_is_build_operator(self):
        assert _BUILD_PERMISSIONS <= self.role_permissions[Role.DATA_SCIENTIST]

    def test_usecase_admin_is_build_operator(self):
        assert _BUILD_PERMISSIONS <= self.role_permissions[Role.USECASE_ADMIN]

    def test_portal_admin_is_build_operator(self):
        assert _BUILD_PERMISSIONS <= self.role_permissions[Role.PORTAL_ADMIN]

    def test_viewer_has_no_build_permissions(self):
        assert not (_BUILD_PERMISSIONS & self.role_permissions[Role.VIEWER])

    def test_operator_has_no_build_permissions(self):
        # builds:read follows Build_Operator (design: read is NOT granted to
        # every authenticated role).
        assert not (_BUILD_PERMISSIONS & self.role_permissions[Role.OPERATOR])


class TestMiddlewareWiring:
    """rbac_middleware exposes the build permission sets and decorators."""

    def test_common_permission_sets(self):
        cp = rbac_middleware.CommonPermissions
        assert cp.SUBMIT_BUILDS == [Permission.BUILDS_SUBMIT]
        assert cp.CANCEL_BUILDS == [Permission.BUILDS_CANCEL]
        assert cp.VIEW_BUILDS == [Permission.BUILDS_READ]

    def test_global_scope_decorators_exist(self):
        # Each decorator wraps a handler; they take no usecase parameter
        # because builds are checked at the 'global' scope (allow_global).
        for factory in (
            rbac_middleware.require_builds_submit,
            rbac_middleware.require_builds_cancel,
            rbac_middleware.require_builds_read,
        ):
            decorator = factory()
            assert callable(decorator)


def _invoke_with_role(role, permissions):
    """Run a handler behind rbac_check(permissions, allow_global=True) as `role`."""
    handler = mock.Mock(return_value={"statusCode": 200, "body": "{}"})
    decorated = rbac_middleware.rbac_check(permissions, allow_global=True)(handler)

    event = {
        "resource": "/builds",
        "httpMethod": "POST",
        "path": "/builds",
    }
    with mock.patch.object(
        rbac_middleware, "get_user_from_event", return_value={"user_id": "test-user"}
    ), mock.patch.object(
        RBACManager, "get_user_role", return_value=role
    ), mock.patch.object(
        rbac_middleware, "log_audit_event"
    ) as audit:
        response = decorated(event, None)
    return response, handler, audit


class TestDenialBehavior:
    """Denials: standard authorization error + denied-access Audit_Log entry
    (Req 1.6, 4.10, 9.6)."""

    def test_viewer_denied_with_authorization_error_and_audit(self):
        response, handler, audit = _invoke_with_role(
            Role.VIEWER, rbac_middleware.CommonPermissions.SUBMIT_BUILDS
        )

        # Standard authorization error envelope
        assert response["statusCode"] == 403
        body = json.loads(response["body"])
        assert body["error"] == "Insufficient permissions"
        assert body["required_permissions"] == ["builds:submit"]
        handler.assert_not_called()

        # Denied-access Audit_Log entry
        audit.assert_called_once()
        kwargs = audit.call_args.kwargs
        assert kwargs["user_id"] == "test-user"
        assert kwargs["action"] == "unauthorized_access"
        assert kwargs["result"] == "denied"
        assert kwargs["details"]["required_permissions"] == ["builds:submit"]
        assert kwargs["details"]["usecase_id"] == "global"

    def test_data_scientist_allowed_at_global_scope(self):
        response, handler, audit = _invoke_with_role(
            Role.DATA_SCIENTIST, rbac_middleware.CommonPermissions.SUBMIT_BUILDS
        )

        assert response["statusCode"] == 200
        handler.assert_called_once()
        audit.assert_not_called()
