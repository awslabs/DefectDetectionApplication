"""
POST /shared-components/provision authorization (portal-authorization-and-
iam-scoping, design I3 "Test strategy"; task 3.2).

The real shared_components handler on the moto use-case and user-roles
tables, with the portal-account S3 client (the component bucket policy), the
cross-account provisioning step and the background thread replaced by
recorders (the thread runs inline). The internal event is the one
usecases.provision_shared_components_internal sends, captured from a fake
Lambda client. Cases whose decision depends on role resolution run with
PORTAL_REGISTRY_ENFORCED off and on.

Validates: Requirements 3.3, 3.5, 5.1, 5.3
"""
import json
import sys
import threading
import uuid
from types import SimpleNamespace

import pytest

import usecase_account_fakes as fx
from usecase_account_fakes import COMPONENT_BUCKET, PORTAL, TRUSTED, access_role, client_error

PROVISION_PATH = "/api/v1/shared-components/provision"


class InlineThread:
    """threading.Thread stand-in that runs the target when started."""

    def __init__(self, target=None, daemon=None, **kwargs):
        self.target = target

    def start(self):
        self.target()


@pytest.fixture(scope="module")
def modules(aws_stack):
    import shared_utils
    mp = pytest.MonkeyPatch()
    mp.setenv("PORTAL_ACCOUNT_ID", PORTAL)
    mp.setenv("AWS_REGION", "us-east-1")
    mp.delenv("COMPONENT_BUCKET", raising=False)
    for name in ("shared_components", "usecases"):
        sys.modules.pop(name, None)
    import shared_components
    import usecases
    yield SimpleNamespace(shared=shared_utils, sc=shared_components, uc=usecases)
    mp.undo()


@pytest.fixture
def env(aws_stack, modules, monkeypatch):
    log = fx.OperationLog()
    s3 = fx.FakeComponentS3(log)
    audit = fx.AuditRecorder()
    provisioned = []
    monkeypatch.setattr(modules.sc, "s3", s3)
    monkeypatch.setattr(modules.sc, "log_audit_event", audit)
    monkeypatch.setattr(modules.sc, "provision_shared_components_for_usecase",
                        lambda **kwargs: provisioned.append(kwargs) or [])
    monkeypatch.setattr(threading, "Thread", InlineThread)
    monkeypatch.setenv("AWS_ACCOUNT_ID", PORTAL)
    monkeypatch.delenv(modules.shared.PORTAL_REGISTRY_ENFORCED_ENV, raising=False)
    usecase_id = f"uc-provision-{uuid.uuid4().hex[:10]}"
    aws_stack.tables.usecases.put_item(Item={
        "usecase_id": usecase_id, "name": "Provision", "account_id": TRUSTED,
        "cross_account_role_arn": access_role(TRUSTED), "external_id": "ext-1",
        "region": "us-east-1", "s3_bucket": "provision-bucket"})
    return SimpleNamespace(stack=aws_stack, sc=modules.sc, uc=modules.uc, shared=modules.shared,
                           s3=s3, audit=audit, provisioned=provisioned, usecase_id=usecase_id,
                           mp=monkeypatch)


@pytest.fixture(params=["enforcement-off", "enforcement-on"])
def enforcement(request, env):
    if request.param == "enforcement-on":
        env.mp.setenv(env.shared.PORTAL_REGISTRY_ENFORCED_ENV, "true")
    return request.param


def caller(env, role):
    """A caller with `role` on the use case (PortalAdmin as a global row)."""
    if role == "PortalAdmin":
        return fx.portal_admin(env.stack)
    if role is None:
        return fx.user(env.stack, "Viewer")
    sub = fx.user(env.stack, role, "global", "Viewer")
    env.stack.tables.user_roles.put_item(Item={
        "user_id": sub["user_id"], "usecase_id": env.usecase_id, "role": role, "status": "enabled"})
    return sub


def gateway(env, user, body=None, **extra):
    event = fx.api_event("POST", PROVISION_PATH, user,
                         body=body if body is not None else {"usecase_id": env.usecase_id})
    event.update(extra)
    return event


def handle(env, event):
    response = env.sc.handler(event, None)
    return response["statusCode"], json.loads(response["body"])


def internal_event(env, creator_id):
    """What usecases.provision_shared_components_internal invokes."""
    lam = fx.FakeLambda(fx.OperationLog())
    env.mp.setenv("SHARED_COMPONENTS_FUNCTION_NAME", fx.SHARED_COMPONENTS_FUNCTION)
    env.mp.setattr(env.uc, "boto3", SimpleNamespace(client=lambda name, **kwargs: lam))
    result = env.uc.provision_shared_components_internal(
        usecase_id=env.usecase_id, account_id=TRUSTED, cross_account_role_arn=access_role(TRUSTED),
        external_id="ext-1", user_id=creator_id)
    assert result == {"status": "accepted"}
    [invocation] = lam.invocations
    return json.loads(invocation["Payload"])


