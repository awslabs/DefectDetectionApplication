"""
Unit tests for POST /api/v1/admin/users/{username}/forgot-password in
user_admin.py (spec: portal-user-manager, task 2.3).

Covers: the verified-email check (400 before any generation) ->
generate_temp_password -> audit-pending -> SES SendEmail ->
admin_set_user_password(Permanent=False) -> verifier capture ->
audit-final flow; the SES-before-set ordering (delivery failure leaves
credentials untouched); set-password failure after a successful send
(action reports failure, emailed password never became valid);
UserNotFoundException -> 404; pending audit failure -> 500 "not
applied"; the PortalAdmin 403 gate; and that the temporary password
value never appears in the response or audit entries.

Cognito and SES are recording fake clients sharing a call timeline
(same pattern as test_user_admin_set_password.py); the edge-credentials
verifier write and the two-phase audit entries run against real
moto-backed DynamoDB.

_Requirements: 4.1, 4.3, 4.4, 4.5, 6.1, 6.3_
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
SENDER = "no-reply@portal.example.com"


# ------------------------------------------------------------ fake clients

def cognito_error(code, message, operation="AdminSetUserPassword"):
    return ClientError({"Error": {"Code": code, "Message": message}},
                       operation)


def user_attributes(email="operator1@example.com", email_verified="true"):
    attrs = []
    if email is not None:
        attrs.append({"Name": "email", "Value": email})
    if email_verified is not None:
        attrs.append({"Name": "email_verified", "Value": email_verified})
    return attrs


class FakeCognitoClient:
    """Recording fake for admin_get_user + admin_set_user_password."""

    def __init__(self, timeline, get_user_attributes=None,
                 get_user_error=None, set_password_error=None):
        self.timeline = timeline
        self.get_user_attributes = (
            user_attributes() if get_user_attributes is None
            else get_user_attributes)
        self.get_user_error = get_user_error
        self.set_password_error = set_password_error

    def admin_get_user(self, **kwargs):
        self.timeline.append(("cognito.admin_get_user", kwargs))
        if self.get_user_error is not None:
            raise self.get_user_error
        return {
            "Username": kwargs["Username"],
            "UserAttributes": self.get_user_attributes,
        }

    def admin_set_user_password(self, **kwargs):
        self.timeline.append(("cognito.admin_set_user_password", kwargs))
        if self.set_password_error is not None:
            raise self.set_password_error

    def calls(self, operation):
        return [kwargs for op, kwargs in self.timeline
                if op == f"cognito.{operation}"]


class FakeSesClient:
    """Recording fake for the SES send_email API."""

    def __init__(self, timeline, error=None):
        self.timeline = timeline
        self.error = error

    def send_email(self, **kwargs):
        self.timeline.append(("ses.send_email", kwargs))
        if self.error is not None:
            raise self.error
        return {"MessageId": "test-message-id"}

    @property
    def sends(self):
        return [kwargs for op, kwargs in self.timeline
                if op == "ses.send_email"]


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
def install_clients(user_admin, monkeypatch):
    """Wire fake cognito + ses clients sharing one timeline into the
    module under test."""
    def _install(**cognito_kwargs):
        timeline = []
        ses_error = cognito_kwargs.pop("ses_error", None)
        fake_cognito = FakeCognitoClient(timeline, **cognito_kwargs)
        fake_ses = FakeSesClient(timeline, error=ses_error)
        monkeypatch.setattr(user_admin, "cognito_client", fake_cognito)
        monkeypatch.setattr(user_admin, "ses_client", fake_ses)
        monkeypatch.setattr(user_admin, "USER_POOL_ID", POOL_ID)
        monkeypatch.setattr(user_admin, "SES_SENDER_ADDRESS", SENDER)
        return fake_cognito, fake_ses, timeline
    return _install


# ---------------------------------------------------------------- helpers

def forgot_event(username, role="PortalAdmin"):
    return {
        "httpMethod": "POST",
        "path": f"/api/v1/admin/users/{username}/forgot-password",
        "pathParameters": {"username": username},
        "body": None,
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


def emailed_password(ses_send):
    """Extract the temporary password value from the sent email body."""
    body = ses_send["Message"]["Body"]["Text"]["Data"]
    for line in body.splitlines():
        if line.startswith("Temporary password: "):
            return line[len("Temporary password: "):]
    raise AssertionError("no temporary password line in the email body")


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
            install_clients, role):
        """Req 1.5: non-PortalAdmin callers get 403 with zero Cognito/SES
        calls, no verifier write, and no audit entry."""
        _, _, timeline = install_clients()
        status, body = invoke(
            user_admin, forgot_event("operator1", role=role))
        assert status == 403
        assert body["error"] == "Access denied"
        assert timeline == []
        assert credentials_table.scan()["Items"] == []
        assert audit_entries(audit_table) == []


class TestForgotPasswordSuccess:
    def test_emails_temp_password_and_sets_it_non_permanent(
            self, user_admin, credentials_table, audit_table,
            install_clients):
        """Req 4.1: a policy-conformant temporary password is delivered
        to the registered email from the configured sender, and the same
        value is applied with Permanent=False."""
        cognito, ses, _ = install_clients()
        status, body = invoke(user_admin, forgot_event("operator1"))

        assert status == 200
        assert body["username"] == "operator1"

        assert len(ses.sends) == 1
        send = ses.sends[0]
        assert send["Source"] == SENDER
        assert send["Destination"]["ToAddresses"] == [
            "operator1@example.com"]
        password = emailed_password(send)

        set_calls = cognito.calls("admin_set_user_password")
        assert set_calls == [{
            "UserPoolId": POOL_ID,
            "Username": "operator1",
            "Password": password,
            "Permanent": False,
        }]

        # The generated password conforms to the pool policy (4.1).
        assert len(password) >= 12
        assert any(c.islower() for c in password)
        assert any(c.isupper() for c in password)
        assert any(c.isdigit() for c in password)
        assert any(c in user_admin.PASSWORD_SYMBOLS for c in password)

    def test_ses_send_happens_before_the_password_set(
            self, user_admin, credentials_table, audit_table,
            install_clients):
        """Req 4.5 (ordering): the email is delivered before Cognito is
        touched, so a delivery failure leaves credentials untouched."""
        _, _, timeline = install_clients()
        status, _ = invoke(user_admin, forgot_event("operator1"))

        assert status == 200
        operations = [op for op, _ in timeline]
        assert operations == [
            "cognito.admin_get_user",
            "ses.send_email",
            "cognito.admin_set_user_password",
        ]

    def test_success_captures_verifier_for_the_emailed_password(
            self, user_admin, credentials_table, audit_table,
            install_clients):
        """A successful flow writes a PBKDF2 verifier row keyed by the
        normalized lowercase username matching the emailed value (D3)."""
        _, ses, _ = install_clients()
        status, _ = invoke(user_admin, forgot_event("OpUser1"))

        assert status == 200
        items = credentials_table.scan()["Items"]
        assert len(items) == 1
        item = items[0]
        assert item["username"] == "opuser1"
        assert item["updatedAt"] > 0
        assert verify_password_against(
            item["verifier"], emailed_password(ses.sends[0]))

    def test_success_finalizes_exactly_one_audit_entry(
            self, user_admin, credentials_table, audit_table,
            install_clients):
        """Req 6.1: exactly one finalized audit entry with acting user,
        affected account, action type, and completion time."""
        install_clients()
        status, _ = invoke(user_admin, forgot_event("operator1"))

        assert status == 200
        entries = audit_entries(audit_table)
        assert len(entries) == 1
        entry = entries[0]
        assert entry["result"] == "success"
        assert entry["action"] == "forgot_password"
        assert entry["resource_type"] == "user_account"
        assert entry["resource_id"] == "operator1"
        assert entry["user_id"] == "admin-1"
        assert entry["completed_at"] > 0

    def test_temp_password_never_in_response_or_audit(
            self, user_admin, credentials_table, audit_table,
            install_clients):
        """Req 4.3 / 6.3: the temporary password value appears in neither
        the HTTP response nor any audit entry."""
        _, ses, _ = install_clients()
        response = user_admin.handler(forgot_event("operator1"), None)

        assert response["statusCode"] == 200
        password = emailed_password(ses.sends[0])
        assert password not in json.dumps(response)
        assert password not in json.dumps(
            audit_entries(audit_table), default=str)


class TestUnverifiedEmail:
    @pytest.mark.parametrize("email_verified", [None, "false", "False"])
    def test_unverified_email_rejected_400_before_any_generation(
            self, user_admin, credentials_table, audit_table,
            install_clients, email_verified):
        """Req 4.4: no verified email -> 400 with nothing generated,
        sent, applied, or audited."""
        cognito, ses, _ = install_clients(
            get_user_attributes=user_attributes(
                email_verified=email_verified))
        status, body = invoke(user_admin, forgot_event("operator1"))

        assert status == 400
        assert body["error"] == "No verified email address"
        assert ses.sends == []
        assert cognito.calls("admin_set_user_password") == []
        assert credentials_table.scan()["Items"] == []
        assert audit_entries(audit_table) == []


class TestDeliveryFailure:
    def test_ses_failure_leaves_credentials_untouched(
            self, user_admin, credentials_table, audit_table,
            install_clients):
        """Req 4.5: SES delivery failure -> 'temporary password was not
        sent', no admin_set_user_password call, no verifier write, audit
        finalized as failure."""
        cognito, _, _ = install_clients(ses_error=cognito_error(
            "MessageRejected", "Email address is not verified.",
            operation="SendEmail"))
        status, body = invoke(user_admin, forgot_event("operator1"))

        assert status == 502
        assert body["error"] == "temporary password was not sent"
        assert cognito.calls("admin_set_user_password") == []
        assert credentials_table.scan()["Items"] == []

        entries = audit_entries(audit_table)
        assert len(entries) == 1
        assert entries[0]["result"] == "failure"

    def test_set_password_failure_after_send_reports_failure(
            self, user_admin, credentials_table, audit_table,
            install_clients):
        """Cognito failure after a successful send: the emailed password
        never became valid -> the action reports failure, no verifier."""
        install_clients(set_password_error=cognito_error(
            "InternalErrorException", "Something went wrong"))
        status, body = invoke(user_admin, forgot_event("operator1"))

        assert status == 502
        assert body["error"] == "forgot-password failed"
        assert credentials_table.scan()["Items"] == []

        entries = audit_entries(audit_table)
        assert len(entries) == 1
        assert entries[0]["result"] == "failure"


class TestUserNotFound:
    def test_unknown_user_maps_to_404(
            self, user_admin, credentials_table, audit_table,
            install_clients):
        cognito, ses, _ = install_clients(get_user_error=cognito_error(
            "UserNotFoundException", "User does not exist.",
            operation="AdminGetUser"))
        status, body = invoke(user_admin, forgot_event("ghost"))

        assert status == 404
        assert body["error"] == "User not found"
        assert ses.sends == []
        assert cognito.calls("admin_set_user_password") == []
        assert audit_entries(audit_table) == []


class TestAuditBeforeEffect:
    def test_pending_audit_failure_blocks_the_action(
            self, user_admin, credentials_table, audit_table,
            install_clients, monkeypatch):
        """Req 6.4/6.5: when the pending audit entry cannot be recorded,
        the action is not applied - no email sent, zero Cognito password
        mutations, no verifier."""
        cognito, ses, _ = install_clients()

        def failing_audit(*args, **kwargs):
            raise RuntimeError("audit table unavailable")

        monkeypatch.setattr(user_admin, "record_audit_event_strict",
                            failing_audit)
        status, body = invoke(user_admin, forgot_event("operator1"))

        assert status == 500
        assert body["message"] == "The action was not applied"
        assert ses.sends == []
        assert cognito.calls("admin_set_user_password") == []
        assert credentials_table.scan()["Items"] == []
