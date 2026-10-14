"""
RBAC registration tests for the build fleet permissions
(portal-build-fleet-and-workflow-gates task 7.1).

Covers Requirements 1.6, 4.10, 6.7, 9.6:

1. The three build permissions (builds:submit, builds:cancel,
   builds:read) are registered in the shared_utils Permission enum.

2. Grant matrix at the 'global' scope (builds are not Use_Case-scoped;
   the allow_global pattern from rbac_middleware.py applies):
   DataScientist, UseCaseAdmin, and PortalAdmin hold the Build_Operator
   capability (submit/cancel/read); Viewer and Operator hold none of it.

3. The standard RBAC denial behavior applies to the new permissions:
   a denied request returns the standard authorization error and
   records a denied-access (unauthorized_access) Audit_Log entry.

Runs against the moto-backed stack from conftest.py, exercising the
real RBAC / audit code paths.
"""
import json
import sys
import uuid

import pytest


# --------------------------------------------------------------- fixtures

@pytest.fixture
def shared(aws_stack):
    """The real shared_utils module imported inside the moto mock."""
    import shared_utils
    return shared_utils


@pytest.fixture
def middleware(aws_stack):
    """The real rbac_middleware module, re-imported inside the moto
    mock so it binds the same shared_utils (Permission / rbac_manager)
    the stack was built with."""
    sys.modules.pop("rbac_middleware", None)
    import rbac_middleware
    return rbac_middleware


def make_user(role="Viewer"):
    user_id = f"user-{uuid.uuid4()}"
    return {
        "user_id": user_id,
        "email": f"{user_id}@example.com",
        "username": user_id,
        "role": role,
    }


def api_event(user, method="POST", resource="/builds", body=None):
    """Synthetic API Gateway event with Cognito claims (no usecase_id:
    build routes are global-scope)."""
    return {
        "httpMethod": method,
        "resource": resource,
        "path": resource,
        "pathParameters": None,
        "queryStringParameters": None,
        "body": json.dumps(body) if body is not None else None,
        "requestContext": {
            "authorizer": {
                "claims": {
                    "sub": user["user_id"],
                    "email": user["email"],
                    "cognito:username": user["username"],
                    "custom:role": user["role"],
                }
            }
        },
    }


def audit_entries(aws_stack, user_id, action):
    """All audit entries for user_id with the given action."""
    items = aws_stack.tables.audit_log.scan()["Items"]
    return [e for e in items
            if e["user_id"] == user_id and e["action"] == action]


# ------------------------------------------------- registration and grants

BUILD_PERMISSION_VALUES = ("builds:submit", "builds:cancel", "builds:read")

ROLES = ("Viewer", "Operator", "DataScientist", "UseCaseAdmin", "PortalAdmin")

# Build_Operator grant matrix (design: DataScientist, UseCaseAdmin,
# PortalAdmin -> Build_Operator; builds:read follows Build_Operator too,
# it is NOT granted to every role).
BUILD_OPERATOR_ROLES = {"DataScientist", "UseCaseAdmin", "PortalAdmin"}


class TestPermissionRegistration:
    """builds:submit / builds:cancel / builds:read are registered."""

    @pytest.mark.parametrize("value", BUILD_PERMISSION_VALUES)
    def test_permission_registered(self, shared, value):
        assert shared.Permission(value).value == value

    def test_enum_members(self, shared):
        assert shared.Permission.BUILDS_SUBMIT.value == "builds:submit"
        assert shared.Permission.BUILDS_CANCEL.value == "builds:cancel"
        assert shared.Permission.BUILDS_READ.value == "builds:read"


class TestGrantMatrix:
    """Role x build-permission resolution at the 'global' scope
    (builds are not Use_Case-scoped, Req 1.6, 4.10)."""

    @pytest.mark.parametrize("role", ROLES)
    @pytest.mark.parametrize("value", BUILD_PERMISSION_VALUES)
    def test_role_permission(self, shared, role, value):
        user = make_user(role=role)
        permission = shared.Permission(value)
        expected = role in BUILD_OPERATOR_ROLES
        granted = shared.rbac_manager.has_permission(
            user["user_id"], "global", permission, user_info=user)
        assert granted is expected, (
            f"{role} {'should' if expected else 'should not'} "
            f"hold {value} at global scope"
        )

    def test_portal_admin_from_dynamodb_global_role(self, aws_stack, shared):
        """PortalAdmin assigned via the UserRoles 'global' item (not the
        JWT claim) also holds the build permissions."""
        user = make_user(role="Viewer")
        aws_stack.tables.user_roles.put_item(Item={
            "user_id": user["user_id"],
            "usecase_id": "global",
            "role": "PortalAdmin",
        })
        for value in BUILD_PERMISSION_VALUES:
            assert shared.rbac_manager.has_permission(
                user["user_id"], "global", shared.Permission(value),
                user_info=user)


# --------------------------------------------- standard denial behavior

class TestMiddlewareDenialBehavior:
    """The standard RBAC denial behavior applies to the new permissions
    through rbac_check(..., allow_global=True): the standard
    authorization error is returned and a denied-access Audit_Log entry
    is recorded (Req 1.6, 4.10, 6.7, 9.6)."""

    @pytest.mark.parametrize("permission_name,resource", [
        ("BUILDS_SUBMIT", "/builds"),
        ("BUILDS_CANCEL", "/builds/{id}/cancel"),
        ("BUILDS_READ", "/builds"),
    ])
    def test_denial_returns_authorization_error_and_audits(
            self, aws_stack, shared, middleware, permission_name, resource):
        permission = getattr(shared.Permission, permission_name)

        @middleware.rbac_check([permission], allow_global=True)
        def handler(event, context):
            return {"statusCode": 200, "body": json.dumps({"ok": True})}

        viewer = make_user(role="Viewer")
        response = handler(api_event(viewer, resource=resource), None)

        # Standard authorization error
        assert response["statusCode"] == 403
        body = json.loads(response["body"])
        assert body["error"] == "Insufficient permissions"
        assert body["required_permissions"] == [permission.value]
        assert body["usecase_id"] == "global"

        # Denied-access Audit_Log entry
        entries = audit_entries(aws_stack, viewer["user_id"],
                                "unauthorized_access")
        assert len(entries) == 1
        entry = entries[0]
        assert entry["result"] == "denied"
        assert entry["timestamp"] > 0
        assert entry["details"]["required_permissions"] == [permission.value]
        assert entry["details"]["usecase_id"] == "global"

    def test_portal_admin_passes_middleware_check(
            self, aws_stack, shared, middleware):
        """A PortalAdmin (UserRoles 'global' assignment) passes the
        global-scope check, so the wiring grants and not just denies."""
        admin = make_user(role="PortalAdmin")
        aws_stack.tables.user_roles.put_item(Item={
            "user_id": admin["user_id"],
            "usecase_id": "global",
            "role": "PortalAdmin",
        })

        @middleware.rbac_check([shared.Permission.BUILDS_SUBMIT],
                               allow_global=True)
        def handler(event, context):
            return {"statusCode": 200, "body": json.dumps({"ok": True})}

        response = handler(api_event(admin), None)
        assert response["statusCode"] == 200
