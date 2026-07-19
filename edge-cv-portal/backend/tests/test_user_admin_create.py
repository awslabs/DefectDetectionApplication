"""
Unit tests for validate_create_request and POST /api/v1/admin/users in
user_admin.py (spec: portal-user-manager, task 13.2).

Covers: the pure validation gate (all three fields present and
non-empty with rejections naming the missing field, email shape, role
in the five defined Portal_Role values; a rejection performs no
User_Pool call), the validate -> audit-pending (account_create) ->
admin_create_user with custom:role / email / email_verified=true and
the Cognito-native email invitation (D12: default MessageAction, no
SES, no portal-generated password, no verifier capture) -> audit-final
flow, UsernameExistsException / AliasExistsException -> 409 "account
already exists" with the Cognito Message passed through (duplicate
username and email-alias conflict cases),
other Cognito errors -> 502 "account was not created" with audit-final
failure, the audit-before-effect abort, and the PortalAdmin 403 gate.

Cognito is a recording fake client (same pattern as the other
test_user_admin_* files); the audit entries and the edge-credentials
table run against real moto-backed DynamoDB.

_Requirements: 12.1, 12.3, 12.5, 12.6, 12.7, 12.8, 12.9, 12.11_
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

VALID_BODY = {
    "username": "newuser1",
    "email": "new.user@example.com",
    "role": "Operator",
}


# ----------------------------------------------------- fake Cognito client

class FakeCognitoClient:
    """Recording fake for the cognito-idp admin_create_user API."""

    def __init__(self, error=None):
        self.error = error
        self.calls = []

    def admin_create_user(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error


def cognito_error(code, message):
    return ClientError({"Error": {"Code": code, "Message": message}},
                       "AdminCreateUser")


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
def credentials_table(user_admin):
    """The moto-backed edge-credentials table, emptied per test."""
    import boto3
    table = boto3.resource("dynamodb", region_name=REGION).Table(
        EDGE_CREDENTIALS_TABLE)
    for item in table.scan()["Items"]:
        table.delete_item(Key={"username": item["username"]})
    return table


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
    def _install(error=None):
        fake = FakeCognitoClient(error=error)
        monkeypatch.setattr(user_admin, "cognito_client", fake)
        monkeypatch.setattr(user_admin, "USER_POOL_ID", POOL_ID)
        return fake
    return _install


# ---------------------------------------------------------------- helpers

def create_event(body=None, role="PortalAdmin"):
    return {
        "httpMethod": "POST",
        "path": "/api/v1/admin/users",
        "body": json.dumps(body) if body is not None else None,
        "requestContext": {
            "authorizer": {
                "claims": {
                    "sub": "admin-1",
                    "email": "admin@example.com",
                    "cognito:username": "admin-1",
                    "custom:role": role,
                }
            }
        },
    }


def invoke(user_admin, event):
    response = user_admin.handler(event, None)
    return response["statusCode"], json.loads(response["body"])


def audit_entries(audit_table):
    return audit_table.scan()["Items"]


# ---------------------------------------------- validate_create_request

class TestValidateCreateRequest:
    def test_valid_payload_passes(self, user_admin):
        assert user_admin.validate_create_request(dict(VALID_BODY)) is None

    @pytest.mark.parametrize("field", ["username", "email", "role"])
    def test_missing_field_rejected_naming_the_field(
            self, user_admin, field):
        """Req 12.7: a missing field is rejected and the rejection names
        the missing field."""
        body = dict(VALID_BODY)
        del body[field]
        rejection = user_admin.validate_create_request(body)
        assert rejection is not None
        assert rejection["field"] == field
        assert field in rejection["message"]

    @pytest.mark.parametrize("field", ["username", "email", "role"])
    def test_empty_field_rejected_naming_the_field(self, user_admin, field):
        body = dict(VALID_BODY)
        body[field] = ""
        rejection = user_admin.validate_create_request(body)
        assert rejection is not None
        assert rejection["field"] == field

    def test_none_body_rejected(self, user_admin):
        rejection = user_admin.validate_create_request(None)
        assert rejection is not None
        assert rejection["field"] == "username"

    @pytest.mark.parametrize("email", [
        "no-at-sign.example.com",   # no @
        "@example.com",             # empty local part
        "user@",                    # empty domain
        "user@nodot",               # domain without a dot
        "user@@example.com",        # two separators
        "a@b@c.com",                # two separators
    ])
    def test_invalid_email_shapes_rejected(self, user_admin, email):
        """Req 12.6: the email must be a non-empty local part, '@', and
        a non-empty domain containing at least one dot."""
        body = dict(VALID_BODY, email=email)
        rejection = user_admin.validate_create_request(body)
        assert rejection is not None
        assert rejection["field"] == "email"

    @pytest.mark.parametrize("email", [
        "user@example.com",
        "first.last@sub.example.co",
        "x@y.z",
    ])
    def test_valid_email_shapes_pass(self, user_admin, email):
        body = dict(VALID_BODY, email=email)
        assert user_admin.validate_create_request(body) is None

    @pytest.mark.parametrize("role", [
        "Admin", "portaladmin", "SuperUser", "viewer", " Operator"])
    def test_undefined_role_rejected(self, user_admin, role):
        """Req 12.8: the role must be one of the five defined
        Portal_Role values."""
        body = dict(VALID_BODY, role=role)
        rejection = user_admin.validate_create_request(body)
        assert rejection is not None
        assert rejection["field"] == "role"

    @pytest.mark.parametrize("role", [
        "PortalAdmin", "UseCaseAdmin", "DataScientist", "Operator",
        "Viewer"])
    def test_all_five_defined_roles_pass(self, user_admin, role):
        body = dict(VALID_BODY, role=role)
        assert user_admin.validate_create_request(body) is None


# ------------------------------------------------------------------ tests

class TestPortalAdminGate:
    @pytest.mark.parametrize(
        "role", ["Viewer", "Operator", "DataScientist", "UseCaseAdmin"])
    def test_non_portal_admin_rejected_403(
            self, user_admin, credentials_table, audit_table,
            install_cognito, role):
        """Req 1.5: non-PortalAdmin callers get 403 with zero Cognito
        calls and no audit entry."""
        fake = install_cognito()
        status, body = invoke(user_admin, create_event(
            dict(VALID_BODY), role=role))
        assert status == 403
        assert body["error"] == "Access denied"
        assert fake.calls == []
        assert audit_entries(audit_table) == []


class TestCreateSuccess:
    def test_creates_account_with_cognito_native_invitation(
            self, user_admin, credentials_table, audit_table,
            install_cognito):
        """Req 12.1, 12.3 (D12): admin_create_user carries the exact
        submitted username, email (email_verified=true), and custom:role,
        with the default MessageAction (Cognito sends the invitation)
        and no portal-generated TemporaryPassword."""
        fake = install_cognito()
        status, body = invoke(user_admin, create_event(dict(VALID_BODY)))

        assert status == 201
        assert body["username"] == VALID_BODY["username"]
        assert body["email"] == VALID_BODY["email"]
        assert body["role"] == VALID_BODY["role"]

        assert len(fake.calls) == 1
        call = fake.calls[0]
        assert call["UserPoolId"] == POOL_ID
        assert call["Username"] == VALID_BODY["username"]
        attrs = {a["Name"]: a["Value"] for a in call["UserAttributes"]}
        assert attrs == {
            "email": VALID_BODY["email"],
            "email_verified": "true",
            "custom:role": VALID_BODY["role"],
        }
        # D12: the Cognito-native invitation is the default MessageAction
        # (neither suppressed nor resent) and the portal generates no
        # password.
        assert "MessageAction" not in call
        assert "TemporaryPassword" not in call

    def test_success_finalizes_audit_with_created_account_details(
            self, user_admin, credentials_table, audit_table,
            install_cognito):
        """Req 12.11: exactly one account_create audit entry, finalized
        to success, carrying the acting administrator and the created
        account's username, email, and role."""
        install_cognito()
        status, _ = invoke(user_admin, create_event(dict(VALID_BODY)))

        assert status == 201
        entries = audit_entries(audit_table)
        assert len(entries) == 1
        entry = entries[0]
        assert entry["action"] == "account_create"
        assert entry["result"] == "success"
        assert entry["resource_type"] == "user_account"
        assert entry["resource_id"] == VALID_BODY["username"]
        assert entry["user_id"] == "admin-1"
        assert entry["completed_at"] > 0
        details = entry["details"]
        assert details["username"] == VALID_BODY["username"]
        assert details["email"] == VALID_BODY["email"]
        assert details["role"] == VALID_BODY["role"]

    def test_no_verifier_captured_at_creation(
            self, user_admin, credentials_table, audit_table,
            install_cognito):
        """D12: the invitation password is never held by the portal, so
        no verifier record is written at creation."""
        install_cognito()
        status, _ = invoke(user_admin, create_event(dict(VALID_BODY)))
        assert status == 201
        assert credentials_table.scan()["Items"] == []


