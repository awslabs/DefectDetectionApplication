"""
Use-case creation permission, Proof_Of_Control, the verify-role contract,
the PUT account rules and the bounded account grants
(portal-authorization-and-iam-scoping, design I1 "Test strategy"; tasks
1.1-1.3).

Harness (usecase_account_fakes.install): recording STS / S3 / EventBridge /
Lambda fakes on the usecases module, a recording attach validator that
admits every bucket, a recording audit log and a write log over the
handler's tables, on a private moto use-case table emptied per test. The
cases whose outcome depends on how roles resolve run with
PORTAL_REGISTRY_ENFORCED off and on.

Fixture accounts: 111111111111 portal, 222222222222 trusted use-case
account, 333333333333 data account (also the second trusted account of the
account-move cases), 444444444444 untrusted.

Validates: Requirements 1.1-1.3, 2.1-2.8, 3.1-3.5
"""
import json
import sys
from types import SimpleNamespace

import pytest
from botocore.exceptions import ConnectTimeoutError, EndpointConnectionError, ReadTimeoutError

import usecase_account_fakes as fx
from usecase_account_fakes import (
    COMPONENT_BUCKET, DATA, PORTAL, TRUSTED, UNTRUSTED, access_role, api_event, call,
    client_error, data_role, root_arn, sagemaker_role)

# A second trusted use-case account for the account-move cases (the host
# lane's synth fixture trusts the same pair).
SECOND = DATA
READ_SID = "AllowGreengrassDeviceRead-{}"
LIST_SID = "AllowGreengrassDeviceList-{}"
EVENT_SID = "AllowUseCaseAccount-{}"
VERIFY_SESSION = "dda-portal-verify-control"
# DELETE also reports the use case's Device_Access_Document revoke in
# grants_removed (design I6). The records here hold no statement in it (the
# device role is absent in moto), so the revoke changes nothing.
DEVICE_ACCESS_UNCHANGED = {"status": "unchanged", "role_name": "GreengrassV2TokenExchangeRole",
                           "policy_name": "DDAUseCaseDeviceAccess"}


@pytest.fixture(scope="module")
def shared(aws_stack):
    import shared_utils
    assert hasattr(shared_utils, "account_trusted"), "this suite needs the real shared layer"
    return shared_utils


@pytest.fixture(scope="module")
def uc(aws_stack, shared):
    sys.modules.pop("usecases", None)
    import usecases
    return usecases


@pytest.fixture
def env(aws_stack, shared, uc, monkeypatch):
    fakes = fx.install(uc, shared, monkeypatch)
    fx.clear_usecases_table()
    return SimpleNamespace(uc=uc, shared=shared, stack=aws_stack, fakes=fakes, mp=monkeypatch)


@pytest.fixture(params=["enforcement-off", "enforcement-on"])
def enforcement(request, env):
    if request.param == "enforcement-on":
        env.mp.setenv(env.shared.PORTAL_REGISTRY_ENFORCED_ENV, "true")
    return request.param


def admin(env):
    return fx.portal_admin(env.stack)


def create(env, caller, body):
    return call(env.uc, api_event("POST", "/usecases", caller, body=body))


def verify(env, caller, body):
    return call(env.uc, api_event("POST", "/usecases/verify-role", caller, body=body))


def put(env, caller, usecase_id, body):
    return call(env.uc, api_event("PUT", f"/usecases/{usecase_id}", caller, usecase_id, body))


def delete(env, caller, usecase_id):
    return call(env.uc, api_event("DELETE", f"/usecases/{usecase_id}", caller, usecase_id))


def named(name):
    return [item for item in fx.usecases_table().scan()["Items"] if item.get("name") == name]


def without_event(rows):
    return [{key: value for key, value in row.items() if key != "event"} for row in rows]


def device_pair(account):
    """The two statements update_component_bucket_policy_for_greengrass
    writes for an account's device role."""
    principal = {"AWS": f"arn:aws:iam::{account}:role/GreengrassV2TokenExchangeRole"}
    return [
        {"Sid": READ_SID.format(account), "Effect": "Allow", "Principal": principal,
         "Action": ["s3:GetObject", "s3:GetObjectVersion"],
         "Resource": f"arn:aws:s3:::{COMPONENT_BUCKET}/*"},
        {"Sid": LIST_SID.format(account), "Effect": "Allow", "Principal": principal,
         "Action": ["s3:ListBucket", "s3:GetBucketLocation"],
         "Resource": f"arn:aws:s3:::{COMPONENT_BUCKET}"},
    ]


def shared_components_statements(*accounts):
    """The two statements the SharedComponents writer maintains."""
    roles = []
    for account in accounts:
        roles += [access_role(account), f"arn:aws:iam::{account}:role/GreengrassV2TokenExchangeRole"]
    resources = [f"arn:aws:s3:::{COMPONENT_BUCKET}", f"arn:aws:s3:::{COMPONENT_BUCKET}/*"]
    return [
        {"Sid": "AllowUseCaseAccountsGreengrassAccess", "Effect": "Allow",
         "Principal": {"AWS": roles},
         "Action": ["s3:GetObject", "s3:GetObjectVersion", "s3:GetBucketLocation"],
         "Resource": resources},
        {"Sid": "AllowGreengrassServiceAccess", "Effect": "Allow",
         "Principal": {"Service": "greengrass.amazonaws.com"},
         "Action": ["s3:GetObject", "s3:GetBucketLocation"], "Resource": resources,
         "Condition": {"StringEquals": {"aws:SourceAccount": [PORTAL, *accounts]}}},
    ]


def move_body(account, **fields):
    body = {"account_id": account, "cross_account_role_arn": access_role(account),
            "external_id": "ext-moved", "sagemaker_execution_role_arn": sagemaker_role(account)}
    body.update(fields)
    return body


