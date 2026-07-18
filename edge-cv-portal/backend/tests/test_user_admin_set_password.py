"""
Unit tests for POST /api/v1/admin/users/{username}/password in
user_admin.py (spec: portal-user-manager, task 2.2).

Covers: the audit-pending -> admin_set_user_password -> verifier capture
-> audit-final flow, the InvalidPasswordException -> 400 policy
pass-through with no verifier write, UserNotFoundException -> 404,
other Cognito errors -> 502 "password change failed", the
audit-before-effect abort (pending audit failure -> 500 "not applied"
with Cognito untouched), and the PortalAdmin 403 gate.

Cognito is a recording fake client (same pattern as
test_user_admin_listing.py); the edge-credentials verifier write and
the two-phase audit entries run against real moto-backed DynamoDB.

_Requirements: 3.1, 3.3, 3.5, 6.1, 6.4_
"""
import base64
import hashlib
import json
import os
import sys

import pytest
from botocore.exceptions import ClientError

REGION = "us-east-1"
EDGE_CREDENTIALS_TABLE = "test-edge-credentials"
AUDIT_LOG_TABLE = "test-audit-log"
POOL_ID = "us-east-1_testpool"

VALID_PASSWORD = "Sup3r-Secret-Pw!"


# ----------------------------------------------------- fake Cognito client

