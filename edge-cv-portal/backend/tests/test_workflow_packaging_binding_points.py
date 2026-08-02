"""
Unit tests for Component_Packager Camera_Input_Node binding points
(functions/workflow_packaging.py).

Task 10.1 (spec: camera-registry-sync) established the ``bindingPoints``
seam. csi-icam-input-nodes (task 2.1) replaces the removed generic
``camera_source`` node with the two typed camera inputs:

- ``icam_source`` (V4L2 smart camera) carries the generic single
  ``device`` slot binding the v4l2src ``device`` argument on every
  physical device architecture (Property 6, Requirement 4.2);
- ``csi_camera_source`` (NVIDIA CSI) carries ``csiSensorBinding: true``
  with empty slots and the rendered ``gain``/``exposure`` parameters on
  every physical device architecture (Property 5, Requirement 4.1).

A workflow containing neither typed camera input packages byte-identically
to its post-removal pre-feature output (Property 7, Requirement 4.3), and
the version item records the ``has_binding_points`` / ``camera_input_nodes``
discriminator either way.
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


def icam_definition():
    """icam_source -> capture. Every plugin dependency of both nodes is
    LocalServer-bundled on every architecture, so packaging needs no
    curated plugin library seeding."""
    return {
        "schemaVersion": 1,
        "nodes": [
            {"id": "cam", "type": "icam_source", "position": {"x": 0, "y": 0},
             "parameters": {"device": "/dev/video1"}},
            {"id": "cap", "type": "capture", "position": {"x": 200, "y": 0},
             "parameters": {"output_path": "/out"}},
        ],
        "connections": [
            {"id": "c1", "from": {"node": "cam", "port": "out"},
             "to": {"node": "cap", "port": "in"}},
        ],
    }


def csi_definition():
    """csi_camera_source -> capture."""
    return {
        "schemaVersion": 1,
        "nodes": [
            {"id": "csi", "type": "csi_camera_source",
             "position": {"x": 0, "y": 0},
             "parameters": {"gain": 10, "exposure": 16000000}},
            {"id": "cap", "type": "capture", "position": {"x": 200, "y": 0},
             "parameters": {"output_path": "/out"}},
        ],
        "connections": [
            {"id": "c1", "from": {"node": "csi", "port": "out"},
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
        key = (f"{COMPONENTS_ROOT}/{self.workflow_id}/1/1.0.0/"
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
        definition = icam_definition()
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
        assert packaging.binding_hints_from_definition(icam_definition()) == {}
        assert packaging.binding_hints_from_definition({"nodes": []}) == {}


# --------------------------------------------------------------------------
# Packaging an ICAM workflow emits a device slot binding point
# (csi-icam-input-nodes Property 6, Requirement 4.2)
# --------------------------------------------------------------------------

class TestIcamBindingPointsEmission:
    ARCHS = ["x86_64", "x86_64_nvidia", "arm64_jp4", "arm64_jp5", "arm64_jp6"]

    @pytest.fixture
    def camera_env(self, env, packaging, monkeypatch):
        e = BindingPointsEnv(env, packaging, monkeypatch, icam_definition())
        status, payload = e.package(self.ARCHS)
        assert status == 201, payload
        return e

    def test_every_device_arch_slot_points_at_the_rendered_device_arg(
            self, camera_env):
        """On every physical device architecture the ICAM binding point
        carries the node's rendered device parameter and exactly one slot
        addressing the v4l2src ``device`` argument in this document."""
        for arch in self.ARCHS:
            compiled = camera_env.compiled_pipeline(arch)
            points = compiled["bindingPoints"]
            assert len(points) == 1
            point = points[0]
            assert point["nodeId"] == "cam"
            assert point["nodeType"] == "icam_source"
            assert point["parameters"] == {"device": "/dev/video1"}
            assert point["slots"] == [{"param": "device", "segment": 0,
                                       "element": 0, "arg": "device"}]
            # No CSI/adapter/aravis markers on an ICAM point.
            assert "csiSensorBinding" not in point
            assert "adapterBinding" not in point
            assert "aravisBinding" not in point
            # The slot resolves to the fully rendered compiled element.
            element = compiled["segments"][0]["elements"][0]
            assert element["nodeId"] == "cam"
            assert element["factory"] == "v4l2src"
            assert element["args"]["device"] == "/dev/video1"
            assert "bindingHint" not in point  # no hint in the definition

    def test_version_item_records_the_binding_discriminator(self, camera_env):
        item = camera_env.version_item()
        assert item["has_binding_points"] is True
        nodes = item["camera_input_nodes"]
        assert len(nodes) == 1
        assert nodes[0]["node_id"] == "cam"
        assert nodes[0]["node_type"] == "icam_source"
        assert nodes[0]["compiled_device_paths"] == {
            arch: "/dev/video1" for arch in self.ARCHS}

    def test_portal_compiled_document_carries_the_same_binding_points(
            self, camera_env):
        item = camera_env.version_item()
        key = item["compiled_arch_keys"]["x86_64"]
        body = camera_env.env.s3.get_object(
            Bucket=camera_env.env.bucket, Key=key)["Body"].read()
        assert json.loads(body) == camera_env.compiled_pipeline("x86_64")


# --------------------------------------------------------------------------
# Packaging a CSI workflow emits a csiSensorBinding point
# (csi-icam-input-nodes Property 5, Requirement 4.1)
# --------------------------------------------------------------------------

class TestCsiBindingPointsEmission:
    ARCHS = ["x86_64", "x86_64_nvidia", "arm64_jp4", "arm64_jp5", "arm64_jp6"]

    @pytest.fixture
    def csi_env(self, env, packaging, monkeypatch):
        e = BindingPointsEnv(env, packaging, monkeypatch, csi_definition())
        status, payload = e.package(self.ARCHS)
        assert status == 201, payload
        return e

    def test_every_arch_marks_csi_sensor_binding_with_empty_slots(
            self, csi_env):
        for arch in self.ARCHS:
            points = csi_env.compiled_pipeline(arch)["bindingPoints"]
            assert len(points) == 1, arch
            point = points[0]
            assert point["nodeId"] == "csi"
            assert point["nodeType"] == "csi_camera_source"
            assert point["csiSensorBinding"] is True
            assert point["slots"] == []
            # Rendered gain/exposure carried in parameters (Requirement 4.1).
            assert point["parameters"] == {"gain": 10, "exposure": 16000000}
            assert "adapterBinding" not in point
            assert "aravisBinding" not in point

    def test_version_item_records_the_csi_node(self, csi_env):
        item = csi_env.version_item()
        assert item["has_binding_points"] is True
        nodes = item["camera_input_nodes"]
        assert len(nodes) == 1
        assert nodes[0]["node_id"] == "csi"
        assert nodes[0]["node_type"] == "csi_camera_source"
        # No parameter lands in an element argument -> no device paths.
        assert nodes[0]["compiled_device_paths"] == {}


# --------------------------------------------------------------------------
# Workflows without Camera_Input_Nodes are untouched
# (csi-icam-input-nodes Property 7, Requirement 4.3)
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
# Packaging snapshot tests (Property 7, Requirements 4.3, 8.4)
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
    """Packaged compiled_pipeline.json versus the pre-feature snapshot."""

    ARCHS = ["x86_64", "arm64_jp5"]

    def test_camera_workflow_is_pre_feature_output_plus_binding_points(
            self, env, packaging, monkeypatch):
        definition = icam_definition()
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

            expected = pre_feature_compiled(env, e.workflow_id, arch)
            assert packaged_doc == expected.to_dict()

            expected_doc = expected.to_dict()
            expected_doc["bindingPoints"] = binding_points
            assert packaged_text == json.dumps(
                expected_doc, sort_keys=True, indent=2, ensure_ascii=True)

            (point,) = binding_points
            assert point["nodeId"] == "cam"
            assert point["bindingHint"] == {"cameraSourceId": "cfg-a1b2",
                                            "cameraName": "Line 1",
                                            "sourceDeviceId": "thing-1"}

        item = e.version_item()
        assert item["has_binding_points"] is True
        assert [n["node_id"] for n in item["camera_input_nodes"]] == ["cam"]

    def test_cameraless_workflow_is_byte_identical_to_pre_feature_output(
            self, env, packaging, monkeypatch):
        e = BindingPointsEnv(env, packaging, monkeypatch,
                             cameraless_definition())
        status, payload = e.package(self.ARCHS)
        assert status == 201, payload

        for arch in self.ARCHS:
            expected = pre_feature_compiled(env, e.workflow_id, arch)
            assert e.compiled_pipeline_text(arch) == expected.to_json()

        item = e.version_item()
        assert item["has_binding_points"] is False
        assert item["camera_input_nodes"] == []


# --------------------------------------------------------------------------
# Aravis binding points (aravis-camera-input Requirements 4.1, 4.2), plus a
# mixed CSI + ICAM + Aravis definition (csi-icam-input-nodes).
# --------------------------------------------------------------------------

ARAVIS_HINT = {"cameraSourceId": "arv-1a2b3c4d5e6f",
               "cameraName": "Basler GigE Line 2",
               "sourceDeviceId": "thing-2"}


def aravis_definition(hint=None):
    aravis_node = {"id": "arv", "type": "aravis_camera_source",
                   "position": {"x": 0, "y": 0},
                   "parameters": {"camera_id": "Aravis-Fake-GV01"}}
    if hint is not None:
        aravis_node["data"] = {"cameraBindingHint": hint}
    return {
        "schemaVersion": 1,
        "nodes": [
            aravis_node,
            {"id": "cap", "type": "capture", "position": {"x": 200, "y": 0},
             "parameters": {"output_path": "/out"}},
        ],
        "connections": [
            {"id": "c1", "from": {"node": "arv", "port": "out"},
             "to": {"node": "cap", "port": "in"}},
        ],
    }


def mixed_camera_definition():
    """icam_source, csi_camera_source, and aravis_camera_source in one
    definition: all three Camera_Input_Node types."""
    return {
        "schemaVersion": 1,
        "nodes": [
            {"id": "cam", "type": "icam_source", "position": {"x": 0, "y": 0},
             "parameters": {"device": "/dev/video1"}},
            {"id": "csi", "type": "csi_camera_source",
             "position": {"x": 0, "y": 100},
             "parameters": {"gain": 7}},
            {"id": "arv", "type": "aravis_camera_source",
             "position": {"x": 0, "y": 200},
             "parameters": {"camera_id": "Aravis-Fake-GV01", "gain": 10}},
            {"id": "cap1", "type": "capture", "position": {"x": 200, "y": 0},
             "parameters": {"output_path": "/out1"}},
            {"id": "cap2", "type": "capture", "position": {"x": 200, "y": 100},
             "parameters": {"output_path": "/out2"}},
            {"id": "cap3", "type": "capture", "position": {"x": 200, "y": 200},
             "parameters": {"output_path": "/out3"}},
        ],
        "connections": [
            {"id": "c1", "from": {"node": "cam", "port": "out"},
             "to": {"node": "cap1", "port": "in"}},
            {"id": "c2", "from": {"node": "csi", "port": "out"},
             "to": {"node": "cap2", "port": "in"}},
            {"id": "c3", "from": {"node": "arv", "port": "out"},
             "to": {"node": "cap3", "port": "in"}},
        ],
    }


class TestAravisBindingPoints:
    ARCHS = ["x86_64", "x86_64_nvidia", "arm64_jp4", "arm64_jp5", "arm64_jp6"]

    @pytest.fixture
    def aravis_env(self, env, packaging, monkeypatch):
        e = BindingPointsEnv(env, packaging, monkeypatch,
                             aravis_definition(hint=ARAVIS_HINT))
        status, payload = e.package(self.ARCHS)
        assert status == 201, payload
        return e

    def test_aravis_binding_marker_on_every_device_arch(self, aravis_env):
        for arch in self.ARCHS:
            points = aravis_env.compiled_pipeline(arch)["bindingPoints"]
            assert len(points) == 1, arch
            point = points[0]
            assert point["nodeId"] == "arv"
            assert point["nodeType"] == "aravis_camera_source"
            assert point["aravisBinding"] is True
            assert point["slots"] == []
            assert point["parameters"] == {"camera_id": "Aravis-Fake-GV01",
                                           "gain": 4, "exposure": 5000000}
            assert point["bindingHint"] == ARAVIS_HINT
            assert "adapterBinding" not in point
            assert "csiSensorBinding" not in point

    def test_version_item_records_the_aravis_node(self, aravis_env):
        item = aravis_env.version_item()
        assert item["has_binding_points"] is True
        nodes = item["camera_input_nodes"]
        assert len(nodes) == 1
        assert nodes[0]["node_id"] == "arv"
        assert nodes[0]["node_type"] == "aravis_camera_source"
        assert nodes[0]["binding_hint"] == ARAVIS_HINT
        assert nodes[0]["compiled_device_paths"] == {}

    def test_mixed_definition_emits_all_nodes_entries(self, env, packaging,
                                                      monkeypatch):
        """icam_source, csi_camera_source and aravis_camera_source in one
        definition each get their own binding point with their own
        marker/slots; none disturbs the others."""
        e = BindingPointsEnv(env, packaging, monkeypatch,
                             mixed_camera_definition())
        status, payload = e.package(["x86_64", "arm64_jp5"])
        assert status == 201, payload

        for arch in ("x86_64", "arm64_jp5"):
            points = {p["nodeId"]: p
                      for p in e.compiled_pipeline(arch)["bindingPoints"]}
            assert set(points) == {"cam", "csi", "arv"}

            aravis = points["arv"]
            assert aravis["aravisBinding"] is True
            assert aravis["slots"] == []
            assert aravis["parameters"] == {"camera_id": "Aravis-Fake-GV01",
                                            "gain": 10, "exposure": 5000000}

            csi = points["csi"]
            assert csi["csiSensorBinding"] is True
            assert csi["slots"] == []
            assert csi["parameters"] == {"gain": 7, "exposure": 5000000}
            assert "aravisBinding" not in csi

            cam = points["cam"]
            assert "aravisBinding" not in cam
            assert "csiSensorBinding" not in cam
            # icam_source keeps its v4l2src device slot on every arch.
            (slot,) = cam["slots"]
            assert slot["param"] == "device"
            assert slot["arg"] == "device"
            compiled = e.compiled_pipeline(arch)
            element = (compiled["segments"][slot["segment"]]
                       ["elements"][slot["element"]])
            assert element["nodeId"] == "cam"
            assert element["factory"] == "v4l2src"
            assert element["args"]["device"] == "/dev/video1"

        item = e.version_item()
        assert item["has_binding_points"] is True
        assert {(n["node_id"], n["node_type"])
                for n in item["camera_input_nodes"]} == {
                    ("cam", "icam_source"),
                    ("csi", "csi_camera_source"),
                    ("arv", "aravis_camera_source")}

    def test_aravis_free_definition_output_is_unchanged(self, env, packaging,
                                                        monkeypatch):
        """An ICAM camera workflow's compiled document is exactly the
        pre-feature output plus the bindingPoints section - no aravisBinding
        marker anywhere."""
        e = BindingPointsEnv(env, packaging, monkeypatch, icam_definition())
        status, payload = e.package(["x86_64", "arm64_jp5"])
        assert status == 201, payload

        for arch in ("x86_64", "arm64_jp5"):
            packaged_text = e.compiled_pipeline_text(arch)
            packaged_doc = json.loads(packaged_text)
            binding_points = packaged_doc.pop("bindingPoints")
            assert all("aravisBinding" not in p for p in binding_points)

            expected_doc = pre_feature_compiled(env, e.workflow_id,
                                                arch).to_dict()
            assert packaged_doc == expected_doc
            expected_doc["bindingPoints"] = binding_points
            assert packaged_text == json.dumps(
                expected_doc, sort_keys=True, indent=2, ensure_ascii=True)
