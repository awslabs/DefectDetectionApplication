"""
Shared pytest fixtures for portal backend integration tests.

Provides a moto-backed AWS environment (DynamoDB tables, GSIs, and the
portal artifacts S3 bucket) plus the real `shared_utils` and
`workflow_core` Lambda layers on sys.path, so handler modules such as
functions/workflows.py can be imported and invoked with synthetic API
Gateway events, exercising the real RBAC / audit / persistence code
paths against local (in-memory) AWS.

Notes on module isolation: some standalone tests in this directory
(e.g. test_captures.py) install a *fake* `shared_utils` into
sys.modules at collection time. The session fixture below therefore
pops any previously imported `shared_utils` / `workflows` modules and
re-imports the real ones from the layer paths while the moto mock is
active, without disturbing modules (like `captures`) that already
bound their imports.
"""
import json
import os
import sys
import uuid
from types import SimpleNamespace

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
_FUNCTIONS_DIR = os.path.join(_BACKEND, "functions")
_SHARED_LAYER = os.path.join(_BACKEND, "layers", "shared", "python")
_WORKFLOW_CORE_LAYER = os.path.join(_BACKEND, "layers", "workflow_core", "python")

REGION = "us-east-1"

# Table / bucket names used by the moto-backed test stack.
TEST_ENV = {
    # Fake credentials so boto3 can never reach real AWS.
    "AWS_ACCESS_KEY_ID": "testing",
    "AWS_SECRET_ACCESS_KEY": "testing",
    "AWS_SECURITY_TOKEN": "testing",
    "AWS_SESSION_TOKEN": "testing",
    "AWS_DEFAULT_REGION": REGION,
    "AWS_REGION": REGION,
    # shared_utils tables
    "USECASES_TABLE": "test-usecases",
    "USER_ROLES_TABLE": "test-user-roles",
    "AUDIT_LOG_TABLE": "test-audit-log",
    "DEPLOYMENTS_TABLE": "test-deployments",
    # workflows.py tables / bucket
    "WORKFLOWS_TABLE": "test-workflows",
    "WORKFLOW_VERSIONS_TABLE": "test-workflow-versions",
    "PORTAL_ARTIFACTS_BUCKET": "test-portal-artifacts",
    "WORKFLOWS_S3_PREFIX": "workflows",
}

# Environment must be in place before shared_utils / workflows are
# imported (both read table names at module import time).
os.environ.update(TEST_ENV)

for _path in (_SHARED_LAYER, _FUNCTIONS_DIR):
    if _path not in sys.path:
        sys.path.insert(0, _path)

# The workflow_core layer directory also carries the layer's vendored
# Lambda-runtime dependencies (CPython 3.11 manylinux wheels, e.g.
# jsonschema's rpds). Appended rather than prepended so they never
# shadow the host interpreter's own packages during local test runs.
if _WORKFLOW_CORE_LAYER not in sys.path:
    sys.path.append(_WORKFLOW_CORE_LAYER)


