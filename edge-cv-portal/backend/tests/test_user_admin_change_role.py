"""
Unit tests for PUT /api/v1/admin/users/{username}/role in user_admin.py
(spec: portal-user-manager, task 2.4).

Covers: role validation against the five defined Portal_Role values,
the audit-pending -> admin_update_user_attributes -> audit-final flow
recording previous and new role, the last-PortalAdmin guard (409 + the
reason, rejected attempt audited), UserNotFoundException -> 404, other
Cognito failures -> 502 "role change failed" with the role unchanged,
the audit-before-effect abort (pending audit failure -> 500 "not
applied" with Cognito untouched), and the PortalAdmin 403 gate.

Cognito is a recording fake client (same pattern as
test_user_admin_listing.py / test_user_admin_set_password.py); the
two-phase audit entries run against real moto-backed DynamoDB.

_Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6_
"""
import json
import os
import sys

import pytest
from botocore.exceptions import ClientError

REGION = "us-east-1"
EDGE_CREDENTIALS_TABLE = "test-edge-credentials"
AUDIT_LOG_TABLE = "test-audit-log"
POOL_ID = "us-east-1_testpool"

PORTAL_ROLES = ("PortalAdmin", "UseCaseAdmin", "DataScientist",
                "Operator", "Viewer")


# ----------------------------------------------------- fake Cognito client

def cognito_error(code, message, operation="AdminUpdateUserAttributes"):
    return ClientError({"Error": {"Code": code, "Message": message}},
                       operation)


class FakeCognitoClient:
    """Recording fake for admin_get_user, list_users (paginated), and
    admin_update_user_attributes over an in-memory user population."""

    def __init__(self, users=None, page_size=60, update_error=None):
        self.users = list(users or [])
        self.page_size = page_size
        self.update_error = update_error
        self.get_calls = []
        self.list_calls = []
        self.update_calls = []

    def _find(self, username):
        for user in self.users:
            if user["Username"] == username:
                return user
        return None

    def admin_get_user(self, **kwargs):
        self.get_calls.append(kwargs)
        assert kwargs["UserPoolId"] == POOL_ID
        user = self._find(kwargs["Username"])
        if user is None:
            raise cognito_error("UserNotFoundException",
                                "User does not exist.", "AdminGetUser")
        return {
            "Username": user["Username"],
            "UserAttributes": user.get("Attributes", []),
            "Enabled": user.get("Enabled", True),
            "UserStatus": user.get("UserStatus", "CONFIRMED"),
        }

    def list_users(self, **kwargs):
        self.list_calls.append(kwargs)
        assert kwargs["UserPoolId"] == POOL_ID
        start = int(kwargs.get("PaginationToken", "0"))
        limit = min(int(kwargs.get("Limit", 60)), self.page_size)
        page = self.users[start:start + limit]
        response = {"Users": page}
        next_start = start + len(page)
        if next_start < len(self.users):
            response["PaginationToken"] = str(next_start)
        return response

    def admin_update_user_attributes(self, **kwargs):
        self.update_calls.append(kwargs)
        if self.update_error is not None:
            raise self.update_error
        assert kwargs["UserPoolId"] == POOL_ID
        user = self._find(kwargs["Username"])
        if user is None:
            raise cognito_error("UserNotFoundException",
                                "User does not exist.")
        for new_attr in kwargs["UserAttributes"]:
            attrs = user.setdefault("Attributes", [])
            for attr in attrs:
                if attr["Name"] == new_attr["Name"]:
                    attr["Value"] = new_attr["Value"]
                    break
            else:
                attrs.append(dict(new_attr))

    def role_of(self, username):
        user = self._find(username)
        attrs = {a["Name"]: a["Value"] for a in user.get("Attributes", [])}
        return attrs.get("custom:role")


def cognito_user(username, role=None, enabled=True, email=None):
    """Build a user record in the Cognito list_users response shape."""
    attrs = [{"Name": "email_verified", "Value": "true"}]
    if email is not None:
        attrs.append({"Name": "email", "Value": email})
    if role is not None:
        attrs.append({"Name": "custom:role", "Value": role})
    return {"Username": username, "Attributes": attrs,
            "Enabled": enabled, "UserStatus": "CONFIRMED"}


