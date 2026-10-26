"""
Authorization of the dataset read routes (functions/datasets.py).

Data-route authorization consistency with the already-gated sibling routes
(pre_labeled_datasets): every dataset read route resolves the caller and checks
use-case membership (`check_user_access(user_id, usecase_id)`, no required role)
BEFORE the data bucket is resolved, the cross-account role is assumed, or any
presigned URL is generated:

- list_datasets    (GET  /datasets)
- count_images     (POST /datasets)
- get_image_preview(GET  /datasets?...preview)

Under PORTAL_REGISTRY_ENFORCED=true a caller with no enabled Portal_Identity row
for the use case is denied 403 with one `unauthorized_access` audit row,
reaching none of that cross-account path; a member (seeded registry row) is
admitted and the route runs normally.

Runs against the moto-backed stack from conftest.py (real shared_utils role
resolution — no RBACManager patching), with the cross-account S3 plumbing stubbed
so the admitted path is deterministic and the denied path can assert it was
never reached.
"""
import json
import sys
import uuid
from types import SimpleNamespace

import pytest


@pytest.fixture(scope="module")
def datasets_mod(aws_stack):
    """The real datasets module imported inside the moto mock."""
    sys.modules.pop("datasets", None)
    import datasets
    return datasets


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
def harness(datasets_mod, aws_stack, monkeypatch):
    usecase_id = f"uc-{uuid.uuid4()}"
    # The gate is what's under test, not S3: stub the cross-account plumbing.
    get_usecase = Spy(result={
        "usecase_id": usecase_id, "account_id": "123456789012",
        "cross_account_role_arn": "arn:aws:iam::123456789012:root",
        "external_id": "x", "s3_bucket": "uc-data-bucket"})
    assume_role = Spy(result={"AccessKeyId": "k", "SecretAccessKey": "s",
                              "SessionToken": "t"})
    monkeypatch.setattr(datasets_mod, "get_usecase", get_usecase)
    monkeypatch.setattr(datasets_mod, "assume_usecase_role", assume_role)
    monkeypatch.setattr(datasets_mod, "boto3",
                        SimpleNamespace(client=lambda *a, **k: EmptyS3()))
    return SimpleNamespace(mod=datasets_mod, usecase_id=usecase_id,
                           get_usecase=get_usecase, assume_role=assume_role,
                           user_roles=aws_stack.tables.user_roles,
                           audit=aws_stack.tables.audit_log)


def new_user(role="DataScientist"):
    uid = f"user-{uuid.uuid4()}"
    return {"user_id": uid, "email": f"{uid}@example.com",
            "username": uid, "role": role}


def seed_member(harness, user, role="DataScientist"):
    """Provision the caller in the Portal_Identity registry (enabled global
    provisioning row + the use-case membership row)."""
    for scope in ("global", harness.usecase_id):
        harness.user_roles.put_item(Item={
            "user_id": user["user_id"], "usecase_id": scope,
            "role": role, "status": "enabled"})


def claims_ctx(user):
    return {
        "authorizer": {"claims": {
            "sub": user["user_id"], "email": user["email"],
            "cognito:username": user["username"],
            "custom:role": user["role"]}},
        "identity": {"sourceIp": "203.0.113.6", "userAgent": "authz-test/1.0"},
    }


def get_event(user, usecase_id, path="/datasets", extra_query=None):
    query = {"usecase_id": usecase_id}
    query.update(extra_query or {})
    return {"httpMethod": "GET", "resource": "/datasets", "path": path,
            "pathParameters": None, "queryStringParameters": query,
            "body": None, "requestContext": claims_ctx(user)}


def post_event(user, body):
    return {"httpMethod": "POST", "resource": "/datasets", "path": "/datasets",
            "pathParameters": None, "queryStringParameters": None,
            "body": json.dumps(body), "requestContext": claims_ctx(user)}


