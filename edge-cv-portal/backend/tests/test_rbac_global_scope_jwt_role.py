"""
JWT-role propagation through the RBAC middleware at global scope
(build-fleet 403 bugfix).

Bug: rbac_middleware.rbac_check calls rbac_manager.has_permission(...)
WITHOUT the user_info kwarg, so the JWT custom:role claim (extracted by
get_user_from_event into user['role']) never reaches role resolution.
For allow_global=True routes the scope is 'global', which has no
per-usecase UserRoles row for JWT-only users, so the role defaults to
Viewer and the request is denied with 403 "Insufficient permissions".
Live evidence: a Cognito PortalAdmin (custom:role=PortalAdmin, no
UserRoles rows) denied builds:read on the deployed BuildFleetHandler.
super_user_only shares the gap via is_portal_admin without user_info.

These tests must FAIL on unfixed code (403) and pass once rbac_check /
super_user_only thread the already-extracted user dict as user_info
into every rbac_manager call.

Runs against the moto-backed stack from conftest.py, exercising the
real shared_utils role resolution (no RBACManager patching): the users
below have NO dda-portal-user-roles rows, only the JWT claim.
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
    """A user with a JWT role claim and NO UserRoles table rows."""
    user_id = f"user-{uuid.uuid4()}"
    return {
        "user_id": user_id,
        "email": f"{user_id}@example.com",
        "username": user_id,
        "role": role,
    }


def api_event(user, method="GET", resource="/builds", body=None):
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


def ok_handler(event, context):
    return {"statusCode": 200, "body": json.dumps({"ok": True})}


# ------------------------------------------- rbac_check at global scope

class TestGlobalScopeJwtRole:
    """A user whose ONLY role source is the JWT custom:role claim must
    be authorized by rbac_check(..., allow_global=True) when that role
    holds the required permission (the live build-fleet 403)."""

    def test_jwt_portal_admin_authorized_for_builds_read(
            self, shared, middleware):
        """custom:role=PortalAdmin, no UserRoles rows -> builds:read at
        global scope must be AUTHORIZED (was 403 on unfixed code)."""
        admin = make_user(role="PortalAdmin")

        decorated = middleware.rbac_check(
            [shared.Permission.BUILDS_READ], allow_global=True)(ok_handler)
        response = decorated(api_event(admin), None)

        assert response["statusCode"] == 200, (
            "JWT PortalAdmin denied builds:read at global scope: "
            f"{response['body']}"
        )

    def test_jwt_data_scientist_authorized_for_builds_submit(
            self, shared, middleware):
        """custom:role=DataScientist (a Build_Operator role), no
        UserRoles rows -> builds:submit at global scope must be
        AUTHORIZED via the JWT-role fallback."""
        scientist = make_user(role="DataScientist")

        decorated = middleware.rbac_check(
            [shared.Permission.BUILDS_SUBMIT], allow_global=True)(ok_handler)
        response = decorated(
            api_event(scientist, method="POST", resource="/builds"), None)

        assert response["statusCode"] == 200, (
            "JWT DataScientist denied builds:submit at global scope: "
            f"{response['body']}"
        )

    def test_jwt_viewer_still_denied(self, shared, middleware):
        """The fix must not over-grant: a JWT Viewer (holds no build
        permissions) is still denied at global scope."""
        viewer = make_user(role="Viewer")

        decorated = middleware.rbac_check(
            [shared.Permission.BUILDS_READ], allow_global=True)(ok_handler)
        response = decorated(api_event(viewer), None)

        assert response["statusCode"] == 403
        body = json.loads(response["body"])
        assert body["error"] == "Insufficient permissions"


# ------------------------------------------------------ super_user_only

class TestSuperUserOnlyJwtRole:
    """super_user_only must honor the JWT PortalAdmin claim (same
    user_info gap through rbac_manager.is_portal_admin)."""

    def test_jwt_portal_admin_passes_super_user_only(self, middleware):
        admin = make_user(role="PortalAdmin")

        decorated = middleware.super_user_only(ok_handler)
        response = decorated(
            api_event(admin, resource="/admin/settings"), None)

        assert response["statusCode"] == 200, (
            "JWT PortalAdmin denied by super_user_only: "
            f"{response['body']}"
        )

    def test_jwt_viewer_denied_by_super_user_only(self, middleware):
        viewer = make_user(role="Viewer")

        decorated = middleware.super_user_only(ok_handler)
        response = decorated(
            api_event(viewer, resource="/admin/settings"), None)

        assert response["statusCode"] == 403
