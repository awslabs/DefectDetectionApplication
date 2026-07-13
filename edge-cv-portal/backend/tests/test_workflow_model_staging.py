"""
Triton model staging for workflow test runs (workflow_model_staging.py
+ the start_test_run wiring in workflow_testing.py).

Cloud test runs execute model_inference nodes for real: starting a run
resolves each node's modelName against the Use_Case's model registry,
picks a CPU-runnable Greengrass component variant (-x86-64-cpu
preferred, then -onnx), copies the component's S3 model artifact zip
into the portal artifacts bucket under the run's prefix, and forwards
the staging manifest [{nodeId, modelName, s3Key}] through the state
machine input (staged_models_json -> STAGED_MODELS env). A model
without a CPU-compatible variant fails the run with a clear per-node
error record before any execution starts (Requirement 12.10
semantics).

Covers:
1. Variant selection: -x86-64-cpu preferred over -onnx, -onnx as the
   fallback, and the exact no-CPU-variant error otherwise.
2. Artifact copy staging against moto S3 with a fake Greengrass client
   (recipe -> artifact URI -> portal-bucket copy, shared models copied
   once, per-node error records for unregistered models / missing
   artifacts).
3. Manifest passing: POST /workflows/{id}/test-runs forwards
   staged_models + staged_models_json in the execution input; staging
   errors fail the run (results document + TestRuns failure) without
   starting an execution; workflows without model nodes stage nothing.
"""
import json
import os
import sys
import uuid
from types import SimpleNamespace

import pytest

from conftest import REGION, TEST_ENV

# Module-unique table names (the shared moto stack is session-scoped;
# other test modules create their own TestDatasets/TestRuns tables).
DATASETS_TABLE_NAME = "staging-test-datasets"
RUNS_TABLE_NAME = "staging-test-runs"
MODELS_TABLE_NAME = "staging-models"

SOURCE_BUCKET = "usecase-data-bucket"

ARN_PREFIX = "arn:aws:greengrass:us-east-1:123456789012:components:"


def component_arn(name, version="2.0.0"):
    return "{0}{1}:versions:{2}".format(ARN_PREFIX, name, version)


ARN_CPU = component_arn("model-cookies-binary-x86-64-cpu")
ARN_ONNX = component_arn("model-cookies-binary-onnx")
ARN_JP5 = component_arn("model-cookies-binary-jetson-xavier-jp5")
ARN_JP6 = component_arn("model-cookies-binary-jetson-xavier-jp6")


def recipe(artifact_uri=None, extra_artifacts=()):
    """A Greengrass component recipe shaped like the live model
    components (Manifests[].Artifacts[].Uri -> the model zip)."""
    artifacts = list(extra_artifacts)
    if artifact_uri:
        artifacts.append({"Uri": artifact_uri,
                          "Digest": "x", "Algorithm": "SHA-256"})
    return {
        "RecipeFormatVersion": "2020-01-25",
        "ComponentName": "model-cookies-binary-x86-64-cpu",
        "ComponentVersion": "2.0.0",
        "Manifests": [{
            "Platform": {"os": "linux", "architecture": "amd64"},
            "Artifacts": artifacts,
        }],
    }


class FakeGreengrass:
    """greengrass:GetComponent stub returning canned recipes (moto has
    no greengrassv2 backend)."""

    def __init__(self, recipes_by_arn):
        self.recipes_by_arn = recipes_by_arn
        self.calls = []

    def get_component(self, arn, recipeOutputFormat=None):
        self.calls.append(arn)
        if arn not in self.recipes_by_arn:
            from botocore.exceptions import ClientError
            raise ClientError(
                {"Error": {"Code": "ResourceNotFoundException",
                           "Message": arn}}, "GetComponent")
        return {
            "recipe": json.dumps(self.recipes_by_arn[arn]).encode("utf-8"),
            "recipeOutputFormat": "JSON",
        }