class TestValidationGate:
    @pytest.mark.parametrize("field", ["username", "email", "role"])
    def test_missing_field_rejected_400_naming_field_no_pool_call(
            self, user_admin, credentials_table, audit_table,
            install_cognito, field):
        """Req 12.7: the 400 response identifies the missing field and
        the rejection performs no User_Pool call and writes no audit
        pending entry."""
        fake = install_cognito()
        body = dict(VALID_BODY)
        del body[field]
        status, resp = invoke(user_admin, create_event(body))

        assert status == 400
        assert resp["field"] == field
        assert fake.calls == []
        assert audit_entries(audit_table) == []

    def test_invalid_email_rejected_400_no_pool_call(
            self, user_admin, credentials_table, audit_table,
            install_cognito):
        """Req 12.6: an invalid email shape is rejected identifying the
        email, with no User_Pool call."""
        fake = install_cognito()
        status, resp = invoke(user_admin, create_event(
            dict(VALID_BODY, email="user@nodot")))

        assert status == 400
        assert resp["field"] == "email"
        assert fake.calls == []
        assert audit_entries(audit_table) == []

    def test_undefined_role_rejected_400_no_pool_call(
            self, user_admin, credentials_table, audit_table,
            install_cognito):
        """Req 12.8: a role outside the five defined values is rejected
        with no User_Pool call."""
        fake = install_cognito()
        status, resp = invoke(user_admin, create_event(
            dict(VALID_BODY, role="SuperUser")))

        assert status == 400
        assert resp["field"] == "role"
        assert fake.calls == []
        assert audit_entries(audit_table) == []

    def test_invalid_json_body_rejected_400(
            self, user_admin, credentials_table, audit_table,
            install_cognito):
        fake = install_cognito()
        event = create_event()
        event["body"] = "{not json"
        status, _ = invoke(user_admin, event)
        assert status == 400
        assert fake.calls == []


