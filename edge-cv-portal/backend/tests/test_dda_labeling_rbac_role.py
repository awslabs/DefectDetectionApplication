"""
Data_Labeler role and labeling permissions in RBAC
(dda-data-labeling, task 2.1).

Feature: dda-data-labeling

Covers:
- Role.DATA_LABELER / Permission.LABELING_TASKS_SELF /
  Permission.MANAGE_LABELING_TEAMS exist in shared_utils (Req 2.1)
- _initialize_role_permissions(): DATA_LABELER -> {LABELING_TASKS_SELF}
  only; LABELING_TASKS_SELF also granted to DataScientist / UseCaseAdmin /
  PortalAdmin; MANAGE_LABELING_TEAMS granted to UseCaseAdmin / PortalAdmin
  only (Req 2.3, 3.7)
- DataLabeler accepted as a valid role value by user_admin.py
  (PUT /admin/users/{username}/role, account creation) and by the
  rbac_utils.Role enum user_roles.py validates against (Req 2.1)
- Per-request enforcement through the existing @rbac_check path: a
  DataLabeler-only caller gets 403 on a non-labeler endpoint with an
  unauthorized_access audit event carrying user, resource, and timestamp
  (Req 2.3), and is authorized on LABELING_TASKS_SELF routes (Req 2.5:
  identity comes from the existing Cognito claims).

Runs against the moto-backed stack from conftest.py, exercising the real
shared_utils role resolution (no RBACManager patching).
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
    """The real rbac_middleware module, re-imported inside the moto mock
    so it binds the same shared_utils the stack was built with."""
    sys.modules.pop("rbac_middleware", None)
    import rbac_middleware
    return rbac_middleware


def make_user(role="DataLabeler"):
    """A user with a JWT role claim and NO UserRoles table rows."""
    user_id = f"user-{uuid.uuid4()}"
    return {
        "user_id": user_id,
        "email": f"{user_id}@example.com",
        "username": user_id,
        "role": role,
    }


def api_event(user, method="GET", resource="/datasets", usecase_id="uc-1"):
    """Synthetic API Gateway event with Cognito claims."""
    return {
        "httpMethod": method,
        "resource": resource,
        "path": resource,
        "pathParameters": None,
        "queryStringParameters": {"usecase_id": usecase_id},
        "body": None,
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


def audit_entries_for(aws_stack, user_id):
    """All audit-log items recorded for the given user."""
    items = aws_stack.tables.audit_log.scan().get("Items", [])
    return [item for item in items if item.get("user_id") == user_id]


# ------------------------------------------------- enum / mapping shape

class TestRoleAndPermissionDefinitions:
    def test_data_labeler_role_value(self, shared):
        """Req 2.1: the Data_Labeler role exists with value 'DataLabeler'."""
        assert shared.Role.DATA_LABELER.value == "DataLabeler"
        assert shared.Role("DataLabeler") is shared.Role.DATA_LABELER

    def test_labeling_permission_values(self, shared):
        assert shared.Permission.LABELING_TASKS_SELF.value == "labeling:tasks-self"
        assert shared.Permission.MANAGE_LABELING_TEAMS.value == "labeling-teams:manage"

    def test_data_labeler_holds_only_labeling_tasks_self(self, shared):
        """Req 2.3: DATA_LABELER -> {LABELING_TASKS_SELF} and nothing else."""
        perms = shared.rbac_manager.role_permissions[shared.Role.DATA_LABELER]
        assert perms == {shared.Permission.LABELING_TASKS_SELF}

    @pytest.mark.parametrize("role", ["DATA_SCIENTIST", "USECASE_ADMIN",
                                      "PORTAL_ADMIN"])
    def test_labeling_tasks_self_granted_to_job_creator_roles(self, shared, role):
        perms = shared.rbac_manager.role_permissions[getattr(shared.Role, role)]
        assert shared.Permission.LABELING_TASKS_SELF in perms

    @pytest.mark.parametrize("role", ["USECASE_ADMIN", "PORTAL_ADMIN"])
    def test_manage_labeling_teams_granted_to_admins(self, shared, role):
        """Req 3.7: team management is admin-only."""
        perms = shared.rbac_manager.role_permissions[getattr(shared.Role, role)]
        assert shared.Permission.MANAGE_LABELING_TEAMS in perms

    @pytest.mark.parametrize("role", ["VIEWER", "OPERATOR", "DATA_SCIENTIST",
                                      "DATA_LABELER"])
    def test_manage_labeling_teams_denied_to_non_admins(self, shared, role):
        """Req 3.7: non-admin roles hold no team-management permission."""
        perms = shared.rbac_manager.role_permissions[getattr(shared.Role, role)]
        assert shared.Permission.MANAGE_LABELING_TEAMS not in perms


# --------------------------------------------- role administration values

class TestRoleAdministrationAcceptsDataLabeler:
    def test_user_admin_portal_roles_include_data_labeler(self, aws_stack):
        """Req 2.1: PUT /admin/users/{username}/role and account creation
        accept DataLabeler (validated against PORTAL_ROLES)."""
        sys.modules.pop("user_admin", None)
        import user_admin
        assert "DataLabeler" in user_admin.PORTAL_ROLES
        rejection = user_admin.validate_create_request({
            "username": "labeler-1",
            "email": "labeler-1@example.com",
            "role": "DataLabeler",
        })
        assert rejection is None

    def test_rbac_utils_role_enum_accepts_data_labeler(self, aws_stack):
        """Req 2.1: user_roles.py validates roles via rbac_utils.Role."""
        import rbac_utils
        assert rbac_utils.Role("DataLabeler") is rbac_utils.Role.DATA_LABELER


# ------------------------------------------ per-request @rbac_check path

class TestDataLabelerEnforcement:
    def test_non_labeler_endpoint_denied_403_with_audit_event(
            self, aws_stack, shared, middleware):
        """Req 2.3: a DataLabeler-only caller requesting a non-labeler API
        gets 403 with no portal data, and an unauthorized_access audit
        event carrying user, resource, and timestamp."""
        labeler = make_user(role="DataLabeler")

        decorated = middleware.rbac_check(
            [shared.Permission.VIEW_DATASETS])(ok_handler)
        response = decorated(api_event(labeler, resource="/datasets"), None)

        assert response["statusCode"] == 403
        body = json.loads(response["body"])
        assert body["error"] == "Insufficient permissions"
        assert "ok" not in body  # no portal data in the denial

        entries = audit_entries_for(aws_stack, labeler["user_id"])
        assert len(entries) == 1
        entry = entries[0]
        assert entry["action"] == "unauthorized_access"
        assert entry["result"] == "denied"
        assert entry["user_id"] == labeler["user_id"]
        assert entry["resource_id"] == "/datasets"
        assert entry["timestamp"] > 0
        assert entry["details"]["user_role"] == "DataLabeler"

    def test_labeler_endpoint_authorized(self, aws_stack, shared, middleware):
        """Req 2.5: the Cognito custom:role claim alone (no UserRoles rows)
        authorizes the labeler APIs on every request."""
        labeler = make_user(role="DataLabeler")

        decorated = middleware.rbac_check(
            [shared.Permission.LABELING_TASKS_SELF])(ok_handler)
        response = decorated(api_event(labeler, resource="/labeler/jobs"), None)

        assert response["statusCode"] == 200
        assert audit_entries_for(aws_stack, labeler["user_id"]) == []

    def test_revocation_enforced_on_next_request(
            self, aws_stack, shared, middleware):
        """Req 2.1: role resolution happens per request, so a usecase-level
        role change takes effect on the caller's next request."""
        user = make_user(role="DataLabeler")
        usecase_id = f"uc-{uuid.uuid4()}"

        decorated = middleware.rbac_check(
            [shared.Permission.VIEW_DATASETS])(ok_handler)

        # DataLabeler-only: denied.
        denied = decorated(api_event(user, usecase_id=usecase_id), None)
        assert denied["statusCode"] == 403

        # Grant a usecase-scoped DataScientist row; the very next request
        # is authorized (per-request resolution, no caching).
        aws_stack.tables.user_roles.put_item(Item={
            "user_id": user["user_id"],
            "usecase_id": usecase_id,
            "role": "DataScientist",
        })
        allowed = decorated(api_event(user, usecase_id=usecase_id), None)
        assert allowed["statusCode"] == 200

        # Revoke the row; the next request is denied again.
        aws_stack.tables.user_roles.delete_item(Key={
            "user_id": user["user_id"],
            "usecase_id": usecase_id,
        })
        denied_again = decorated(api_event(user, usecase_id=usecase_id), None)
        assert denied_again["statusCode"] == 403
