"""Shared moto-backed test environment for the synthetic-defect-data-
generation backend tests (tasks 4.2-4.13).

Builds on the session-scoped ``aws_stack`` fixture from conftest.py (real
shared_utils, usecases / user-roles / audit tables) and adds the feature's
own tables (SyntheticSessions with the usecase-index GSI, PromptTemplates),
the training-jobs table, and the Use_Case data bucket, then imports the
real ``synthetic_data`` handler module inside the moto mock.

Deliberately a separate module (not a conftest.py edit): the parallel
data-labeling branch may touch conftest.py, and this feature's shared-file
edits must stay minimal.
"""
import json
import os
import sys
import uuid

import boto3
from botocore.exceptions import ClientError

REGION = "us-east-1"

SYNTHETIC_ENV = {
    "SYNTHETIC_SESSIONS_TABLE": "test-synthetic-sessions",
    "PROMPT_TEMPLATES_TABLE": "test-prompt-templates",
    # Unique name: test_workflow_validation_model_registry.py creates its
    # own (differently-shaped) "test-training-jobs" table in the same
    # session-scoped moto mock, so sharing that name collides.
    "TRAINING_JOBS_TABLE": "test-synthetic-training-jobs",
    "SYNTHETIC_DATA_FUNCTION_NAME": "test-synthetic-data",
}

DATA_BUCKET = "test-synthetic-data-bucket"


def _create_table_idempotent(dynamodb, **kwargs):
    try:
        dynamodb.create_table(**kwargs)
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ResourceInUseException":
            raise


