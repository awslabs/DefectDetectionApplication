"""
Unit tests for POST /api/v1/admin/users/{username}/disable and /enable
in user_admin.py (spec: portal-user-manager, task 13.3).

Covers: the read-current-state-first no-op (already in the requested
state -> 200 returning the current state with no Cognito mutation, no
audit-pending write, and no sync staging, 13.6), the disable flow with
the last-PortalAdmin guard (shared predicate, D14: 409 + reason with
the rejected attempt audited before any mutation, 13.9) then
audit-pending (account_disable) -> admin_disable_user -> sync staging
pending with enabled=false -> audit-final, the enable flow
(account_enable -> admin_enable_user -> sync staging enabled=true ->
audit-final), Cognito failure -> 502 "action failed" with the state
unchanged and audit-final failure (13.7), the audit-before-effect
abort, and the PortalAdmin 403 gate.

Cognito is a recording fake client (same pattern as the other
test_user_admin_* files); the audit entries and the account-sync
staging table run against real moto-backed DynamoDB.

_Requirements: 13.2, 13.3, 13.6, 13.7, 13.9, 7.2, 7.8_
"""
import json
import os
import sys

import pytest
from botocore.exceptions import ClientError

REGION = "us-east-1"
EDGE_CREDENTIALS_TABLE = "test-edge-credentials"
ACCOUNT_SYNC_TABLE = "test-account-sync"
AUDIT_LOG_TABLE = "test-audit-log"
POOL_ID = "us-east-1_testpool"


# ----------------------------------------------------- fake Cognito client

def cognito_error(code, message, operation="AdminDisableUser"):
    return ClientError({"Error": {"Code": code, "Message": message}},
                       operation)