class FakeStepFunctions:
    def __init__(self):
        self.calls = []

    def start_execution(self, **kwargs):
        self.calls.append(kwargs)
        return {"executionArn":
                "arn:aws:states:us-east-1:123456789012:execution:test:"
                + kwargs.get("name", "x")}


# A definition with one camera source feeding two model inference nodes
# (one model shared is exercised separately). start_test_run does not
# validate, so the raw JSON shape is all that matters here.
def model_definition(*inference_nodes):
    nodes = [{"id": "src", "type": "camera_source",
              "position": {"x": 0, "y": 0}, "parameters": {}}]
    connections = []
    for index, (node_id, model_name) in enumerate(inference_nodes):
        parameters = {} if model_name is None else {"modelName": model_name}
        nodes.append({"id": node_id, "type": "model_inference",
                      "position": {"x": 100, "y": index * 100},
                      "parameters": parameters})
        connections.append({
            "id": "c{0}".format(index),
            "from": {"node": "src", "port": "out"},
            "to": {"node": node_id, "port": "in"},
        })
    return {"schemaVersion": 1, "nodes": nodes, "connections": connections}


@pytest.fixture(scope="module")
def staging_env(aws_stack):
    """Module tables + freshly imported workflow_testing bound to them."""
    import boto3

    os.environ["TEST_DATASETS_TABLE"] = DATASETS_TABLE_NAME
    os.environ["TEST_RUNS_TABLE"] = RUNS_TABLE_NAME
    os.environ["MODELS_TABLE"] = MODELS_TABLE_NAME

    client = boto3.client("dynamodb", region_name=REGION)
    client.create_table(
        TableName=DATASETS_TABLE_NAME,
        KeySchema=[{"AttributeName": "dataset_id", "KeyType": "HASH"}],
        AttributeDefinitions=[
            {"AttributeName": "dataset_id", "AttributeType": "S"},
            {"AttributeName": "usecase_id", "AttributeType": "S"},
        ],
        GlobalSecondaryIndexes=[{
            "IndexName": "usecase-datasets-index",
            "KeySchema": [{"AttributeName": "usecase_id", "KeyType": "HASH"}],
            "Projection": {"ProjectionType": "ALL"},
        }],
        BillingMode="PAY_PER_REQUEST",
    )
    client.create_table(
        TableName=RUNS_TABLE_NAME,
        KeySchema=[{"AttributeName": "test_run_id", "KeyType": "HASH"}],
        AttributeDefinitions=[
            {"AttributeName": "test_run_id", "AttributeType": "S"},
            {"AttributeName": "workflow_id", "AttributeType": "S"},
            {"AttributeName": "started_at", "AttributeType": "N"},
        ],
        GlobalSecondaryIndexes=[{
            "IndexName": "workflow-runs-index",
            "KeySchema": [
                {"AttributeName": "workflow_id", "KeyType": "HASH"},
                {"AttributeName": "started_at", "KeyType": "RANGE"},
            ],
            "Projection": {"ProjectionType": "ALL"},
        }],
        BillingMode="PAY_PER_REQUEST",
    )
    client.create_table(
        TableName=MODELS_TABLE_NAME,
        KeySchema=[{"AttributeName": "model_id", "KeyType": "HASH"}],
        AttributeDefinitions=[
            {"AttributeName": "model_id", "AttributeType": "S"},
            {"AttributeName": "usecase_id", "AttributeType": "S"},
            {"AttributeName": "created_at", "AttributeType": "N"},
        ],
        GlobalSecondaryIndexes=[{
            "IndexName": "usecase-models-index",
            "KeySchema": [
                {"AttributeName": "usecase_id", "KeyType": "HASH"},
                {"AttributeName": "created_at", "KeyType": "RANGE"},
            ],
            "Projection": {"ProjectionType": "ALL"},
        }],
        BillingMode="PAY_PER_REQUEST",
    )

    s3 = boto3.client("s3", region_name=REGION)
    s3.create_bucket(Bucket=SOURCE_BUCKET)

    for module_name in ("workflow_testing", "workflow_model_staging"):
        sys.modules.pop(module_name, None)
    import workflow_model_staging
    import workflow_testing

    resource = boto3.resource("dynamodb", region_name=REGION)
    yield SimpleNamespace(
        testing=workflow_testing,
        staging=workflow_model_staging,
        datasets_table=resource.Table(DATASETS_TABLE_NAME),
        runs_table=resource.Table(RUNS_TABLE_NAME),
        models_table=resource.Table(MODELS_TABLE_NAME),
        s3=s3,
        bucket=TEST_ENV["PORTAL_ARTIFACTS_BUCKET"],
        source_bucket=SOURCE_BUCKET,
    )


