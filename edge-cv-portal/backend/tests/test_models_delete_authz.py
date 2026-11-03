"""
Authorization of the model delete route (functions/models.py `delete_model`,
DELETE /models/{id}).

The delete handler runs the same shared role check as its sibling
`update_model_stage`: `check_user_access(user_id, usecase_id, 'DataScientist')`,
the role floor whose holders carry `Permission.DELETE_MODELS`. The admitted
roles are therefore exactly the `DELETE_MODELS` holders of
`rbac_manager.role_permissions` (DataScientist, UseCaseAdmin, PortalAdmin);
Viewer, Operator and DataLabeler members, and a non-member, are denied 403
before the record is touched.

Runs against the moto-backed stack from conftest.py with the real
shared_utils role resolution (no RBACManager patching) under
PORTAL_REGISTRY_ENFORCED=true, the way production runs it. The training-jobs
table the handler reads is created here with the production key shape.
"""
import json
import os
import sys
import uuid
from types import SimpleNamespace

import pytest

from conftest import REGION

TRAINING_JOBS_TABLE_NAME = "test-training-jobs-model-delete"


@pytest.fixture(scope="module")
def models_env(aws_stack):
    """Training-jobs table + the real models module imported inside the mock."""
    import boto3

    os.environ["TRAINING_JOBS_TABLE"] = TRAINING_JOBS_TABLE_NAME
    client = boto3.client("dynamodb", region_name=REGION)
    client.create_table(
        TableName=TRAINING_JOBS_TABLE_NAME,
        KeySchema=[{"AttributeName": "training_id", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "training_id", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    # Re-import so the module binds the table name above and the
    # moto-intercepted boto3 clients (conftest pattern).
    sys.modules.pop("models", None)
    import models

    resource = boto3.resource("dynamodb", region_name=REGION)
    yield SimpleNamespace(
        mod=models,
        training_jobs=resource.Table(TRAINING_JOBS_TABLE_NAME),
        user_roles=aws_stack.tables.user_roles,
        audit=aws_stack.tables.audit_log,
    )


@pytest.fixture
def enforcement_on(aws_stack, monkeypatch):
    """Registry enforcement as production runs it (design.md Decision 4)."""
    import shared_utils
    monkeypatch.setenv(shared_utils.PORTAL_REGISTRY_ENFORCED_ENV, "true")


def new_user(role="DataScientist"):
    uid = f"user-{uuid.uuid4()}"
    return {"user_id": uid, "email": f"{uid}@example.com",
            "username": uid, "role": role}


def seed_member(env, user, usecase_id, role):
    """Provision the caller in the Portal_Identity registry (enabled global
    provisioning row + the use-case membership row)."""
    for scope in ("global", usecase_id):
        env.user_roles.put_item(Item={
            "user_id": user["user_id"], "usecase_id": scope,
            "role": role, "status": "enabled"})


def seed_model(env, usecase_id):
    """An undeployed registry model of the use case; returns its id."""
    model_id = f"model-{uuid.uuid4()}"
    env.training_jobs.put_item(Item={
        "training_id": model_id, "usecase_id": usecase_id,
        "model_name": "fake-model", "status": "Completed", "created_at": 1})
    return model_id


def delete_event(user, model_id):
    return {
        "httpMethod": "DELETE", "resource": "/models/{id}",
        "path": f"/models/{model_id}", "pathParameters": {"id": model_id},
        "queryStringParameters": None, "body": None,
        "requestContext": {
            "authorizer": {"claims": {
                "sub": user["user_id"], "email": user["email"],
                "cognito:username": user["username"],
                "custom:role": user["role"]}},
            "identity": {"sourceIp": "203.0.113.6", "userAgent": "authz-test/1.0"},
        },
    }


def delete_audit_rows(env, user_id):
    return [item for item in env.audit.scan().get("Items", [])
            if item.get("user_id") == user_id and item.get("action") == "delete_model"]


def stored(env, model_id):
    return env.training_jobs.get_item(Key={"training_id": model_id}).get("Item")


def delete_models_holders():
    """The roles whose permission set carries DELETE_MODELS."""
    import shared_utils
    return {role for role, permissions in shared_utils.rbac_manager.role_permissions.items()
            if shared_utils.Permission.DELETE_MODELS in permissions}


# ------------------------------------------------------ role floor, exact

def test_delete_models_holders_are_the_data_scientist_floor():
    """The shared role check admits DataScientist and above; those are the
    DELETE_MODELS holders, so the handler's floor and the permission agree."""
    import shared_utils
    holders = {role.value for role in delete_models_holders()}
    assert holders == {"DataScientist", "UseCaseAdmin", "PortalAdmin"}
    assert shared_utils.Role.VIEWER not in delete_models_holders()
    assert shared_utils.Role.OPERATOR not in delete_models_holders()
    assert shared_utils.Role.DATA_LABELER not in delete_models_holders()


@pytest.mark.parametrize("role", ["Viewer", "Operator", "DataScientist",
                                  "UseCaseAdmin", "PortalAdmin", "DataLabeler"])
def test_delete_model_admits_exactly_the_delete_models_holders(models_env, enforcement_on,
                                                               role):
    import shared_utils
    usecase_id = f"uc-{uuid.uuid4()}"
    user = new_user(role)
    seed_member(models_env, user, usecase_id, role)
    model_id = seed_model(models_env, usecase_id)

    response = models_env.mod.delete_model(delete_event(user, model_id), None)

    if shared_utils.Role(role) in delete_models_holders():
        assert response["statusCode"] == 200, response["body"]
        assert json.loads(response["body"]) == {
            "model_id": model_id, "message": "Model deleted successfully"}
        assert stored(models_env, model_id) is None
        [row] = delete_audit_rows(models_env, user["user_id"])
        assert row["result"] == "success"
        assert row["resource_id"] == model_id
        assert row["details"]["usecase_id"] == usecase_id
    else:
        assert response["statusCode"] == 403
        assert json.loads(response["body"]) == {"error": "Insufficient permissions"}
        assert stored(models_env, model_id) is not None
        assert delete_audit_rows(models_env, user["user_id"]) == []


# ------------------------------------------------------- the admitted path

def test_delete_model_data_scientist_member_admitted(models_env, enforcement_on):
    usecase_id = f"uc-{uuid.uuid4()}"
    user = new_user("DataScientist")
    seed_member(models_env, user, usecase_id, "DataScientist")
    model_id = seed_model(models_env, usecase_id)

    response = models_env.mod.delete_model(delete_event(user, model_id), None)

    assert response["statusCode"] == 200, response["body"]
    assert stored(models_env, model_id) is None
    assert len(delete_audit_rows(models_env, user["user_id"])) == 1


def test_delete_model_usecase_admin_member_admitted(models_env, enforcement_on):
    usecase_id = f"uc-{uuid.uuid4()}"
    user = new_user("UseCaseAdmin")
    seed_member(models_env, user, usecase_id, "UseCaseAdmin")
    model_id = seed_model(models_env, usecase_id)

    response = models_env.mod.delete_model(delete_event(user, model_id), None)

    assert response["statusCode"] == 200, response["body"]
    assert stored(models_env, model_id) is None
    assert len(delete_audit_rows(models_env, user["user_id"])) == 1


# --------------------------------------------------------- the denied path

def test_delete_model_viewer_member_denied(models_env, enforcement_on):
    usecase_id = f"uc-{uuid.uuid4()}"
    user = new_user("Viewer")
    seed_member(models_env, user, usecase_id, "Viewer")
    model_id = seed_model(models_env, usecase_id)

    response = models_env.mod.delete_model(delete_event(user, model_id), None)

    assert response["statusCode"] == 403
    assert json.loads(response["body"]) == {"error": "Insufficient permissions"}
    assert stored(models_env, model_id) is not None
    assert delete_audit_rows(models_env, user["user_id"]) == []


def test_delete_model_non_member_denied(models_env, enforcement_on):
    """A caller with no enabled Portal_Identity row for the use case."""
    usecase_id = f"uc-{uuid.uuid4()}"
    user = new_user("UseCaseAdmin")   # the claim grants nothing under enforcement
    model_id = seed_model(models_env, usecase_id)

    response = models_env.mod.delete_model(delete_event(user, model_id), None)

    assert response["statusCode"] == 403
    assert json.loads(response["body"]) == {"error": "Insufficient permissions"}
    assert stored(models_env, model_id) is not None
    assert delete_audit_rows(models_env, user["user_id"]) == []


# ------------------------------------------------------ ordering: 404 first

def test_unknown_model_is_404_before_the_role_check(models_env, enforcement_on):
    """The record lookup precedes the role check: the use case is read from
    the stored item, so a missing model answers 404 for any caller."""
    user = new_user("Viewer")   # no rows; would be denied if the check ran first
    response = models_env.mod.delete_model(
        delete_event(user, f"model-{uuid.uuid4()}"), None)
    assert response["statusCode"] == 404
    assert json.loads(response["body"]) == {"error": "Model not found"}
