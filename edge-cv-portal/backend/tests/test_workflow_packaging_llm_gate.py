"""
Unit tests for the Component_Packager LLM architecture packaging gate
and the ``has_llm_inference`` version-item discriminator
(functions/workflow_packaging.py).

Task 6.1 (spec: vllm-triton-inference). ``llm_arch_gate_findings`` is a
pure gate alongside ``custom_plugin_gate_findings``: one finding
``{code: 'V6_LLM_ARCH_UNSUPPORTED', nodeId, arch}`` per
(``llm_inference`` node, requested architecture outside
``VLLM_ARCHITECTURES``), empty when the workflow has no
``llm_inference`` node. Findings reject the packaging request (409,
complete list, no component version registered). On success the
packager writes ``has_llm_inference`` and the packaged architecture
list onto the workflow version item for the deployment gate.
_Requirements: 7.1, 7.2, 8.1_
"""
import json
import sys
import uuid
from unittest.mock import MagicMock

import pytest


# --------------------------------------------------------------------------
# Fixtures / helpers
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def packaging(aws_stack):
    """Import workflow_packaging inside the moto mock so its module-level
    boto3 clients (portal DynamoDB / S3) are intercepted."""
    sys.modules.pop("workflow_packaging", None)
    import workflow_packaging

    return workflow_packaging


def make_deployable_greengrass():
    gg = MagicMock(name="greengrassv2")
    gg.create_component_version.return_value = {
        "arn": f"arn:aws:greengrass:us-east-1:123456789012:components:test:versions:{uuid.uuid4()}"
    }
    gg.describe_component.return_value = {
        "status": {"componentState": "DEPLOYABLE", "message": "simulated"}
    }
    return gg