# The body the Edit modal sends: UseCases.tsx handleEdit loads the form from
# the stored use case (every account field but external_id and
# sagemaker_execution_role_arn, the optional ones as '') and handleUpdate
# spreads the whole form into the PUT body, tuning_sample_retention_days only
# when that field is filled. Keep this key set in step with
# edge-cv-portal/frontend/src/pages/UseCases.tsx handleEdit / handleUpdate.
EDIT_MODAL_KEYS = {
    "name", "account_id", "s3_bucket", "s3_prefix", "cross_account_role_arn",
    "data_account_id", "data_account_role_arn", "data_account_external_id",
    "data_s3_bucket", "data_s3_prefix", "tuning_sample_export",
}


def edit_modal_body(stored, **changes):
    body = {
        "name": stored["name"],
        "account_id": stored.get("account_id"),
        "s3_bucket": stored["s3_bucket"],
        "s3_prefix": stored.get("s3_prefix") or "",
        "cross_account_role_arn": stored.get("cross_account_role_arn") or "",
        "data_account_id": stored.get("data_account_id") or "",
        "data_account_role_arn": stored.get("data_account_role_arn") or "",
        "data_account_external_id": stored.get("data_account_external_id") or "",
        "data_s3_bucket": stored.get("data_s3_bucket") or "",
        "data_s3_prefix": stored.get("data_s3_prefix") or "",
        "tuning_sample_export": stored.get("tuning_sample_export") is True,
    }
    body.update(changes)
    return body


# ============================================================ POST /usecases

@pytest.mark.parametrize("claim,scope,registry_role", [
    ("DataScientist", None, None),
    ("UseCaseAdmin", "uc-elsewhere", "UseCaseAdmin"),
    ("Viewer", "global", "Viewer"),
])
def test_create_without_the_permission_is_403_with_only_an_audit_row(env, enforcement, claim,
                                                                     scope, registry_role):
    caller = fx.user(env.stack, claim, scope, registry_role)
    body = fx.multi_account_body()
    status, payload, _ = create(env, caller, body)
    assert status == 403, payload
    assert payload == {"error": "Insufficient permissions to create use cases",
                       "error_code": "INSUFFICIENT_PERMISSIONS"}
    assert without_event(env.fakes.audit.rows) == [{
        "user_id": caller["user_id"], "action": "unauthorized_access",
        "resource_type": "api_endpoint", "resource_id": "/usecases", "result": "denied",
        "details": {"required_permission": "create_usecases"}}]
    assert env.fakes.audit.rows[0]["event"] is not None
    assert env.fakes.writes() == [] and env.fakes.sts.calls == []
    assert env.fakes.validator.calls == [] and env.fakes.s3.calls == []
    assert env.fakes.events.calls == [] and env.fakes.lam.invocations == []
    assert named(body["name"]) == []


def test_registry_unavailable_is_500_on_create_and_verify_role(env):
    def unavailable(user_id, usecase_id=None):
        raise env.shared.RegistryUnavailable("registry lookup failed")

    env.mp.setenv(env.shared.PORTAL_REGISTRY_ENFORCED_ENV, "true")
    env.mp.setattr(env.shared, "_lookup_identity", unavailable)
    caller = fx.user(env.stack, "PortalAdmin")
    body = fx.multi_account_body()
    status, payload, _ = create(env, caller, body)
    assert (status, payload) == (500, {"error": "Authorization check failed"})
    status, payload, _ = verify(env, caller, {"role_arn": access_role(TRUSTED)})
    assert (status, payload) == (500, {"error": "Authorization check failed"})
    assert [row["details"] for row in env.fakes.audit.rows if row["result"] == "failure"] == [
        {"reason": "registry_unavailable"}, {"reason": "registry_unavailable"}]
    assert env.fakes.sts.calls == [] and env.fakes.writes() == [] and named(body["name"]) == []


def test_create_multi_account_proves_control_before_anything_is_written(env, enforcement):
    caller = admin(env)
    body = fx.multi_account_body(external_id="ext-1")
    status, payload, raw = create(env, caller, body)
    assert status == 201, payload
    assert env.fakes.sts.calls == [("assume_role", {
        "RoleArn": access_role(TRUSTED), "RoleSessionName": VERIFY_SESSION,
        "DurationSeconds": 900, "ExternalId": "ext-1"})]
    ops = env.fakes.log.ops()
    assert ops[0] == ("sts", "assume_role")
    assert ops.index(("sts", "assume_role")) < ops.index(("dynamodb", "put_item"))
    assert len(env.fakes.validator.calls) == 1
    stored = fx.record(payload["usecase"]["usecase_id"])
    assert (stored["account_id"], stored["cross_account_role_arn"], stored["external_id"]) == (
        TRUSTED, access_role(TRUSTED), "ext-1")
    assert env.stack.tables.user_roles.get_item(Key={
        "user_id": caller["user_id"], "usecase_id": stored["usecase_id"]})["Item"]["role"] == "UseCaseAdmin"
    assert env.fakes.s3.sids() == sorted([READ_SID.format(TRUSTED), LIST_SID.format(TRUSTED)])
    assert set(env.fakes.events.statements) == {EVENT_SID.format(TRUSTED)}
    assert "fake-secret" not in raw and "fake-token" not in raw


def test_create_untrusted_account_is_refused_without_an_sts_call(env):
    body = fx.multi_account_body(UNTRUSTED)
    status, payload, _ = create(env, admin(env), body)
    assert status == 400, payload
    assert payload == {
        "error": (f"account {UNTRUSTED} is not in the portal's trusted use-case accounts; "
                  "add it to trustedUseCaseAccountIds and redeploy the portal"),
        "error_code": "ACCOUNT_NOT_TRUSTED", "remedy": env.uc.REMEDY_ACCOUNT_NOT_TRUSTED}
    assert env.fakes.sts.calls == [] and env.fakes.validator.calls == []
    assert env.fakes.writes() == [] and named(body["name"]) == []
    assert [row["details"] for row in env.fakes.audit.of("create_usecase", result="failure")] == [
        {"reason": "account_not_trusted", "account_id": UNTRUSTED}]