@pytest.mark.parametrize("role", [None, "DataScientist", "UseCaseAdmin"])
def test_caller_without_portal_admin_is_refused_and_writes_no_policy(env, enforcement, role):
    user = caller(env, role)
    status, payload = handle(env, gateway(env, user))
    assert status == 403, payload
    assert payload == {"error": "Insufficient permissions", "error_code": "INSUFFICIENT_PERMISSIONS"}
    assert env.s3.calls == [] and env.provisioned == []
    [row] = env.audit.rows
    assert (row["user_id"], row["action"], row["result"]) == (user["user_id"], "unauthorized_access",
                                                             "denied")
    assert row["details"] == {"required_role": "PortalAdmin", "usecase_id": env.usecase_id}


def test_body_user_id_no_longer_skips_the_check(env, enforcement):
    portal_admin = fx.portal_admin(env.stack)
    user = caller(env, "DataScientist")
    status, _ = handle(env, gateway(env, user, {"usecase_id": env.usecase_id,
                                                "user_id": portal_admin["user_id"]}))
    assert status == 403
    assert env.s3.calls == [] and env.provisioned == []


def test_portal_admin_is_accepted_and_attributed_to_themselves(env, enforcement):
    user = caller(env, "PortalAdmin")
    status, payload = handle(env, gateway(env, user))
    assert status == 202, payload
    assert payload["status"] == "accepted" and payload["bucket_policy_updated"] is True
    principals = {s["Sid"]: s for s in env.s3.statements()}["AllowUseCaseAccountsGreengrassAccess"]
    assert access_role(TRUSTED) in principals["Principal"]["AWS"]
    assert [call["user_id"] for call in env.provisioned] == [user["user_id"]]
    assert [row["user_id"] for row in env.audit.rows
            if row["action"] == "provision_shared_components"] == [user["user_id"]]


def test_creation_invoke_is_accepted_and_attributed_to_the_creator(env, enforcement):
    creator = fx.user(env.stack, "DataScientist")        # holds no role on this use case
    event = internal_event(env, creator["user_id"])
    assert event["dda_internal"] == {"source": "usecases", "purpose": "provision_on_create",
                                     "user_id": creator["user_id"]}
    status, payload = handle(env, event)
    assert status == 202, payload
    assert [call["user_id"] for call in env.provisioned] == [creator["user_id"]]
    assert [row["user_id"] for row in env.audit.rows
            if row["action"] == "provision_shared_components"] == [creator["user_id"]]
    assert not [row for row in env.audit.rows if row["action"] == "unauthorized_access"]


def test_marker_on_a_gateway_event_is_a_user_call(env):
    user = caller(env, "DataScientist")
    marker = {"dda_internal": {"source": "usecases", "purpose": "provision_on_create",
                               "user_id": user["user_id"]}}
    status, _ = handle(env, gateway(env, user, **marker))
    assert status == 403 and env.s3.calls == []


def test_unknown_use_case_is_404(env):
    status, payload = handle(env, gateway(env, fx.portal_admin(env.stack), {"usecase_id": "uc-missing"}))
    assert (status, payload) == (404, {"error": "Usecase not found"})


def test_component_bucket_is_the_shared_layer_name(env):
    assert env.sc.COMPONENT_BUCKET == COMPONENT_BUCKET == env.shared.component_bucket_name()


def test_policy_over_the_size_bound_is_not_written(env):
    limit = env.shared.COMPONENT_BUCKET_POLICY_MAX_BYTES
    env.s3.seed([{"Sid": "Padding", "Effect": "Allow",
                  "Principal": {"AWS": f"arn:aws:iam::{PORTAL}:root"}, "Action": "s3:GetObject",
                  "Resource": "arn:aws:s3:::padding/" + "x" * (limit - 300)}])
    assert env.sc.add_usecase_to_component_bucket_policy(access_role(TRUSTED)) is False
    assert ("put_bucket_policy", COMPONENT_BUCKET) not in env.s3.calls


def test_principals_are_added_once(env):
    assert env.sc.add_usecase_to_component_bucket_policy(access_role(TRUSTED)) is True
    assert env.sc.add_usecase_to_component_bucket_policy(access_role(TRUSTED)) is True
    statements = {s["Sid"]: s for s in env.s3.statements()}
    roles = statements["AllowUseCaseAccountsGreengrassAccess"]["Principal"]["AWS"]
    assert sorted(roles) == sorted([access_role(TRUSTED),
                                    f"arn:aws:iam::{TRUSTED}:role/GreengrassV2TokenExchangeRole"])
    assert statements["AllowGreengrassServiceAccess"]["Condition"] == {
        "StringEquals": {"aws:SourceAccount": [PORTAL, TRUSTED]}}


def test_put_failure_is_reported_not_raised(env):
    env.s3.put_error = client_error("AccessDenied", "denied", 403, "PutBucketPolicy")
    assert env.sc.add_usecase_to_component_bucket_policy(access_role(TRUSTED)) is False
