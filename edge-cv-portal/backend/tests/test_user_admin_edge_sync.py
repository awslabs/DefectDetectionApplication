"""
Unit tests for the Account_Sync_Service portal side in user_admin.py
(spec: portal-user-manager, task 3.1).

Covers: the pure build_sync_document builder (whitelisted fields, no
plaintext, disabled/deleted accounts marked enabled=false and never
dropped, 8 KB shadow-limit validation), GET /api/v1/admin/edge-sync/
devices (devices table joined with dda-portal-account-sync), POST
/api/v1/admin/edge-sync/devices/{deviceId} (staging the selected full
account set with a fresh syncId, pendingChanges=true, sync Lambda
invocation tolerated absent), and the attribute-change hook that marks
every device's staged set updated and pending after verifier capture
and role changes (Req 7.2).

Cognito is a recording fake client (same pattern as the other
test_user_admin_* files); DynamoDB tables run against real moto.

_Requirements: 7.1, 7.2, 7.3, 7.8_
"""
import json
import os
import sys

import pytest
from botocore.exceptions import ClientError

REGION = "us-east-1"
EDGE_CREDENTIALS_TABLE = "test-edge-credentials"
ACCOUNT_SYNC_TABLE = "test-account-sync"
DEVICES_TABLE = "test-devices"
AUDIT_LOG_TABLE = "test-audit-log"
POOL_ID = "us-east-1_testpool"


# ----------------------------------------------------- fake Cognito client

def cognito_error(code, message, operation="ListUsers"):
    return ClientError({"Error": {"Code": code, "Message": message}},
                       operation)


class FakeCognitoClient:
    """Recording fake for list_users (paginated), admin_get_user,
    admin_set_user_password, and admin_update_user_attributes."""

    def __init__(self, users=None, page_size=60):
        self.users = list(users or [])
        self.list_calls = []
        self.page_size = page_size

    def _find(self, username):
        for user in self.users:
            if user["Username"] == username:
                return user
        return None

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

    def admin_get_user(self, **kwargs):
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

    def admin_set_user_password(self, **kwargs):
        if self._find(kwargs["Username"]) is None:
            raise cognito_error("UserNotFoundException",
                                "User does not exist.",
                                "AdminSetUserPassword")

    def admin_update_user_attributes(self, **kwargs):
        user = self._find(kwargs["Username"])
        if user is None:
            raise cognito_error("UserNotFoundException",
                                "User does not exist.",
                                "AdminUpdateUserAttributes")
        for new_attr in kwargs["UserAttributes"]:
            attrs = user.setdefault("Attributes", [])
            for attr in attrs:
                if attr["Name"] == new_attr["Name"]:
                    attr["Value"] = new_attr["Value"]
                    break
            else:
                attrs.append(dict(new_attr))


