"""
Preservation tests for the audit finalize path of user_admin.py
(spec: user-manager-datalabeler-role bugfix, task 2, Property 2).

Pins the audit-before-effect protocol invariant that MUST survive the
fix (Requirement 3.5, design Decision 3): a user-admin mutating action

  (a) records its audit entry 'pending' BEFORE the Cognito effect,
  (b) issues a `dynamodb:Query` against the audit-log table during
      `finalize_audit_event` (the (event_id, timestamp) range-key
      recovery — observed via a recording wrapper around the
      shared_utils DynamoDB resource), and
  (c) lands the entry at a terminal result with `completed_at`.

PASSES on the UNFIXED tree: moto does not enforce IAM, so the live
AccessDeniedException (bugfix.md Incident Record) is NOT reproducible
host-side — that deployed-IAM truth belongs to the jest CDK grant test
and the live USER ACTION verification. What this test pins is that
`dynamodb:Query` is exactly the action the finalize path exercises
(so the CDK grant is the right and minimal fix) and that the protocol
and shared audit code stay untouched (no backend code change).

Conventions follow test_user_admin_create.py: moto `aws_stack` from
conftest, recording fake Cognito, module-scope `user_admin` import
inside the mock; the audit entries run against real moto-backed
DynamoDB.

**Validates: Requirements 3.5**
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
    "username": "finalize-preservation-user",
    "email": "finalize.preservation@example.com",
    "role": "Operator",
}

AUDIT_TERMINAL_RESULTS = {"success", "failure", "rejected"}


# ----------------------------------------------- recording table wrapper

class RecordingAuditTable:
    """Delegating proxy over the real moto-backed audit table that
    records every `query` call (finalize_audit_event's range-key
    recovery is the only Query issuer on this path)."""

    def __init__(self, real_table, query_calls):
        self._real_table = real_table
        self._query_calls = query_calls

    def query(self, **kwargs):
        self._query_calls.append(kwargs)
        return self._real_table.query(**kwargs)

    def __getattr__(self, name):
        return getattr(self._real_table, name)


class RecordingDynamoResource:
    """Delegating proxy over the shared_utils boto3 DynamoDB resource:
    hands out a RecordingAuditTable for the audit-log table and the
    real table for everything else."""

    def __init__(self, real_resource, query_calls):
        self._real_resource = real_resource
        self._query_calls = query_calls

    def Table(self, name):  # noqa: N802 - boto3 resource API name
        table = self._real_resource.Table(name)
        if name == AUDIT_LOG_TABLE:
            return RecordingAuditTable(table, self._query_calls)
        return table

    def __getattr__(self, name):
        return getattr(self._real_resource, name)


# ----------------------------------------------------- fake Cognito client

class SnapshottingFakeCognitoClient:
    """Recording fake for cognito-idp admin_create_user that snapshots
    the audit table AND the Query-call count at effect time, so the
    test can assert audit-pending-BEFORE-effect and Query-DURING-
    finalize (i.e. after the effect)."""

    def __init__(self, audit_table, query_calls, error=None):
        self.audit_table = audit_table
        self.query_calls = query_calls
        self.error = error
        self.calls = []
        self.audit_snapshots_at_effect = []
        self.query_count_at_effect = []

    def admin_create_user(self, **kwargs):
        self.calls.append(kwargs)
        self.audit_snapshots_at_effect.append(
            self.audit_table.scan()["Items"])
        self.query_count_at_effect.append(len(self.query_calls))
        if self.error is not None:
            raise self.error


def cognito_error(code, message):
    return ClientError({"Error": {"Code": code, "Message": message}},
                       "AdminCreateUser")


# --------------------------------------------------------------- fixtures

@pytest.fixture(scope="module")
def user_admin(aws_stack):
    """The real user_admin module imported inside the moto mock, with the
    edge-credentials table created (test_user_admin_create.py pattern)."""
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
def recording_audit_queries(user_admin, monkeypatch):
    """Wrap the shared_utils DynamoDB resource so every `query` against
    the audit-log table is recorded. finalize_audit_event resolves
    `dynamodb` in the shared_utils module namespace at call time, so
    patching shared_utils.dynamodb observes its range-key recovery
    Query without touching the code under test."""
    import shared_utils

    query_calls = []
    monkeypatch.setattr(
        shared_utils, "dynamodb",
        RecordingDynamoResource(shared_utils.dynamodb, query_calls))
    return query_calls


@pytest.fixture
def install_cognito(user_admin, audit_table, recording_audit_queries,
                    monkeypatch):
    """Wire the snapshotting fake cognito client + pool id into the
    module under test."""
    def _install(error=None):
        fake = SnapshottingFakeCognitoClient(
            audit_table, recording_audit_queries, error=error)
        monkeypatch.setattr(user_admin, "cognito_client", fake)
        monkeypatch.setattr(user_admin, "USER_POOL_ID", POOL_ID)
        return fake
    return _install


# ---------------------------------------------------------------- helpers

def create_event(body):
    return {
        "httpMethod": "POST",
        "path": "/api/v1/admin/users",
        "body": json.dumps(body),
        "requestContext": {
            "authorizer": {
                "claims": {
                    "sub": "admin-1",
                    "email": "admin@example.com",
                    "cognito:username": "admin-1",
                    "custom:role": "PortalAdmin",
                }
            }
        },
    }


def invoke(user_admin, event):
    response = user_admin.handler(event, None)
    return response["statusCode"], json.loads(response["body"])


# ------------------------------------------------------------------ tests

class TestFinalizePathPreservation:
    """Property 2: Preservation — the audit-before-effect protocol,
    including finalize's Query-based range-key recovery, is unchanged
    (Requirement 3.5)."""

    def test_create_records_pending_before_effect_queries_during_finalize_and_terminalizes(
            self, user_admin, audit_table, recording_audit_queries,
            install_cognito):
        """(a) audit entry 'pending' before the Cognito effect,
        (b) finalize_audit_event issues a Query on the audit-log table,
        (c) the entry lands terminal with completed_at."""
        fake = install_cognito()
        status, _ = invoke(user_admin, create_event(dict(VALID_BODY)))
        assert status == 201

        # (a) At effect time exactly one audit entry existed, 'pending',
        # not yet completed — audit-before-effect.
        assert len(fake.audit_snapshots_at_effect) == 1
        pending = fake.audit_snapshots_at_effect[0]
        assert len(pending) == 1
        assert pending[0]["action"] == "account_create"
        assert pending[0]["result"] == "pending"
        assert "completed_at" not in pending[0]

        # (b) No audit-table Query had happened before the effect; the
        # finalize step afterwards issued exactly the range-key recovery
        # Query (event_id key condition) — the dynamodb:Query action the
        # deployed role must be granted.
        assert fake.query_count_at_effect == [0]
        assert len(recording_audit_queries) == 1
        assert "KeyConditionExpression" in recording_audit_queries[0]

        # (c) The entry landed at a terminal result with completed_at.
        entries = audit_table.scan()["Items"]
        assert len(entries) == 1
        assert entries[0]["result"] in AUDIT_TERMINAL_RESULTS
        assert entries[0]["result"] == "success"
        assert entries[0]["completed_at"] > 0

    def test_failure_path_also_queries_during_finalize_and_terminalizes(
            self, user_admin, audit_table, recording_audit_queries,
            install_cognito):
        """The failure finalize (duplicate username -> 409) exercises the
        same Query call site and still terminalizes — under the deployed
        IAM gap these 4xx paths would also degrade to 500s (design
        'Edge case (failure finalize)')."""
        install_cognito(error=cognito_error(
            "UsernameExistsException", "User account already exists."))
        status, _ = invoke(user_admin, create_event(dict(VALID_BODY)))
        assert status == 409

        assert len(recording_audit_queries) == 1
        assert "KeyConditionExpression" in recording_audit_queries[0]

        entries = audit_table.scan()["Items"]
        assert len(entries) == 1
        assert entries[0]["result"] == "failure"
        assert entries[0]["completed_at"] > 0
