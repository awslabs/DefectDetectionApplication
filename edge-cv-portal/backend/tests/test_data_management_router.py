"""
Router test for the DataManagement handler: the dead `/transform-manifest`
branch is gone (portal-authorization-and-iam-scoping, tasks.md 4.1; design I4).

The branch called a function the module never defined, so a request to that
path raised NameError and answered the generic 500; no API Gateway method
exists for it. With the branch deleted the request falls through to the
router's existing 404. Driven through `data_management.handler` with a
registry user on the moto stack.

Validates: Requirements 6.3
"""
import json
import sys
import uuid

import pytest


@pytest.fixture(scope="module")
def dm(aws_stack):
    sys.modules.pop("data_management", None)
    import data_management
    return data_management


@pytest.fixture
def caller(aws_stack):
    usecase_id = f"uc-{uuid.uuid4().hex[:12]}"
    aws_stack.tables.usecases.put_item(Item={
        "usecase_id": usecase_id, "name": "Router test", "account_id": "111111111111",
        "cross_account_role_arn": "arn:aws:iam::111111111111:root", "s3_bucket": "router-bucket"})
    user_id = f"user-{uuid.uuid4().hex[:12]}"
    aws_stack.tables.user_roles.put_item(Item={
        "user_id": user_id, "usecase_id": usecase_id, "role": "UseCaseAdmin",
        "username": user_id, "email": f"{user_id}@example.com", "status": "enabled",
        "assigned_by": "test", "assigned_at": 1})
    return usecase_id, {"user_id": user_id, "email": f"{user_id}@example.com",
                        "username": user_id, "role": "UseCaseAdmin"}


def _event(method, path, usecase_id, user, body=None):
    return {
        "httpMethod": method, "path": path, "resource": path,
        "pathParameters": {"id": usecase_id},
        "queryStringParameters": None,
        "body": json.dumps(body) if body is not None else None,
        "requestContext": {"authorizer": {"claims": {
            "sub": user["user_id"], "email": user["email"],
            "cognito:username": user["username"], "custom:role": user["role"]}}},
    }


@pytest.mark.parametrize("prefix", ["", "/api/v1"])
@pytest.mark.parametrize("method", ["POST", "GET"])
def test_transform_manifest_path_answers_404(dm, caller, prefix, method):
    usecase_id, user = caller
    body = {"manifestPath": "s3://router-bucket/in.manifest",
            "outputPath": "s3://router-bucket/out.manifest"}
    response = dm.handler(_event(method, f"{prefix}/usecases/{usecase_id}/data/transform-manifest",
                                 usecase_id, user, body), None)
    assert response["statusCode"] == 404
    assert json.loads(response["body"]) == {"error": "Not found"}


def test_router_no_longer_names_the_missing_function(dm):
    assert not hasattr(dm, "transform_manifest")
    import inspect
    assert "transform-manifest" not in inspect.getsource(dm.handler)