def cognito_user(username, role=None, enabled=True, email=None):
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
    for table_name in (EDGE_CREDENTIALS_TABLE,):
        if table_name not in existing:
            ddb.create_table(
                TableName=table_name,
                KeySchema=[{"AttributeName": "username",
                            "KeyType": "HASH"}],
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


def _clear_table(table, key_names):
    for item in table.scan()["Items"]:
        table.delete_item(Key={k: item[k] for k in key_names})


@pytest.fixture
def tables(user_admin):
    """The moto-backed tables this feature touches, emptied per test."""
    import boto3
    resource = boto3.resource("dynamodb", region_name=REGION)
    sync = resource.Table(ACCOUNT_SYNC_TABLE)
    credentials = resource.Table(EDGE_CREDENTIALS_TABLE)
    devices = resource.Table(DEVICES_TABLE)
    audit = resource.Table(AUDIT_LOG_TABLE)
    _clear_table(sync, ("device_id",))
    _clear_table(credentials, ("username",))
    _clear_table(devices, ("device_id",))
    _clear_table(audit, ("event_id", "timestamp"))
    return {"sync": sync, "credentials": credentials,
            "devices": devices, "audit": audit}


@pytest.fixture
def install_cognito(user_admin, monkeypatch):
    def _install(users=None, page_size=60):
        fake = FakeCognitoClient(users=users, page_size=page_size)
        monkeypatch.setattr(user_admin, "cognito_client", fake)
        monkeypatch.setattr(user_admin, "USER_POOL_ID", POOL_ID)
        return fake
    return _install


# ---------------------------------------------------------------- helpers

def admin_event(method, path, path_parameters=None, body=None,
                caller_role="PortalAdmin"):
    return {
        "httpMethod": method,
        "path": path,
        "pathParameters": path_parameters,
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


def list_devices_event(caller_role="PortalAdmin"):
    return admin_event("GET", "/api/v1/admin/edge-sync/devices",
                       caller_role=caller_role)


def sync_event(device_id, usernames, caller_role="PortalAdmin"):
    return admin_event(
        "POST", f"/api/v1/admin/edge-sync/devices/{device_id}",
        path_parameters={"deviceId": device_id},
        body={"usernames": usernames}, caller_role=caller_role)


def invoke(user_admin, event):
    response = user_admin.handler(event, None)
    return response["statusCode"], json.loads(response["body"])


class RecordingLambdaClient:
    def __init__(self, error=None):
        self.calls = []
        self.error = error

    def invoke(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return {"StatusCode": 202}


# ------------------------------------------------- build_sync_document

class TestBuildSyncDocument:
    def test_complete_account_set_with_whitelisted_fields(
            self, user_admin):
        """Req 7.1, 7.3: the document carries exactly the selected
        records with only {email, role, enabled, deleted?, verifier?};
        unexpected input keys (e.g. plaintext) are dropped
        structurally."""
        verifier = user_admin.make_verifier("Sup3rSecret!pw", iterations=10)
        accounts = {
            "op1": {"email": "op1@example.com", "role": "Operator",
                    "enabled": True, "verifier": verifier,
                    "password": "Sup3rSecret!pw"},
            "viewer": {"email": "v@example.com", "role": "Viewer",
                       "enabled": True},
        }
        document = user_admin.build_sync_document(accounts, "sync-1")

        assert document["syncId"] == "sync-1"
        assert document["version"] == 1
        assert set(document["accounts"]) == {"op1", "viewer"}
        assert document["accounts"]["op1"] == {
            "email": "op1@example.com",
            "role": "Operator",
            "enabled": True,
            "verifier": {
                "algorithm": verifier["algorithm"],
                "iterations": 10,
                "salt": verifier["salt"],
                "hash": verifier["hash"],
            },
        }
        assert "Sup3rSecret!pw" not in json.dumps(document)
        assert "verifier" not in document["accounts"]["viewer"]

    def test_disabled_and_deleted_accounts_marked_never_dropped(
            self, user_admin):
        """Req 7.8: disabled/deleted accounts appear marked
        enabled=false; a deleted account is flagged, never dropped."""
        accounts = {
            "disabled-op": {"email": "d@example.com", "role": "Operator",
                            "enabled": False},
            "gone": {"email": "g@example.com", "role": "Viewer",
                     "enabled": True, "deleted": True},
        }
        document = user_admin.build_sync_document(accounts, "sync-2")

        assert document["accounts"]["disabled-op"]["enabled"] is False
        assert document["accounts"]["gone"]["enabled"] is False
        assert document["accounts"]["gone"]["deleted"] is True
        assert set(document["accounts"]) == {"disabled-op", "gone"}

    def test_missing_role_defaults_to_viewer(self, user_admin):
        document = user_admin.build_sync_document(
            {"u": {"email": "u@example.com", "enabled": True}}, "s")
        assert document["accounts"]["u"]["role"] == "Viewer"

    def test_document_over_8kb_raises_with_explicit_reason(
            self, user_admin):
        accounts = {
            f"user-{i:04d}": {"email": "x" * 60 + "@example.com",
                              "role": "Operator", "enabled": True}
            for i in range(100)
        }
        with pytest.raises(user_admin.SyncDocumentTooLarge) as excinfo:
            user_admin.build_sync_document(accounts, "sync-3")
        assert "8 KB" in str(excinfo.value)

    def test_document_within_limit_builds(self, user_admin):
        accounts = {"u": {"email": "u@example.com", "role": "Viewer",
                          "enabled": True}}
        assert user_admin.build_sync_document(accounts, "s")


# ------------------------------------------- GET /admin/edge-sync/devices

class TestListSyncDevices:
    @pytest.mark.parametrize(
        "caller_role", ["Viewer", "Operator", "DataScientist",
                        "UseCaseAdmin"])
    def test_non_portal_admin_rejected_403(
            self, user_admin, tables, caller_role):
        status, body = invoke(
            user_admin, list_devices_event(caller_role=caller_role))
        assert status == 403
        assert body["error"] == "Access denied"

    def test_device_without_sync_row_reports_never_synced(
            self, user_admin, tables):
        tables["devices"].put_item(Item={"device_id": "edge-1"})
        status, body = invoke(user_admin, list_devices_event())

        assert status == 200
        assert body["count"] == 1
        assert body["devices"] == [{
            "device_id": "edge-1",
            "lastSyncStatus": None,
            "lastSyncAt": None,
            "pendingChanges": False,
            "failureReason": None,
        }]

    def test_join_carries_sync_state_fields(self, user_admin, tables):
        """Req 7.4 display data: lastSyncStatus, lastSyncAt,
        pendingChanges, and failureReason come from the sync-state
        table."""
        tables["devices"].put_item(Item={"device_id": "edge-1"})
        tables["devices"].put_item(Item={"device_id": "edge-2"})
        tables["sync"].put_item(Item={
            "device_id": "edge-1", "syncId": "s1", "status": "success",
            "lastSyncAt": 1700000000000, "pendingChanges": False,
        })
        tables["sync"].put_item(Item={
            "device_id": "edge-2", "syncId": "s2", "status": "failed",
            "failureReason": "device unreachable",
            "pendingChanges": True,
        })

        status, body = invoke(user_admin, list_devices_event())

        assert status == 200
        by_id = {d["device_id"]: d for d in body["devices"]}
        assert by_id["edge-1"]["lastSyncStatus"] == "success"
        assert by_id["edge-1"]["lastSyncAt"] == 1700000000000
        assert by_id["edge-1"]["pendingChanges"] is False
        assert by_id["edge-2"]["lastSyncStatus"] == "failed"
        assert by_id["edge-2"]["failureReason"] == "device unreachable"
        assert by_id["edge-2"]["pendingChanges"] is True

    def test_staged_device_missing_from_devices_table_still_listed(
            self, user_admin, tables):
        tables["sync"].put_item(Item={
            "device_id": "ghost-device", "syncId": "s", "status":
            "pending", "pendingChanges": True,
        })
        status, body = invoke(user_admin, list_devices_event())
        assert status == 200
        assert [d["device_id"] for d in body["devices"]] == ["ghost-device"]


# ------------------------------- POST /admin/edge-sync/devices/{deviceId}

class TestSyncDevice:
    @pytest.mark.parametrize(
        "caller_role", ["Viewer", "Operator", "DataScientist",
                        "UseCaseAdmin"])
    def test_non_portal_admin_rejected_403(
            self, user_admin, tables, install_cognito, caller_role):
        fake = install_cognito(users=[cognito_user("op1")])
        status, body = invoke(user_admin, sync_event(
            "edge-1", ["op1"], caller_role=caller_role))
        assert status == 403
        assert body["error"] == "Access denied"
        assert fake.list_calls == []
        assert tables["sync"].scan()["Items"] == []

    @pytest.mark.parametrize("bad", [None, [], ["op1", 42], "op1", [""]])
    def test_invalid_usernames_body_rejected_400(
            self, user_admin, tables, install_cognito, bad):
        install_cognito(users=[cognito_user("op1")])
        status, body = invoke(user_admin, admin_event(
            "POST", "/api/v1/admin/edge-sync/devices/edge-1",
            path_parameters={"deviceId": "edge-1"},
            body={"usernames": bad}))
        assert status == 400
        assert tables["sync"].scan()["Items"] == []

    def test_unknown_usernames_rejected_400_nothing_staged(
            self, user_admin, tables, install_cognito):
        install_cognito(users=[cognito_user("op1")])
        status, body = invoke(user_admin, sync_event(
            "edge-1", ["op1", "ghost"]))
        assert status == 400
        assert body["error"] == "Unknown usernames"
        assert "ghost" in body["message"]
        assert tables["sync"].scan()["Items"] == []

    def test_stages_full_account_set_with_fresh_sync_id_and_pending(
            self, user_admin, tables, install_cognito):
        """Req 7.1, 7.3, 7.8: the staged set carries each selected
        account's email, role, enabled state, and captured verifier
        (never plaintext); a disabled account is staged marked
        enabled=false, not dropped."""
        install_cognito(users=[
            cognito_user("op1", role="Operator", email="op1@example.com"),
            cognito_user("Viewer1", role=None, email="v1@example.com",
                         enabled=False),
            cognito_user("unselected", role="Viewer"),
        ])
        # op1 has a captured verifier (edge-login-capable)
        tables["credentials"].put_item(Item={
            "username": "op1",
            "verifier": user_admin.make_verifier("S0me!Password", 10),
            "updatedAt": 1,
        })

        status, body = invoke(user_admin, sync_event(
            "edge-1", ["op1", "Viewer1"]))

        assert status == 200
        assert body["device_id"] == "edge-1"
        assert body["accountCount"] == 2
        assert body["pendingChanges"] is True
        assert body["syncId"]
        # Sync Lambda absent (env unset) is tolerated gracefully
        assert body["syncInvoked"] is False

        rows = tables["sync"].scan()["Items"]
        assert len(rows) == 1
        row = rows[0]
        assert row["device_id"] == "edge-1"
        assert row["syncId"] == body["syncId"]
        assert row["status"] == "pending"
        assert row["pendingChanges"] is True
        assert set(row["accounts"]) == {"op1", "Viewer1"}
        assert row["accounts"]["op1"]["email"] == "op1@example.com"
        assert row["accounts"]["op1"]["role"] == "Operator"
        assert row["accounts"]["op1"]["enabled"] is True
        assert row["accounts"]["op1"]["verifier"]["algorithm"] == \
            "pbkdf2-sha256"
        # Disabled account staged marked enabled=false, never dropped
        assert row["accounts"]["Viewer1"]["enabled"] is False
        assert row["accounts"]["Viewer1"]["role"] == "Viewer"
        # Never plaintext anywhere in the staged set (7.3)
        assert "S0me!Password" not in json.dumps(
            row["accounts"], default=str)

    def test_restaging_preserves_last_sync_at_and_clears_failure(
            self, user_admin, tables, install_cognito):
        install_cognito(users=[cognito_user("op1", role="Operator")])
        tables["sync"].put_item(Item={
            "device_id": "edge-1", "syncId": "old-sync",
            "status": "failed", "failureReason": "device unreachable",
            "lastSyncAt": 1700000000000, "pendingChanges": False,
            "accounts": {},
        })

        status, body = invoke(user_admin, sync_event("edge-1", ["op1"]))

        assert status == 200
        row = tables["sync"].get_item(
            Key={"device_id": "edge-1"})["Item"]
        assert row["syncId"] == body["syncId"] != "old-sync"
        assert row["status"] == "pending"
        assert row["pendingChanges"] is True
        assert row["lastSyncAt"] == 1700000000000
        assert "failureReason" not in row

    def test_invokes_sync_lambda_when_configured(
            self, user_admin, tables, install_cognito, monkeypatch):
        install_cognito(users=[cognito_user("op1", role="Operator")])
        recorder = RecordingLambdaClient()
        monkeypatch.setattr(user_admin, "ACCOUNT_SYNC_FUNCTION",
                            "test-account-sync-fn")
        monkeypatch.setattr(user_admin, "lambda_client", recorder)

        status, body = invoke(user_admin, sync_event("edge-1", ["op1"]))

        assert status == 200
        assert body["syncInvoked"] is True
        assert len(recorder.calls) == 1
        call = recorder.calls[0]
        assert call["FunctionName"] == "test-account-sync-fn"
        assert call["InvocationType"] == "Event"
        payload = json.loads(call["Payload"])
        assert payload["device_id"] == "edge-1"
        assert payload["syncId"] == body["syncId"]

    def test_sync_lambda_invoke_failure_tolerated(
            self, user_admin, tables, install_cognito, monkeypatch):
        """An invoke failure never fails the staging: the row stays
        pending for the scheduled attempt."""
        install_cognito(users=[cognito_user("op1", role="Operator")])
        recorder = RecordingLambdaClient(
            error=RuntimeError("lambda unavailable"))
        monkeypatch.setattr(user_admin, "ACCOUNT_SYNC_FUNCTION",
                            "test-account-sync-fn")
        monkeypatch.setattr(user_admin, "lambda_client", recorder)

        status, body = invoke(user_admin, sync_event("edge-1", ["op1"]))

        assert status == 200
        assert body["syncInvoked"] is False
        row = tables["sync"].get_item(
            Key={"device_id": "edge-1"})["Item"]
        assert row["pendingChanges"] is True

    def test_document_over_8kb_rejected_400_nothing_staged(
            self, user_admin, tables, install_cognito):
        users = [
            cognito_user(f"user-{i:04d}", role="Operator",
                         email="x" * 60 + "@example.com")
            for i in range(100)
        ]
        install_cognito(users=users)

        status, body = invoke(user_admin, sync_event(
            "edge-1", [u["Username"] for u in users]))

        assert status == 400
        assert body["error"] == "Sync document too large"
        assert "8 KB" in body["message"]
        assert tables["sync"].scan()["Items"] == []


# --------------------------------------- attribute-change hook (Req 7.2)

def password_event(username, body):
    return admin_event(
        "POST", f"/api/v1/admin/users/{username}/password",
        path_parameters={"username": username}, body=body)


def role_event(username, body):
    return admin_event(
        "PUT", f"/api/v1/admin/users/{username}/role",
        path_parameters={"username": username}, body=body)


class TestAttributeChangePropagation:
    def _stage(self, tables, device_id, accounts, sync_id="initial-sync"):
        tables["sync"].put_item(Item={
            "device_id": device_id, "syncId": sync_id,
            "status": "success", "pendingChanges": False,
            "lastSyncAt": 1700000000000, "accounts": accounts,
        })

    def test_verifier_capture_marks_every_staged_device_pending(
            self, user_admin, tables, install_cognito):
        """Req 7.2: after a password set captures a fresh verifier,
        every device whose staged set contains the account is
        refreshed (new verifier, fresh syncId, pendingChanges=true)."""
        install_cognito(users=[cognito_user(
            "op1", role="Operator", email="op1@example.com")])
        staged = {"op1": {"email": "op1@example.com", "role": "Operator",
                          "enabled": True}}
        self._stage(tables, "edge-1", dict(staged))
        self._stage(tables, "edge-2", dict(staged), sync_id="other-sync")
        self._stage(tables, "edge-3", {
            "someone-else": {"email": "x@example.com", "role": "Viewer",
                             "enabled": True}})

        status, _ = invoke(user_admin, password_event(
            "op1", {"password": "N3w!Password4Op1", "permanent": True}))
        assert status == 200

        for device_id, old_sync_id in (("edge-1", "initial-sync"),
                                       ("edge-2", "other-sync")):
            row = tables["sync"].get_item(
                Key={"device_id": device_id})["Item"]
            assert row["pendingChanges"] is True, device_id
            assert row["status"] == "pending"
            assert row["syncId"] != old_sync_id
            record = row["accounts"]["op1"]
            assert record["verifier"]["algorithm"] == "pbkdf2-sha256"
            assert record["email"] == "op1@example.com"
            # Never plaintext in the staged set (7.3)
            assert "N3w!Password4Op1" not in json.dumps(
                row["accounts"], default=str)

        # A device whose staged set does not contain the account is
        # untouched.
        row = tables["sync"].get_item(Key={"device_id": "edge-3"})["Item"]
        assert row["pendingChanges"] is False
        assert row["syncId"] == "initial-sync"

    def test_role_change_refreshes_staged_record_and_marks_pending(
            self, user_admin, tables, install_cognito):
        """Req 7.2: a successful role change updates the staged record's
        role on every device containing the account."""
        install_cognito(users=[
            cognito_user("op1", role="Operator", email="op1@example.com"),
            cognito_user("admin-1", role="PortalAdmin"),
        ])
        self._stage(tables, "edge-1", {
            "op1": {"email": "op1@example.com", "role": "Operator",
                    "enabled": True}})

        status, _ = invoke(user_admin, role_event(
            "op1", {"role": "DataScientist"}))
        assert status == 200

        row = tables["sync"].get_item(Key={"device_id": "edge-1"})["Item"]
        assert row["pendingChanges"] is True
        assert row["status"] == "pending"
        assert row["syncId"] != "initial-sync"
        assert row["accounts"]["op1"]["role"] == "DataScientist"
        assert row["accounts"]["op1"]["enabled"] is True

    def test_staged_username_matched_case_insensitively(
            self, user_admin, tables, install_cognito):
        """Staged sets key accounts by the Cognito username; the hook
        matches case-insensitively (the credentials table lowercases)."""
        install_cognito(users=[cognito_user(
            "Op1", role="Operator", email="op1@example.com")])
        self._stage(tables, "edge-1", {
            "Op1": {"email": "op1@example.com", "role": "Operator",
                    "enabled": True}})

        status, _ = invoke(user_admin, password_event(
            "Op1", {"password": "N3w!Password4Op1", "permanent": True}))
        assert status == 200

        row = tables["sync"].get_item(Key={"device_id": "edge-1"})["Item"]
        assert row["pendingChanges"] is True
        assert "verifier" in row["accounts"]["Op1"]

    def test_propagation_failure_never_fails_the_primary_action(
            self, user_admin, tables, install_cognito, monkeypatch):
        """A failing sync-table update is logged, not raised: the
        password change itself still succeeds."""
        install_cognito(users=[cognito_user(
            "op1", role="Operator", email="op1@example.com")])

        real_table = user_admin.dynamodb.Table

        def failing_table(name):
            if name == ACCOUNT_SYNC_TABLE:
                raise RuntimeError("sync table unavailable")
            return real_table(name)

        monkeypatch.setattr(user_admin.dynamodb, "Table", failing_table)

        status, body = invoke(user_admin, password_event(
            "op1", {"password": "N3w!Password4Op1", "permanent": True}))
        assert status == 200
        assert body["username"] == "op1"
