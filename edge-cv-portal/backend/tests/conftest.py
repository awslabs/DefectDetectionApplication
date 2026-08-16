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
    # devices.py / deployments.py (custom-node-designer task 10.5):
    # portal-recorded device attributes (test_device, target_architecture)
    "DEVICES_TABLE": "test-devices",
    # workflows.py tables / bucket
    "WORKFLOWS_TABLE": "test-workflows",
    "WORKFLOW_VERSIONS_TABLE": "test-workflow-versions",
    "PORTAL_ARTIFACTS_BUCKET": "test-portal-artifacts",
    "WORKFLOWS_S3_PREFIX": "workflows",
    # plugin_records.py (custom-node-designer)
    "PLUGIN_RECORDS_TABLE": "test-plugin-records",
    "PLUGIN_SOURCES_S3_PREFIX": "plugin-sources",
    # plugin_importer.py (custom-node-designer)
    "FETCH_PROJECT_NAME": "dda-plugin-fetch",
    "MODULE_INDEX_CACHE_TABLE": "test-module-index-cache",
    # custom_node_types.py (custom-node-designer)
    "CUSTOM_NODE_TYPES_TABLE": "test-custom-node-types",
    # plugin_simulator.py (custom-node-designer)
    "SIMULATION_RUNS_TABLE": "test-simulation-runs",
    # Distinct from the "test-test-datasets" table test_workflow_testing_
    # errors.py creates for itself (that module re-points the env var and
    # re-imports workflow_testing inside its own fixture).
    "TEST_DATASETS_TABLE": "test-simulator-datasets",
    "PLUGIN_SIMULATIONS_PREFIX": "plugin-simulations",
    # SIMULATOR_STATE_MACHINE_ARN is set inside the aws_stack fixture (the
    # moto state machine only exists once the mock is active).
    # plugin_builds.py (custom-node-designer)
    "PLUGIN_STAGING_PREFIX": "plugin-staging",
    "PLUGIN_LIBRARY_CUSTOM_PREFIX": "workflow-plugins/custom",
    "PLUGIN_COMPONENTS_FUNCTION_NAME": "test-plugin-components",
    "BUILD_PROJECTS_JSON": json.dumps({
        arch: f"dda-plugin-build-{arch}"
        for arch in ("x86_64", "x86_64_nvidia",
                     "arm64_jp4", "arm64_jp5", "arm64_jp6")
    }),
    # PLUGIN_SIGNING_KEY_ARN is set inside the aws_stack fixture (the
    # moto KMS key only exists once the mock is active).
    # dda_labeling.py (dda-data-labeling)
    "LABELING_TEAMS_TABLE": "test-labeling-teams",
    "LABELING_JOBS_TABLE": "test-labeling-jobs",
    "LABELING_TASKS_TABLE": "test-labeling-tasks",
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
        TableName=TEST_ENV["DEVICES_TABLE"],
        KeySchema=[{"AttributeName": "device_id", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "device_id", "AttributeType": "S"}],
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
        TableName=TEST_ENV["PLUGIN_RECORDS_TABLE"],
        KeySchema=[
            {"AttributeName": "plugin_id", "KeyType": "HASH"},
            {"AttributeName": "version", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "plugin_id", "AttributeType": "S"},
            {"AttributeName": "version", "AttributeType": "N"},
            {"AttributeName": "usecase_id", "AttributeType": "S"},
            {"AttributeName": "created_at", "AttributeType": "N"},
        ],
        GlobalSecondaryIndexes=[{
            "IndexName": "usecase-plugins-index",
            "KeySchema": [
                {"AttributeName": "usecase_id", "KeyType": "HASH"},
                {"AttributeName": "created_at", "KeyType": "RANGE"},
            ],
            "Projection": {"ProjectionType": "ALL"},
        }],
        BillingMode="PAY_PER_REQUEST",
    )
    dynamodb.create_table(
        TableName=TEST_ENV["CUSTOM_NODE_TYPES_TABLE"],
        KeySchema=[
            {"AttributeName": "node_type_id", "KeyType": "HASH"},
            {"AttributeName": "version", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "node_type_id", "AttributeType": "S"},
            {"AttributeName": "version", "AttributeType": "N"},
            {"AttributeName": "usecase_id", "AttributeType": "S"},
        ],
        GlobalSecondaryIndexes=[{
            "IndexName": "usecase-node-types-index",
            "KeySchema": [
                {"AttributeName": "usecase_id", "KeyType": "HASH"},
                {"AttributeName": "node_type_id", "KeyType": "RANGE"},
            ],
            "Projection": {"ProjectionType": "ALL"},
        }],
        BillingMode="PAY_PER_REQUEST",
    )
    dynamodb.create_table(
        TableName=TEST_ENV["MODULE_INDEX_CACHE_TABLE"],
        KeySchema=[{"AttributeName": "cache_key", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "cache_key", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    dynamodb.create_table(
        TableName=TEST_ENV["SIMULATION_RUNS_TABLE"],
        KeySchema=[{"AttributeName": "run_id", "KeyType": "HASH"}],
        AttributeDefinitions=[
            {"AttributeName": "run_id", "AttributeType": "S"},
            {"AttributeName": "usecase_id", "AttributeType": "S"},
            {"AttributeName": "started_at", "AttributeType": "N"},
        ],
        GlobalSecondaryIndexes=[{
            "IndexName": "usecase-runs-index",
            "KeySchema": [
                {"AttributeName": "usecase_id", "KeyType": "HASH"},
                {"AttributeName": "started_at", "KeyType": "RANGE"},
            ],
            "Projection": {"ProjectionType": "ALL"},
        }],
        BillingMode="PAY_PER_REQUEST",
    )
    dynamodb.create_table(
        TableName=TEST_ENV["TEST_DATASETS_TABLE"],
        KeySchema=[{"AttributeName": "dataset_id", "KeyType": "HASH"}],
        AttributeDefinitions=[
            {"AttributeName": "dataset_id", "AttributeType": "S"},
            {"AttributeName": "usecase_id", "AttributeType": "S"},
            {"AttributeName": "created_at", "AttributeType": "N"},
        ],
        GlobalSecondaryIndexes=[{
            "IndexName": "usecase-datasets-index",
            "KeySchema": [
                {"AttributeName": "usecase_id", "KeyType": "HASH"},
                {"AttributeName": "created_at", "KeyType": "RANGE"},
            ],
            "Projection": {"ProjectionType": "ALL"},
        }],
        BillingMode="PAY_PER_REQUEST",
    )
    # dda-data-labeling: single-table teams store (META / MEMBER#<user_id>
    # sort keys; usecase-teams-index holds META items only).
    dynamodb.create_table(
        TableName=TEST_ENV["LABELING_TEAMS_TABLE"],
        KeySchema=[
            {"AttributeName": "team_id", "KeyType": "HASH"},
            {"AttributeName": "sk", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "team_id", "AttributeType": "S"},
            {"AttributeName": "sk", "AttributeType": "S"},
            {"AttributeName": "usecase_id", "AttributeType": "S"},
            {"AttributeName": "created_at", "AttributeType": "N"},
        ],
        GlobalSecondaryIndexes=[{
            "IndexName": "usecase-teams-index",
            "KeySchema": [
                {"AttributeName": "usecase_id", "KeyType": "HASH"},
                {"AttributeName": "created_at", "KeyType": "RANGE"},
            ],
            "Projection": {"ProjectionType": "ALL"},
        }],
        BillingMode="PAY_PER_REQUEST",
    )
    # dda-data-labeling: labeling jobs table (existing dda-portal-labeling-jobs
    # shape — PK job_id, usecase-jobs-index for use-case-scoped listings).
    dynamodb.create_table(
        TableName=TEST_ENV["LABELING_JOBS_TABLE"],
        KeySchema=[{"AttributeName": "job_id", "KeyType": "HASH"}],
        AttributeDefinitions=[
            {"AttributeName": "job_id", "AttributeType": "S"},
            {"AttributeName": "usecase_id", "AttributeType": "S"},
            {"AttributeName": "created_at", "AttributeType": "N"},
        ],
        GlobalSecondaryIndexes=[{
            "IndexName": "usecase-jobs-index",
            "KeySchema": [
                {"AttributeName": "usecase_id", "KeyType": "HASH"},
                {"AttributeName": "created_at", "KeyType": "RANGE"},
            ],
            "Projection": {"ProjectionType": "ALL"},
        }],
        BillingMode="PAY_PER_REQUEST",
    )
    # dda-data-labeling: Task_Assignments (PK job_id, SK task_id;
    # assignee-index backs the labeler-facing "my jobs" queries).
    dynamodb.create_table(
        TableName=TEST_ENV["LABELING_TASKS_TABLE"],
        KeySchema=[
            {"AttributeName": "job_id", "KeyType": "HASH"},
            {"AttributeName": "task_id", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "job_id", "AttributeType": "S"},
            {"AttributeName": "task_id", "AttributeType": "S"},
            {"AttributeName": "assignee_user_id", "AttributeType": "S"},
        ],
        GlobalSecondaryIndexes=[{
            "IndexName": "assignee-index",
            "KeySchema": [
                {"AttributeName": "assignee_user_id", "KeyType": "HASH"},
                {"AttributeName": "job_id", "KeyType": "RANGE"},
            ],
            "Projection": {"ProjectionType": "ALL"},
        }],
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

        # plugin_builds.py: the asymmetric portal signing key (ECDSA
        # P-256) and the five per-arch CodeBuild projects.
        kms = boto3.client("kms", region_name=REGION)
        signing_key = kms.create_key(
            KeySpec="ECC_NIST_P256", KeyUsage="SIGN_VERIFY",
            Description="test plugin signing key",
        )
        os.environ["PLUGIN_SIGNING_KEY_ARN"] = signing_key["KeyMetadata"]["Arn"]

        codebuild = boto3.client("codebuild", region_name=REGION)
        # The per-arch build projects plus plugin_importer.py's
        # lightweight repository-fetch project (async import StartBuild).
        project_names = list(json.loads(TEST_ENV["BUILD_PROJECTS_JSON"]).values())
        project_names.append(TEST_ENV["FETCH_PROJECT_NAME"])
        for project_name in project_names:
            codebuild.create_project(
                name=project_name,
                source={"type": "S3",
                        "location": f"{TEST_ENV['PORTAL_ARTIFACTS_BUCKET']}/plugin-sources/"},
                artifacts={"type": "NO_ARTIFACTS"},
                environment={"type": "LINUX_CONTAINER", "image": "test-image",
                             "computeType": "BUILD_GENERAL1_LARGE"},
                serviceRole="arn:aws:iam::123456789012:role/service-role/test-build-role",
            )

        # plugin_simulator.py: the Plugin_Simulator state machine (task 8.2).
        # A trivial pass-state definition suffices for StartExecution /
        # DescribeExecution against moto.
        stepfunctions = boto3.client("stepfunctions", region_name=REGION)
        simulator_machine = stepfunctions.create_state_machine(
            name="dda-plugin-simulator",
            definition=json.dumps({
                "StartAt": "Noop",
                "States": {"Noop": {"Type": "Pass", "End": True}},
            }),
            roleArn="arn:aws:iam::123456789012:role/service-role/test-sfn-role",
        )
        os.environ["SIMULATOR_STATE_MACHINE_ARN"] = simulator_machine["stateMachineArn"]

        # Import the real layer + handler modules inside the mock so
        # their module-level boto3 clients are intercepted by moto.
        # Pop any fake shared_utils installed by standalone tests.
        # node_catalog_resolution is imported at module scope (i.e. at
        # collection time, before this mock is active) by
        # test_property_catalog_membership.py; its module-level boto3
        # client would otherwise stay cached in sys.modules and poison
        # every later consumer (workflow_validation, workflow_generator,
        # workflow_packaging) imported inside the mock.
        for module_name in ("workflows", "plugin_records", "plugin_importer",
                            "plugin_builds", "plugin_simulator",
                            "custom_node_types", "plugin_components",
                            "workflow_packaging", "node_catalog_resolution",
                            "shared_utils"):
            sys.modules.pop(module_name, None)
        import workflows  # noqa: F401
        import plugin_records  # noqa: F401
        import plugin_importer  # noqa: F401
        import plugin_builds  # noqa: F401
        import plugin_simulator  # noqa: F401
        import custom_node_types  # noqa: F401

        resource = boto3.resource("dynamodb", region_name=REGION)
        yield SimpleNamespace(
            workflows=workflows,
            plugin_records=plugin_records,
            plugin_importer=plugin_importer,
            plugin_builds=plugin_builds,
            plugin_simulator=plugin_simulator,
            custom_node_types=custom_node_types,
            s3=s3,
            kms=kms,
            codebuild=codebuild,
            stepfunctions=stepfunctions,
            tables=SimpleNamespace(
                workflows=resource.Table(TEST_ENV["WORKFLOWS_TABLE"]),
                versions=resource.Table(TEST_ENV["WORKFLOW_VERSIONS_TABLE"]),
                deployments=resource.Table(TEST_ENV["DEPLOYMENTS_TABLE"]),
                devices=resource.Table(TEST_ENV["DEVICES_TABLE"]),
                usecases=resource.Table(TEST_ENV["USECASES_TABLE"]),
                user_roles=resource.Table(TEST_ENV["USER_ROLES_TABLE"]),
                plugin_records=resource.Table(TEST_ENV["PLUGIN_RECORDS_TABLE"]),
                custom_node_types=resource.Table(TEST_ENV["CUSTOM_NODE_TYPES_TABLE"]),
                module_index_cache=resource.Table(TEST_ENV["MODULE_INDEX_CACHE_TABLE"]),
                simulation_runs=resource.Table(TEST_ENV["SIMULATION_RUNS_TABLE"]),
                test_datasets=resource.Table(TEST_ENV["TEST_DATASETS_TABLE"]),
                audit_log=resource.Table(TEST_ENV["AUDIT_LOG_TABLE"]),
                labeling_teams=resource.Table(TEST_ENV["LABELING_TEAMS_TABLE"]),
                labeling_jobs=resource.Table(TEST_ENV["LABELING_JOBS_TABLE"]),
                labeling_tasks=resource.Table(TEST_ENV["LABELING_TASKS_TABLE"]),
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


# ---------------------------------------------------------------------------
# Hypothesis profile: cap property tests at 25 examples for fast local runs.
# Per-test @settings(max_examples=...) decorators take precedence; keep them
# at or below this budget. Override with HYPOTHESIS_PROFILE=ci for a larger
# run.
# ---------------------------------------------------------------------------
from hypothesis import settings as _hyp_settings  # noqa: E402

_hyp_settings.register_profile("portal-fast", max_examples=25)
_hyp_settings.register_profile("ci", max_examples=100)
_hyp_settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "portal-fast"))