@pytest.mark.parametrize("outcome", [
    client_error("AccessDenied", "not authorized to perform sts:AssumeRole", 403, "AssumeRole"),
    client_error("NoSuchEntity", "the role does not exist", 404, "AssumeRole"),
    "assumed-in-another-account",
])
def test_create_failed_proof_is_400_and_writes_and_grants_nothing(env, outcome):
    if outcome == "assumed-in-another-account":
        env.fakes.sts.assumed_account = DATA
    else:
        env.fakes.sts.outcome = outcome
    body = fx.multi_account_body()
    status, payload, _ = create(env, admin(env), body)
    assert status == 400, payload
    assert payload["error_code"] == "ACCOUNT_CONTROL_NOT_VERIFIED"
    assert access_role(TRUSTED) in payload["error"] and "DDAPortalUseCaseAccountStack" in payload["error"]
    assert payload["remedy"] == env.uc.REMEDY_ACCOUNT_CONTROL
    assert env.fakes.writes() == [] and named(body["name"]) == []
    assert env.fakes.validator.calls == [] and env.fakes.s3.calls == []
    assert env.fakes.events.calls == [] and env.fakes.lam.invocations == []
    assert [row["details"] for row in env.fakes.audit.of("create_usecase", result="failure")] == [
        {"reason": "account_control_not_verified", "account_id": TRUSTED}]


STS_OUTAGES = [
    client_error("Throttling", status=400, operation="AssumeRole"),
    client_error("ThrottlingException", status=400, operation="AssumeRole"),
    client_error("RequestLimitExceeded", status=400, operation="AssumeRole"),
    EndpointConnectionError(endpoint_url="sts-endpoint"),
    ConnectTimeoutError(endpoint_url="sts-endpoint"),
    ReadTimeoutError(endpoint_url="sts-endpoint"),
]


@pytest.mark.parametrize("error", STS_OUTAGES, ids=[
    "Throttling", "ThrottlingException", "RequestLimitExceeded", "EndpointConnectionError",
    "ConnectTimeoutError", "ReadTimeoutError"])
def test_create_sts_unavailable_is_503_and_writes_nothing(env, error):
    env.fakes.sts.outcome = error
    body = fx.multi_account_body()
    status, payload, _ = create(env, admin(env), body)
    assert (status, payload) == (503, {"error": "STS is temporarily unavailable; retry",
                                       "error_code": "STS_UNAVAILABLE"})
    assert env.fakes.writes() == [] and named(body["name"]) == []
    assert [row["details"] for row in env.fakes.audit.of("create_usecase", result="failure")] == [
        {"reason": "sts_unavailable", "account_id": TRUSTED}]


def test_create_single_account_body_makes_no_assume_role_and_no_trusted_check(env, enforcement):
    env.mp.delenv("TRUSTED_USECASE_ACCOUNT_IDS")      # a trusted check would answer 500
    status, payload, _ = create(env, admin(env), fx.single_account_body())
    assert status == 201, payload
    assert env.fakes.sts.calls == [("get_caller_identity", {})]
    stored = fx.record(payload["usecase"]["usecase_id"])
    assert (stored["account_id"], stored["cross_account_role_arn"]) == (PORTAL, root_arn(PORTAL))


@pytest.mark.parametrize("fields,code", [
    ({"cross_account_role_arn": f"arn:aws:iam::{TRUSTED}:role/SomeOtherRole"}, "INVALID_ROLE_ARN"),
    ({"cross_account_role_arn": access_role(DATA)}, "INVALID_ROLE_ARN"),
    ({"cross_account_role_arn": f"arn:aws:iam::{TRUSTED}:role/path/DDAPortalAccessRole"},
     "INVALID_ROLE_ARN"),
    ({"cross_account_role_arn": "DDAPortalAccessRole"}, "INVALID_ROLE_ARN"),
    ({"account_id": "acct-remote"}, "INVALID_ACCOUNT_ID"),
    ({"account_id": f"{TRUSTED}\n"}, "INVALID_ACCOUNT_ID"),
    ({"sagemaker_execution_role_arn": sagemaker_role(DATA)}, "INVALID_ROLE_ARN"),
    ({"sagemaker_execution_role_arn": "DDASageMakerExecutionRole"}, "INVALID_ROLE_ARN"),
    ({"data_account_id": DATA}, "INVALID_ROLE_ARN"),
    ({"data_account_id": DATA, "data_account_role_arn": access_role(DATA)}, "INVALID_ROLE_ARN"),
    ({"data_account_id": DATA, "data_account_role_arn": data_role(UNTRUSTED)}, "INVALID_ROLE_ARN"),
    ({"data_account_id": "acct-data", "data_account_role_arn": data_role(DATA)}, "INVALID_ACCOUNT_ID"),
    # Grammar comes before the trusted check.
    ({"account_id": UNTRUSTED, "cross_account_role_arn": f"arn:aws:iam::{UNTRUSTED}:role/Other",
      "sagemaker_execution_role_arn": sagemaker_role(UNTRUSTED)}, "INVALID_ROLE_ARN"),
])
def test_create_grammar_refusals_come_before_any_sts_call(env, fields, code):
    body = fx.multi_account_body(**fields)
    status, payload, _ = create(env, admin(env), body)
    assert status == 400, payload
    assert payload["error_code"] == code and payload["error"]
    if fields.get("data_account_id") == DATA and "data_account_role_arn" not in fields:
        assert "supply data_account_role_arn" in payload["error"]
    assert env.fakes.sts.calls == [] and env.fakes.writes() == [] and named(body["name"]) == []


def test_create_single_account_body_with_a_data_account_but_no_role_is_refused(env):
    body = fx.single_account_body(data_account_id=DATA, data_s3_bucket="data-bucket")
    status, payload, _ = create(env, admin(env), body)
    assert status == 400, payload
    assert payload["error_code"] == "INVALID_ROLE_ARN"
    assert "supply data_account_role_arn" in payload["error"]
    assert env.fakes.sts.calls == [("get_caller_identity", {})]     # never an AssumeRole
    assert env.fakes.writes() == [] and named(body["name"]) == []


