"""
Unit tests for DELETE /api/v1/admin/users/{username} in user_admin.py
(spec: portal-user-manager, task 13.4).

Covers the D13 ordering: admin_get_user captures username/email/role
for the audit entry (14.8) and maps UserNotFoundException -> 404 with
nothing modified (14.11) -> last-PortalAdmin guard -> 409 + reason
with the rejected attempt audited (14.3, 14.4) -> audit-pending
(account_delete) -> admin_delete_user (14.2) -> delete the
edge-credentials verifier record (14.5) -> mark sync staging pending
with enabled=false, deleted=true (7.8) -> audit-final.

Failure semantics: a Cognito failure aborts before the verifier record
is touched (14.6); a verifier-delete failure after a successful
Cognito delete retains the record for a subsequent attempt, finalizes
the audit entry with a partial-cleanup detail, and returns an error
whose message states the account was deleted but its verifier record
was not removed (14.10 - the frontend classifier matches /deleted/i
and /not removed/i).

Cognito is a recording fake client (same pattern as the other
test_user_admin_* files); the audit entries, the edge-credentials
table, and the account-sync staging table run against real
moto-backed DynamoDB.

_Requirements: 14.2, 14.3, 14.4, 14.5, 14.6, 14.8, 14.10, 14.11, 7.8_
"""
import json
import os
import re
import sys

import pytest
from botocore.exceptions import ClientError

REGION = "us-east-1"
EDGE_CREDENTIALS_TABLE = "test-edge-credentials"
ACCOUNT_SYNC_TABLE = "test-account-sync"
AUDIT_LOG_TABLE = "test-audit-log"
POOL_ID = "us-east-1_testpool"


# ----------------------------------------------------- fake Cognito client

def cognito_error(code, message, operation="AdminDeleteUser"):
    return ClientError({"Error": {"Code": code, "Message": message}},
                       operation)


class FakeCognitoClient:
    """Recording fake for admin_get_user, list_users (paginated), and
    admin_delete_user over an in-memory pool."""

    def __init__(self, users=None, page_size=60, delete_error=None):
        self.users = list(users or [])
        self.page_size = page_size
        self.delete_error = delete_error
        self.get_calls = []
        self.list_calls = []
        self.delete_calls = []

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

    def admin_delete_user(self, **kwargs):
        self.delete_calls.append(kwargs)
        if self.delete_error is not None:
            raise self.delete_error
        assert kwargs["UserPoolId"] == POOL_ID
        user = self._find(kwargs["Username"])
        if user is None:
            raise cognito_error("UserNotFoundException",
                                "User does not exist.")
        self.users.remove(user)

    def exists(self, username):
        return self._find(username) is not None


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
def credentials_table(user_admin):
    """The moto-backed edge-credentials table, emptied per test."""
    import boto3
    table = boto3.resource("dynamodb", region_name=REGION).Table(
        EDGE_CREDENTIALS_TABLE)
    for item in table.scan()["Items"]:
        table.delete_item(Key={"username": item["username"]})
    return table


@pytest.fixture
def install_cognito(user_admin, monkeypatch):
    """Wire a fake cognito client + pool id into the module under test."""
    def _install(users=None, page_size=60, delete_error=None):
        fake = FakeCognitoClient(users=users, page_size=page_size,
                                 delete_error=delete_error)
        monkeypatch.setattr(user_admin, "cognito_client", fake)
        monkeypatch.setattr(user_admin, "USER_POOL_ID", POOL_ID)
        return fake
    return _install


# ---------------------------------------------------------------- helpers