@pytest.fixture
def ctx(env):
    """A fresh Use_Case and a DataScientist (workflow:test) on it."""
    usecase_id = env.create_usecase()
    user = env.make_user()
    env.assign_role(user, usecase_id, "DataScientist")
    return SimpleNamespace(usecase_id=usecase_id, user=user)


def register_model(staging_env, usecase_id, name, component_arns,
                   created_at=1):
    staging_env.models_table.put_item(Item={
        "model_id": "model-{0}".format(uuid.uuid4()),
        "usecase_id": usecase_id,
        "name": name,
        "version": "2.0.0",
        "created_at": created_at,
        "component_arns": component_arns,
    })


def put_artifact(staging_env, key=None, body=b"PK-model-zip-bytes"):
    key = key or "model_artifacts/model-abc/abc_greengrass_model_component.zip"
    staging_env.s3.put_object(Bucket=staging_env.source_bucket, Key=key,
                              Body=body)
    return "s3://{0}/{1}".format(staging_env.source_bucket, key), body


# ===========================================================================
# 1. Variant selection
# ===========================================================================

class TestCpuVariantSelection:

    def test_prefers_x86_64_cpu_over_onnx(self, staging_env):
        arns = {"x86_64-cpu": ARN_CPU, "onnx": ARN_ONNX,
                "jetson-xavier-jp5": ARN_JP5}
        assert staging_env.staging.select_cpu_component_arn(arns) == ARN_CPU

    def test_falls_back_to_onnx(self, staging_env):
        arns = {"onnx": ARN_ONNX, "jetson-xavier-jp5": ARN_JP5}
        assert staging_env.staging.select_cpu_component_arn(arns) == ARN_ONNX

    def test_no_cpu_variant_yields_none(self, staging_env):
        arns = {"jetson-xavier-jp5": ARN_JP5, "jetson-xavier-jp6": ARN_JP6}
        assert staging_env.staging.select_cpu_component_arn(arns) is None
        assert staging_env.staging.select_cpu_component_arn({}) is None
        assert staging_env.staging.select_cpu_component_arn(None) is None

    def test_selection_is_by_component_name_suffix_not_key(self, staging_env):
        # The device-type key can be arbitrary; the component NAME suffix
        # decides (the recipes/name are what encode the architecture).
        arns = {"weird-key": ARN_CPU}
        assert staging_env.staging.select_cpu_component_arn(arns) == ARN_CPU

    def test_no_cpu_variant_message_is_exact(self, staging_env):
        assert staging_env.staging.no_cpu_variant_message("m") == (
            "Model m has no CPU-compatible (x86_64/ONNX) variant for "
            "cloud testing")

    def test_newest_registry_item_with_cpu_variant_wins(self, staging_env):
        items = [
            {"name": "m", "created_at": 1,
             "component_arns": {"x86_64-cpu": component_arn("m-x86-64-cpu", "1.0.0")}},
            {"name": "m", "created_at": 3,
             "component_arns": {"jetson-xavier-jp5": ARN_JP5}},
            {"name": "m", "created_at": 2,
             "component_arns": {"x86_64-cpu": component_arn("m-x86-64-cpu", "2.0.0")}},
        ]
        item, arn = staging_env.staging.resolve_model_item(items, "m")
        assert item["created_at"] == 2
        assert arn == component_arn("m-x86-64-cpu", "2.0.0")

    def test_registered_without_variant_vs_unregistered(self, staging_env):
        items = [{"name": "m", "created_at": 1,
                  "component_arns": {"jetson-xavier-jp5": ARN_JP5}}]
        item, arn = staging_env.staging.resolve_model_item(items, "m")
        assert item is not None and arn is None
        item, arn = staging_env.staging.resolve_model_item(items, "other")
        assert item is None and arn is None