@pytest.mark.parametrize("trusted", [None, "", " , "])
def test_create_without_a_trusted_list_is_500(env, trusted):
    if trusted is None:
        env.mp.delenv("TRUSTED_USECASE_ACCOUNT_IDS")
    else:
        env.mp.setenv("TRUSTED_USECASE_ACCOUNT_IDS", trusted)
    status, payload, _ = create(env, admin(env), fx.multi_account_body())
    assert (status, payload) == (500, {"error": "Trusted account list is not configured"})
    assert env.fakes.sts.calls == [] and env.fakes.writes() == []


def test_component_bucket_name_is_derived_and_agrees_with_shared_components(env):
    status, payload, _ = create(env, admin(env), fx.single_account_body())
    assert status == 201, payload
    assert {bucket for _, bucket in env.fakes.s3.calls} == {COMPONENT_BUCKET}
    assert env.uc.component_bucket_name() == f"dda-component-us-east-1-{PORTAL}" == COMPONENT_BUCKET
    sys.modules.pop("shared_components", None)
    import shared_components
    assert shared_components.COMPONENT_BUCKET == COMPONENT_BUCKET


# ================================================== POST /usecases/verify-role

def test_verify_role_frontend_body_answers_the_200_shape_byte_for_byte(env, enforcement):
    status, _, raw = verify(env, admin(env), {"role_arn": access_role(TRUSTED), "external_id": "ext-1"})
    assert status == 200, raw
    assumed = f"arn:aws:sts::{TRUSTED}:assumed-role/DDAPortalAccessRole/{VERIFY_SESSION}"
    assert raw == json.dumps({"status": "success", "account_id": TRUSTED, "assumed_role": assumed})
    assert env.fakes.sts.calls == [("assume_role", {
        "RoleArn": access_role(TRUSTED), "RoleSessionName": VERIFY_SESSION,
        "DurationSeconds": 900, "ExternalId": "ext-1"})]
    assert env.fakes.writes() == []


@pytest.mark.parametrize("code,message", [
    ("AccessDenied", "User is not authorized to perform: sts:AssumeRole"),
    ("NoSuchEntity", "The role cannot be found"),
])
def test_verify_role_sts_denial_answers_the_400_shape_byte_for_byte(env, enforcement, code, message):
    env.fakes.sts.outcome = client_error(code, message, 403, "AssumeRole")
    status, _, raw = verify(env, admin(env), {"role_arn": access_role(TRUSTED), "external_id": "ext-1"})
    assert status == 400
    assert raw == json.dumps({"status": "failed", "error": f"{code}: {message}"})


def test_verify_role_missing_role_arn_keeps_its_400(env):
    status, _, raw = verify(env, admin(env), {"external_id": "ext-1"})
    assert (status, raw) == (400, json.dumps({"error": "role_arn is required"}))
    assert env.fakes.sts.calls == []


def _forbidden(env):
    return fx.user(env.stack, "DataScientist"), {"role_arn": access_role(TRUSTED), "external_id": "e"}


def _proof_failure(env):
    env.fakes.sts.outcome = client_error("AccessDenied", "denied", 403, "AssumeRole")
    return admin(env), {"role_arn": access_role(TRUSTED), "external_id": "e"}


def _invalid_role_arn(env):
    return admin(env), {"role_arn": f"arn:aws:iam::{TRUSTED}:role/SomeOtherRole", "external_id": "e"}


def _invalid_account_id(env):
    return admin(env), {"role_arn": access_role(TRUSTED), "external_id": "e", "account_id": DATA}


def _not_trusted(env):
    return admin(env), {"role_arn": access_role(UNTRUSTED), "external_id": "e"}


def _sts_unavailable(env):
    env.fakes.sts.outcome = EndpointConnectionError(endpoint_url="sts-endpoint")
    return admin(env), {"role_arn": access_role(TRUSTED), "external_id": "e"}


VERIFY_REFUSALS = {
    # case: (setup, status, error_code, reaches STS)
    "forbidden": (_forbidden, 403, "INSUFFICIENT_PERMISSIONS", False),
    "proof_failure": (_proof_failure, 400, None, True),
    "invalid_role_arn": (_invalid_role_arn, 400, "INVALID_ROLE_ARN", False),
    "invalid_account_id": (_invalid_account_id, 400, "INVALID_ACCOUNT_ID", False),
    "not_trusted": (_not_trusted, 400, "ACCOUNT_NOT_TRUSTED", False),
    "sts_unavailable": (_sts_unavailable, 503, "STS_UNAVAILABLE", True),
}


@pytest.mark.parametrize("case", list(VERIFY_REFUSALS))
def test_every_verify_role_refusal_carries_error_and_failed_status(env, enforcement, case):
    setup, expected_status, expected_code, reaches_sts = VERIFY_REFUSALS[case]
    caller, body = setup(env)
    status, payload, _ = verify(env, caller, body)
    assert status == expected_status, payload
    assert payload["status"] == "failed"
    assert isinstance(payload["error"], str) and payload["error"].strip()
    assert payload.get("error_code") == expected_code
    assert bool(env.fakes.sts.calls) is reaches_sts
    assert env.fakes.writes() == []


def test_verify_role_without_the_permission_is_audited_and_never_reaches_sts(env, enforcement):
    caller = fx.user(env.stack, "UseCaseAdmin", "uc-elsewhere", "UseCaseAdmin")
    status, payload, _ = verify(env, caller, {"role_arn": access_role(TRUSTED), "external_id": "e"})
    assert status == 403
    assert payload == {"status": "failed",
                       "error": "Insufficient permissions to verify roles for use-case onboarding",
                       "error_code": "INSUFFICIENT_PERMISSIONS"}
    assert without_event(env.fakes.audit.rows) == [{
        "user_id": caller["user_id"], "action": "unauthorized_access",
        "resource_type": "api_endpoint", "resource_id": "/usecases/verify-role",
        "result": "denied", "details": {"required_permission": "create_usecases"}}]
    assert env.fakes.sts.calls == []