def delete_event(username, caller_role="PortalAdmin"):
    return {
        "httpMethod": "DELETE",
        "path": f"/api/v1/admin/users/{username}",
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


def put_verifier(credentials_table, username):
    credentials_table.put_item(Item={
        "username": username.lower(),
        "verifier": {"algorithm": "pbkdf2-sha256", "iterations": 1,
                     "salt": "c2FsdA==", "hash": "aGFzaA=="},
        "updatedAt": 1,
    })


def verifier_record(credentials_table, username):
    return credentials_table.get_item(
        Key={"username": username.lower()}).get("Item")


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
    @pytest.mark.parametrize(
        "caller_role", ["Viewer", "Operator", "DataScientist",
                        "UseCaseAdmin"])
    def test_non_portal_admin_rejected_403(
            self, user_admin, audit_table, sync_table, credentials_table,
            install_cognito, caller_role):
        """Req 1.5: non-PortalAdmin callers get 403 with zero Cognito
        calls and no audit entry."""
        fake = install_cognito(users=[cognito_user("op1", role="Viewer")])
        status, body = invoke(user_admin, delete_event(
            "op1", caller_role=caller_role))
        assert status == 403
        assert body["error"] == "Access denied"
        assert fake.get_calls == []
        assert fake.delete_calls == []
        assert audit_entries(audit_table) == []


class TestDeleteSuccess:
    def test_deletes_the_account_and_its_verifier_record(
            self, user_admin, audit_table, sync_table, credentials_table,
            install_cognito):
        """Req 14.2, 14.5: a confirmed deletion invokes
        admin_delete_user exactly and removes the account's
        edge-credentials verifier record."""
        fake = install_cognito(users=[
            cognito_user("op1", role="Operator", email="op1@example.com"),
            cognito_user("admin-1", role="PortalAdmin"),
        ])
        put_verifier(credentials_table, "op1")

        status, body = invoke(user_admin, delete_event("op1"))

        assert status == 200
        assert body["username"] == "op1"
        assert body["deleted"] is True
        assert fake.delete_calls == [
            {"UserPoolId": POOL_ID, "Username": "op1"}]
        assert fake.exists("op1") is False
        assert verifier_record(credentials_table, "op1") is None

    def test_success_finalizes_account_delete_audit_with_identity(
            self, user_admin, audit_table, sync_table, credentials_table,
            install_cognito):
        """Req 14.8: exactly one finalized account_delete entry carrying
        the acting administrator and the deleted account's username,
        email, and role at the time of deletion."""
        install_cognito(users=[
            cognito_user("op1", role="Operator", email="op1@example.com"),
            cognito_user("admin-1", role="PortalAdmin"),
        ])
        status, _ = invoke(user_admin, delete_event("op1"))

        assert status == 200
        entries = audit_entries(audit_table)
        assert len(entries) == 1
        entry = entries[0]
        assert entry["action"] == "account_delete"
        assert entry["result"] == "success"
        assert entry["resource_type"] == "user_account"
        assert entry["resource_id"] == "op1"
        assert entry["user_id"] == "admin-1"
        assert entry["details"]["email"] == "op1@example.com"
        assert entry["details"]["role"] == "Operator"
        assert entry["completed_at"] > 0

    def test_delete_marks_staged_syncs_pending_disabled_and_deleted(
            self, user_admin, audit_table, sync_table, credentials_table,
            install_cognito):
        """Req 7.8: every device whose staged set carries the account is
        refreshed with enabled=false, deleted=true, marked pending, and
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

        status, _ = invoke(user_admin, delete_event("op1"))
        assert status == 200

        for device_id in ("dev-1", "dev-2"):
            row = sync_row(sync_table, device_id)
            assert row["accounts"]["op1"]["enabled"] is False
            assert row["accounts"]["op1"]["deleted"] is True
            assert row["pendingChanges"] is True
            assert row["status"] == "pending"
            assert row["syncId"] != "sync-old"
        # A device whose staged set does not carry the account is
        # untouched.
        untouched = sync_row(sync_table, "dev-3")
        assert untouched["pendingChanges"] is False
        assert untouched["syncId"] == "sync-old"

    def test_delete_without_a_verifier_record_succeeds(
            self, user_admin, audit_table, sync_table, credentials_table,
            install_cognito):
        """Deleting an account that never became edge-login-capable
        (no verifier record) succeeds - the DynamoDB delete_item of a
        nonexistent key is a no-op."""
        fake = install_cognito(users=[
            cognito_user("op1", role="Operator"),
            cognito_user("admin-1", role="PortalAdmin"),
        ])
        status, _ = invoke(user_admin, delete_event("op1"))
        assert status == 200
        assert fake.exists("op1") is False


class TestNotFound:
    def test_unknown_user_maps_to_404_with_nothing_modified(
            self, user_admin, audit_table, sync_table, credentials_table,
            install_cognito):
        """Req 14.11: a deletion targeting a nonexistent account -> 404
        "not found" with no mutation and no audit entry."""
        put_verifier(credentials_table, "ghost")
        fake = install_cognito(users=[
            cognito_user("admin-1", role="PortalAdmin")])

        status, body = invoke(user_admin, delete_event("ghost"))

        assert status == 404
        assert body["error"] == "User not found"
        assert "not found" in body["message"]
        assert fake.delete_calls == []
        assert audit_entries(audit_table) == []
        # The verifier record (however it got there) is untouched: the
        # 404 path modifies nothing.
        assert verifier_record(credentials_table, "ghost") is not None


class TestLastPortalAdminGuard:
    def test_deleting_the_last_enabled_portal_admin_rejected_409(
            self, user_admin, audit_table, sync_table, credentials_table,
            install_cognito):
        """Req 14.3/14.4 (D14): deleting the last remaining enabled
        PortalAdmin -> 409 with the reason, nothing modified, and the
        rejected attempt audited."""
        put_verifier(credentials_table, "admin-1")
        fake = install_cognito(users=[
            cognito_user("admin-1", role="PortalAdmin"),
            cognito_user("op1", role="Operator"),
            cognito_user("disabled-admin", role="PortalAdmin",
                         enabled=False),
        ])
        status, body = invoke(user_admin, delete_event("admin-1"))

        assert status == 409
        assert "last remaining enabled PortalAdmin" in body["message"]
        assert fake.delete_calls == []
        assert fake.exists("admin-1") is True
        assert verifier_record(credentials_table, "admin-1") is not None

        entries = audit_entries(audit_table)
        assert len(entries) == 1
        entry = entries[0]
        assert entry["result"] == "rejected"
        assert entry["action"] == "account_delete"
        assert entry["resource_id"] == "admin-1"
        assert entry["user_id"] == "admin-1"
        assert "last remaining enabled PortalAdmin" in \
            entry["details"]["reason"]

    def test_delete_allowed_when_another_enabled_portal_admin_remains(
            self, user_admin, audit_table, sync_table, credentials_table,
            install_cognito):
        fake = install_cognito(users=[
            cognito_user("admin-1", role="PortalAdmin"),
            cognito_user("admin-2", role="PortalAdmin"),
        ])
        status, _ = invoke(user_admin, delete_event("admin-1"))
        assert status == 200
        assert fake.exists("admin-1") is False

    def test_deleting_a_disabled_portal_admin_is_not_guarded(
            self, user_admin, audit_table, sync_table, credentials_table,
            install_cognito):
        """A disabled PortalAdmin does not count toward the enabled
        pool, so deleting it never reduces the count - the pool is not
        paginated and the delete proceeds."""
        fake = install_cognito(users=[
            cognito_user("admin-1", role="PortalAdmin"),
            cognito_user("old-admin", role="PortalAdmin", enabled=False),
        ])
        status, _ = invoke(user_admin, delete_event("old-admin"))
        assert status == 200
        assert fake.list_calls == []
        assert fake.exists("old-admin") is False

    def test_deleting_a_non_portal_admin_is_not_guarded(
            self, user_admin, audit_table, sync_table, credentials_table,
            install_cognito):
        fake = install_cognito(users=[
            cognito_user("admin-1", role="PortalAdmin"),
            cognito_user("op1", role="Operator"),
        ])
        status, _ = invoke(user_admin, delete_event("op1"))
        assert status == 200
        assert fake.list_calls == []


class TestCognitoFailure:
    def test_cognito_failure_aborts_before_the_verifier_is_touched(
            self, user_admin, audit_table, sync_table, credentials_table,
            install_cognito):
        """Req 14.6: a Cognito failure -> 502 with the account and its
        verifier record unchanged, audit-final failure, and no sync
        staging."""
        put_verifier(credentials_table, "op1")
        fake = install_cognito(
            users=[
                cognito_user("op1", role="Operator"),
                cognito_user("admin-1", role="PortalAdmin"),
            ],
            delete_error=cognito_error(
                "InternalErrorException", "Something went wrong"),
        )
        stage_device(sync_table, "dev-1", {
            "op1": {"email": "op1@example.com", "role": "Operator",
                    "enabled": True}})

        status, body = invoke(user_admin, delete_event("op1"))

        assert status == 502
        assert body["error"] == "deletion failed"
        assert fake.exists("op1") is True
        assert verifier_record(credentials_table, "op1") is not None

        entries = audit_entries(audit_table)
        assert len(entries) == 1
        assert entries[0]["result"] == "failure"
        assert entries[0]["action"] == "account_delete"

        row = sync_row(sync_table, "dev-1")
        assert row["pendingChanges"] is False
        assert row["syncId"] == "sync-old"


class TestPartialVerifierCleanupFailure:
    def test_verifier_delete_failure_reports_partial_cleanup(
            self, user_admin, audit_table, sync_table, credentials_table,
            install_cognito, monkeypatch):
        """Req 14.10: a verifier-delete failure after a successful
        Cognito delete retains the record for a subsequent attempt,
        finalizes the audit entry with a partial-cleanup detail, and
        returns an error stating the account was deleted but its
        verifier record was not removed."""
        put_verifier(credentials_table, "op1")
        fake = install_cognito(users=[
            cognito_user("op1", role="Operator", email="op1@example.com"),
            cognito_user("admin-1", role="PortalAdmin"),
        ])

        real_table = user_admin.dynamodb.Table

        class FailingCredentialsTable:
            def delete_item(self, **kwargs):
                raise RuntimeError("DynamoDB unavailable")

        def table_router(name):
            if name == EDGE_CREDENTIALS_TABLE:
                return FailingCredentialsTable()
            return real_table(name)

        monkeypatch.setattr(user_admin.dynamodb, "Table", table_router)

        status, body = invoke(user_admin, delete_event("op1"))

        # The account is gone from the pool; the error message carries
        # the exact frontend-classifier phrases (/deleted/i and
        # /not removed/i).
        assert status == 502
        assert fake.exists("op1") is False
        assert re.search(r"deleted", body["message"], re.IGNORECASE)
        assert re.search(r"not removed", body["message"], re.IGNORECASE)

        # The verifier record is retained for a subsequent attempt.
        assert verifier_record(credentials_table, "op1") is not None

        # The audit entry is finalized with a partial-cleanup detail.
        entries = audit_entries(audit_table)
        assert len(entries) == 1
        entry = entries[0]
        assert entry["action"] == "account_delete"
        assert entry["result"] == "success"
        assert "not removed" in entry["details"]["partial_cleanup"]

    def test_partial_cleanup_still_stages_the_deletion_for_sync(
            self, user_admin, audit_table, sync_table, credentials_table,
            install_cognito, monkeypatch):
        """The account was deleted from the User_Pool, so devices are
        still told it is disabled/deleted (7.8) even when the verifier
        cleanup fails."""
        put_verifier(credentials_table, "op1")
        install_cognito(users=[
            cognito_user("op1", role="Operator", email="op1@example.com"),
            cognito_user("admin-1", role="PortalAdmin"),
        ])
        stage_device(sync_table, "dev-1", {
            "op1": {"email": "op1@example.com", "role": "Operator",
                    "enabled": True}})

        real_table = user_admin.dynamodb.Table

        class FailingCredentialsTable:
            def delete_item(self, **kwargs):
                raise RuntimeError("DynamoDB unavailable")

        def table_router(name):
            if name == EDGE_CREDENTIALS_TABLE:
                return FailingCredentialsTable()
            return real_table(name)

        monkeypatch.setattr(user_admin.dynamodb, "Table", table_router)

        status, _ = invoke(user_admin, delete_event("op1"))
        assert status == 502

        row = sync_row(sync_table, "dev-1")
        assert row["accounts"]["op1"]["enabled"] is False
        assert row["accounts"]["op1"]["deleted"] is True
        assert row["pendingChanges"] is True


class TestAuditBeforeEffect:
    def test_pending_audit_failure_blocks_the_deletion(
            self, user_admin, audit_table, sync_table, credentials_table,
            install_cognito, monkeypatch):
        """Req 6.4/6.5: when the pending audit entry cannot be recorded,
        the deletion is not applied - zero mutation calls and an error
        stating the action was not applied."""
        put_verifier(credentials_table, "op1")
        fake = install_cognito(users=[
            cognito_user("op1", role="Operator"),
            cognito_user("admin-1", role="PortalAdmin"),
        ])

        def failing_audit(*args, **kwargs):
            raise RuntimeError("audit table unavailable")

        monkeypatch.setattr(user_admin, "record_audit_event_strict",
                            failing_audit)
        status, body = invoke(user_admin, delete_event("op1"))

        assert status == 500
        assert body["message"] == "The action was not applied"
        assert fake.delete_calls == []
        assert fake.exists("op1") is True
        assert verifier_record(credentials_table, "op1") is not None
