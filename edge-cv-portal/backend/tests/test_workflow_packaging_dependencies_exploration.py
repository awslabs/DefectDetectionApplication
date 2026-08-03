"""Bug-condition exploration test (Task 1, case 3) for edge-deploy-reliability.

Property 3: Bug Condition — Generated workflow recipes carry model and
LocalServer dependencies (Defect C, `isBugCondition_C`).

**These tests assert the FIXED (post-fix) packaging output, so they are
EXPECTED TO FAIL on the UNFIXED tree.** The failure is the counterexample
confirming the defect: for a workflow whose `llm_inference` node binds a
model ref (modelName: opt125m-smoke), packaged for arm64_jp6, the registered
`dda.workflow.*` recipe's ComponentDependencies contains no
`model-vllm-opt125m-smoke` entry and no `aws.edgeml.dda.LocalServer.arm64JP6`
entry — only `dda.plugin.*` entries are ever emitted (or, plugin-free as
here, no ComponentDependencies block at all). Greengrass therefore has no
ordering or health relationship between the workflow, its model components,
and LocalServer (the incident's failure surfaced as an unrelated-looking
model component break).

The SAME tests are re-run in task 3.5 against the fixed
`workflow_packaging.py`, where they must PASS: HARD entries for each distinct
published model component of the workflow's model refs and for the LocalServer
variant of each target architecture.

Harness: the established moto-backed packaging stack (conftest `aws_stack` +
the `LlmPackagingEnv` pattern from test_workflow_packaging_llm_gate.py), plus
the training-jobs Model_Registry table with the production
`usecase-training-index` GSI shape (test_workflow_test_steps_model_registry
pattern) seeded with published records for both bound models — so the fixed
resolution path (`published_component.component_name`) has real records to
resolve against. The recipe under assertion is captured from the
`create_component_version(inlineRecipe=...)` call on the mocked Use_Case
Greengrass client.

Validates: Requirements 1.7, 1.8
"""
import json
import os
import sys
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from conftest import REGION

TRAINING_JOBS_TABLE_NAME = "test-edge-deploy-reliability-training-jobs"

LLM_MODEL_NAME = "opt125m-smoke"
LLM_MODEL_COMPONENT = "model-vllm-opt125m-smoke"
VISION_MODEL_NAME = "defect-model"
VISION_MODEL_COMPONENT = "model-defect-model"
JP6_LOCAL_SERVER_COMPONENT = "aws.edgeml.dda.LocalServer.arm64JP6"


# --------------------------------------------------------------------------
# Fixtures / helpers
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def packaging_env(aws_stack):
    """The training-jobs Model_Registry table (production GSI shape) plus a
    freshly imported workflow_packaging bound to it inside moto."""
    import boto3

    os.environ["TRAINING_JOBS_TABLE"] = TRAINING_JOBS_TABLE_NAME

    client = boto3.client("dynamodb", region_name=REGION)
    client.create_table(
        TableName=TRAINING_JOBS_TABLE_NAME,
        KeySchema=[{"AttributeName": "training_id", "KeyType": "HASH"}],
        AttributeDefinitions=[
            {"AttributeName": "training_id", "AttributeType": "S"},
            {"AttributeName": "usecase_id", "AttributeType": "S"},
        ],
        GlobalSecondaryIndexes=[{
            "IndexName": "usecase-training-index",
            "KeySchema": [{"AttributeName": "usecase_id", "KeyType": "HASH"}],
            "Projection": {"ProjectionType": "ALL"},
        }],
        BillingMode="PAY_PER_REQUEST",
    )

    # Re-import so the module binds the table name above and moto-intercepted
    # clients (test_workflow_test_steps_model_registry pattern).
    for module_name in ("workflow_packaging", "node_catalog_resolution",
                        "model_registry_snapshot"):
        sys.modules.pop(module_name, None)
    import workflow_packaging

    resource = boto3.resource("dynamodb", region_name=REGION)
    yield SimpleNamespace(
        packaging=workflow_packaging,
        training_table=resource.Table(TRAINING_JOBS_TABLE_NAME),
    )
    os.environ.pop("TRAINING_JOBS_TABLE", None)
    sys.modules.pop("workflow_packaging", None)


