"""
vLLM deployment gate activation, GSI lookup, and 409 response
(functions/deployments.py).

Task 5.2 (spec: vllm-triton-inference, Requirements 3.3, 3.4, 3.7,
8.5, 8.6). The pre-submit pass evaluates the vLLM architecture gate iff
the requested component set contains a model-vllm-* component or a
workflow component whose version item records ``has_llm_inference``:

- model-vllm-* supported sets are loaded from the backing
  vLLM_Model_Record via the ``component_name-index`` GSI on the
  training-jobs table; unresolvable records fail closed (like
  ``load_plugin_record``);
- workflow components use the ``packaged_architectures`` set the
  Component_Packager recorded on the version item;
- deployments containing neither contribute zero findings, so
  pre-feature validation applies verbatim — jp4 included (8.5);
- any violation returns ``409 VLLM_ARCH_UNSUPPORTED`` with the complete
  offending list and submits nothing (3.4).

The property tests for the pure gate decision logic and activation are
tasks 5.3/5.4; these are the example-based unit and integration tests
for the wiring.
"""
import json
import os
import sys
import uuid

import pytest

from conftest import REGION
from test_workflow_packaging_deployment_integration import (
    ACCOUNT_ID, FakeGreengrass, FakeIot, make_dewarp_definition)

TRAINING_JOBS_TABLE_NAME = "test-training-jobs-vllm-gate"


