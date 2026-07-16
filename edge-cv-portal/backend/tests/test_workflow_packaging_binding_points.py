"""
Unit tests for Component_Packager Camera_Input_Node binding points
(functions/workflow_packaging.py).

Task 10.1 (spec: camera-registry-sync). Packaging a workflow containing a
Camera_Input_Node appends a ``bindingPoints`` entry per camera node to
compiled_pipeline.json — nodeId, nodeType, bindingHint from the
definition, rendered default parameters, and arch-specific slots
(v4l2src ``device`` arg on x86_64/x86_64_nvidia; ``adapterBinding: true``
with empty slots on JP4/JP5; CSI sensor selection on JP6) — and records
the ``has_binding_points`` / ``camera_input_nodes`` discriminator on the
workflow version item. Compiled elements keep their fully rendered
defaults, and workflows without camera nodes serialize exactly as before.
_Requirements: 8.6, 11.5_

Task 10.3 (spec: camera-registry-sync) adds the packaging snapshot tests
(``TestPackagingSnapshots``): the packaged compiled_pipeline.json of a
camera workflow is byte-for-byte the pre-feature compiler serialization
of the same definition plus only the ``bindingPoints`` section, and a
workflow without Camera_Input_Nodes packages byte-identically to the
compiler's own output — pre-feature definitions and deployments are
untouched by the feature.
_Requirements: 11.1, 11.5_
"""
import io
import json
import sys
import uuid
import zipfile
from unittest.mock import MagicMock

import pytest

COMPONENTS_ROOT = "workflows/components"


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


def camera_definition():
    """camera_source -> capture. Every plugin dependency of both nodes is
    LocalServer-bundled on every architecture, so packaging needs no
    curated plugin library seeding."""
    return {
        "schemaVersion": 1,
        "nodes": [
            {"id": "cam", "type": "camera_source", "position": {"x": 0, "y": 0},
             "parameters": {"device": "/dev/video1"}},
            {"id": "cap", "type": "capture", "position": {"x": 200, "y": 0},
             "parameters": {"output_path": "/out"}},
        ],
        "connections": [
            {"id": "c1", "from": {"node": "cam", "port": "out"},
             "to": {"node": "cap", "port": "in"}},
        ],
    }


def cameraless_definition():
    """folder_source -> capture: no Camera_Input_Node anywhere."""
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