def unauthorized_rows(harness, user_id):
    return [item for item in harness.audit.scan().get("Items", [])
            if item.get("user_id") == user_id
            and item.get("action") == "unauthorized_access"]


def assert_denied(harness, response, user_id, usecase_id):
    assert response["statusCode"] == 403
    assert json.loads(response["body"]) == {"error": "Insufficient permissions"}
    # The data bucket / role-assumption / presign path was never reached.
    assert harness.get_usecase.calls == []
    assert harness.assume_role.calls == []
    denials = unauthorized_rows(harness, user_id)
    assert len(denials) == 1
    assert denials[0]["result"] == "denied"
    assert denials[0]["details"]["usecase_id"] == usecase_id


# ------------------------------------------------------------- list_datasets

def test_list_datasets_non_member_denied(harness, enforcement_on):
    user = new_user()
    response = harness.mod.list_datasets(get_event(user, harness.usecase_id))
    assert_denied(harness, response, user["user_id"], harness.usecase_id)


def test_list_datasets_member_admitted(harness, enforcement_on):
    user = new_user()
    seed_member(harness, user)
    response = harness.mod.list_datasets(get_event(user, harness.usecase_id))
    assert response["statusCode"] == 200, response["body"]
    assert json.loads(response["body"])["datasets"] == []
    assert len(harness.get_usecase.calls) == 1
    assert unauthorized_rows(harness, user["user_id"]) == []


# --------------------------------------------------------------- count_images

def test_count_images_non_member_denied(harness, enforcement_on):
    user = new_user()
    response = harness.mod.count_images(
        post_event(user, {"usecase_id": harness.usecase_id, "prefix": "a/"}))
    assert_denied(harness, response, user["user_id"], harness.usecase_id)


def test_count_images_member_admitted(harness, enforcement_on):
    user = new_user()
    seed_member(harness, user)
    response = harness.mod.count_images(
        post_event(user, {"usecase_id": harness.usecase_id, "prefix": "a/"}))
    assert response["statusCode"] == 200, response["body"]
    assert json.loads(response["body"])["image_count"] == 0
    assert len(harness.get_usecase.calls) == 1
    assert unauthorized_rows(harness, user["user_id"]) == []


# ----------------------------------------------------------- get_image_preview

def test_get_image_preview_non_member_denied(harness, enforcement_on):
    user = new_user()
    response = harness.mod.get_image_preview(
        get_event(user, harness.usecase_id, path="/datasets/preview",
                  extra_query={"prefix": "a/"}))
    assert_denied(harness, response, user["user_id"], harness.usecase_id)


def test_get_image_preview_member_admitted(harness, enforcement_on):
    user = new_user()
    seed_member(harness, user)
    response = harness.mod.get_image_preview(
        get_event(user, harness.usecase_id, path="/datasets/preview",
                  extra_query={"prefix": "a/"}))
    assert response["statusCode"] == 200, response["body"]
    payload = json.loads(response["body"])
    assert payload["images"] == []
    assert payload["total_found"] == 0
    assert len(harness.get_usecase.calls) == 1
    assert unauthorized_rows(harness, user["user_id"]) == []


# ------------------------------------------------------ ordering: 400 vs 403

def test_missing_params_is_400_before_authorization(harness, enforcement_on):
    """The required-parameter 400 precedes the membership check on each route."""
    user = new_user()  # no rows; would be denied if the gate ran first
    list_missing = harness.mod.list_datasets(
        {"queryStringParameters": {}, "requestContext": claims_ctx(user)})
    count_missing = harness.mod.count_images(
        post_event(user, {"usecase_id": harness.usecase_id}))
    preview_missing = harness.mod.get_image_preview(
        get_event(user, harness.usecase_id, path="/datasets/preview"))

    assert list_missing["statusCode"] == 400
    assert count_missing["statusCode"] == 400
    assert preview_missing["statusCode"] == 400
    assert harness.get_usecase.calls == []