def make_deployable_greengrass():
    gg = MagicMock(name="greengrassv2")
    gg.create_component_version.return_value = {
        "arn": ("arn:aws:greengrass:us-east-1:123456789012:"
                f"components:test:versions:{uuid.uuid4()}")
    }
    gg.describe_component.return_value = {
        "status": {"componentState": "DEPLOYABLE", "message": "simulated"}
    }
    return gg


def llm_workflow_definition():
    """folder_source -> model_inference -> llm_inference -> mqtt_publish:
    compiles for arm64_jp6 with no custom plugin dependencies; binds two
    model refs (model_inference.modelName and llm_inference.modelName)."""
    return {
        "schemaVersion": 1,
        "nodes": [
            {"id": "src", "type": "folder_source", "position": {"x": 0, "y": 0},
             "parameters": {"location": "/data/images"}},
            {"id": "inf", "type": "model_inference", "position": {"x": 200, "y": 0},
             "parameters": {"modelName": VISION_MODEL_NAME}},
            {"id": "llm", "type": "llm_inference", "position": {"x": 400, "y": 0},
             "parameters": {"modelName": LLM_MODEL_NAME,
                            "prompt_template": "Summarize: {confidence}"}},
            {"id": "pub", "type": "mqtt_publish", "position": {"x": 600, "y": 0},
             "parameters": {"topic": "results", "broker_host": "localhost"}},
        ],
        "connections": [
            {"id": "c1", "from": {"node": "src", "port": "out"},
             "to": {"node": "inf", "port": "in"}},
            {"id": "c2", "from": {"node": "inf", "port": "out"},
             "to": {"node": "llm", "port": "in"}},
            {"id": "c3", "from": {"node": "llm", "port": "out"},
             "to": {"node": "pub", "port": "in"}},
        ],
    }


class DependencyPackagingEnv:
    """Packaging harness: a validated workflow version bound to published
    model records, a Use_Case with an S3 bucket, and patched Use_Case-account
    clients (LlmPackagingEnv pattern)."""

    def __init__(self, env, packaging_env, monkeypatch):
        self.env = env
        self.packaging = packaging_env.packaging
        monkeypatch.setattr(self.packaging, "COMPONENT_STATUS_POLL_SECONDS", 0)

        self.user = env.make_user(role="UseCaseAdmin")
        self.usecase_bucket = f"usecase-bucket-{uuid.uuid4()}"
        env.s3.create_bucket(Bucket=self.usecase_bucket)
        self.usecase_id = f"uc-{uuid.uuid4()}"
        env.stack.tables.usecases.put_item(Item={
            "usecase_id": self.usecase_id,
            "name": "Edge Deploy Reliability Exploration",
            "account_id": "123456789012",
            "s3_bucket": self.usecase_bucket,
        })

        # Model_Registry records with published Greengrass components, the
        # shape greengrass_publish.py writes (published_component map): the
        # fixed resolution path extracts published_component.component_name.
        packaging_env.training_table.put_item(Item={
            "training_id": f"tr-{uuid.uuid4()}",
            "usecase_id": self.usecase_id,
            "model_name": LLM_MODEL_NAME,
            "model_type": "vllm",
            "created_at": 1,
            "published_component": {
                "component_name": LLM_MODEL_COMPONENT,
                "component_version": "1.0.0",
                "runtime": "vllm",
                "supported_architectures": ["arm64_jp6"],
            },
        })
        packaging_env.training_table.put_item(Item={
            "training_id": f"tr-{uuid.uuid4()}",
            "usecase_id": self.usecase_id,
            "model_name": VISION_MODEL_NAME,
            "model_type": "anomaly_detection",
            "created_at": 1,
            "published_component": {
                "component_name": VISION_MODEL_COMPONENT,
                "component_version": "1.0.0",
            },
        })

        status, payload = env.invoke("POST", "/workflows", self.user, body={
            "usecase_id": self.usecase_id,
            "name": "llm workflow with model refs",
            "definition": llm_workflow_definition(),
        })
        assert status == 201, payload
        self.workflow_id = payload["workflow"]["workflow_id"]

        env.stack.tables.versions.update_item(
            Key={"workflow_id": self.workflow_id, "version": 1},
            UpdateExpression="SET validation_status = :v",
            ExpressionAttributeValues={
                ":v": {"status": "passed", "validated_at": 1,
                       "findings_key": "findings/none.json"},
            },
        )

        self.greengrass = make_deployable_greengrass()

        def fake_get_usecase_client(service_name, usecase, session_name=None,
                                    region=None):
            if service_name == "s3":
                return env.s3
            if service_name == "greengrassv2":
                return self.greengrass
            raise AssertionError(f"unexpected usecase client: {service_name}")

        monkeypatch.setattr(self.packaging, "get_usecase_client",
                            fake_get_usecase_client)

    def package(self, architectures):
        event = self.env.event(
            "POST", "/workflows/{id}/package", self.user,
            workflow_id=self.workflow_id,
            body={"architectures": architectures},
        )
        response = self.packaging.handler(event, None)
        return response["statusCode"], json.loads(response["body"])

    def registered_recipe(self):
        assert self.greengrass.create_component_version.called, (
            "no component version was registered")
        call = self.greengrass.create_component_version.call_args
        return json.loads(call.kwargs["inlineRecipe"])