class TestDefinitionParsing:

    def test_model_inference_nodes_extracted_in_order(self, staging_env):
        definition = model_definition(("infA", "model-a"), ("infB", "model-b"))
        assert staging_env.staging.model_inference_nodes(definition) == [
            {"nodeId": "infA", "modelName": "model-a"},
            {"nodeId": "infB", "modelName": "model-b"},
        ]

    def test_missing_model_name_yields_none(self, staging_env):
        definition = model_definition(("inf", None))
        assert staging_env.staging.model_inference_nodes(definition) == [
            {"nodeId": "inf", "modelName": None},
        ]

    def test_non_model_definitions_yield_nothing(self, staging_env):
        assert staging_env.staging.model_inference_nodes(None) == []
        assert staging_env.staging.model_inference_nodes({}) == []
        assert staging_env.staging.model_inference_nodes(
            {"nodes": [{"id": "n", "type": "capture", "parameters": {}}]}) == []


class TestRecipeArtifactLocation:

    def test_zip_artifact_uri_parsed(self, staging_env):
        uri = "s3://bkt/model_artifacts/m/x_greengrass_model_component.zip"
        location = staging_env.staging.artifact_location_from_recipe(
            recipe(uri))
        assert location == (
            "bkt", "model_artifacts/m/x_greengrass_model_component.zip")

    def test_zip_preferred_over_other_artifacts(self, staging_env):
        r = recipe("s3://bkt/m/model.zip",
                   extra_artifacts=[{"Uri": "s3://bkt/m/notes.txt"}])
        assert staging_env.staging.artifact_location_from_recipe(r) == (
            "bkt", "m/model.zip")

    def test_recipe_without_s3_artifact_yields_none(self, staging_env):
        assert staging_env.staging.artifact_location_from_recipe(
            recipe(None)) is None
        assert staging_env.staging.artifact_location_from_recipe({}) is None


# ===========================================================================
# 2. Artifact copy staging (moto S3 + fake Greengrass)
# ===========================================================================