class TestAccountConflict:
    def test_username_exists_maps_to_409_with_cognito_message(
            self, user_admin, credentials_table, audit_table,
            install_cognito):
        """Req 12.5: UsernameExistsException -> 409 "account already
        exists" with the Cognito Message ("User account already exists"
        for a true duplicate username) passed through, no account
        created or modified, and the audit entry finalized to failure."""
        install_cognito(error=cognito_error(
            "UsernameExistsException", "User account already exists."))
        status, body = invoke(user_admin, create_event(dict(VALID_BODY)))

        assert status == 409
        assert body["error"] == "account already exists"
        assert body["message"] == "User account already exists."
        assert credentials_table.scan()["Items"] == []

        entries = audit_entries(audit_table)
        assert len(entries) == 1
        assert entries[0]["result"] == "failure"
        assert entries[0]["action"] == "account_create"

    def test_email_alias_conflict_passes_cognito_message_through(
            self, user_admin, credentials_table, audit_table,
            install_cognito):
        """Email is an alias attribute on the pool, so a new username
        with an email already used by another account raises
        UsernameExistsException whose Message describes the email
        conflict; the message is passed through so the administrator
        sees the real reason."""
        email_conflict = "An account with the given email already exists."
        install_cognito(error=cognito_error(
            "UsernameExistsException", email_conflict))
        status, body = invoke(user_admin, create_event(dict(VALID_BODY)))

        assert status == 409
        assert body["error"] == "account already exists"
        assert body["message"] == email_conflict
        assert credentials_table.scan()["Items"] == []

        entries = audit_entries(audit_table)
        assert len(entries) == 1
        assert entries[0]["result"] == "failure"

    def test_alias_exists_exception_maps_to_409_with_message(
            self, user_admin, credentials_table, audit_table,
            install_cognito):
        """AliasExistsException is mapped the same way: 409 with the
        Cognito Message passed through."""
        alias_conflict = "An account with the email already exists."
        install_cognito(error=cognito_error(
            "AliasExistsException", alias_conflict))
        status, body = invoke(user_admin, create_event(dict(VALID_BODY)))

        assert status == 409
        assert body["error"] == "account already exists"
        assert body["message"] == alias_conflict
        assert credentials_table.scan()["Items"] == []


class TestOtherFailures:
    def test_other_cognito_error_maps_to_502(
            self, user_admin, credentials_table, audit_table,
            install_cognito):
        """Req 12.9: any other Cognito failure -> 502 "account was not
        created" with no partial record and audit-final failure."""
        install_cognito(error=cognito_error(
            "InternalErrorException", "Something went wrong"))
        status, body = invoke(user_admin, create_event(dict(VALID_BODY)))

        assert status == 502
        assert body["error"] == "account was not created"
        assert credentials_table.scan()["Items"] == []

        entries = audit_entries(audit_table)
        assert len(entries) == 1
        assert entries[0]["result"] == "failure"


class TestAuditBeforeEffect:
    def test_pending_audit_failure_blocks_the_action(
            self, user_admin, credentials_table, audit_table,
            install_cognito, monkeypatch):
        """Req 6.4/6.5: when the pending audit entry cannot be recorded,
        the account is not created - zero Cognito calls and an error
        stating the action was not applied."""
        fake = install_cognito()

        def failing_audit(*args, **kwargs):
            raise RuntimeError("audit table unavailable")

        monkeypatch.setattr(user_admin, "record_audit_event_strict",
                            failing_audit)
        status, body = invoke(user_admin, create_event(dict(VALID_BODY)))

        assert status == 500
        assert body["message"] == "The action was not applied"
        assert fake.calls == []