@pytest.fixture
def dep_env(env, packaging_env, monkeypatch):
    return DependencyPackagingEnv(env, packaging_env, monkeypatch)


# --------------------------------------------------------------------------
# Exploration case 3: isBugCondition_C
# --------------------------------------------------------------------------

class TestWorkflowRecipeDependencies:

    def test_recipe_declares_hard_dependency_on_used_model_component(
            self, dep_env):
        """isBugCondition_C: the unfixed build_recipe output for a workflow
        binding modelName: opt125m-smoke carries no modelComponent(m) entry —
        Greengrass gets no ordering/health edge to the model component.

        Validates: Requirements 1.7 (expected behavior 2.8)
        """
        status, payload = dep_env.package(["arm64_jp6"])
        assert status == 201, payload

        recipe = dep_env.registered_recipe()
        deps = recipe.get("ComponentDependencies") or {}
        assert LLM_MODEL_COMPONENT in deps, (
            "COUNTEREXAMPLE (Defect C): recipe for workflow '{}' "
            "(llm_inference modelName: {}) has ComponentDependencies {} — "
            "no {} entry (only dda.plugin.* entries are ever emitted)"
            .format(dep_env.workflow_id, LLM_MODEL_NAME,
                    sorted(deps.keys()), LLM_MODEL_COMPONENT))
        entry = deps[LLM_MODEL_COMPONENT]
        assert entry.get("DependencyType") == "HARD", (
            "COUNTEREXAMPLE (Defect C): {} dependency is not HARD: {!r}"
            .format(LLM_MODEL_COMPONENT, entry))
        assert entry.get("VersionRequirement"), (
            "model component dependency carries no VersionRequirement")

    def test_recipe_declares_dependency_on_target_arch_local_server(
            self, dep_env):
        """isBugCondition_C: the unfixed recipe carries no
        localServerComponent(a) entry for the target architecture, so nothing
        gates the workflow on LocalServer health (the incident's HARD edge
        existed only on the model component, and RUNNING lied).

        Validates: Requirements 1.8 (expected behavior 2.9)
        """
        status, payload = dep_env.package(["arm64_jp6"])
        assert status == 201, payload

        recipe = dep_env.registered_recipe()
        deps = recipe.get("ComponentDependencies") or {}
        assert JP6_LOCAL_SERVER_COMPONENT in deps, (
            "COUNTEREXAMPLE (Defect C): recipe packaged for arm64_jp6 has "
            "ComponentDependencies {} — no {} entry"
            .format(sorted(deps.keys()), JP6_LOCAL_SERVER_COMPONENT))
        entry = deps[JP6_LOCAL_SERVER_COMPONENT]
        assert entry.get("DependencyType") == "HARD", (
            "COUNTEREXAMPLE (Defect C): {} dependency is not HARD: {!r}"
            .format(JP6_LOCAL_SERVER_COMPONENT, entry))
        assert str(entry.get("VersionRequirement", "")).startswith(">="), (
            "LocalServer dependency must carry the per-arch minimum-version "
            "floor (>=...); got {!r}".format(entry.get("VersionRequirement")))