class TestArtifactCopyStaging:

    def stage(self, staging_env, nodes, model_items, recipes,
              results_key="workflows/uc/test-runs/run-1/results.json"):
        greengrass = FakeGreengrass(recipes)
        staged, errors = staging_env.staging.stage_models_for_run(
            nodes, model_items, greengrass,
            staging_env.s3, staging_env.s3,
            staging_env.bucket, results_key)
        return staged, errors, greengrass

    def test_artifact_copied_under_run_prefix(self, staging_env):
        uri, body = put_artifact(staging_env)
        staged, errors, greengrass = self.stage(
            staging_env,
            [{"nodeId": "inf", "modelName": "model-cookies-binary"}],
            [{"name": "model-cookies-binary", "created_at": 1,
              "component_arns": {"x86_64-cpu": ARN_CPU}}],
            {ARN_CPU: recipe(uri)})

        assert errors == []
        assert staged == [{
            "nodeId": "inf",
            "modelName": "model-cookies-binary",
            "s3Key": "workflows/uc/test-runs/run-1/models/"
                     "model-cookies-binary.zip",
        }]
        assert greengrass.calls == [ARN_CPU]
        copied = staging_env.s3.get_object(
            Bucket=staging_env.bucket, Key=staged[0]["s3Key"])
        assert copied["Body"].read() == body

    def test_shared_model_copied_once_with_one_entry_per_node(self,
                                                              staging_env):
        uri, _ = put_artifact(staging_env, key="m/shared.zip")
        staged, errors, greengrass = self.stage(
            staging_env,
            [{"nodeId": "infA", "modelName": "model-cookies-binary"},
             {"nodeId": "infB", "modelName": "model-cookies-binary"}],
            [{"name": "model-cookies-binary", "created_at": 1,
              "component_arns": {"onnx": ARN_ONNX}}],
            {ARN_ONNX: recipe(uri)})

        assert errors == []
        assert [entry["nodeId"] for entry in staged] == ["infA", "infB"]
        assert len({entry["s3Key"] for entry in staged}) == 1
        # The recipe (and the copy) happened exactly once.
        assert greengrass.calls == [ARN_ONNX]

    def test_no_cpu_variant_records_the_exact_error(self, staging_env):
        staged, errors, greengrass = self.stage(
            staging_env,
            [{"nodeId": "inf", "modelName": "model-cookies-binary"}],
            [{"name": "model-cookies-binary", "created_at": 1,
              "component_arns": {"jetson-xavier-jp5": ARN_JP5}}],
            {})

        assert staged == []
        assert greengrass.calls == []
        assert len(errors) == 1
        record = errors[0]
        assert record["nodeId"] == "inf"
        assert record["status"] == "error"
        assert record["error"]["code"] == "MODEL_NO_CPU_VARIANT"
        assert record["error"]["message"] == (
            "Model model-cookies-binary has no CPU-compatible (x86_64/ONNX) "
            "variant for cloud testing")

    def test_unregistered_model_records_error(self, staging_env):
        staged, errors, _ = self.stage(
            staging_env,
            [{"nodeId": "inf", "modelName": "ghost-model"}],
            [], {})
        assert staged == []
        assert errors[0]["error"]["code"] == "MODEL_NOT_REGISTERED"
        assert "ghost-model" in errors[0]["error"]["message"]

    def test_missing_source_artifact_records_error(self, staging_env):
        # Recipe points at a key that does not exist in the source bucket.
        uri = "s3://{0}/does/not/exist.zip".format(staging_env.source_bucket)
        staged, errors, _ = self.stage(
            staging_env,
            [{"nodeId": "inf", "modelName": "model-cookies-binary"}],
            [{"name": "model-cookies-binary", "created_at": 1,
              "component_arns": {"x86_64-cpu": ARN_CPU}}],
            {ARN_CPU: recipe(uri)})
        assert staged == []
        assert errors[0]["nodeId"] == "inf"
        assert errors[0]["error"]["code"] == "MODEL_STAGING_FAILED"
        assert "could not be copied" in errors[0]["error"]["message"]

    def test_recipe_without_artifact_records_error(self, staging_env):
        staged, errors, _ = self.stage(
            staging_env,
            [{"nodeId": "inf", "modelName": "model-cookies-binary"}],
            [{"name": "model-cookies-binary", "created_at": 1,
              "component_arns": {"x86_64-cpu": ARN_CPU}}],
            {ARN_CPU: recipe(None)})
        assert staged == []
        assert errors[0]["error"]["code"] == "MODEL_STAGING_FAILED"
        assert "declares no S3 model artifact" in errors[0]["error"]["message"]


# ===========================================================================
# 3. Manifest passing through POST /workflows/{id}/test-runs
# ===========================================================================

def invoke(staging_env, ctx, method, resource, body=None, resource_id=None):
    event = {
        "httpMethod": method,
        "resource": resource,
        "path": resource.replace("{id}", resource_id or ""),
        "pathParameters": {"id": resource_id} if resource_id else None,
        "queryStringParameters": None,
        "body": json.dumps(body) if body is not None else None,
        "requestContext": {
            "authorizer": {
                "claims": {
                    "sub": ctx.user["user_id"],
                    "email": ctx.user["email"],
                    "cognito:username": ctx.user["username"],
                    "custom:role": ctx.user["role"],
                }
            }
        },
    }
    response = staging_env.testing.handler(event, None)
    return response["statusCode"], json.loads(response["body"])