class BindingPointsEnv:
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
            "name": "Binding Points Test",
            "account_id": "123456789012",
            "s3_bucket": self.usecase_bucket,
        })

        status, payload = env.invoke("POST", "/workflows", self.user, body={
            "usecase_id": self.usecase_id,
            "name": "camera workflow",
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

    def compiled_pipeline_text(self, arch):
        """The exact compiled_pipeline.json text inside the artifact zip."""
        key = (f"{COMPONENTS_ROOT}/{self.workflow_id}/1/"
               f"{arch}/workflow-{arch}.zip")
        body = self.env.s3.get_object(
            Bucket=self.usecase_bucket, Key=key)["Body"].read()
        with zipfile.ZipFile(io.BytesIO(body)) as zf:
            return zf.read("compiled_pipeline.json").decode("utf-8")

    def compiled_pipeline(self, arch):
        return json.loads(self.compiled_pipeline_text(arch))

    def version_item(self):
        return self.env.stack.tables.versions.get_item(
            Key={"workflow_id": self.workflow_id, "version": 1})["Item"]


# --------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------

class TestCameraBackedTypeIds:
    def test_snake_case_flag_marks_type_camera_backed(self, packaging):
        items = [{"declaration": {"typeId": "custom_rtsp",
                                  "camera_backed": True}}]
        assert packaging.camera_backed_type_ids(items) == {"custom_rtsp"}

    def test_camel_case_flag_marks_type_camera_backed(self, packaging):
        items = [{"declaration": {"typeId": "custom_rtsp",
                                  "cameraBacked": True}}]
        assert packaging.camera_backed_type_ids(items) == {"custom_rtsp"}

    def test_absent_or_false_flag_is_not_camera_backed(self, packaging):
        items = [
            {"declaration": {"typeId": "plain_type"}},
            {"declaration": {"typeId": "flagged_off", "camera_backed": False}},
            {"declaration": None},
            {},
        ]
        assert packaging.camera_backed_type_ids(items) == set()


class TestBindingHintExtraction:
    def test_hint_extracted_from_node_data(self, packaging):
        definition = camera_definition()
        definition["nodes"][0]["data"] = {
            "cameraBindingHint": {"cameraSourceId": "cfg-a1b2",
                                  "cameraName": "Line 1",
                                  "sourceDeviceId": "thing-1"},
        }
        hints = packaging.binding_hints_from_definition(definition)
        assert hints == {"cam": {"cameraSourceId": "cfg-a1b2",
                                 "cameraName": "Line 1",
                                 "sourceDeviceId": "thing-1"}}

    def test_definitions_without_node_data_yield_no_hints(self, packaging):
        assert packaging.binding_hints_from_definition(camera_definition()) == {}
        assert packaging.binding_hints_from_definition({"nodes": []}) == {}


# --------------------------------------------------------------------------
# Packaging a camera workflow emits bindingPoints (8.6)
# --------------------------------------------------------------------------

class TestBindingPointsEmission:
    ARCHS = ["x86_64", "x86_64_nvidia", "arm64_jp5", "arm64_jp6"]

    @pytest.fixture
    def camera_env(self, env, packaging, monkeypatch):
        e = BindingPointsEnv(env, packaging, monkeypatch, camera_definition())
        status, payload = e.package(self.ARCHS)
        assert status == 201, payload
        return e

    def test_v4l2_archs_slot_points_at_the_rendered_device_arg(self, camera_env):
        """On x86_64 / x86_64_nvidia the binding point carries the node's
        rendered default parameters and one slot addressing the v4l2src
        ``device`` argument in this document."""
        for arch in ("x86_64", "x86_64_nvidia"):
            compiled = camera_env.compiled_pipeline(arch)
            points = compiled["bindingPoints"]
            assert len(points) == 1
            point = points[0]
            assert point["nodeId"] == "cam"
            assert point["nodeType"] == "camera_source"
            # Rendered defaults: explicit device + declared gain/exposure.
            assert point["parameters"] == {"device": "/dev/video1",
                                           "gain": 4, "exposure": 5000000}
            assert point["slots"] == [{"param": "device", "segment": 0,
                                       "element": 0, "arg": "device"}]
            # The slot resolves to the fully rendered compiled element.
            element = compiled["segments"][0]["elements"][0]
            assert element["nodeId"] == "cam"
            assert element["factory"] == "v4l2src"
            assert element["args"]["device"] == "/dev/video1"
            assert "bindingHint" not in point  # no hint in the definition

    def test_jp5_binding_point_is_adapter_bound_with_empty_slots(self, camera_env):
        point = camera_env.compiled_pipeline("arm64_jp5")["bindingPoints"][0]
        assert point["adapterBinding"] is True
        assert point["slots"] == []
        assert point["parameters"]["device"] == "/dev/video1"

    def test_jp6_binding_point_selects_the_csi_sensor(self, camera_env):
        point = camera_env.compiled_pipeline("arm64_jp6")["bindingPoints"][0]
        assert point["csiSensorBinding"] is True
        assert point["slots"] == []

    def test_version_item_records_the_binding_discriminator(self, camera_env):
        """has_binding_points: true plus the Camera_Input_Node list with the
        per-arch compiled device paths (the strict-vs-legacy discriminator
        the Deployment_Service reads)."""
        item = camera_env.version_item()
        assert item["has_binding_points"] is True
        nodes = item["camera_input_nodes"]
        assert len(nodes) == 1
        assert nodes[0]["node_id"] == "cam"
        assert nodes[0]["node_type"] == "camera_source"
        assert nodes[0]["compiled_device_paths"] == {
            "x86_64": "/dev/video1", "x86_64_nvidia": "/dev/video1"}
        assert "binding_hint" not in nodes[0]

    def test_portal_compiled_document_carries_the_same_binding_points(
            self, camera_env):
        """The compiled document persisted to portal S3 matches the one in
        the artifact zip, bindingPoints included."""
        item = camera_env.version_item()
        key = item["compiled_arch_keys"]["x86_64"]
        body = camera_env.env.s3.get_object(
            Bucket=camera_env.env.bucket, Key=key)["Body"].read()
        assert json.loads(body) == camera_env.compiled_pipeline("x86_64")


# --------------------------------------------------------------------------
# Workflows without Camera_Input_Nodes are untouched (11.5)
# --------------------------------------------------------------------------

class TestCameralessWorkflow:
    def test_no_binding_points_and_discriminator_false(self, env, packaging,
                                                       monkeypatch):
        e = BindingPointsEnv(env, packaging, monkeypatch,
                             cameraless_definition())
        status, payload = e.package(["x86_64"])
        assert status == 201, payload

        compiled = e.compiled_pipeline("x86_64")
        assert "bindingPoints" not in compiled

        item = e.version_item()
        assert item["has_binding_points"] is False
        assert item["camera_input_nodes"] == []


# --------------------------------------------------------------------------
# Task 10.3 - packaging snapshot tests (11.1, 11.5)
#
# "Pre-feature output" is reproduced exactly the way the packager built
# compiled_pipeline.json before this feature: parse the stored
# Workflow_Definition, compile with the same CompileContext, and take the
# compiler's canonical serialization (CompiledPipeline.to_json). The
# snapshots assert the packaged document is that output byte-for-byte,
# plus only the appended bindingPoints section for camera workflows.
# --------------------------------------------------------------------------

def pre_feature_compiled(env, workflow_id, arch):
    """Compile the stored definition the pre-feature way: same stored
    document, same CompileContext, plain compiler output."""
    from workflow_core.compiler import CompileContext
    from workflow_core.compiler import compile as compile_workflow
    from workflow_core.catalog.custom import resolve_catalog
    from workflow_core.serializer import parse

    version_item = env.stack.tables.versions.get_item(
        Key={"workflow_id": workflow_id, "version": 1})["Item"]
    definition_json = env.s3.get_object(
        Bucket=env.bucket,
        Key=version_item["s3_definition_key"])["Body"].read().decode("utf-8")

    parse_result = parse(definition_json)
    assert parse_result.ok
    context = CompileContext(workflow_id=workflow_id, workflow_version="1")
    compiled = compile_workflow(parse_result.graph, arch, context,
                                simulation=False, catalog=resolve_catalog([]))
    assert not isinstance(compiled, list), compiled
    return compiled


class TestPackagingSnapshots:
    """Packaged compiled_pipeline.json versus the pre-feature snapshot
    (Requirements 11.1, 11.5)."""

    ARCHS = ["x86_64", "arm64_jp5"]

    def test_camera_workflow_is_pre_feature_output_plus_binding_points(
            self, env, packaging, monkeypatch):
        """A camera workflow's compiled_pipeline.json is byte-for-byte the
        pre-feature compiler serialization with only the bindingPoints
        section added: identical rendered segments, defaults, and
        serialization form on every architecture."""
        definition = camera_definition()
        definition["nodes"][0]["data"] = {
            "cameraBindingHint": {"cameraSourceId": "cfg-a1b2",
                                  "cameraName": "Line 1",
                                  "sourceDeviceId": "thing-1"},
        }
        e = BindingPointsEnv(env, packaging, monkeypatch, definition)
        status, payload = e.package(self.ARCHS)
        assert status == 201, payload

        for arch in self.ARCHS:
            packaged_text = e.compiled_pipeline_text(arch)
            packaged_doc = json.loads(packaged_text)
            binding_points = packaged_doc.pop("bindingPoints")

            # The document minus bindingPoints IS the pre-feature output:
            # same segments, same rendered element args and defaults.
            expected = pre_feature_compiled(env, e.workflow_id, arch)
            assert packaged_doc == expected.to_dict()

            # Byte-level: pre-feature dict + bindingPoints, re-serialized
            # in the compiler's canonical form, reproduces the packaged
            # file exactly - nothing else changed.
            expected_doc = expected.to_dict()
            expected_doc["bindingPoints"] = binding_points
            assert packaged_text == json.dumps(
                expected_doc, sort_keys=True, indent=2, ensure_ascii=True)

            # The appended section is the camera node's binding point,
            # hint included.
            (point,) = binding_points
            assert point["nodeId"] == "cam"
            assert point["bindingHint"] == {"cameraSourceId": "cfg-a1b2",
                                            "cameraName": "Line 1",
                                            "sourceDeviceId": "thing-1"}

        # Version-item discriminator recorded for the camera workflow.
        item = e.version_item()
        assert item["has_binding_points"] is True
        assert [n["node_id"] for n in item["camera_input_nodes"]] == ["cam"]

    def test_cameraless_workflow_is_byte_identical_to_pre_feature_output(
            self, env, packaging, monkeypatch):
        """Packaging a workflow without Camera_Input_Nodes produces a
        compiled_pipeline.json byte-identical to the compiler's own
        serialization - exactly what packaging produced before this
        feature (Requirements 11.1, 11.5)."""
        e = BindingPointsEnv(env, packaging, monkeypatch,
                             cameraless_definition())
        status, payload = e.package(self.ARCHS)
        assert status == 201, payload

        for arch in self.ARCHS:
            expected = pre_feature_compiled(env, e.workflow_id, arch)
            assert e.compiled_pipeline_text(arch) == expected.to_json()

        # Discriminator recorded as the legacy/no-binding shape.
        item = e.version_item()
        assert item["has_binding_points"] is False
        assert item["camera_input_nodes"] == []
