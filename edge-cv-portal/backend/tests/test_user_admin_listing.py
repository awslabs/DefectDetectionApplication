"""
Unit tests for GET /api/v1/admin/users account listing in user_admin.py
(spec: portal-user-manager, task 2.1).

Covers: full Cognito list_users pagination, the edge-credentials
edge_capable join (normalized lowercase usernames), the Viewer default
role, the returned field set, the PortalAdmin 403 gate, and the 502
retrieval-failure path.

Cognito is a recording fake client (moto's cognito-idp backend needs an
extra optional dependency, and the spec's test plan uses recording fakes
for cognito-idp anyway); the edge-credentials join runs against the real
moto-backed DynamoDB table.

_Requirements: 1.5, 1.7, 2.1_
"""
import json
import os
import sys

import pytest
from botocore.exceptions import ClientError

REGION = "us-east-1"
EDGE_CREDENTIALS_TABLE = "test-edge-credentials"
POOL_ID = "us-east-1_testpool"


# ----------------------------------------------------- fake Cognito client

class FakeCognitoClient:
    """Recording fake for the cognito-idp list_users API with pagination."""

    def __init__(self, users=None, page_size=60, error=None):
        self.users = list(users or [])
        self.page_size = page_size
        self.error = error
        self.calls = []

    def list_users(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        assert kwargs["UserPoolId"] == POOL_ID
        start = int(kwargs.get("PaginationToken", "0"))
        limit = min(int(kwargs.get("Limit", 60)), self.page_size)
        page = self.users[start:start + limit]
        response = {"Users": page}
        next_start = start + len(page)
        if next_start < len(self.users):
            response["PaginationToken"] = str(next_start)
        return response


def cognito_user(username, email=None, role=None, email_verified=True,
                 enabled=True, status="CONFIRMED"):
    """Build a user record in the Cognito list_users response shape."""
    attrs = [{"Name": "email_verified",
              "Value": "true" if email_verified else "false"}]
    if email is not None:
        attrs.append({"Name": "email", "Value": email})
    if role is not None:
        attrs.append({"Name": "custom:role", "Value": role})
    return {"Username": username, "Attributes": attrs,
            "Enabled": enabled, "UserStatus": status}


def not_found_error():
    return ClientError(
        {"Error": {"Code": "ResourceNotFoundException",
                   "Message": "User pool us-east-1_doesnotexist does not exist."}},
        "ListUsers",
    )


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
def install_cognito(user_admin, monkeypatch):
    """Wire a fake cognito client + pool id into the module under test."""
    def _install(users=None, page_size=60, error=None):
        fake = FakeCognitoClient(users=users, page_size=page_size,
                                 error=error)
        monkeypatch.setattr(user_admin, "cognito_client", fake)
        monkeypatch.setattr(user_admin, "USER_POOL_ID", POOL_ID)
        return fake
    return _install


# ---------------------------------------------------------------- helpers

def list_event(role="PortalAdmin"):
    return {
        "httpMethod": "GET",
        "path": "/api/v1/admin/users",
        "requestContext": {
            "authorizer": {
                "claims": {
                    "sub": "caller-1",
                    "email": "caller@example.com",
                    "cognito:username": "caller-1",
                    "custom:role": role,
                }
            }
        },
    }


def invoke_list(user_admin, role="PortalAdmin"):
    response = user_admin.handler(list_event(role), None)
    return response["statusCode"], json.loads(response["body"])


# ------------------------------------------------------------------ tests

class TestPortalAdminGate:
    @pytest.mark.parametrize(
        "role", ["Viewer", "Operator", "DataScientist", "UseCaseAdmin"])
    def test_non_portal_admin_rejected_403(self, user_admin, credentials_table,
                                           install_cognito, role):
        """Req 1.5: non-PortalAdmin callers get 403 and no operation
        is performed (zero Cognito calls)."""
        fake = install_cognito(users=[cognito_user("operator1")])
        status, body = invoke_list(user_admin, role=role)
        assert status == 403
        assert "users" not in body
        assert body["error"] == "Access denied"
        assert fake.calls == []


class TestAccountListing:
    def test_lists_all_accounts_with_expected_fields(
            self, user_admin, credentials_table, install_cognito):
        """Req 2.1: every account is returned with username, email,
        email_verified, role, User_Pool status, and enabled state."""
        install_cognito(users=[
            cognito_user("operator1", email="op1@example.com",
                         role="Operator"),
            cognito_user("newbie", email="new@example.com", role="Viewer",
                         email_verified=False,
                         status="FORCE_CHANGE_PASSWORD"),
            cognito_user("disabled-admin", email="da@example.com",
                         role="PortalAdmin", enabled=False),
        ])

        status, body = invoke_list(user_admin)
        assert status == 200
        assert body["total_count"] == 3
        rows = {u["username"]: u for u in body["users"]}
        assert set(rows) == {"operator1", "newbie", "disabled-admin"}

        assert rows["operator1"] == {
            "username": "operator1",
            "email": "op1@example.com",
            "email_verified": True,
            "role": "Operator",
            "user_status": "CONFIRMED",
            "enabled": True,
            "edge_capable": False,
        }

        assert rows["newbie"]["email_verified"] is False
        assert rows["newbie"]["user_status"] == "FORCE_CHANGE_PASSWORD"
        assert rows["newbie"]["enabled"] is True

        assert rows["disabled-admin"]["enabled"] is False
        assert rows["disabled-admin"]["role"] == "PortalAdmin"

    def test_role_defaults_to_viewer_when_attribute_missing(
            self, user_admin, credentials_table, install_cognito):
        install_cognito(users=[
            cognito_user("roleless", email="r@example.com", role=None)])

        status, body = invoke_list(user_admin)
        assert status == 200
        assert body["users"][0]["role"] == "Viewer"

    def test_pagination_returns_every_user(
            self, user_admin, credentials_table, install_cognito):
        """list_users pages must be followed until the PaginationToken
        is exhausted so the listing is complete."""
        expected = {f"user-{i:03d}" for i in range(8)}
        fake = install_cognito(
            users=[cognito_user(u, email=f"{u}@example.com", role="Viewer")
                   for u in sorted(expected)],
            page_size=3,
        )

        status, body = invoke_list(user_admin)
        assert status == 200
        assert body["total_count"] == 8
        assert {u["username"] for u in body["users"]} == expected
        # 3 + 3 + 2: every page fetched, tokens threaded through
        assert len(fake.calls) == 3
        assert "PaginationToken" not in fake.calls[0]
        assert fake.calls[1]["PaginationToken"] == "3"
        assert fake.calls[2]["PaginationToken"] == "6"


class TestEdgeCapableJoin:
    def test_verifier_row_marks_account_edge_capable(
            self, user_admin, credentials_table, install_cognito):
        """Accounts with a verifier row in the edge-credentials table
        (keyed by normalized lowercase username) are edge_capable."""
        install_cognito(users=[
            cognito_user("OpUser1", email="op@example.com", role="Operator"),
            cognito_user("plainuser", email="p@example.com", role="Viewer"),
        ])
        credentials_table.put_item(Item={
            "username": "opuser1",
            "verifier": user_admin.make_verifier("Sup3r-Secret-Pw!",
                                                 iterations=10),
            "updatedAt": 1700000000000,
        })

        status, body = invoke_list(user_admin)
        assert status == 200
        rows = {u["username"]: u for u in body["users"]}
        assert rows["OpUser1"]["edge_capable"] is True
        assert rows["plainuser"]["edge_capable"] is False


class TestListFailure:
    def test_cognito_failure_returns_502(self, user_admin, credentials_table,
                                         install_cognito):
        """A retrieval failure surfaces as an error, never a partial
        list (backend side of Req 2.5)."""
        install_cognito(error=not_found_error())
        status, body = invoke_list(user_admin)
        assert status == 502
        assert body["error"] == "Failed to retrieve account list"
        assert "users" not in body
