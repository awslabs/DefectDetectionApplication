"""
Authorization of the captures read route (functions/captures.py:list_captures).

Data-route authorization consistency with the already-gated sibling routes
(pre_labeled_datasets and the use case's other read routes): GET /captures
resolves the caller and checks use-case membership
(`check_user_access(user_id, usecase_id)`, no required role) BEFORE the
inference-results bucket is resolved, the cross-account role is assumed, or any
presigned URL is generated.

Under PORTAL_REGISTRY_ENFORCED=true a caller with no enabled Portal_Identity row
for the use case resolves to no role and is denied 403 with one
`unauthorized_access` audit row, reaching none of that cross-account path; a
member (seeded registry row) is admitted and the route runs normally.

Runs against the moto-backed stack from conftest.py (real shared_utils role
resolution — no RBACManager patching), with the cross-account S3 plumbing stubbed
so the admitted path is deterministic and the denied path can assert it was
never reached. The captures module is (re)imported inside the moto mock so it
binds the real shared_utils the stack was built with (test_captures.py installs
a fake shared_utils for its own metadata/presign unit tests).
"""
import json
import sys
import uuid
from types import SimpleNamespace

import pytest


@pytest.fixture(scope="module")
def captures_mod(aws_stack):
    """The real captures module imported inside the moto mock."""
    sys.modules.pop("captures", None)
    import captures
    return captures


@pytest.fixture
def enforcement_on(aws_stack, monkeypatch):
    """Registry enforcement as production runs it (design.md Decision 4)."""
    import shared_utils
    monkeypatch.setenv(shared_utils.PORTAL_REGISTRY_ENFORCED_ENV, "true")


class Spy:
    def __init__(self, result=None):
        self.calls = []
        self.result = result

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.result


class EmptyS3:
    """Minimal S3 client stand-in: every prefix lists empty."""

    def get_paginator(self, name):
        assert name == "list_objects_v2"
        return SimpleNamespace(paginate=lambda **kwargs: iter([{"Contents": []}]))

    def generate_presigned_url(self, *args, **kwargs):  # pragma: no cover
        return "https://signed.example/none"


@pytest.fixture
def harness(captures_mod, aws_stack, monkeypatch):
    usecase_id = f"uc-{uuid.uuid4()}"
    # The gate is what's under test, not S3: stub the cross-account plumbing so
    # the admitted path is deterministic and the denied path can prove it was
    # never reached.
    get_usecase = Spy(result={
        "usecase_id": usecase_id, "account_id": "123456789012",
        "cross_account_role_arn": "arn:aws:iam::123456789012:root",
        "external_id": "x"})
    assume_role = Spy(result={"AccessKeyId": "k", "SecretAccessKey": "s",
                              "SessionToken": "t"})
    monkeypatch.setattr(captures_mod, "get_usecase", get_usecase)
    monkeypatch.setattr(captures_mod, "assume_usecase_role", assume_role)
    monkeypatch.setattr(captures_mod, "boto3",
                        SimpleNamespace(client=lambda *a, **k: EmptyS3()))
    return SimpleNamespace(mod=captures_mod, usecase_id=usecase_id,
                           get_usecase=get_usecase, assume_role=assume_role,
                           user_roles=aws_stack.tables.user_roles,
                           audit=aws_stack.tables.audit_log)


def new_user(role="DataScientist"):
    uid = f"user-{uuid.uuid4()}"
    return {"user_id": uid, "email": f"{uid}@example.com",
            "username": uid, "role": role}


def seed_member(harness, user, role="DataScientist"):
    """Provision the caller in the Portal_Identity registry.

    Under enforcement the enabled global row is the provisioning record that
    membership resolves from (shared_utils._lookup_identity requires it); the
    use-case row records membership of this use case specifically.
    """
    for scope in ("global", harness.usecase_id):
        harness.user_roles.put_item(Item={
            "user_id": user["user_id"], "usecase_id": scope,
            "role": role, "status": "enabled"})


def event(user, usecase_id, prefix="dev/"):
    return {
        "httpMethod": "GET", "resource": "/captures", "path": "/captures",
        "pathParameters": None,
        "queryStringParameters": {"usecase_id": usecase_id, "prefix": prefix},
        "body": None,
        "requestContext": {
            "authorizer": {"claims": {
                "sub": user["user_id"], "email": user["email"],
                "cognito:username": user["username"],
                "custom:role": user["role"]}},
            "identity": {"sourceIp": "203.0.113.5", "userAgent": "authz-test/1.0"},
        },
    }


def unauthorized_rows(harness, user_id):
    return [item for item in harness.audit.scan().get("Items", [])
            if item.get("user_id") == user_id
            and item.get("action") == "unauthorized_access"]


def test_non_member_denied_before_cross_account_path(harness, enforcement_on):
    """A caller with no registry row is denied 403, and the inference-results
    bucket / role-assumption / presign path is never reached."""
    user = new_user()  # no registry rows seeded
    response = harness.mod.list_captures(event(user, harness.usecase_id))

    assert response["statusCode"] == 403
    assert json.loads(response["body"]) == {"error": "Insufficient permissions"}
    assert harness.get_usecase.calls == []
    assert harness.assume_role.calls == []

    denials = unauthorized_rows(harness, user["user_id"])
    assert len(denials) == 1
    assert denials[0]["result"] == "denied"
    assert denials[0]["details"]["usecase_id"] == harness.usecase_id


def test_member_is_admitted_and_route_runs(harness, enforcement_on):
    """A member of the use case is admitted and the route runs normally."""
    user = new_user()
    seed_member(harness, user)

    response = harness.mod.list_captures(event(user, harness.usecase_id))

    assert response["statusCode"] == 200, response["body"]
    payload = json.loads(response["body"])
    assert payload["captures"] == []
    assert payload["total_found"] == 0
    assert len(harness.get_usecase.calls) == 1  # reached the use-case lookup
    assert unauthorized_rows(harness, user["user_id"]) == []


def test_missing_params_is_400_before_authorization(harness, enforcement_on):
    """The required-parameter 400 precedes the membership check (a non-member
    with a malformed request still gets 400, and nothing downstream runs)."""
    response = harness.mod.list_captures(
        {"queryStringParameters": {"usecase_id": harness.usecase_id}})

    assert response["statusCode"] == 400
    assert harness.get_usecase.calls == []
