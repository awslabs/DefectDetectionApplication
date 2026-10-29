"""
Tests for the server-side Greengrass token exchange role preparation of
Station Quick Setup (backend/functions/token_exchange_role.py and its use in
quick_setup.exchange_credentials).

The station's provisioning credentials carry no IAM write action, so the
quick_setup Lambda prepares the role itself:

* the four inline policies it writes are the documents setup_station.sh writes
  (parsed out of the script, so the two copies cannot drift apart);
* same-account use cases get the Greengrass installer's steps (role, role
  alias, access policy) when the resources are missing, and existing
  resources are left as the installer would leave them;
* cross-account use cases only get the inline policies, and a missing role or
  a denied write is "not prepared" rather than a failed exchange;
* the preparation session is narrowed to exact names, and a preparation
  failure returns 503 with the setup token left unconsumed.

AWS is moto-backed. functions/ and the shared layer are on sys.path through
the tests conftest.
"""
import json
import os
import pathlib
import re
import sys
import uuid

import pytest
from botocore.exceptions import ClientError

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
SETUP_STATION = REPO_ROOT / "station_install" / "setup_station.sh"

ACCOUNT_ID = "123456789012"
REGION = "us-west-2"
PROVISIONING_ROLE_ARN = f"arn:aws:iam::{ACCOUNT_ID}:role/DDAStationProvisioningRole"
REGISTRATIONS_TABLE = "test-tes-device-registrations"
TES_ROLE = "GreengrassV2TokenExchangeRole"
TES_ALIAS = "GreengrassCoreTokenExchangeRoleAlias"
ACCESS_POLICY_ARN = f"arn:aws:iam::{ACCOUNT_ID}:policy/GreengrassV2TokenExchangeRoleAccess"
NOW = 1_730_000_000

IAM_WRITES = {
    "iam:CreateRole", "iam:AttachRolePolicy", "iam:PutRolePolicy",
    "iam:CreatePolicy", "iam:CreatePolicyVersion", "iam:PassRole",
    "iam:UpdateAssumeRolePolicy", "iam:DeleteRolePolicy",
    "iam:DetachRolePolicy", "iot:CreateRoleAlias", "iot:UpdateRoleAlias",
}


def _setup_station_inline_policies(account_id):
    """The put-role-policy documents of setup_station.sh, by policy name."""
    text = SETUP_STATION.read_text(encoding="utf-8")
    found = {}
    for name, document in re.findall(
            r"--policy-name (\w+) \\\n\s+--policy-document '(\{.*?\})'\"; then",
            text, flags=re.S):
        document = document.replace('\\"', '"').replace(
            "${aws_account_id}", account_id)
        found[name] = json.loads(document)
    return found


def _actions(policy):
    actions = set()
    for statement in policy["Statement"]:
        action = statement["Action"]
        actions.update([action] if isinstance(action, str) else action)
    return actions


def _client_error(code, operation="PutRolePolicy"):
    return ClientError({"Error": {"Code": code, "Message": code}}, operation)


# ---------------------------------------------------------------------------
# Pure checks
# ---------------------------------------------------------------------------

def test_inline_policies_are_the_setup_station_documents():
    import token_exchange_role

    station = _setup_station_inline_policies(ACCOUNT_ID)
    assert set(station) == {
        "GreengrassComponentS3Access", "CloudWatchLogsPolicy",
        "ECRComponentAccess", "ShadowManagerSyncPolicy",
    }
    assert token_exchange_role.inline_policies(ACCOUNT_ID) == station


def test_preparation_policy_names_only_the_fixed_resources():
    from session_policy import build_tes_preparation_policy

    policy = build_tes_preparation_policy(REGION, ACCOUNT_ID)
    resources = set()
    for statement in policy["Statement"]:
        assert statement["Effect"] == "Allow"
        resource = statement["Resource"]
        resources.update([resource] if isinstance(resource, str) else resource)
        for action in ([statement["Action"]] if isinstance(statement["Action"], str)
                       else statement["Action"]):
            assert "*" not in action, action
    assert resources == {
        f"arn:aws:iam::{ACCOUNT_ID}:role/{TES_ROLE}",
        ACCESS_POLICY_ARN,
        f"arn:aws:iot:{REGION}:{ACCOUNT_ID}:rolealias/{TES_ALIAS}",
    }
    attach = [s for s in policy["Statement"]
              if "iam:AttachRolePolicy" in _actions({"Statement": [s]})]
    assert len(attach) == 1
    assert attach[0]["Condition"] == {
        "ArnEquals": {"iam:PolicyARN": ACCESS_POLICY_ARN}}