class FakeCognitoClient:
    """Recording fake for admin_get_user, list_users (paginated), and
    admin_disable_user / admin_enable_user over an in-memory pool."""

    def __init__(self, users=None, page_size=60,
                 disable_error=None, enable_error=None):
        self.users = list(users or [])
        self.page_size = page_size
        self.disable_error = disable_error
        self.enable_error = enable_error
        self.get_calls = []
        self.list_calls = []
        self.disable_calls = []
        self.enable_calls = []

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

    def admin_disable_user(self, **kwargs):
        self.disable_calls.append(kwargs)
        if self.disable_error is not None:
            raise self.disable_error
        assert kwargs["UserPoolId"] == POOL_ID
        user = self._find(kwargs["Username"])
        if user is None:
            raise cognito_error("UserNotFoundException",
                                "User does not exist.")
        user["Enabled"] = False

    def admin_enable_user(self, **kwargs):
        self.enable_calls.append(kwargs)
        if self.enable_error is not None:
            raise self.enable_error
        assert kwargs["UserPoolId"] == POOL_ID
        user = self._find(kwargs["Username"])
        if user is None:
            raise cognito_error("UserNotFoundException",
                                "User does not exist.", "AdminEnableUser")
        user["Enabled"] = True

    def enabled_of(self, username):
        return self._find(username)["Enabled"]


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
    """The real user_admin module imported inside the moto mock, with
    the edge-credentials and account-sync tables created."""
    import boto3

    os.environ["EDGE_CREDENTIALS_TABLE"] = EDGE_CREDENTIALS_TABLE
    os.environ["ACCOUNT_SYNC_TABLE"] = ACCOUNT_SYNC_TABLE
    os.environ.pop("ACCOUNT_SYNC_FUNCTION", None)

    ddb = boto3.client("dynamodb", region_name=REGION)
    existing = ddb.list_tables()["TableNames"]
    if EDGE_CREDENTIALS_TABLE not in existing:
        ddb.create_table(
            TableName=EDGE_CREDENTIALS_TABLE,
            KeySchema=[{"AttributeName": "username", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "username", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
    if ACCOUNT_SYNC_TABLE not in existing:
        ddb.create_table(
            TableName=ACCOUNT_SYNC_TABLE,
            KeySchema=[{"AttributeName": "device_id", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "device_id", "AttributeType": "S"}],
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
def sync_table(user_admin):
    """The moto-backed account-sync staging table, emptied per test."""
    import boto3
    table = boto3.resource("dynamodb", region_name=REGION).Table(
        ACCOUNT_SYNC_TABLE)
    for item in table.scan()["Items"]:
        table.delete_item(Key={"device_id": item["device_id"]})
    return table


@pytest.fixture
def install_cognito(user_admin, monkeypatch):
    """Wire a fake cognito client + pool id into the module under test."""
    def _install(users=None, page_size=60,
                 disable_error=None, enable_error=None):
        fake = FakeCognitoClient(users=users, page_size=page_size,
                                 disable_error=disable_error,
                                 enable_error=enable_error)
        monkeypatch.setattr(user_admin, "cognito_client", fake)
        monkeypatch.setattr(user_admin, "USER_POOL_ID", POOL_ID)
        return fake
    return _install


# ---------------------------------------------------------------- helpers

def action_event(username, action, caller_role="PortalAdmin"):
    return {
        "httpMethod": "POST",
        "path": f"/api/v1/admin/users/{username}/{action}",
        "pathParameters": {"username": username},
        "body": None,
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


def stage_device(sync_table, device_id, accounts, sync_id="sync-old",
                 pending=False, status="success"):
    """Seed a device's staged account set in the sync-state table."""
    sync_table.put_item(Item={
        "device_id": device_id,
        "accounts": accounts,
        "syncId": sync_id,
        "pendingChanges": pending,
        "status": status,
    })


def sync_row(sync_table, device_id):
    return sync_table.get_item(Key={"device_id": device_id})["Item"]


# ------------------------------------------------------------------ tests

class TestPortalAdminGate:
    @pytest.mark.parametrize("action", ["disable", "enable"])
    @pytest.mark.parametrize(
        "caller_role", ["Viewer", "Operator", "DataScientist",
                        "UseCaseAdmin"])
    def test_non_portal_admin_rejected_403(
            self, user_admin, audit_table, sync_table, install_cognito,
            action, caller_role):
        """Req 1.5: non-PortalAdmin callers get 403 with zero Cognito
        calls and no audit entry."""
        fake = install_cognito(users=[cognito_user("op1", role="Viewer")])
        status, body = invoke(user_admin, action_event(
            "op1", action, caller_role=caller_role))
        assert status == 403
        assert body["error"] == "Access denied"
        assert fake.get_calls == []
        assert fake.disable_calls == []
        assert fake.enable_calls == []
        assert audit_entries(audit_table) == []


class TestDisableSuccess:
    def test_disables_an_enabled_account(
            self, user_admin, audit_table, sync_table, install_cognito):
        """Req 13.2: a confirmed disable of an enabled account invokes
        admin_disable_user exactly and reports the resulting state."""
        fake = install_cognito(users=[
            cognito_user("op1", role="Operator"),
            cognito_user("admin-1", role="PortalAdmin"),
        ])
        status, body = invoke(user_admin, action_event("op1", "disable"))

        assert status == 200
        assert body["username"] == "op1"
        assert body["enabled"] is False
        assert body["changed"] is True
        assert fake.disable_calls == [
            {"UserPoolId": POOL_ID, "Username": "op1"}]
        assert fake.enabled_of("op1") is False

    def test_success_finalizes_account_disable_audit(
            self, user_admin, audit_table, sync_table, install_cognito):
        """Req 6.1: exactly one finalized account_disable entry with
        acting user, affected account, and completion timestamp."""
        install_cognito(users=[
            cognito_user("op1", role="Operator"),
            cognito_user("admin-1", role="PortalAdmin"),
        ])
        status, _ = invoke(user_admin, action_event("op1", "disable"))

        assert status == 200
        entries = audit_entries(audit_table)
        assert len(entries) == 1
        entry = entries[0]
        assert entry["action"] == "account_disable"
        assert entry["result"] == "success"
        assert entry["resource_type"] == "user_account"
        assert entry["resource_id"] == "op1"
        assert entry["user_id"] == "admin-1"
        assert entry["completed_at"] > 0

    def test_disable_marks_staged_syncs_pending_enabled_false(
            self, user_admin, audit_table, sync_table, install_cognito):
        """Req 7.2, 7.8: every device whose staged set carries the
        account is refreshed with enabled=false, marked pending, and
        assigned a fresh syncId."""
        install_cognito(users=[
            cognito_user("op1", role="Operator", email="op1@example.com"),
            cognito_user("admin-1", role="PortalAdmin"),
        ])
        record = {"email": "op1@example.com", "role": "Operator",
                  "enabled": True}
        stage_device(sync_table, "dev-1", {"op1": dict(record)})
        stage_device(sync_table, "dev-2", {"op1": dict(record)})
        stage_device(sync_table, "dev-3", {"other": dict(record)})

        status, _ = invoke(user_admin, action_event("op1", "disable"))
        assert status == 200

        for device_id in ("dev-1", "dev-2"):
            row = sync_row(sync_table, device_id)
            assert row["accounts"]["op1"]["enabled"] is False
            assert row["pendingChanges"] is True
            assert row["status"] == "pending"
            assert row["syncId"] != "sync-old"
        # A device whose staged set does not carry the account is
        # untouched.
        untouched = sync_row(sync_table, "dev-3")
        assert untouched["pendingChanges"] is False
        assert untouched["syncId"] == "sync-old"


class TestEnableSuccess:
    def test_enables_a_disabled_account(
            self, user_admin, audit_table, sync_table, install_cognito):
        """Req 13.3: a confirmed enable of a disabled account invokes
        admin_enable_user exactly and reports the resulting state."""
        fake = install_cognito(users=[
            cognito_user("op1", role="Operator", enabled=False),
            cognito_user("admin-1", role="PortalAdmin"),
        ])
        status, body = invoke(user_admin, action_event("op1", "enable"))

        assert status == 200
        assert body["username"] == "op1"
        assert body["enabled"] is True
        assert body["changed"] is True
        assert fake.enable_calls == [
            {"UserPoolId": POOL_ID, "Username": "op1"}]
        assert fake.enabled_of("op1") is True

    def test_success_finalizes_account_enable_audit(
            self, user_admin, audit_table, sync_table, install_cognito):
        install_cognito(users=[
            cognito_user("op1", role="Operator", enabled=False),
            cognito_user("admin-1", role="PortalAdmin"),
        ])
        status, _ = invoke(user_admin, action_event("op1", "enable"))

        assert status == 200
        entries = audit_entries(audit_table)
        assert len(entries) == 1
        entry = entries[0]
        assert entry["action"] == "account_enable"
        assert entry["result"] == "success"
        assert entry["resource_id"] == "op1"
        assert entry["user_id"] == "admin-1"

    def test_enable_marks_staged_syncs_pending_enabled_true(
            self, user_admin, audit_table, sync_table, install_cognito):
        """Req 7.2: the enabled state change refreshes staged sets with
        enabled=true and marks the devices pending."""
        install_cognito(users=[
            cognito_user("op1", role="Operator", enabled=False),
            cognito_user("admin-1", role="PortalAdmin"),
        ])
        stage_device(sync_table, "dev-1", {
            "op1": {"email": "op1@example.com", "role": "Operator",
                    "enabled": False}})

        status, _ = invoke(user_admin, action_event("op1", "enable"))
        assert status == 200

        row = sync_row(sync_table, "dev-1")
        assert row["accounts"]["op1"]["enabled"] is True
        assert row["pendingChanges"] is True
        assert row["syncId"] != "sync-old"

    def test_enable_never_runs_the_last_portal_admin_guard(
            self, user_admin, audit_table, sync_table, install_cognito):
        """Enabling can only grow the enabled-PortalAdmin count, so the
        guard never fires - even for the only (disabled) PortalAdmin."""
        fake = install_cognito(users=[
            cognito_user("old-admin", role="PortalAdmin", enabled=False),
        ])
        status, _ = invoke(user_admin, action_event("old-admin", "enable"))
        assert status == 200
        assert fake.list_calls == []
        assert fake.enabled_of("old-admin") is True


class TestAlreadyInRequestedState:
    @pytest.mark.parametrize("action,enabled", [
        ("disable", False),
        ("enable", True),
    ])
    def test_no_op_returns_current_state_without_side_effects(
            self, user_admin, audit_table, sync_table, install_cognito,
            action, enabled):
        """Req 13.6: already in the requested state -> 200 no-op
        returning the current state with no Cognito mutation, no
        audit-pending write, and no sync staging."""
        fake = install_cognito(users=[
            cognito_user("op1", role="Operator", enabled=enabled),
            cognito_user("admin-1", role="PortalAdmin"),
        ])
        stage_device(sync_table, "dev-1", {
            "op1": {"email": "op1@example.com", "role": "Operator",
                    "enabled": enabled}})

        status, body = invoke(user_admin, action_event("op1", action))

        assert status == 200
        assert body["username"] == "op1"
        assert body["enabled"] is enabled
        assert body["changed"] is False

        assert fake.disable_calls == []
        assert fake.enable_calls == []
        assert audit_entries(audit_table) == []

        row = sync_row(sync_table, "dev-1")
        assert row["pendingChanges"] is False
        assert row["syncId"] == "sync-old"
        assert row["status"] == "success"


class TestLastPortalAdminGuard:
    def test_disabling_the_last_enabled_portal_admin_rejected_409(
            self, user_admin, audit_table, sync_table, install_cognito):
        """Req 5.3/13.9 (D14): disabling the last remaining enabled
        PortalAdmin -> 409 with the reason, state untouched, and the
        rejected attempt audited before any mutation."""
        fake = install_cognito(users=[
            cognito_user("admin-1", role="PortalAdmin"),
            cognito_user("op1", role="Operator"),
            cognito_user("disabled-admin", role="PortalAdmin",
                         enabled=False),
        ])
        status, body = invoke(user_admin, action_event(
            "admin-1", "disable"))

        assert status == 409
        assert "last remaining enabled PortalAdmin" in body["message"]
        assert fake.disable_calls == []
        assert fake.enabled_of("admin-1") is True

        entries = audit_entries(audit_table)
        assert len(entries) == 1
        entry = entries[0]
        assert entry["result"] == "rejected"
        assert entry["action"] == "account_disable"
        assert entry["resource_id"] == "admin-1"
        assert entry["user_id"] == "admin-1"
        assert "last remaining enabled PortalAdmin" in \
            entry["details"]["reason"]

    def test_disable_allowed_when_another_enabled_portal_admin_remains(
            self, user_admin, audit_table, sync_table, install_cognito):
        fake = install_cognito(users=[
            cognito_user("admin-1", role="PortalAdmin"),
            cognito_user("admin-2", role="PortalAdmin"),
        ])
        status, _ = invoke(user_admin, action_event("admin-1", "disable"))
        assert status == 200
        assert fake.enabled_of("admin-1") is False

    def test_disabling_a_non_portal_admin_is_not_guarded(
            self, user_admin, audit_table, sync_table, install_cognito):
        """Disabling a non-PortalAdmin cannot reduce the enabled-
        PortalAdmin count, so the pool is never paginated."""
        fake = install_cognito(users=[
            cognito_user("admin-1", role="PortalAdmin"),
            cognito_user("op1", role="Operator"),
        ])
        status, _ = invoke(user_admin, action_event("op1", "disable"))
        assert status == 200
        assert fake.list_calls == []

    def test_guard_counts_across_pagination(
            self, user_admin, audit_table, sync_table, install_cognito):
        """The shared predicate paginates the full pool: the only other
        enabled PortalAdmin sits on a later list_users page."""
        users = [cognito_user(f"filler-{i}", role="Viewer")
                 for i in range(5)]
        users.insert(0, cognito_user("admin-1", role="PortalAdmin"))
        users.append(cognito_user("admin-2", role="PortalAdmin"))
        fake = install_cognito(users=users, page_size=3)

        status, _ = invoke(user_admin, action_event("admin-1", "disable"))
        assert status == 200
        assert len(fake.list_calls) == 3  # 3 + 3 + 1: all pages fetched


class TestFailurePaths:
    @pytest.mark.parametrize("action", ["disable", "enable"])
    def test_unknown_user_maps_to_404(
            self, user_admin, audit_table, sync_table, install_cognito,
            action):
        fake = install_cognito(users=[
            cognito_user("admin-1", role="PortalAdmin")])
        status, body = invoke(user_admin, action_event("ghost", action))
        assert status == 404
        assert body["error"] == "User not found"
        assert fake.disable_calls == []
        assert fake.enable_calls == []
        assert audit_entries(audit_table) == []

    def test_cognito_failure_on_disable_maps_to_502_state_unchanged(
            self, user_admin, audit_table, sync_table, install_cognito):
        """Req 13.7: a Cognito failure -> 502 "action failed" with the
        state unchanged, audit-final failure, and no sync staging."""
        fake = install_cognito(
            users=[
                cognito_user("op1", role="Operator"),
                cognito_user("admin-1", role="PortalAdmin"),
            ],
            disable_error=cognito_error(
                "InternalErrorException", "Something went wrong"),
        )
        stage_device(sync_table, "dev-1", {
            "op1": {"email": "op1@example.com", "role": "Operator",
                    "enabled": True}})

        status, body = invoke(user_admin, action_event("op1", "disable"))

        assert status == 502
        assert body["error"] == "action failed"
        assert fake.enabled_of("op1") is True

        entries = audit_entries(audit_table)
        assert len(entries) == 1
        assert entries[0]["result"] == "failure"
        assert entries[0]["action"] == "account_disable"

        row = sync_row(sync_table, "dev-1")
        assert row["pendingChanges"] is False
        assert row["syncId"] == "sync-old"

    def test_cognito_failure_on_enable_maps_to_502_state_unchanged(
            self, user_admin, audit_table, sync_table, install_cognito):
        fake = install_cognito(
            users=[
                cognito_user("op1", role="Operator", enabled=False),
                cognito_user("admin-1", role="PortalAdmin"),
            ],
            enable_error=cognito_error(
                "InternalErrorException", "Something went wrong",
                "AdminEnableUser"),
        )
        status, body = invoke(user_admin, action_event("op1", "enable"))

        assert status == 502
        assert body["error"] == "action failed"
        assert fake.enabled_of("op1") is False

        entries = audit_entries(audit_table)
        assert len(entries) == 1
        assert entries[0]["result"] == "failure"
        assert entries[0]["action"] == "account_enable"


class TestAuditBeforeEffect:
    @pytest.mark.parametrize("action,enabled", [
        ("disable", True),
        ("enable", False),
    ])
    def test_pending_audit_failure_blocks_the_action(
            self, user_admin, audit_table, sync_table, install_cognito,
            monkeypatch, action, enabled):
        """Req 6.4/6.5: when the pending audit entry cannot be recorded,
        the action is not applied - zero mutation calls and an error
        stating the action was not applied."""
        fake = install_cognito(users=[
            cognito_user("op1", role="Operator", enabled=enabled),
            cognito_user("admin-1", role="PortalAdmin"),
        ])

        def failing_audit(*args, **kwargs):
            raise RuntimeError("audit table unavailable")

        monkeypatch.setattr(user_admin, "record_audit_event_strict",
                            failing_audit)
        status, body = invoke(user_admin, action_event("op1", action))

        assert status == 500
        assert body["message"] == "The action was not applied"
        assert fake.disable_calls == []
        assert fake.enable_calls == []
        assert fake.enabled_of("op1") is enabled