def test_verify_role_messages(env):
    status, payload, _ = verify(env, admin(env), {"role_arn": "not-an-arn"})
    assert (status, payload) == (400, {
        "status": "failed",
        "error": "role_arn must be arn:aws:iam::<account>:role/DDAPortalAccessRole",
        "error_code": "INVALID_ROLE_ARN"})
    status, payload, _ = verify(env, admin(env), {"role_arn": access_role(TRUSTED), "account_id": DATA})
    assert (status, payload) == (400, {"status": "failed", "error": "account_id does not match role_arn",
                                       "error_code": "INVALID_ACCOUNT_ID"})
    status, payload, _ = verify(env, admin(env), {"role_arn": access_role(TRUSTED), "account_id": TRUSTED})
    assert status == 200 and payload["account_id"] == TRUSTED
    assert [row["details"] for row in env.fakes.audit.of("verify_role", result="failure")] == []


# ====================================================== PUT /usecases/{id}

@pytest.mark.parametrize("kind", ["single", "multi"])
def test_edit_modal_full_form_body_carries_no_account_field_change(env, enforcement, kind):
    usecase_id = fx.put_single_account_record() if kind == "single" else fx.put_multi_account_record()
    body = edit_modal_body(fx.record(usecase_id), name="Renamed in the modal")
    assert set(body) == EDIT_MODAL_KEYS
    status, payload, _ = put(env, admin(env), usecase_id, body)
    assert status == 200, payload
    assert env.fakes.sts.calls == [] and env.fakes.validator.calls == []
    assert "grants_removed" not in payload and "grants_added" not in payload
    assert fx.record(usecase_id)["name"] == "Renamed in the modal"


def test_edit_modal_body_with_a_changed_role_arn_on_a_single_account_record_is_refused(env):
    usecase_id = fx.put_single_account_record()
    before = fx.record(usecase_id)
    body = edit_modal_body(before, cross_account_role_arn=access_role(PORTAL))
    status, payload, _ = put(env, admin(env), usecase_id, body)
    assert status == 400, payload
    assert payload == {
        "error": ("a single-account use case does not carry a cross-account role; "
                  "supply account_id to move it to another account"),
        "error_code": "INVALID_ROLE_ARN"}
    assert env.fakes.sts.calls == []          # neither AssumeRole nor GetCallerIdentity
    assert env.fakes.writes() == [] and fx.record(usecase_id) == before


def test_edit_modal_body_with_a_changed_role_arn_on_a_multi_account_record_is_refused(env):
    usecase_id = fx.put_multi_account_record()
    before = fx.record(usecase_id)
    body = edit_modal_body(before, cross_account_role_arn=access_role(SECOND))
    status, payload, _ = put(env, admin(env), usecase_id, body)
    assert status == 400 and payload["error_code"] == "INVALID_ROLE_ARN"
    assert env.fakes.sts.calls == [] and fx.record(usecase_id) == before


def test_put_of_a_non_account_field_makes_no_sts_call(env):
    usecase_id = fx.put_multi_account_record()
    status, payload, _ = put(env, admin(env), usecase_id, {"description": "only this"})
    assert status == 200, payload
    assert env.fakes.sts.calls == [] and fx.record(usecase_id)["description"] == "only this"


def test_put_of_s3_bucket_on_a_single_account_record_applies_no_account_grammar(env):
    usecase_id = fx.put_single_account_record()
    status, payload, _ = put(env, admin(env), usecase_id, {"s3_bucket": "another-bucket"})
    assert status == 200, payload
    assert env.fakes.sts.calls == [] and len(env.fakes.validator.calls) == 1
    assert fx.record(usecase_id)["s3_bucket"] == "another-bucket"


def test_single_to_multi_without_a_role_arn_is_refused(env):
    usecase_id = fx.put_single_account_record()
    before = fx.record(usecase_id)
    status, payload, _ = put(env, admin(env), usecase_id, {
        "account_id": TRUSTED, "sagemaker_execution_role_arn": sagemaker_role(TRUSTED)})
    assert status == 400, payload
    assert payload == {
        "error": "a cross-account role ARN is required to move a use case to another account",
        "error_code": "INVALID_ROLE_ARN"}
    assert env.fakes.sts.calls == [] and fx.record(usecase_id) == before


def test_single_to_multi_is_proven_and_writes_the_new_accounts_grants(env):
    usecase_id = fx.put_single_account_record()
    status, payload, _ = put(env, admin(env), usecase_id, move_body(TRUSTED))
    assert status == 200, payload
    assert env.fakes.sts.calls == [("assume_role", {
        "RoleArn": access_role(TRUSTED), "RoleSessionName": VERIFY_SESSION,
        "DurationSeconds": 900, "ExternalId": "ext-moved"})]
    stored = fx.record(usecase_id)
    assert (stored["account_id"], stored["cross_account_role_arn"], stored["external_id"],
            stored["sagemaker_execution_role_arn"]) == (
        TRUSTED, access_role(TRUSTED), "ext-moved", sagemaker_role(TRUSTED))
    assert payload["grants_removed"] == {"status": "skipped", "reason": "single_account",
                                         "account_id": PORTAL}
    assert payload["grants_added"]["component_bucket_policy"]["status"] == "success"
    assert payload["grants_added"]["eventbridge"]["status"] == "success"
    assert env.fakes.s3.sids() == sorted([READ_SID.format(TRUSTED), LIST_SID.format(TRUSTED)])
    assert set(env.fakes.events.statements) == {EVENT_SID.format(TRUSTED)}


def test_single_to_multi_keeping_the_portal_sagemaker_role_is_refused(env):
    usecase_id = fx.put_single_account_record()
    before = fx.record(usecase_id)
    body = move_body(TRUSTED)
    del body["sagemaker_execution_role_arn"]
    status, payload, _ = put(env, admin(env), usecase_id, body)
    assert (status, payload) == (400, {
        "error": f"supply sagemaker_execution_role_arn in account {TRUSTED}",
        "error_code": "INVALID_ROLE_ARN"})
    assert env.fakes.sts.calls == [] and fx.record(usecase_id) == before