def test_both_session_policies_stay_far_below_the_sts_limit():
    from session_policy import build_session_policy, build_tes_preparation_policy

    longest = "x" * 128
    for policy in (build_tes_preparation_policy("ap-southeast-2", ACCOUNT_ID),
                   build_session_policy(longest, longest, "ap-southeast-2", ACCOUNT_ID)):
        assert len(json.dumps(policy, separators=(",", ":"))) < 1500


# ---------------------------------------------------------------------------
# moto-backed stack
# ---------------------------------------------------------------------------

def _create_table(ddb, **kwargs):
    # Another module's mock may still hold the shared tables (the conftest's
    # session stack), so creation is idempotent.
    try:
        ddb.create_table(BillingMode="PAY_PER_REQUEST", **kwargs)
    except ddb.exceptions.ResourceInUseException:
        pass


_RELOADED = ("shared_utils", "token_service", "rate_limiter",
             "session_policy", "token_exchange_role", "quick_setup")


@pytest.fixture(scope="module")
def stack():
    """moto-backed AWS plus the real quick_setup modules imported inside the
    mock, so their module-level boto3 clients and resources hit moto."""
    from moto import mock_aws
    with pytest.MonkeyPatch.context() as env, mock_aws():
        env.setenv("REGISTRATIONS_TABLE", REGISTRATIONS_TABLE)
        env.setenv("QUICK_SETUP_PROVISIONING_ROLE_ARN", PROVISIONING_ROLE_ARN)
        import boto3
        ddb_region = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
        ddb = boto3.client("dynamodb", region_name=ddb_region)
        _create_table(
            ddb, TableName=REGISTRATIONS_TABLE,
            KeySchema=[{"AttributeName": "registration_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "registration_id", "AttributeType": "S"}])
        _create_table(
            ddb, TableName=os.environ["USECASES_TABLE"],
            KeySchema=[{"AttributeName": "usecase_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "usecase_id", "AttributeType": "S"}])
        _create_table(
            ddb, TableName=os.environ["AUDIT_LOG_TABLE"],
            KeySchema=[{"AttributeName": "event_id", "KeyType": "HASH"},
                       {"AttributeName": "timestamp", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "event_id", "AttributeType": "S"},
                                  {"AttributeName": "timestamp", "AttributeType": "N"}])
        for module_name in _RELOADED:
            sys.modules.pop(module_name, None)
        import quick_setup
        import token_exchange_role
        import token_service
        resource = boto3.resource("dynamodb", region_name=ddb_region)
        yield {
            "qs": quick_setup,
            "ter": token_exchange_role,
            "ts": token_service,
            "registrations": resource.Table(REGISTRATIONS_TABLE),
            "usecases": resource.Table(os.environ["USECASES_TABLE"]),
            "iam": boto3.client("iam", region_name=REGION),
            "iot": boto3.client("iot", region_name=REGION),
        }
    # Later modules import their own copies, bound to their own mocks.
    for module_name in _RELOADED:
        sys.modules.pop(module_name, None)


@pytest.fixture
def clean_tes_role(stack):
    """Remove the token exchange role, its alias and its access policy so each
    test starts from an account that has none of them."""
    iam, iot = stack["iam"], stack["iot"]

    def _clean():
        try:
            for name in iam.list_role_policies(RoleName=TES_ROLE)["PolicyNames"]:
                iam.delete_role_policy(RoleName=TES_ROLE, PolicyName=name)
            for attached in iam.list_attached_role_policies(
                    RoleName=TES_ROLE)["AttachedPolicies"]:
                iam.detach_role_policy(RoleName=TES_ROLE, PolicyArn=attached["PolicyArn"])
            iam.delete_role(RoleName=TES_ROLE)
        except iam.exceptions.NoSuchEntityException:
            pass
        try:
            iam.delete_policy(PolicyArn=ACCESS_POLICY_ARN)
        except iam.exceptions.NoSuchEntityException:
            pass
        try:
            iot.delete_role_alias(roleAlias=TES_ALIAS)
        except iot.exceptions.ResourceNotFoundException:
            pass

    _clean()
    yield
    _clean()


class _RecordingSTS:
    """Wraps moto's STS client and records every assume_role call."""

    def __init__(self, real):
        self.real = real
        self.calls = []

    def assume_role(self, **kwargs):
        self.calls.append(kwargs)
        return self.real.assume_role(**kwargs)


def _seed(stack, role_arn, external_id=None):
    usecase_id = f"uc-{uuid.uuid4()}"
    item = {"usecase_id": usecase_id, "name": "Test Use Case",
            "account_id": ACCOUNT_ID, "region": REGION,
            "cross_account_role_arn": role_arn}
    if external_id:
        item["external_id"] = external_id
    stack["usecases"].put_item(Item=item)
    registration_id = f"reg-{uuid.uuid4()}"
    token, token_hash, expires_at = stack["ts"].generate_token(registration_id, now=NOW)
    stack["registrations"].put_item(Item={
        "registration_id": registration_id, "usecase_id": usecase_id,
        "device_name": "station-42", "device_group": "Line3_Group",
        "status": "pending", "token_hash": token_hash,
        "token_expires_at": expires_at, "consumed_at": 0,
        "created_at": NOW, "updated_at": NOW})
    return registration_id, token


def _exchange(stack, token, monkeypatch):
    """POST /quick-setup/credentials with the clock at NOW."""
    import time as _time
    with monkeypatch.context() as clock:
        clock.setattr(_time, "time", lambda: NOW)
        response = stack["qs"].handler({
            "httpMethod": "POST", "path": "/v1/quick-setup/credentials",
            "body": json.dumps({"token": token}),
            "requestContext": {"domainName": "api.example.com", "stage": "v1",
                               "identity": {"sourceIp": f"ip-{uuid.uuid4()}"}},
        }, None)
    return response, json.loads(response["body"])


def _registration(stack, registration_id):
    return stack["registrations"].get_item(
        Key={"registration_id": registration_id})["Item"]


def _role_policy_document(iam, name):
    document = iam.get_role_policy(RoleName=TES_ROLE, PolicyName=name)["PolicyDocument"]
    return json.loads(document) if isinstance(document, str) else document


# ---------------------------------------------------------------------------
# token_exchange_role.prepare
# ---------------------------------------------------------------------------

def test_same_account_creates_what_the_installer_would(stack, clean_tes_role):
    iam, iot, ter = stack["iam"], stack["iot"], stack["ter"]

    assert ter.prepare(iam, iot, ACCOUNT_ID, create_missing=True) is True

    role = iam.get_role(RoleName=TES_ROLE)["Role"]
    trust = role["AssumeRolePolicyDocument"]
    trust = json.loads(trust) if isinstance(trust, str) else trust
    assert trust == ter.TES_ROLE_TRUST_POLICY
    alias = iot.describe_role_alias(roleAlias=TES_ALIAS)["roleAliasDescription"]
    assert alias["roleArn"] == role["Arn"]
    iam.get_policy(PolicyArn=ACCESS_POLICY_ARN)
    attached = iam.list_attached_role_policies(RoleName=TES_ROLE)["AttachedPolicies"]
    assert [p["PolicyArn"] for p in attached] == [ACCESS_POLICY_ARN]
    expected = _setup_station_inline_policies(ACCOUNT_ID)
    assert sorted(iam.list_role_policies(RoleName=TES_ROLE)["PolicyNames"]) == sorted(expected)
    for name, document in expected.items():
        assert _role_policy_document(iam, name) == document, name


def test_same_account_is_idempotent(stack, clean_tes_role):
    iam, iot, ter = stack["iam"], stack["iot"], stack["ter"]

    assert ter.prepare(iam, iot, ACCOUNT_ID, create_missing=True) is True
    assert ter.prepare(iam, iot, ACCOUNT_ID, create_missing=True) is True

    attached = iam.list_attached_role_policies(RoleName=TES_ROLE)["AttachedPolicies"]
    assert [p["PolicyArn"] for p in attached] == [ACCESS_POLICY_ARN]
    assert len(iam.list_role_policies(RoleName=TES_ROLE)["PolicyNames"]) == 4


def test_existing_alias_and_access_policy_are_left_as_they_are(stack, clean_tes_role):
    iam, iot, ter = stack["iam"], stack["iot"], stack["ter"]
    assert ter.prepare(iam, iot, ACCOUNT_ID, create_missing=True) is True
    # An administrator pointed the alias elsewhere and detached the policy.
    other = iam.create_role(
        RoleName="SomeOtherDeviceRole",
        AssumeRolePolicyDocument=json.dumps(ter.TES_ROLE_TRUST_POLICY))["Role"]["Arn"]
    iot.update_role_alias(roleAlias=TES_ALIAS, roleArn=other)
    iam.detach_role_policy(RoleName=TES_ROLE, PolicyArn=ACCESS_POLICY_ARN)
    try:
        assert ter.prepare(iam, iot, ACCOUNT_ID, create_missing=True) is True
        alias = iot.describe_role_alias(roleAlias=TES_ALIAS)["roleAliasDescription"]
        assert alias["roleArn"] == other
        assert iam.list_attached_role_policies(RoleName=TES_ROLE)["AttachedPolicies"] == []
    finally:
        iam.delete_role(RoleName="SomeOtherDeviceRole")


def test_cross_account_writes_only_the_inline_policies(stack, clean_tes_role):
    iam, iot, ter = stack["iam"], stack["iot"], stack["ter"]
    iam.create_role(RoleName=TES_ROLE,
                    AssumeRolePolicyDocument=json.dumps(ter.TES_ROLE_TRUST_POLICY))

    assert ter.prepare(iam, iot, ACCOUNT_ID, create_missing=False) is True

    assert len(iam.list_role_policies(RoleName=TES_ROLE)["PolicyNames"]) == 4
    assert iam.list_attached_role_policies(RoleName=TES_ROLE)["AttachedPolicies"] == []
    with pytest.raises(ClientError):
        iot.describe_role_alias(roleAlias=TES_ALIAS)
    with pytest.raises(ClientError):
        iam.get_policy(PolicyArn=ACCESS_POLICY_ARN)


def test_cross_account_missing_role_is_not_prepared(stack, clean_tes_role):
    iam, iot, ter = stack["iam"], stack["iot"], stack["ter"]

    assert ter.prepare(iam, iot, ACCOUNT_ID, create_missing=False) is False

    with pytest.raises(ClientError):
        iam.get_role(RoleName=TES_ROLE)


class _FailingIam:
    def __init__(self, code):
        self.code = code

    def put_role_policy(self, **kwargs):
        raise _client_error(self.code)

    def get_role(self, **kwargs):
        raise _client_error(self.code, "GetRole")


@pytest.mark.parametrize("code", ["AccessDenied", "AccessDeniedException"])
def test_cross_account_denied_write_is_not_prepared(code):
    import token_exchange_role

    assert token_exchange_role.prepare(
        _FailingIam(code), None, ACCOUNT_ID, create_missing=False) is False


@pytest.mark.parametrize("create_missing", [True, False])
def test_other_errors_are_raised(create_missing):
    import token_exchange_role

    with pytest.raises(ClientError):
        token_exchange_role.prepare(
            _FailingIam("Throttling"), None, ACCOUNT_ID, create_missing=create_missing)


def test_same_account_access_denied_is_raised():
    import token_exchange_role

    with pytest.raises(ClientError):
        token_exchange_role.prepare(
            _FailingIam("AccessDenied"), None, ACCOUNT_ID, create_missing=True)


# ---------------------------------------------------------------------------
# quick_setup.exchange_credentials
# ---------------------------------------------------------------------------

def test_same_account_exchange_prepares_the_role_in_its_own_session(
        stack, clean_tes_role, monkeypatch):
    qs = stack["qs"]
    recorder = _RecordingSTS(qs.sts)
    monkeypatch.setattr(qs, "sts", recorder)
    registration_id, token = _seed(stack, f"arn:aws:iam::{ACCOUNT_ID}:root")

    response, body = _exchange(stack, token, monkeypatch)

    assert response["statusCode"] == 200, body
    assert body["tes_role_prepared"] is True
    assert [c["RoleArn"] for c in recorder.calls] == [PROVISIONING_ROLE_ARN] * 2
    station, preparation = recorder.calls
    assert station["RoleSessionName"] == f"dda-qs-{registration_id}"[:64]
    assert preparation["RoleSessionName"] == f"dda-qs-tes-{registration_id}"[:64]
    assert preparation["DurationSeconds"] == qs.STS_MIN_DURATION_SECONDS
    assert "ExternalId" not in station and "ExternalId" not in preparation
    assert not _actions(json.loads(station["Policy"])) & IAM_WRITES
    assert json.loads(preparation["Policy"]) == qs.build_tes_preparation_policy(
        REGION, ACCOUNT_ID)
    stack["iam"].get_role(RoleName=TES_ROLE)
    stack["iot"].describe_role_alias(roleAlias=TES_ALIAS)
    assert _registration(stack, registration_id)["status"] == "in_progress"


def test_cross_account_exchange_uses_the_external_id_for_both_sessions(
        stack, clean_tes_role, monkeypatch):
    qs = stack["qs"]
    stack["iam"].create_role(
        RoleName=TES_ROLE,
        AssumeRolePolicyDocument=json.dumps(stack["ter"].TES_ROLE_TRUST_POLICY))
    recorder = _RecordingSTS(qs.sts)
    monkeypatch.setattr(qs, "sts", recorder)
    _, token = _seed(stack, f"arn:aws:iam::{ACCOUNT_ID}:role/DDAPortalAccessRole", "ext-1")

    response, body = _exchange(stack, token, monkeypatch)

    assert response["statusCode"] == 200, body
    assert body["tes_role_prepared"] is True
    assert len(recorder.calls) == 2
    assert all(c["ExternalId"] == "ext-1" for c in recorder.calls)
    assert len(stack["iam"].list_role_policies(RoleName=TES_ROLE)["PolicyNames"]) == 4


def test_cross_account_exchange_without_the_role_still_issues_credentials(
        stack, clean_tes_role, monkeypatch):
    _, token = _seed(stack, f"arn:aws:iam::{ACCOUNT_ID}:role/DDAPortalAccessRole", "ext-2")

    response, body = _exchange(stack, token, monkeypatch)

    assert response["statusCode"] == 200, body
    assert body["tes_role_prepared"] is False
    assert body["credentials"]["access_key_id"]


def test_preparation_failure_returns_503_and_keeps_the_token(
        stack, clean_tes_role, monkeypatch):
    def throttled(iam, account_id):
        raise _client_error("Throttling")

    registration_id, token = _seed(stack, f"arn:aws:iam::{ACCOUNT_ID}:root")

    with monkeypatch.context() as failing:
        failing.setattr(stack["ter"], "_put_inline_policies", throttled)
        response, body = _exchange(stack, token, monkeypatch)

    assert response["statusCode"] == 503, body
    assert body == {"error": "credential_issuance_failed"}
    stored = _registration(stack, registration_id)
    assert stored["status"] == "pending"
    assert int(stored["consumed_at"]) == 0
    assert not stored.get("report_secret_hash")

    # The same token works once the preparation succeeds.
    retry, retry_body = _exchange(stack, token, monkeypatch)
    assert retry["statusCode"] == 200, retry_body
    assert retry_body["tes_role_prepared"] is True
    assert _registration(stack, registration_id)["status"] == "in_progress"