def llm_definition():
    """folder_source -> model_inference -> llm_inference -> mqtt_publish.
    Compiles for arm64_jp6 with no curated plugin dependencies; the
    llm_inference node has no mapping for x86_64/x86_64_nvidia/arm64_jp4."""
    return {
        "schemaVersion": 1,
        "nodes": [
            {"id": "src", "type": "folder_source", "position": {"x": 0, "y": 0},
             "parameters": {"location": "/data/images"}},
            {"id": "inf", "type": "model_inference", "position": {"x": 200, "y": 0},
             "parameters": {"modelName": "defect-model"}},
            {"id": "llm", "type": "llm_inference", "position": {"x": 400, "y": 0},
             "parameters": {"modelName": "opt-125m",
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


def llm_free_definition():
    """folder_source -> capture: no llm_inference node anywhere."""
    return {
        "schemaVersion": 1,
        "nodes": [
            {"id": "src", "type": "folder_source", "position": {"x": 0, "y": 0},
             "parameters": {"location": "/data/images"}},
            {"id": "cap", "type": "capture", "position": {"x": 200, "y": 0},
             "parameters": {"output_path": "/out"}},
        ],
        "connections": [
            {"id": "c1", "from": {"node": "src", "port": "out"},
             "to": {"node": "cap", "port": "in"}},
        ],
    }


class LlmPackagingEnv:
    """Packaging harness: a validated workflow version, a Use_Case with an
    S3 bucket, and patched Use_Case-account clients."""

    def __init__(self, env, packaging, monkeypatch, definition):
        self.env = env
        self.packaging = packaging
        monkeypatch.setattr(packaging, "COMPONENT_STATUS_POLL_SECONDS", 0)

        self.user = env.make_user(role="UseCaseAdmin")
        self.usecase_bucket = f"usecase-bucket-{uuid.uuid4()}"
        env.s3.create_bucket(Bucket=self.usecase_bucket)
        self.usecase_id = f"uc-{uuid.uuid4()}"
        env.stack.tables.usecases.put_item(Item={
            "usecase_id": self.usecase_id,
            "name": "LLM Gate Test",
            "account_id": "123456789012",
            "s3_bucket": self.usecase_bucket,
        })

        status, payload = env.invoke("POST", "/workflows", self.user, body={
            "usecase_id": self.usecase_id,
            "name": "llm workflow",
            "definition": definition,
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

        monkeypatch.setattr(packaging, "get_usecase_client",
                            fake_get_usecase_client)

    def package(self, architectures):
        event = self.env.event(
            "POST", "/workflows/{id}/package", self.user,
            workflow_id=self.workflow_id,
            body={"architectures": architectures},
        )
        response = self.packaging.handler(event, None)
        return response["statusCode"], json.loads(response["body"])

    def version_item(self):
        return self.env.stack.tables.versions.get_item(
            Key={"workflow_id": self.workflow_id, "version": 1})["Item"]


# --------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------

class TestGatherLlmInferenceNodeIds:
    def test_extracts_llm_nodes_in_definition_order(self, packaging):
        definition = {"nodes": [
            {"id": "a", "type": "llm_inference"},
            {"id": "b", "type": "model_inference"},
            {"id": "c", "type": "llm_inference"},
        ]}
        assert packaging.gather_llm_inference_node_ids(definition) == ["a", "c"]

    def test_empty_for_llm_free_definitions(self, packaging):
        assert packaging.gather_llm_inference_node_ids(llm_free_definition()) == []
        assert packaging.gather_llm_inference_node_ids({}) == []
        assert packaging.gather_llm_inference_node_ids({"nodes": None}) == []

    def test_tolerates_malformed_node_entries(self, packaging):
        definition = {"nodes": ["junk", {"type": "llm_inference"},
                                {"id": "ok", "type": "llm_inference"}]}
        assert packaging.gather_llm_inference_node_ids(definition) == ["ok"]


class TestLlmArchGateFindings:
    def test_no_llm_node_yields_no_findings_for_any_archs(self, packaging):
        """Requirement 8.1: pre-feature workflows contribute zero findings."""
        archs = ["x86_64", "x86_64_nvidia", "arm64_jp4", "arm64_jp5", "arm64_jp6"]
        assert packaging.llm_arch_gate_findings(llm_free_definition(), archs) == []

    def test_vllm_archs_only_yields_no_findings(self, packaging):
        from workflow_core.catalog import VLLM_ARCHITECTURES

        findings = packaging.llm_arch_gate_findings(
            llm_definition(), list(VLLM_ARCHITECTURES))
        assert findings == []

    def test_one_finding_per_node_and_unsupported_arch(self, packaging):
        """Requirement 7.2: the complete (node, arch) finding list."""
        findings = packaging.llm_arch_gate_findings(
            llm_definition(), ["arm64_jp6", "x86_64", "arm64_jp4"])
        assert [(f["nodeId"], f["arch"]) for f in findings] == [
            ("llm", "x86_64"), ("llm", "arm64_jp4")]
        for finding in findings:
            assert finding["code"] == "V6_LLM_ARCH_UNSUPPORTED"
            assert "llm" in finding["message"]
            assert finding["arch"] in finding["message"]

    def test_cross_product_over_multiple_llm_nodes(self, packaging):
        definition = {"nodes": [
            {"id": "llm1", "type": "llm_inference"},
            {"id": "llm2", "type": "llm_inference"},
        ]}
        findings = packaging.llm_arch_gate_findings(
            definition, ["x86_64_nvidia", "arm64_jp4"])
        assert {(f["nodeId"], f["arch"]) for f in findings} == {
            ("llm1", "x86_64_nvidia"), ("llm1", "arm64_jp4"),
            ("llm2", "x86_64_nvidia"), ("llm2", "arm64_jp4")}


# --------------------------------------------------------------------------
# Packaging handler: 409 rejection and version-item discriminator
# --------------------------------------------------------------------------

class TestLlmGateRejection:
    @pytest.fixture
    def llm_env(self, env, packaging, monkeypatch):
        return LlmPackagingEnv(env, packaging, monkeypatch, llm_definition())

    def test_unsupported_arch_rejected_409_with_complete_findings(self, llm_env):
        """Requirement 7.2: 409 identifying the node and every unsupported
        requested architecture; no component version registered."""
        status, payload = llm_env.package(["arm64_jp6", "x86_64", "arm64_jp4"])
        assert status == 409
        assert payload["error"]["code"] == "V6_LLM_ARCH_UNSUPPORTED"
        findings = payload["error"]["details"]["findings"]
        assert {(f["nodeId"], f["arch"]) for f in findings} == {
            ("llm", "x86_64"), ("llm", "arm64_jp4")}
        llm_env.greengrass.create_component_version.assert_not_called()
        item = llm_env.version_item()
        assert not item.get("component_arn")
        assert "has_llm_inference" not in item
        assert "packaged_architectures" not in item

    def test_jp6_only_request_packages_and_records_discriminator(self, llm_env):
        """Requirement 7.1: success writes has_llm_inference and the
        packaged architecture list onto the workflow version item."""
        status, payload = llm_env.package(["arm64_jp6"])
        assert status == 201, payload
        item = llm_env.version_item()
        assert item["has_llm_inference"] is True
        assert item["packaged_architectures"] == ["arm64_jp6"]
        assert "arm64_jp6" in item["compiled_arch_keys"]


class TestLlmFreeWorkflowDiscriminator:
    def test_llm_free_workflow_records_discriminator_false(self, env, packaging,
                                                           monkeypatch):
        """Requirement 8.1: llm-free workflows package for any architecture
        with has_llm_inference recorded false."""
        harness = LlmPackagingEnv(env, packaging, monkeypatch,
                                  llm_free_definition())
        status, payload = harness.package(["x86_64", "arm64_jp4"])
        assert status == 201, payload
        item = harness.version_item()
        assert item["has_llm_inference"] is False
        assert item["packaged_architectures"] == ["x86_64", "arm64_jp4"]