@pytest.mark.parametrize("account_id", [PORTAL, ""])
def test_multi_to_single_is_refused(env, account_id):
    usecase_id = fx.put_multi_account_record()
    before = fx.record(usecase_id)
    status, payload, _ = put(env, admin(env), usecase_id, {"account_id": account_id})
    assert (status, payload) == (400, {
        "error": "a multi-account use case cannot be converted to single-account; create a new use case",
        "error_code": "INVALID_ACCOUNT_ID"})
    assert env.fakes.sts.calls == [] and fx.record(usecase_id) == before


def test_multi_account_record_storing_the_portal_account_echoed_is_no_change(env):
    usecase_id = fx.put_multi_account_record(PORTAL)
    status, payload, _ = put(env, admin(env), usecase_id, edit_modal_body(fx.record(usecase_id)))
    assert status == 200, payload
    assert env.fakes.sts.calls == []


def test_external_id_change_is_proven_then_persisted(env):
    usecase_id = fx.put_multi_account_record()
    status, payload, _ = put(env, admin(env), usecase_id, {"external_id": "ext-rotated"})
    assert status == 200, payload
    assert env.fakes.sts.calls == [("assume_role", {
        "RoleArn": access_role(TRUSTED), "RoleSessionName": VERIFY_SESSION,
        "DurationSeconds": 900, "ExternalId": "ext-rotated"})]
    assert fx.record(usecase_id)["external_id"] == "ext-rotated"
    assert "grants_removed" not in payload and "grants_added" not in payload

    env.fakes.sts.outcome = client_error("AccessDenied", "denied", 403, "AssumeRole")
    status, payload, _ = put(env, admin(env), usecase_id, {"external_id": "ext-wrong"})
    assert status == 400 and payload["error_code"] == "ACCOUNT_CONTROL_NOT_VERIFIED"
    assert fx.record(usecase_id)["external_id"] == "ext-rotated"
    assert [row["details"] for row in env.fakes.audit.of("update_usecase", result="failure")] == [
        {"reason": "account_control_not_verified", "account_id": TRUSTED}]


def test_put_move_to_an_untrusted_account_is_refused_without_sts(env):
    usecase_id = fx.put_multi_account_record()
    before = fx.record(usecase_id)
    status, payload, _ = put(env, admin(env), usecase_id, move_body(UNTRUSTED))
    assert status == 400 and payload["error_code"] == "ACCOUNT_NOT_TRUSTED"
    assert env.fakes.sts.calls == [] and fx.record(usecase_id) == before


def test_put_sts_unavailable_is_503(env):
    usecase_id = fx.put_multi_account_record()
    before = fx.record(usecase_id)
    env.fakes.sts.outcome = EndpointConnectionError(endpoint_url="sts-endpoint")
    status, payload, _ = put(env, admin(env), usecase_id, {"external_id": "ext-new"})
    assert (status, payload) == (503, {"error": "STS is temporarily unavailable; retry",
                                       "error_code": "STS_UNAVAILABLE"})
    assert fx.record(usecase_id) == before


@pytest.mark.parametrize("kind", ["single", "multi"])
def test_put_differing_data_account_without_its_role_is_refused(env, kind):
    usecase_id = fx.put_single_account_record() if kind == "single" else fx.put_multi_account_record()
    before = fx.record(usecase_id)
    status, payload, _ = put(env, admin(env), usecase_id, {"data_account_id": DATA})
    assert status == 400 and payload["error_code"] == "INVALID_ROLE_ARN"
    assert "supply data_account_role_arn" in payload["error"]
    assert env.fakes.sts.calls == [] and fx.record(usecase_id) == before


def test_put_change_confined_to_the_data_account_triple_gets_the_grammar_only(env):
    usecase_id = fx.put_multi_account_record()
    status, payload, _ = put(env, admin(env), usecase_id, {
        "data_account_id": DATA, "data_account_role_arn": data_role(DATA),
        "data_account_external_id": "ext-data"})
    assert status == 200, payload
    assert env.fakes.sts.calls == []
    assert fx.record(usecase_id)["data_account_role_arn"] == data_role(DATA)


# ------------------------------------------- sagemaker_execution_role_arn

def test_move_keeping_the_old_accounts_sagemaker_role_is_refused(env):
    env.mp.setenv("TRUSTED_USECASE_ACCOUNT_IDS", f"{TRUSTED},{SECOND}")
    usecase_id = fx.put_multi_account_record(TRUSTED)
    before = fx.record(usecase_id)
    body = move_body(SECOND)
    del body["sagemaker_execution_role_arn"]
    status, payload, _ = put(env, admin(env), usecase_id, body)
    assert (status, payload) == (400, {
        "error": f"supply sagemaker_execution_role_arn in account {SECOND}",
        "error_code": "INVALID_ROLE_ARN"})
    assert env.fakes.sts.calls == [] and fx.record(usecase_id) == before


def test_move_supplying_the_sagemaker_role_in_the_new_account_is_stored(env):
    env.mp.setenv("TRUSTED_USECASE_ACCOUNT_IDS", f"{TRUSTED},{SECOND}")
    usecase_id = fx.put_multi_account_record(TRUSTED)
    status, payload, _ = put(env, admin(env), usecase_id, move_body(SECOND))
    assert status == 200, payload
    assert fx.record(usecase_id)["sagemaker_execution_role_arn"] == sagemaker_role(SECOND)