# --------------------------------------------------------------- fixtures

@pytest.fixture(scope="module")
def user_admin(aws_stack):
    """The real user_admin module imported inside the moto mock, with the
    edge-credentials table created."""
    import boto3

    os.environ["EDGE_CREDENTIALS_TABLE"] = EDGE_CREDENTIALS_TABLE
    ddb = boto3.client("dynamodb", region_name=REGION)
    if EDGE_CREDENTIALS_TABLE not in ddb.list_tables()["TableNames"]:
        ddb.create_table(
            TableName=EDGE_CREDENTIALS_TABLE,
            KeySchema=[{"AttributeName": "username", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "username", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )

    sys.modules.pop("user_admin", None)
    import user_admin as module
    return module


@pytest.fixture
def audit_table(user_admin):
    """The moto-backed audit-log table, emptied per test."""
    import boto3
    table = boto3.resource("dynamodb", region_name=REGION).Table(
        AUDIT_LOG_TABLE)
    for item in table.scan()["Items"]:
        table.delete_item(Key={"event_id": item["event_id"],
                               "timestamp": item["timestamp"]})
    return table


@pytest.fixture
def install_cognito(user_admin, monkeypatch):
    """Wire a fake cognito client + pool id into the module under test."""
    def _install(users=None, page_size=60, update_error=None):
        fake = FakeCognitoClient(users=users, page_size=page_size,
                                 update_error=update_error)
        monkeypatch.setattr(user_admin, "cognito_client", fake)
        monkeypatch.setattr(user_admin, "USER_POOL_ID", POOL_ID)
        return fake
    return _install


# ---------------------------------------------------------------- helpers

def role_event(username, body=None, caller_role="PortalAdmin"):
    return {
        "httpMethod": "PUT",
        "path": f"/api/v1/admin/users/{username}/role",
        "pathParameters": {"username": username},
        "body": json.dumps(body) if body is not None else None,
        "requestContext": {
            "authorizer": {
                "claims": {
                    "sub": "admin-1",
                    "email": "admin@example.com",
                    "cognito:username": "admin-1",
                    "custom:role": caller_role,
                }
            }
        },
    }


def invoke(user_admin, event):
    response = user_admin.handler(event, None)
    return response["statusCode"], json.loads(response["body"])


def audit_entries(audit_table):
    return audit_table.scan()["Items"]


# ------------------------------------------------------------------ tests

class TestPortalAdminGate:
    @pytest.mark.parametrize(
        "caller_role", ["Viewer", "Operator", "DataScientist",
                        "UseCaseAdmin"])
    def test_non_portal_admin_rejected_403(
            self, user_admin, audit_table, install_cognito, caller_role):
        """Req 1.5: non-PortalAdmin callers get 403 with zero Cognito
        calls and no audit entry."""
        fake = install_cognito(users=[cognito_user("op1", role="Viewer")])
        status, body = invoke(user_admin, role_event(
            "op1", {"role": "Operator"}, caller_role=caller_role))
        assert status == 403
        assert body["error"] == "Access denied"
        assert fake.update_calls == []
        assert fake.get_calls == []
        assert audit_entries(audit_table) == []


class TestRoleValidation:
    @pytest.mark.parametrize(
        "bad_role", ["Admin", "portaladmin", "", None, 42, "SuperUser"])
    def test_undefined_role_rejected_400_before_any_effect(
            self, user_admin, audit_table, install_cognito, bad_role):
        """Req 5.2: only the five defined Portal_Role values are
        accepted; anything else is a 400 with no Cognito mutation."""
        fake = install_cognito(users=[cognito_user("op1", role="Viewer")])
        status, body = invoke(user_admin, role_event(
            "op1", {"role": bad_role}))
        assert status == 400
        assert body["error"] == "Invalid role"
        assert fake.update_calls == []
        assert audit_entries(audit_table) == []

    @pytest.mark.parametrize("role", PORTAL_ROLES)
    def test_all_five_defined_roles_accepted(
            self, user_admin, audit_table, install_cognito, role):
        fake = install_cognito(users=[
            cognito_user("op1", role="Viewer"),
            cognito_user("admin-1", role="PortalAdmin"),
        ])
        status, _ = invoke(user_admin, role_event("op1", {"role": role}))
        assert status == 200
        assert fake.role_of("op1") == role


class TestRoleChangeSuccess:
    def test_updates_custom_role_attribute_exactly(
            self, user_admin, audit_table, install_cognito):
        """Req 5.1: the exact new role is applied to custom:role via
        admin_update_user_attributes."""
        fake = install_cognito(users=[
            cognito_user("op1", role="Operator"),
            cognito_user("admin-1", role="PortalAdmin"),
        ])
        status, body = invoke(user_admin, role_event(
            "op1", {"role": "DataScientist"}))

        assert status == 200
        assert body["username"] == "op1"
        assert body["previous_role"] == "Operator"
        assert body["role"] == "DataScientist"
        assert fake.update_calls == [{
            "UserPoolId": POOL_ID,
            "Username": "op1",
            "UserAttributes": [
                {"Name": "custom:role", "Value": "DataScientist"}],
        }]

    def test_success_audits_previous_and_new_role(
            self, user_admin, audit_table, install_cognito):
        """Req 5.4, 6.1: exactly one finalized audit entry with acting
        user, affected account, action type, completion time, and the
        previous and new role."""
        install_cognito(users=[
            cognito_user("op1", role="Operator"),
            cognito_user("admin-1", role="PortalAdmin"),
        ])
        status, _ = invoke(user_admin, role_event(
            "op1", {"role": "UseCaseAdmin"}))

        assert status == 200
        entries = audit_entries(audit_table)
        assert len(entries) == 1
        entry = entries[0]
        assert entry["result"] == "success"
        assert entry["action"] == "role_change"
        assert entry["resource_type"] == "user_account"
        assert entry["resource_id"] == "op1"
        assert entry["user_id"] == "admin-1"
        assert entry["completed_at"] > 0
        assert entry["details"]["previous_role"] == "Operator"
        assert entry["details"]["new_role"] == "UseCaseAdmin"

    def test_previous_role_defaults_to_viewer_when_attribute_missing(
            self, user_admin, audit_table, install_cognito):
        install_cognito(users=[
            cognito_user("roleless", role=None),
            cognito_user("admin-1", role="PortalAdmin"),
        ])
        status, body = invoke(user_admin, role_event(
            "roleless", {"role": "Operator"}))
        assert status == 200
        assert body["previous_role"] == "Viewer"


class TestLastPortalAdminGuard:
    def test_demoting_the_last_enabled_portal_admin_rejected_409(
            self, user_admin, audit_table, install_cognito):
        """Req 5.3, 5.5: the change would leave zero enabled
        PortalAdmins -> 409 with the reason, role untouched, and the
        rejected attempt audited with the reason."""
        fake = install_cognito(users=[
            cognito_user("admin-1", role="PortalAdmin"),
            cognito_user("op1", role="Operator"),
            cognito_user("disabled-admin", role="PortalAdmin",
                         enabled=False),
        ])
        status, body = invoke(user_admin, role_event(
            "admin-1", {"role": "Viewer"}))

        assert status == 409
        assert "last remaining enabled PortalAdmin" in body["message"]
        assert fake.update_calls == []
        assert fake.role_of("admin-1") == "PortalAdmin"

        entries = audit_entries(audit_table)
        assert len(entries) == 1
        entry = entries[0]
        assert entry["result"] == "rejected"
        assert entry["action"] == "role_change"
        assert entry["resource_id"] == "admin-1"
        assert entry["user_id"] == "admin-1"
        assert "last remaining enabled PortalAdmin" in \
            entry["details"]["reason"]

    def test_demotion_allowed_when_another_enabled_portal_admin_remains(
            self, user_admin, audit_table, install_cognito):
        """Req 5.3: with a second enabled PortalAdmin the guard does not
        fire."""
        fake = install_cognito(users=[
            cognito_user("admin-1", role="PortalAdmin"),
            cognito_user("admin-2", role="PortalAdmin"),
        ])
        status, _ = invoke(user_admin, role_event(
            "admin-1", {"role": "Viewer"}))
        assert status == 200
        assert fake.role_of("admin-1") == "Viewer"

    def test_changing_a_disabled_portal_admin_is_not_guarded(
            self, user_admin, audit_table, install_cognito):
        """A disabled PortalAdmin does not count toward the enabled pool,
        so changing its role cannot reduce the enabled count."""
        install_cognito(users=[
            cognito_user("admin-1", role="PortalAdmin"),
            cognito_user("old-admin", role="PortalAdmin", enabled=False),
        ])
        status, _ = invoke(user_admin, role_event(
            "old-admin", {"role": "Viewer"}))
        assert status == 200

    def test_keeping_portal_admin_role_is_not_guarded(
            self, user_admin, audit_table, install_cognito):
        """Re-selecting PortalAdmin for the last admin never triggers
        the guard (the enabled-PortalAdmin count is unchanged)."""
        fake = install_cognito(users=[
            cognito_user("admin-1", role="PortalAdmin"),
        ])
        status, _ = invoke(user_admin, role_event(
            "admin-1", {"role": "PortalAdmin"}))
        assert status == 200
        assert fake.list_calls == []

    def test_guard_counts_across_pagination(
            self, user_admin, audit_table, install_cognito):
        """The guard paginates the full pool: the only other enabled
        PortalAdmin sits on a later list_users page."""
        users = [cognito_user(f"filler-{i}", role="Viewer")
                 for i in range(5)]
        users.insert(0, cognito_user("admin-1", role="PortalAdmin"))
        users.append(cognito_user("admin-2", role="PortalAdmin"))
        fake = install_cognito(users=users, page_size=3)

        status, _ = invoke(user_admin, role_event(
            "admin-1", {"role": "Operator"}))
        assert status == 200
        assert len(fake.list_calls) == 3  # 3 + 3 + 1: all pages fetched


class TestFailurePaths:
    def test_unknown_user_maps_to_404(
            self, user_admin, audit_table, install_cognito):
        fake = install_cognito(users=[
            cognito_user("admin-1", role="PortalAdmin")])
        status, body = invoke(user_admin, role_event(
            "ghost", {"role": "Viewer"}))
        assert status == 404
        assert body["error"] == "User not found"
        assert fake.update_calls == []

    def test_cognito_failure_maps_to_502_role_unchanged(
            self, user_admin, audit_table, install_cognito):
        """Req 5.6: a Cognito failure during the update -> 502 'role
        change failed', role unchanged, audit finalized to failure."""
        fake = install_cognito(
            users=[
                cognito_user("op1", role="Operator"),
                cognito_user("admin-1", role="PortalAdmin"),
            ],
            update_error=cognito_error(
                "InternalErrorException", "Something went wrong"),
        )
        status, body = invoke(user_admin, role_event(
            "op1", {"role": "Viewer"}))

        assert status == 502
        assert body["error"] == "role change failed"
        assert fake.role_of("op1") == "Operator"

        entries = audit_entries(audit_table)
        assert len(entries) == 1
        assert entries[0]["result"] == "failure"


class TestAuditBeforeEffect:
    def test_pending_audit_failure_blocks_the_action(
            self, user_admin, audit_table, install_cognito, monkeypatch):
        """Req 6.4/6.5: when the pending audit entry cannot be recorded,
        the role change is not applied - zero update calls and an error
        stating the action was not applied."""
        fake = install_cognito(users=[
            cognito_user("op1", role="Operator"),
            cognito_user("admin-1", role="PortalAdmin"),
        ])

        def failing_audit(*args, **kwargs):
            raise RuntimeError("audit table unavailable")

        monkeypatch.setattr(user_admin, "record_audit_event_strict",
                            failing_audit)
        status, body = invoke(user_admin, role_event(
            "op1", {"role": "Viewer"}))

        assert status == 500
        assert body["message"] == "The action was not applied"
        assert fake.update_calls == []
        assert fake.role_of("op1") == "Operator"


class TestRequestValidation:
    def test_missing_body_rejected_400(
            self, user_admin, audit_table, install_cognito):
        fake = install_cognito(users=[cognito_user("op1", role="Viewer")])
        status, _ = invoke(user_admin, role_event("op1", None))
        assert status == 400
        assert fake.update_calls == []