class TestStartRunManifestPassing:

    def stage_workflow(self, staging_env, ctx, monkeypatch, definition):
        """A stored workflow version + dataset, stubbed Step Functions,
        and staging clients bound to moto S3 + a fake Greengrass."""
        import boto3
        resource = boto3.resource("dynamodb", region_name=REGION)
        workflow_id = "wf-{0}".format(uuid.uuid4())
        resource.Table(TEST_ENV["WORKFLOWS_TABLE"]).put_item(Item={
            "workflow_id": workflow_id,
            "usecase_id": ctx.usecase_id,
            "name": "wf",
            "latest_version": 1,
        })
        definition_key = "workflows/{0}/defs/{1}.json".format(
            ctx.usecase_id, workflow_id)
        staging_env.s3.put_object(
            Bucket=staging_env.bucket, Key=definition_key,
            Body=json.dumps(definition).encode("utf-8"),
        )
        resource.Table(TEST_ENV["WORKFLOW_VERSIONS_TABLE"]).put_item(Item={
            "workflow_id": workflow_id,
            "version": 1,
            "s3_definition_key": definition_key,
        })
        dataset_id = "ds-{0}".format(uuid.uuid4())
        staging_env.datasets_table.put_item(Item={
            "dataset_id": dataset_id,
            "usecase_id": ctx.usecase_id,
            "s3_prefix": "workflows/{0}/test-datasets/{1}/".format(
                ctx.usecase_id, dataset_id),
        })
        fake_sfn = FakeStepFunctions()
        monkeypatch.setattr(staging_env.testing, "stepfunctions", fake_sfn)
        monkeypatch.setattr(
            staging_env.testing, "TEST_RUN_STATE_MACHINE_ARN",
            "arn:aws:states:us-east-1:123456789012:stateMachine:test-runner")
        return workflow_id, dataset_id, fake_sfn

    def bind_staging_clients(self, staging_env, monkeypatch, recipes):
        greengrass = FakeGreengrass(recipes)
        monkeypatch.setattr(
            staging_env.testing, "model_staging_clients",
            lambda usecase: (greengrass, staging_env.s3))
        return greengrass

    def start(self, staging_env, ctx, workflow_id, dataset_id):
        return invoke(staging_env, ctx, "POST", "/workflows/{id}/test-runs",
                      {"dataset_id": dataset_id}, resource_id=workflow_id)

    def test_staged_manifest_forwarded_in_execution_input(
            self, staging_env, ctx, monkeypatch):
        uri, _ = put_artifact(staging_env, key="m/wf-model.zip")
        register_model(staging_env, ctx.usecase_id, "model-cookies-binary",
                       {"x86_64-cpu": ARN_CPU, "jetson-xavier-jp5": ARN_JP5})
        workflow_id, dataset_id, fake_sfn = self.stage_workflow(
            staging_env, ctx, monkeypatch,
            model_definition(("inf", "model-cookies-binary")))
        self.bind_staging_clients(staging_env, monkeypatch,
                                  {ARN_CPU: recipe(uri)})

        status, body = self.start(staging_env, ctx, workflow_id, dataset_id)
        assert status == 202, body
        assert body["test_run"]["status"] == "running"

        assert len(fake_sfn.calls) == 1
        inp = json.loads(fake_sfn.calls[0]["input"])
        assert inp["staged_models"] == [{
            "nodeId": "inf",
            "modelName": "model-cookies-binary",
            "s3Key": "workflows/{0}/test-runs/{1}/models/"
                     "model-cookies-binary.zip".format(
                         ctx.usecase_id, inp["test_run_id"]),
        }]
        assert json.loads(inp["staged_models_json"]) == inp["staged_models"]
        # The copy actually landed in the portal artifacts bucket.
        staging_env.s3.head_object(Bucket=staging_env.bucket,
                                   Key=inp["staged_models"][0]["s3Key"])

    def test_no_cpu_variant_fails_run_without_starting_execution(
            self, staging_env, ctx, monkeypatch):
        register_model(staging_env, ctx.usecase_id, "model-cookies-binary",
                       {"jetson-xavier-jp5": ARN_JP5})
        workflow_id, dataset_id, fake_sfn = self.stage_workflow(
            staging_env, ctx, monkeypatch,
            model_definition(("inf", "model-cookies-binary")))
        self.bind_staging_clients(staging_env, monkeypatch, {})

        status, body = self.start(staging_env, ctx, workflow_id, dataset_id)
        assert status == 202, body
        run = body["test_run"]
        assert run["status"] == "failed"
        assert run["failure"]["nodeId"] == "inf"
        assert run["failure"]["message"] == (
            "Model model-cookies-binary has no CPU-compatible (x86_64/ONNX) "
            "variant for cloud testing")
        assert run["failure"]["timeout"] is False
        assert fake_sfn.calls == []

        # The per-node error record is readable through the results
        # document (the shape GET /test-runs/{id} serves).
        stored = staging_env.runs_table.get_item(
            Key={"test_run_id": run["test_run_id"]})["Item"]
        assert stored["status"] == "failed"
        results = json.loads(staging_env.s3.get_object(
            Bucket=staging_env.bucket,
            Key=stored["results_s3_key"])["Body"].read())
        assert results["nodes"] == [{
            "nodeId": "inf",
            "status": "error",
            "outputs": [],
            "stubActivity": [],
            "error": {
                "code": "MODEL_NO_CPU_VARIANT",
                "message": "Model model-cookies-binary has no CPU-compatible "
                           "(x86_64/ONNX) variant for cloud testing",
            },
        }]

    def test_workflow_without_model_nodes_stages_nothing(
            self, staging_env, ctx, monkeypatch):
        definition = {
            "schemaVersion": 1,
            "nodes": [{"id": "src", "type": "camera_source",
                       "position": {"x": 0, "y": 0}, "parameters": {}}],
            "connections": [],
        }
        workflow_id, dataset_id, fake_sfn = self.stage_workflow(
            staging_env, ctx, monkeypatch, definition)

        def must_not_be_called(usecase):  # pragma: no cover - guard
            raise AssertionError("staging clients built without model nodes")

        monkeypatch.setattr(staging_env.testing, "model_staging_clients",
                            must_not_be_called)

        status, body = self.start(staging_env, ctx, workflow_id, dataset_id)
        assert status == 202, body
        assert body["test_run"]["status"] == "running"
        inp = json.loads(fake_sfn.calls[0]["input"])
        assert inp["staged_models"] == []
        assert inp["staged_models_json"] == "[]"

    def test_one_bad_model_fails_even_when_others_stage(
            self, staging_env, ctx, monkeypatch):
        uri, _ = put_artifact(staging_env, key="m/good-model.zip")
        register_model(staging_env, ctx.usecase_id, "good-model",
                       {"onnx": component_arn("good-model-onnx")})
        register_model(staging_env, ctx.usecase_id, "gpu-only-model",
                       {"jetson-xavier-jp5":
                        component_arn("gpu-only-model-jetson-xavier-jp5")})
        workflow_id, dataset_id, fake_sfn = self.stage_workflow(
            staging_env, ctx, monkeypatch,
            model_definition(("infA", "good-model"),
                             ("infB", "gpu-only-model")))
        self.bind_staging_clients(
            staging_env, monkeypatch,
            {component_arn("good-model-onnx"): recipe(uri)})

        status, body = self.start(staging_env, ctx, workflow_id, dataset_id)
        assert status == 202, body
        assert body["test_run"]["status"] == "failed"
        assert body["test_run"]["failure"]["nodeId"] == "infB"
        assert fake_sfn.calls == []