@pytest.mark.parametrize("kind", ["single", "multi"])
def test_sagemaker_role_change_alone_gets_the_grammar_and_no_sts(env, kind):
    account = PORTAL if kind == "single" else TRUSTED
    usecase_id = fx.put_single_account_record() if kind == "single" else fx.put_multi_account_record()
    other_role = f"arn:aws:iam::{account}:role/DDAOtherExecutionRole"
    status, payload, _ = put(env, admin(env), usecase_id, {"sagemaker_execution_role_arn": other_role})
    assert status == 200, payload
    assert fx.record(usecase_id)["sagemaker_execution_role_arn"] == other_role
    status, payload, _ = put(env, admin(env), usecase_id, {
        "sagemaker_execution_role_arn": sagemaker_role(UNTRUSTED)})
    assert status == 400 and payload["error_code"] == "INVALID_ROLE_ARN"
    assert fx.record(usecase_id)["sagemaker_execution_role_arn"] == other_role
    assert env.fakes.sts.calls == []


def test_echoed_sagemaker_role_is_no_change(env):
    usecase_id = fx.put_multi_account_record()
    stored = fx.record(usecase_id)
    status, payload, _ = put(env, admin(env), usecase_id, {
        "sagemaker_execution_role_arn": stored["sagemaker_execution_role_arn"],
        "account_id": stored["account_id"], "external_id": stored["external_id"]})
    assert status == 200, payload
    assert env.fakes.sts.calls == []


def test_account_and_bucket_change_with_a_failed_proof_makes_no_eligibility_probe(env):
    env.mp.setenv("TRUSTED_USECASE_ACCOUNT_IDS", f"{TRUSTED},{SECOND}")
    usecase_id = fx.put_multi_account_record(TRUSTED)
    before = fx.record(usecase_id)
    env.fakes.sts.outcome = client_error("AccessDenied", "denied", 403, "AssumeRole")
    status, payload, _ = put(env, admin(env), usecase_id, move_body(SECOND, s3_bucket="moved-bucket"))
    assert status == 400 and payload["error_code"] == "ACCOUNT_CONTROL_NOT_VERIFIED"
    assert env.fakes.validator.calls == []
    assert [op for op in env.fakes.log.ops() if op == ("dynamodb", "update_item")] == []
    assert fx.record(usecase_id) == before


# ------------------------------------------------- grant maintenance (move)

def test_move_removes_the_unreferenced_old_grants_then_writes_the_new_ones(env):
    env.mp.setenv("TRUSTED_USECASE_ACCOUNT_IDS", f"{TRUSTED},{SECOND}")
    usecase_id = fx.put_multi_account_record(TRUSTED)
    env.fakes.s3.seed(device_pair(TRUSTED) + shared_components_statements(TRUSTED))
    env.fakes.events.statements[EVENT_SID.format(TRUSTED)] = TRUSTED
    status, payload, _ = put(env, admin(env), usecase_id, move_body(SECOND))
    assert status == 200, payload
    assert payload["grants_removed"] == {
        "status": "completed", "account_id": TRUSTED,
        "component_bucket_policy": {"status": "success", "bucket": COMPONENT_BUCKET},
        "eventbridge": {"status": "success", "statement_id": EVENT_SID.format(TRUSTED)}}
    assert payload["grants_added"]["component_bucket_policy"]["status"] == "success"
    assert payload["grants_added"]["eventbridge"]["status"] == "success"
    statements = {s["Sid"]: s for s in env.fakes.s3.statements()}
    assert set(statements) == {READ_SID.format(SECOND), LIST_SID.format(SECOND),
                               "AllowGreengrassServiceAccess"}
    assert statements["AllowGreengrassServiceAccess"]["Condition"] == {
        "StringEquals": {"aws:SourceAccount": [PORTAL]}}
    assert set(env.fakes.events.statements) == {EVENT_SID.format(SECOND)}
    ops = env.fakes.log.ops()
    assert ops.index(("events", "remove_permission")) < ops.index(("events", "put_permission"))
    assert ops.index(("dynamodb", "update_item")) < ops.index(("events", "remove_permission"))


def test_move_keeps_the_old_grants_while_another_use_case_references_the_account(env):
    env.mp.setenv("TRUSTED_USECASE_ACCOUNT_IDS", f"{TRUSTED},{SECOND}")
    fx.put_multi_account_record(TRUSTED)
    usecase_id = fx.put_multi_account_record(TRUSTED)
    env.fakes.s3.seed(device_pair(TRUSTED))
    env.fakes.events.statements[EVENT_SID.format(TRUSTED)] = TRUSTED
    status, payload, _ = put(env, admin(env), usecase_id, move_body(SECOND))
    assert status == 200, payload
    assert payload["grants_removed"] == {"status": "skipped", "reason": "account_still_referenced",
                                         "account_id": TRUSTED}
    assert env.fakes.s3.sids() == sorted([READ_SID.format(TRUSTED), LIST_SID.format(TRUSTED),
                                          READ_SID.format(SECOND), LIST_SID.format(SECOND)])
    assert set(env.fakes.events.statements) == {EVENT_SID.format(TRUSTED), EVENT_SID.format(SECOND)}


# ================================================== DELETE /usecases/{id}

def test_delete_removes_the_grants_of_an_account_no_use_case_references(env):
    usecase_id = fx.put_multi_account_record(TRUSTED)
    env.fakes.s3.seed(device_pair(PORTAL) + device_pair(TRUSTED) + shared_components_statements(TRUSTED))
    env.fakes.events.statements[EVENT_SID.format(TRUSTED)] = TRUSTED
    status, payload, _ = delete(env, admin(env), usecase_id)
    assert status == 200, payload
    assert payload["message"] == "Use case deleted successfully"
    assert payload["grants_removed"]["status"] == "completed"
    assert env.fakes.s3.sids() == sorted([READ_SID.format(PORTAL), LIST_SID.format(PORTAL),
                                          "AllowGreengrassServiceAccess"])
    assert env.fakes.events.statements == {}
    assert fx.record(usecase_id) is None