def install_synthetic_stack(aws_stack):
    """Create the feature tables + data bucket inside the active moto mock
    and (re)import synthetic_data / training so their module-level boto3
    clients are intercepted. Returns the imported synthetic_data module."""
    os.environ.update(SYNTHETIC_ENV)

    dynamodb = boto3.client("dynamodb", region_name=REGION)
    _create_table_idempotent(
        dynamodb,
        TableName=SYNTHETIC_ENV["SYNTHETIC_SESSIONS_TABLE"],
        KeySchema=[
            {"AttributeName": "session_id", "KeyType": "HASH"},
            {"AttributeName": "sk", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "session_id", "AttributeType": "S"},
            {"AttributeName": "sk", "AttributeType": "S"},
            {"AttributeName": "usecase_id", "AttributeType": "S"},
            {"AttributeName": "created_at", "AttributeType": "N"},
        ],
        GlobalSecondaryIndexes=[{
            "IndexName": "usecase-index",
            "KeySchema": [
                {"AttributeName": "usecase_id", "KeyType": "HASH"},
                {"AttributeName": "created_at", "KeyType": "RANGE"},
            ],
            "Projection": {"ProjectionType": "ALL"},
        }],
        BillingMode="PAY_PER_REQUEST",
    )
    _create_table_idempotent(
        dynamodb,
        TableName=SYNTHETIC_ENV["PROMPT_TEMPLATES_TABLE"],
        KeySchema=[
            {"AttributeName": "usecase_id", "KeyType": "HASH"},
            {"AttributeName": "template_key", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "usecase_id", "AttributeType": "S"},
            {"AttributeName": "template_key", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    _create_table_idempotent(
        dynamodb,
        TableName=SYNTHETIC_ENV["TRAINING_JOBS_TABLE"],
        KeySchema=[{"AttributeName": "training_id", "KeyType": "HASH"}],
        AttributeDefinitions=[
            {"AttributeName": "training_id", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )

    s3 = boto3.client("s3", region_name=REGION)
    try:
        s3.create_bucket(Bucket=DATA_BUCKET)
    except ClientError as exc:
        if exc.response["Error"]["Code"] not in ("BucketAlreadyOwnedByYou",
                                                 "BucketAlreadyExists"):
            raise

    # Import the handler modules inside the mock so module-level clients
    # are intercepted by moto.
    for module_name in ("synthetic_data", "synthetic_core", "training"):
        sys.modules.pop(module_name, None)
    import synthetic_data  # noqa: F401
    import training  # noqa: F401
    return synthetic_data


class SyntheticEnv:
    """Facade for invoking the synthetic_data handler in tests.

    Fresh uuid-based Use_Case / user ids per call keep examples isolated
    without table truncation (same pattern as WorkflowStoreEnv)."""

    def __init__(self, aws_stack):
        self.stack = aws_stack
        self.synthetic_data = install_synthetic_stack(aws_stack)
        import training as training_module
        self.training = training_module
        self.s3 = boto3.client("s3", region_name=REGION)
        self.bucket = DATA_BUCKET
        resource = boto3.resource("dynamodb", region_name=REGION)
        self.sessions_table = resource.Table(
            SYNTHETIC_ENV["SYNTHETIC_SESSIONS_TABLE"])
        self.templates_table = resource.Table(
            SYNTHETIC_ENV["PROMPT_TEMPLATES_TABLE"])
        self.training_jobs_table = resource.Table(
            SYNTHETIC_ENV["TRAINING_JOBS_TABLE"])

    # ------------------------------------------------------------- setup
    def create_usecase(self, name="Synthetic Test Use Case"):
        usecase_id = f"uc-{uuid.uuid4()}"
        self.stack.tables.usecases.put_item(Item={
            "usecase_id": usecase_id,
            "name": name,
            "account_id": "123456789012",
            "s3_bucket": self.bucket,
            "cross_account_role_arn":
                "arn:aws:iam::123456789012:role/TestUseCaseRole",
            "external_id": "test-external-id",
            "region": REGION,
        })
        return usecase_id

    def make_user(self, role="Viewer"):
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

    def actor_with_role(self, usecase_id, role_name):
        """A fresh user holding role_name for usecase_id. PortalAdmin
        arrives via the JWT custom:role claim; 'none' means no role at
        all; every other role via a UserRoles assignment."""
        if role_name == "PortalAdmin":
            return self.make_user(role="PortalAdmin")
        user = self.make_user(role="Viewer")
        if role_name != "none":
            self.assign_role(user, usecase_id, role_name)
        return user

    def put_session_meta(self, usecase_id, **overrides):
        """Directly persist a session META item (test setup)."""
        session_id = overrides.pop("session_id", str(uuid.uuid4()))
        meta = {
            "session_id": session_id,
            "sk": "META",
            "usecase_id": usecase_id,
            "status": "draft",
            "generation_model_id": "amazon.nova-canvas-v1:0",
            "object_type": "metal casting",
            "defect_type": "scratch",
            "prompt_template_text":
                "A {object_type} with a {defect_type}",
            "source_class": "defect",
            "source_images": [{"bucket": self.bucket,
                               "key": f"datasets/demo/{session_id}.png"}],
            "generation_params": {"variation_count": 2},
            "generation_pass": 0,
            "target_dataset_prefix": f"datasets/demo-{session_id}/",
            "target_manifest_key":
                f"datasets/demo-{session_id}/manifests/train.manifest",
            "created_by": "seed-user",
            "created_at": 1,
            "updated_at": 1,
        }
        meta.update(overrides)
        self.sessions_table.put_item(Item=meta)
        return session_id

    def put_preview(self, session_id, **overrides):
        """Directly persist a PREVIEW item (test setup)."""
        preview_id = overrides.pop("preview_id", str(uuid.uuid4()))
        item = {
            "session_id": session_id,
            "sk": f"PREVIEW#{preview_id}",
            "preview_id": preview_id,
            "source_image_key": "datasets/demo/src.png",
            "variation_index": 0,
            "generation_pass": 1,
            "staging_key":
                f"synthetic-staging/{session_id}/{preview_id}.png",
            "generation_method": "image_variation",
            "resolved_prompt": "a prompt",
            "seed": 0,
            "status": "completed",
            "approval_state": "pending",
            "created_at": 1,
        }
        item.update(overrides)
        self.sessions_table.put_item(Item=item)
        return preview_id

    # ----------------------------------------------------------- invoke
    def event(self, method, resource, user, session_id=None, body=None,
              query=None):
        return {
            "httpMethod": method,
            "resource": resource,
            "path": "/api/v1" + resource.replace("{id}", session_id or ""),
            "pathParameters": {"id": session_id} if session_id else None,
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

    def invoke(self, method, resource, user, session_id=None, body=None,
               query=None):
        """Invoke the handler; returns (status_code, parsed_body)."""
        response = self.synthetic_data.handler(
            self.event(method, resource, user, session_id, body, query),
            None)
        return response["statusCode"], json.loads(response["body"])

    # ------------------------------------------------------------ audit
    def audit_entries(self, action, user_id=None):
        entries = [e for e in self.stack.tables.audit_log.scan()["Items"]
                   if e["action"] == action]
        if user_id is not None:
            entries = [e for e in entries if e["user_id"] == user_id]
        return entries

    # --------------------------------------------------------- snapshots
    def state_snapshot(self):
        """Deterministic snapshot of the feature's persisted state (for
        no-state-change assertions)."""
        def dump(table):
            items = table.scan()["Items"]
            return sorted(json.dumps(item, sort_keys=True, default=str)
                          for item in items)
        return (dump(self.sessions_table), dump(self.templates_table),
                dump(self.training_jobs_table))