@pytest.fixture(scope="module")
def deployments(aws_stack):
    """The training-jobs table with the component_name-index GSI
    (storage-stack.ts shape), plus deployments imported inside the moto
    mock so its module-level boto3 clients are intercepted and it binds
    TRAINING_JOBS_TABLE."""
    import boto3

    os.environ["TRAINING_JOBS_TABLE"] = TRAINING_JOBS_TABLE_NAME
    boto3.client("dynamodb", region_name=REGION).create_table(
        TableName=TRAINING_JOBS_TABLE_NAME,
        KeySchema=[{"AttributeName": "training_id", "KeyType": "HASH"}],
        AttributeDefinitions=[
            {"AttributeName": "training_id", "AttributeType": "S"},
            {"AttributeName": "usecase_id", "AttributeType": "S"},
            {"AttributeName": "created_at", "AttributeType": "N"},
            {"AttributeName": "component_name", "AttributeType": "S"},
        ],
        GlobalSecondaryIndexes=[
            {
                "IndexName": "usecase-training-index",
                "KeySchema": [
                    {"AttributeName": "usecase_id", "KeyType": "HASH"},
                    {"AttributeName": "created_at", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            },
            {
                "IndexName": "component_name-index",
                "KeySchema": [
                    {"AttributeName": "component_name", "KeyType": "HASH"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            },
        ],
        BillingMode="PAY_PER_REQUEST",
    )

    for module_name in ("deployments", "workflow_guards"):
        sys.modules.pop(module_name, None)
    import deployments

    resource = boto3.resource("dynamodb", region_name=REGION)
    deployments._test_training_jobs = resource.Table(TRAINING_JOBS_TABLE_NAME)
    yield deployments

    # The env var must not leak into later test modules: consumers like
    # workflow_validation take the pre-feature "resolution skipped" path
    # when TRAINING_JOBS_TABLE is unset (the moto conftest default).
    os.environ.pop("TRAINING_JOBS_TABLE", None)


def seed_vllm_record(deployments, component_name, supported_architectures,
                     model_name="summarizer"):
    """A published vLLM_Model_Record with the top-level component_name
    attribute greengrass_publish.py materializes for the GSI lookup."""
    training_id = f"vllm-{uuid.uuid4()}"
    deployments._test_training_jobs.put_item(Item={
        "training_id": training_id,
        "usecase_id": "uc-any",
        "model_name": model_name,
        "model_type": "vllm",
        "source": "vllm",
        "status": "published",
        "created_at": 1,
        "component_name": component_name,
        "published_component": {
            "component_name": component_name,
            "component_version": "1.0.0",
            "supported_architectures": list(supported_architectures),
            "runtime": "vllm",
        },
    })
    return training_id


# ==========================================================================
# GSI lookup and fail-closed record resolution (3.3, 8.6)
# ==========================================================================

class TestVllmRecordLookup:
    def test_component_name_index_resolves_backing_record(self, deployments):
        seed_vllm_record(deployments, "model-vllm-lookup-ok", ["arm64_jp6"])

        record = deployments.load_vllm_model_record("model-vllm-lookup-ok")

        assert record is not None
        assert record["model_type"] == "vllm"
        assert deployments.vllm_component_architectures(record) == \
            ["arm64_jp6"]

    def test_unresolvable_component_fails_closed(self, deployments):
        """A model-vllm-* component with no backing record resolves to an
        empty supported set — unsupported for every device (plugin-gate
        rule)."""
        record = deployments.load_vllm_model_record("model-vllm-ghost")
        assert record is None
        assert deployments.vllm_component_architectures(record) == []

    def test_manifest_collection_activates_only_on_vllm_content(
            self, deployments):
        """Vision model components and plain workflow-free component sets
        produce no manifests, so the gate contributes zero findings
        (8.5)."""
        manifests = deployments.collect_vllm_component_manifests({
            "model-defect-classifier": "1.0.0",
            "aws.edgeml.dda.LocalServer.jp4": "1.2.0",
            "aws.greengrass.Nucleus": "2.4.0",
        })
        assert manifests == {}

    def test_manifest_collection_includes_vllm_component(self, deployments):
        seed_vllm_record(deployments, "model-vllm-manifest", ["arm64_jp6"])

        manifests = deployments.collect_vllm_component_manifests({
            "model-vllm-manifest": "1.0.0",
            "model-defect-classifier": "2.0.0",
        })

        assert manifests == {"model-vllm-manifest": {
            "version": "1.0.0", "architectures": ["arm64_jp6"]}}


# ==========================================================================
# Integration harness (mirrors test_deployment_plugin_gates.PluginGateEnv)
# ==========================================================================

class VllmGateEnv:
    """A validated + packaged workflow version whose LLM discriminator and
    packaged architecture set are seeded directly, with the stateful
    Greengrass/IoT fakes on the deployment side."""

    def __init__(self, env, deployments, monkeypatch):
        self.env = env
        self.deployments = deployments

        self.user = env.make_user(role="UseCaseAdmin")
        self.usecase_id = f"uc-{uuid.uuid4()}"
        env.stack.tables.usecases.put_item(Item={
            "usecase_id": self.usecase_id,
            "name": "vLLM Gate Test",
            "account_id": ACCOUNT_ID,
        })

        status, payload = env.invoke("POST", "/workflows", self.user, body={
            "usecase_id": self.usecase_id,
            "name": "llm workflow",
            "definition": make_dewarp_definition(),
        })
        assert status == 201, payload
        self.workflow_id = payload["workflow"]["workflow_id"]

        self.gg = FakeGreengrass()
        self.iot = FakeIot()

        def deployment_client(service_name, usecase, session_name=None,
                              region=None):
            assert usecase["usecase_id"] == self.usecase_id
            if service_name == "greengrassv2":
                return self.gg
            if service_name == "iot":
                return self.iot
            raise AssertionError(f"unexpected client: {service_name}")

        monkeypatch.setattr(deployments, "get_usecase_client",
                            deployment_client)

    # ------------------------------------------------------------- setup
    def mark_packaged(self, has_llm_inference, packaged_architectures,
                      version=1):
        """Validated + packaged version item carrying the LLM
        discriminator and packaged arch set workflow_packaging.py
        persists."""
        self.env.stack.tables.versions.update_item(
            Key={"workflow_id": self.workflow_id, "version": version},
            UpdateExpression=("SET validation_status = :v, "
                              "component_arn = :arn, plugin_components = :pc, "
                              "has_llm_inference = :hli, "
                              "packaged_architectures = :pa"),
            ExpressionAttributeValues={
                ":v": {"status": "passed", "validated_at": 1,
                       "findings_key": "findings/none.json"},
                ":arn": (f"arn:aws:greengrass:{REGION}:{ACCOUNT_ID}:components:"
                         f"dda.workflow.{self.workflow_id}:versions:1.0.0"),
                ":pc": {},
                ":hli": bool(has_llm_inference),
                ":pa": list(packaged_architectures),
            },
        )

    def put_device_record(self, thing_name, arch=None):
        item = {"device_id": thing_name, "usecase_id": self.usecase_id,
                "test_device": False}
        if arch is not None:
            item["target_architecture"] = arch
        self.env.stack.tables.devices.put_item(Item=item)

    def register_device(self, thing_name, local_server_version="1.2.0"):
        self.gg.register_device(thing_name,
                                local_server_version=local_server_version)

    # ------------------------------------------------------------ invoke
    def deploy_workflow(self, **body):
        body = {"component_type": "workflow", "usecase_id": self.usecase_id,
                "workflow_id": self.workflow_id, **body}
        event = self.env.event("POST", "/deployments", self.user, body=body)
        response = self.deployments.handler(event, None)
        return response["statusCode"], json.loads(response["body"])

    def deploy_components(self, components, **body):
        body = {"usecase_id": self.usecase_id, "components": components,
                **body}
        event = self.env.event("POST", "/deployments", self.user, body=body)
        response = self.deployments.handler(event, None)
        return response["statusCode"], json.loads(response["body"])


@pytest.fixture
def gate_env(env, deployments, monkeypatch):
    return VllmGateEnv(env, deployments, monkeypatch)


# ==========================================================================
# Workflow deployments: activation via has_llm_inference (3.3, 3.4, 8.5)
# ==========================================================================

class TestWorkflowVllmGate:
    def test_llm_workflow_rejected_for_jp4_device(self, gate_env):
        """A workflow version recorded as containing an LLM_Inference_Node
        rejects jp4 targets with the JetPack-4 reason and submits
        nothing (3.4, 3.5)."""
        gate_env.mark_packaged(True, ["arm64_jp6"])
        gate_env.register_device("jp4-cam-01")
        gate_env.put_device_record("jp4-cam-01", arch="arm64_jp4")

        status, payload = gate_env.deploy_workflow(
            target_devices=["jp4-cam-01"])

        assert status == 409
        assert payload["error"]["code"] == "VLLM_ARCH_UNSUPPORTED"
        [entry] = payload["error"]["details"]["unsupported"]
        assert entry["component"] == \
            f"dda.workflow.{gate_env.workflow_id}"
        assert entry["version"] == "1.0.0"
        assert entry["device"] == "jp4-cam-01"
        assert entry["deviceArch"] == "arm64_jp4"
        assert entry["supported"] == ["arm64_jp6"]
        assert entry["reason"] == "JP4_UNSUPPORTED"
        assert gate_env.gg.create_deployment_calls == []

    def test_llm_workflow_deploys_to_supported_arch(self, gate_env):
        gate_env.mark_packaged(True, ["arm64_jp6"])
        gate_env.register_device("jp6-cam-01")
        gate_env.put_device_record("jp6-cam-01", arch="arm64_jp6")

        status, payload = gate_env.deploy_workflow(
            target_devices=["jp6-cam-01"])

        assert status == 201, payload
        assert len(gate_env.gg.create_deployment_calls) == 1

    def test_workflow_without_llm_content_is_untouched(self, gate_env):
        """Versions without has_llm_inference contribute zero findings —
        pre-feature validation verbatim, jp4 included (8.5)."""
        gate_env.mark_packaged(False, ["arm64_jp4"])
        gate_env.register_device("jp4-cam-01")
        gate_env.put_device_record("jp4-cam-01", arch="arm64_jp4")

        status, payload = gate_env.deploy_workflow(
            target_devices=["jp4-cam-01"])

        assert status == 201, payload

    def test_unrecorded_device_architecture_fails_closed(self, gate_env):
        gate_env.mark_packaged(True, ["arm64_jp6"])
        gate_env.register_device("mystery-device")
        gate_env.put_device_record("mystery-device", arch=None)

        status, payload = gate_env.deploy_workflow(
            target_devices=["mystery-device"])

        assert status == 409
        assert payload["error"]["code"] == "VLLM_ARCH_UNSUPPORTED"
        [entry] = payload["error"]["details"]["unsupported"]
        assert entry["deviceArch"] is None
        assert entry["reason"] == "ARCH_UNSUPPORTED"


# ==========================================================================
# Generic component deployments: activation via model-vllm-* + GSI lookup
# (3.3, 3.4, 8.6)
# ==========================================================================

class TestModelComponentVllmGate:
    def test_vllm_component_rejected_for_jp4_device(self, gate_env):
        """A model-vllm-* component resolves its supported set from the
        backing record via the component_name-index GSI; jp4 misses carry
        the JetPack-4 reason and nothing is submitted (3.4, 3.5)."""
        seed_vllm_record(gate_env.deployments, "model-vllm-summarizer",
                         ["arm64_jp6"])
        gate_env.put_device_record("jp4-cam-01", arch="arm64_jp4")

        status, payload = gate_env.deploy_components(
            [{"component_name": "model-vllm-summarizer",
              "component_version": "1.0.0"}],
            target_devices=["jp4-cam-01"])

        assert status == 409
        assert payload["error"]["code"] == "VLLM_ARCH_UNSUPPORTED"
        [entry] = payload["error"]["details"]["unsupported"]
        assert entry["component"] == "model-vllm-summarizer"
        assert entry["device"] == "jp4-cam-01"
        assert entry["deviceArch"] == "arm64_jp4"
        assert entry["supported"] == ["arm64_jp6"]
        assert entry["reason"] == "JP4_UNSUPPORTED"
        assert gate_env.gg.create_deployment_calls == []

    def test_unresolvable_backing_record_fails_closed(self, gate_env):
        """A model-vllm-* component whose backing record cannot be
        resolved via the GSI is treated as unsupported for every device
        (fail closed, like load_plugin_record)."""
        gate_env.put_device_record("jp6-cam-01", arch="arm64_jp6")

        status, payload = gate_env.deploy_components(
            [{"component_name": "model-vllm-unpublished",
              "component_version": "1.0.0"}],
            target_devices=["jp6-cam-01"])

        assert status == 409
        assert payload["error"]["code"] == "VLLM_ARCH_UNSUPPORTED"
        [entry] = payload["error"]["details"]["unsupported"]
        assert entry["component"] == "model-vllm-unpublished"
        assert entry["supported"] == []
        assert entry["reason"] == "ARCH_UNSUPPORTED"
        assert gate_env.gg.create_deployment_calls == []

    def test_every_offending_device_is_listed(self, gate_env):
        """The 409 carries the complete offending list (3.4)."""
        seed_vllm_record(gate_env.deployments, "model-vllm-multi",
                         ["arm64_jp6"])
        gate_env.put_device_record("jp4-cam-01", arch="arm64_jp4")
        gate_env.put_device_record("jp5-cam-01", arch="arm64_jp5")
        gate_env.put_device_record("jp6-cam-01", arch="arm64_jp6")

        status, payload = gate_env.deploy_components(
            [{"component_name": "model-vllm-multi",
              "component_version": "1.0.0"}],
            target_devices=["jp4-cam-01", "jp5-cam-01", "jp6-cam-01"])

        assert status == 409
        unsupported = payload["error"]["details"]["unsupported"]
        assert [(e["device"], e["reason"]) for e in unsupported] == [
            ("jp4-cam-01", "JP4_UNSUPPORTED"),
            ("jp5-cam-01", "ARCH_UNSUPPORTED"),
        ]