def test_delete_keeps_the_grants_while_another_use_case_references_the_account(env):
    fx.put_multi_account_record(TRUSTED)
    usecase_id = fx.put_multi_account_record(TRUSTED)
    env.fakes.s3.seed(device_pair(TRUSTED))
    status, payload, _ = delete(env, admin(env), usecase_id)
    assert status == 200
    assert payload["grants_removed"] == {"status": "skipped", "reason": "account_still_referenced",
                                         "account_id": TRUSTED, "device_access": DEVICE_ACCESS_UNCHANGED}
    assert env.fakes.s3.sids() == sorted([READ_SID.format(TRUSTED), LIST_SID.format(TRUSTED)])


@pytest.mark.parametrize("kind,reason", [("single", "single_account"), ("portal", "portal_account")])
def test_delete_never_removes_the_portal_accounts_grants(env, kind, reason):
    usecase_id = (fx.put_single_account_record() if kind == "single"
                  else fx.put_multi_account_record(PORTAL))
    env.fakes.s3.seed(device_pair(PORTAL))
    env.fakes.events.statements[EVENT_SID.format(PORTAL)] = PORTAL
    status, payload, _ = delete(env, admin(env), usecase_id)
    assert status == 200
    assert payload["grants_removed"] == {"status": "skipped", "reason": reason, "account_id": PORTAL,
                                         "device_access": DEVICE_ACCESS_UNCHANGED}
    assert env.fakes.s3.calls == [] and env.fakes.events.calls == []


def test_removal_that_would_leave_no_statement_writes_nothing(env):
    usecase_id = fx.put_multi_account_record(TRUSTED)
    env.fakes.s3.seed(device_pair(TRUSTED))
    status, payload, _ = delete(env, admin(env), usecase_id)
    assert status == 200
    assert payload["grants_removed"]["component_bucket_policy"] == {
        "status": "failed", "bucket": COMPONENT_BUCKET,
        "error": "removing the account would leave the component bucket policy empty"}
    assert ("put_bucket_policy", COMPONENT_BUCKET) not in env.fakes.s3.calls
    assert env.fakes.s3.sids() == sorted([READ_SID.format(TRUSTED), LIST_SID.format(TRUSTED)])


def test_delete_of_an_unknown_use_case_still_answers_200(env):
    status, payload, _ = delete(env, admin(env), "uc-does-not-exist")
    assert status == 200
    assert payload["grants_removed"] == {"status": "skipped", "reason": "usecase_not_found"}


# ============================== shared-components provisioning on create

def test_provisioning_invoke_is_issued_after_the_component_bucket_write(env):
    caller = admin(env)
    status, payload, _ = create(env, caller, fx.multi_account_body(provision_shared_components=True))
    assert status == 201, payload
    ops = env.fakes.log.ops()
    assert ops.index(("s3", "put_bucket_policy")) < ops.index(("lambda", "invoke"))
    [invocation] = env.fakes.lam.invocations
    assert (invocation["FunctionName"], invocation["InvocationType"]) == (
        fx.SHARED_COMPONENTS_FUNCTION, "Event")
    event = json.loads(invocation["Payload"])
    assert (event["httpMethod"], event["path"]) == ("POST", "/api/v1/shared-components/provision")
    assert json.loads(event["body"]) == {"usecase_id": payload["usecase"]["usecase_id"],
                                         "account_id": TRUSTED}
    assert event["dda_internal"] == {"source": "usecases", "purpose": "provision_on_create",
                                     "user_id": caller["user_id"]}
    assert event["requestContext"] == {"authorizer": {"claims": {"sub": caller["user_id"]}}}
    assert payload["shared_components"] == {"status": "accepted"}
    assert payload["usecase"]["shared_components"] == {"status": "provisioning",
                                                       "message": "Provisioning in progress"}


@pytest.mark.parametrize("failure", ["invoke-error", "not-configured"])
def test_failed_provisioning_invoke_still_creates_the_use_case(env, failure):
    if failure == "invoke-error":
        env.fakes.lam.error = client_error("AccessDeniedException", "not authorized", 403, "Invoke")
    else:
        env.mp.delenv("SHARED_COMPONENTS_FUNCTION_NAME")
    status, payload, _ = create(env, admin(env), fx.multi_account_body(provision_shared_components=True))
    assert status == 201, payload
    assert payload["shared_components"]["status"] == "failed"
    assert payload["shared_components"]["error"]
    assert fx.record(payload["usecase"]["usecase_id"]) is not None


# ======================================= component bucket policy writer

def test_component_bucket_grants_are_idempotent(env):
    first = env.uc.update_component_bucket_policy_for_greengrass(COMPONENT_BUCKET, TRUSTED)
    second = env.uc.update_component_bucket_policy_for_greengrass(COMPONENT_BUCKET, TRUSTED)
    assert (first["status"], second["status"]) == ("success", "already_configured")
    assert env.fakes.s3.sids() == sorted([READ_SID.format(TRUSTED), LIST_SID.format(TRUSTED)])


def test_component_bucket_policy_over_the_size_bound_is_not_written(env):
    limit = env.shared.COMPONENT_BUCKET_POLICY_MAX_BYTES
    padding = {"Sid": "Padding", "Effect": "Allow", "Principal": {"AWS": f"arn:aws:iam::{PORTAL}:root"},
               "Action": "s3:GetObject", "Resource": "arn:aws:s3:::padding/" + "x" * (limit - 300)}
    env.fakes.s3.seed([padding])
    result = env.uc.update_component_bucket_policy_for_greengrass(COMPONENT_BUCKET, TRUSTED)
    assert result == {"status": "failed", "error": "component bucket policy would exceed the size limit",
                      "bucket": COMPONENT_BUCKET}
    assert ("put_bucket_policy", COMPONENT_BUCKET) not in env.fakes.s3.calls

    padding["Resource"] = "arn:aws:s3:::padding/" + "x" * (limit - 1200)
    env.fakes.s3.seed([padding])
    result = env.uc.update_component_bucket_policy_for_greengrass(COMPONENT_BUCKET, TRUSTED)
    assert result["status"] == "success"
    assert len(env.fakes.s3.policies[COMPONENT_BUCKET].encode()) <= limit