class FakeCognitoClient:
    """Recording fake for the cognito-idp admin_set_user_password API."""

    def __init__(self, error=None):
        self.error = error
        self.calls = []

    def admin_set_user_password(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error


def cognito_error(code, message):
    return ClientError({"Error": {"Code": code, "Message": message}},
                       "AdminSetUserPassword")


POLICY_MESSAGE = ("Password did not conform with policy: "
                  "Password must have symbol characters")


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

def password_event(username, body=None, role="PortalAdmin",
                   with_path_parameters=True):
    event = {
        "httpMethod": "POST",
        "path": f"/api/v1/admin/users/{username}/password",
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
    if with_path_parameters:
        event["pathParameters"] = {"username": username}
    return event


def invoke(user_admin, event):
    response = user_admin.handler(event, None)
    return response["statusCode"], json.loads(response["body"])


def audit_entries(audit_table):
    return audit_table.scan()["Items"]


def verify_password_against(verifier, password):
    """Recompute the PBKDF2 hash to check the stored verifier matches."""
    derived = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        base64.b64decode(verifier["salt"]),
        int(verifier["iterations"]),
        dklen=32,
    )
    return base64.b64encode(derived).decode("ascii") == verifier["hash"]


# ------------------------------------------------------------------ tests

class TestPortalAdminGate:
    @pytest.mark.parametrize(
        "role", ["Viewer", "Operator", "DataScientist", "UseCaseAdmin"])
    def test_non_portal_admin_rejected_403(
            self, user_admin, credentials_table, audit_table,
            install_cognito, role):
        """Req 1.5: non-PortalAdmin callers get 403 with zero Cognito
        calls, no verifier write, and no audit entry."""
        fake = install_cognito()
        status, body = invoke(user_admin, password_event(
            "operator1", {"password": VALID_PASSWORD, "permanent": True},
            role=role))
        assert status == 403
        assert body["error"] == "Access denied"
        assert fake.calls == []
        assert credentials_table.scan()["Items"] == []
        assert audit_entries(audit_table) == []


class TestPasswordChangeSuccess:
    @pytest.mark.parametrize("permanent", [True, False])
    def test_sets_password_with_selected_permanence(
            self, user_admin, credentials_table, audit_table,
            install_cognito, permanent):
        """Req 3.1: the exact password and admin-selected permanence are
        passed to admin_set_user_password."""
        fake = install_cognito()
        status, body = invoke(user_admin, password_event(
            "operator1",
            {"password": VALID_PASSWORD, "permanent": permanent}))

        assert status == 200
        assert body["username"] == "operator1"
        assert body["permanent"] is permanent
        assert fake.calls == [{
            "UserPoolId": POOL_ID,
            "Username": "operator1",
            "Password": VALID_PASSWORD,
            "Permanent": permanent,
        }]

    def test_success_captures_verifier_keyed_lowercase(
            self, user_admin, credentials_table, audit_table,
            install_cognito):
        """A successful set writes a PBKDF2 verifier row keyed by the
        normalized lowercase username with updatedAt (design D3)."""
        install_cognito()
        status, _ = invoke(user_admin, password_event(
            "OpUser1", {"password": VALID_PASSWORD, "permanent": True}))

        assert status == 200
        items = credentials_table.scan()["Items"]
        assert len(items) == 1
        item = items[0]
        assert item["username"] == "opuser1"
        assert item["updatedAt"] > 0
        verifier = item["verifier"]
        assert verifier["algorithm"] == "pbkdf2-sha256"
        assert verify_password_against(verifier, VALID_PASSWORD)

    def test_success_finalizes_exactly_one_audit_entry(
            self, user_admin, credentials_table, audit_table,
            install_cognito):
        """Req 6.1: exactly one audit entry, finalized to success, with
        acting user, affected account, action type, and completion time;
        no password material anywhere in it."""
        install_cognito()
        status, _ = invoke(user_admin, password_event(
            "operator1", {"password": VALID_PASSWORD, "permanent": True}))

        assert status == 200
        entries = audit_entries(audit_table)
        assert len(entries) == 1
        entry = entries[0]
        assert entry["result"] == "success"
        assert entry["action"] == "password_change"
        assert entry["resource_type"] == "user_account"
        assert entry["resource_id"] == "operator1"
        assert entry["user_id"] == "admin-1"
        assert entry["completed_at"] > 0
        assert VALID_PASSWORD not in json.dumps(entries, default=str)

    def test_response_never_contains_the_password(
            self, user_admin, credentials_table, audit_table,
            install_cognito):
        install_cognito()
        response = user_admin.handler(password_event(
            "operator1", {"password": VALID_PASSWORD, "permanent": False}),
            None)
        assert VALID_PASSWORD not in json.dumps(response)

    def test_username_extracted_from_raw_path_without_path_parameters(
            self, user_admin, credentials_table, audit_table,
            install_cognito):
        fake = install_cognito()
        status, body = invoke(user_admin, password_event(
            "operator1", {"password": VALID_PASSWORD, "permanent": True},
            with_path_parameters=False))
        assert status == 200
        assert fake.calls[0]["Username"] == "operator1"


class TestPolicyViolation:
    def test_invalid_password_maps_to_400_with_policy_message(
            self, user_admin, credentials_table, audit_table,
            install_cognito):
        """Req 3.3: InvalidPasswordException -> 400 with the policy
        message passed through, no verifier written."""
        install_cognito(error=cognito_error(
            "InvalidPasswordException", POLICY_MESSAGE))
        status, body = invoke(user_admin, password_event(
            "operator1", {"password": "weak", "permanent": True}))

        assert status == 400
        assert body["message"] == POLICY_MESSAGE
        assert credentials_table.scan()["Items"] == []

        entries = audit_entries(audit_table)
        assert len(entries) == 1
        assert entries[0]["result"] == "failure"


class TestOtherFailures:
    def test_user_not_found_maps_to_404(
            self, user_admin, credentials_table, audit_table,
            install_cognito):
        install_cognito(error=cognito_error(
            "UserNotFoundException", "User does not exist."))
        status, body = invoke(user_admin, password_event(
            "ghost", {"password": VALID_PASSWORD, "permanent": True}))

        assert status == 404
        assert body["error"] == "User not found"
        assert credentials_table.scan()["Items"] == []

    def test_other_cognito_error_maps_to_502(
            self, user_admin, credentials_table, audit_table,
            install_cognito):
        """Req 3.5: non-policy Cognito failures -> 502 'password change
        failed', account untouched, no verifier written."""
        install_cognito(error=cognito_error(
            "InternalErrorException", "Something went wrong"))
        status, body = invoke(user_admin, password_event(
            "operator1", {"password": VALID_PASSWORD, "permanent": True}))

        assert status == 502
        assert body["error"] == "password change failed"
        assert credentials_table.scan()["Items"] == []

        entries = audit_entries(audit_table)
        assert len(entries) == 1
        assert entries[0]["result"] == "failure"


class TestAuditBeforeEffect:
    def test_pending_audit_failure_blocks_the_action(
            self, user_admin, credentials_table, audit_table,
            install_cognito, monkeypatch):
        """Req 6.4/6.5: when the pending audit entry cannot be recorded,
        the action is not applied - zero Cognito calls, no verifier, and
        an error stating the action was not applied."""
        fake = install_cognito()

        def failing_audit(*args, **kwargs):
            raise RuntimeError("audit table unavailable")

        monkeypatch.setattr(user_admin, "record_audit_event_strict",
                            failing_audit)
        status, body = invoke(user_admin, password_event(
            "operator1", {"password": VALID_PASSWORD, "permanent": True}))

        assert status == 500
        assert body["message"] == "The action was not applied"
        assert fake.calls == []
        assert credentials_table.scan()["Items"] == []


class TestRequestValidation:
    def test_missing_password_rejected_400_before_any_effect(
            self, user_admin, credentials_table, audit_table,
            install_cognito):
        fake = install_cognito()
        status, _ = invoke(user_admin, password_event(
            "operator1", {"permanent": True}))
        assert status == 400
        assert fake.calls == []

    def test_missing_permanence_selection_rejected_400(
            self, user_admin, credentials_table, audit_table,
            install_cognito):
        """The permanence setting is an explicit required boolean (3.1:
        the admin-selected setting is what gets applied)."""
        fake = install_cognito()
        status, _ = invoke(user_admin, password_event(
            "operator1", {"password": VALID_PASSWORD}))
        assert status == 400
        assert fake.calls == []