def _create_tables(dynamodb):
    """Create the DynamoDB tables (and GSIs) the workflow stack uses."""
    dynamodb.create_table(
        TableName=TEST_ENV["WORKFLOWS_TABLE"],
        KeySchema=[{"AttributeName": "workflow_id", "KeyType": "HASH"}],
        AttributeDefinitions=[
            {"AttributeName": "workflow_id", "AttributeType": "S"},
            {"AttributeName": "usecase_id", "AttributeType": "S"},
            {"AttributeName": "created_at", "AttributeType": "N"},
        ],
        GlobalSecondaryIndexes=[{
            "IndexName": "usecase-workflows-index",
            "KeySchema": [
                {"AttributeName": "usecase_id", "KeyType": "HASH"},
                {"AttributeName": "created_at", "KeyType": "RANGE"},
            ],
            "Projection": {"ProjectionType": "ALL"},
        }],
        BillingMode="PAY_PER_REQUEST",
    )
    dynamodb.create_table(
        TableName=TEST_ENV["WORKFLOW_VERSIONS_TABLE"],
        KeySchema=[
            {"AttributeName": "workflow_id", "KeyType": "HASH"},
            {"AttributeName": "version", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "workflow_id", "AttributeType": "S"},
            {"AttributeName": "version", "AttributeType": "N"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    dynamodb.create_table(
        TableName=TEST_ENV["DEPLOYMENTS_TABLE"],
        KeySchema=[{"AttributeName": "deployment_id", "KeyType": "HASH"}],
        AttributeDefinitions=[
            {"AttributeName": "deployment_id", "AttributeType": "S"},
            {"AttributeName": "usecase_id", "AttributeType": "S"},
            {"AttributeName": "created_at", "AttributeType": "N"},
        ],
        GlobalSecondaryIndexes=[{
            "IndexName": "usecase-deployments-index",
            "KeySchema": [
                {"AttributeName": "usecase_id", "KeyType": "HASH"},
                {"AttributeName": "created_at", "KeyType": "RANGE"},
            ],
            "Projection": {"ProjectionType": "ALL"},
        }],
        BillingMode="PAY_PER_REQUEST",
    )
    dynamodb.create_table(
        TableName=TEST_ENV["USECASES_TABLE"],
        KeySchema=[{"AttributeName": "usecase_id", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "usecase_id", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    dynamodb.create_table(
        TableName=TEST_ENV["USER_ROLES_TABLE"],
        KeySchema=[
            {"AttributeName": "user_id", "KeyType": "HASH"},
            {"AttributeName": "usecase_id", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "user_id", "AttributeType": "S"},
            {"AttributeName": "usecase_id", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    dynamodb.create_table(
        TableName=TEST_ENV["AUDIT_LOG_TABLE"],
        KeySchema=[
            {"AttributeName": "event_id", "KeyType": "HASH"},
            {"AttributeName": "timestamp", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "event_id", "AttributeType": "S"},
            {"AttributeName": "timestamp", "AttributeType": "N"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )


@pytest.fixture(scope="session")
def aws_stack():
    """moto-backed AWS with the workflow tables, bucket, and real modules."""
    from moto import mock_aws

    with mock_aws():
        import boto3

        dynamodb = boto3.client("dynamodb", region_name=REGION)
        s3 = boto3.client("s3", region_name=REGION)
        _create_tables(dynamodb)
        s3.create_bucket(Bucket=TEST_ENV["PORTAL_ARTIFACTS_BUCKET"])

        # Import the real layer + handler modules inside the mock so
        # their module-level boto3 clients are intercepted by moto.
        # Pop any fake shared_utils installed by standalone tests.
        for module_name in ("workflows", "shared_utils"):
            sys.modules.pop(module_name, None)
        import workflows  # noqa: F401

        resource = boto3.resource("dynamodb", region_name=REGION)
        yield SimpleNamespace(
            workflows=workflows,
            s3=s3,
            tables=SimpleNamespace(
                workflows=resource.Table(TEST_ENV["WORKFLOWS_TABLE"]),
                versions=resource.Table(TEST_ENV["WORKFLOW_VERSIONS_TABLE"]),
                deployments=resource.Table(TEST_ENV["DEPLOYMENTS_TABLE"]),
                usecases=resource.Table(TEST_ENV["USECASES_TABLE"]),
                user_roles=resource.Table(TEST_ENV["USER_ROLES_TABLE"]),
            ),
        )


class WorkflowStoreEnv:
    """Helper facade for invoking the Workflow_Store API in tests.

    Each test gets fresh uuid-based use case and user ids, so tests are
    isolated from one another without table truncation.
    """

    def __init__(self, stack):
        self.stack = stack
        self.workflows = stack.workflows
        self.s3 = stack.s3
        self.bucket = TEST_ENV["PORTAL_ARTIFACTS_BUCKET"]

    # ------------------------------------------------------------- setup
    def create_usecase(self, name="Test Use Case"):
        usecase_id = f"uc-{uuid.uuid4()}"
        self.stack.tables.usecases.put_item(Item={
            "usecase_id": usecase_id,
            "name": name,
            "account_id": "123456789012",
        })
        return usecase_id

    def make_user(self, role="DataScientist"):
        user_id = f"user-{uuid.uuid4()}"
        return {
            "user_id": user_id,
            "email": f"{user_id}@example.com",
            "username": user_id,
            "role": role,
        }

    def assign_role(self, user, usecase_id, role):
        self.stack.tables.user_roles.put_item(Item={
            "user_id": user["user_id"],
            "usecase_id": usecase_id,
            "role": role,
        })

    def put_deployment(self, usecase_id, status="IN_PROGRESS", **attrs):
        deployment_id = f"dep-{uuid.uuid4()}"
        item = {
            "deployment_id": deployment_id,
            "usecase_id": usecase_id,
            "created_at": 1,
            "deployment_status": status,
        }
        item.update(attrs)
        self.stack.tables.deployments.put_item(Item=item)
        return deployment_id

    # ----------------------------------------------------------- invoke
    def event(self, method, resource, user, workflow_id=None, body=None, query=None):
        return {
            "httpMethod": method,
            "resource": resource,
            "path": resource.replace("{id}", workflow_id or ""),
            "pathParameters": {"id": workflow_id} if workflow_id else None,
            "queryStringParameters": query,
            "body": json.dumps(body) if body is not None else None,
            "requestContext": {
                "authorizer": {
                    "claims": {
                        "sub": user["user_id"],
                        "email": user["email"],
                        "cognito:username": user["username"],
                        "custom:role": user["role"],
                    }
                }
            },
        }

    def invoke(self, method, resource, user, workflow_id=None, body=None, query=None):
        """Invoke the handler; returns (status_code, parsed_body)."""
        response = self.workflows.handler(
            self.event(method, resource, user, workflow_id, body, query), None
        )
        return response["statusCode"], json.loads(response["body"])


@pytest.fixture
def env(aws_stack):
    return WorkflowStoreEnv(aws_stack)
