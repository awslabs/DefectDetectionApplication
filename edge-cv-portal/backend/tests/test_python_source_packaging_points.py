"""Packaging example and preservation tests for the Custom Python
source binding point (custom-python-source task 3.3).

Example tests: the ``pythonSourceBinding`` bindingPoints entry carries
the node id, node type, and the rendered ``allowed_uri_prefixes``
parameter with empty slots, and omits ``code``/``requirements`` (they
ship as artifact files).

Preservation tests: a source-free document's packaging output is
byte-identical to today's output — the pre-feature oracle is
reconstructed by construction over the unchanged code paths, the same
way test_property_aravis_free_packaging_identity.py (which this suite
re-runs as the packaging-identity oracle) pins its structure: a
binding-point-free document IS the compiler's own serialization, a
camera document is that serialization plus only the pre-existing
bindingPoints section, and no ``pythonSourceBinding`` key appears
anywhere in any source-free document.

_Requirements: 9.1, 9.2, 11.5_

The module is imported through the shared moto-backed session fixture
only so its module-level boto3 clients bind to the mock (same re-import
pattern as the other packaging tests). All functions under test are
pure — no AWS calls are made.
"""

from __future__ import annotations

import json
import sys

import pytest

from workflow_core.serializer import parse
from workflow_core.serializer.models import Node, Position, WorkflowGraph
from workflow_core.compiler import compile as compile_workflow, CompileContext
from workflow_core.catalog import DEVICE_ARCHITECTURES
from workflow_core.catalog.custom import resolve_catalog


@pytest.fixture(scope="module")
def packaging(aws_stack):
    """Import workflow_packaging inside the moto mock so its module-level
    boto3 clients (DynamoDB / S3 / KMS) are intercepted."""
    sys.modules.pop("workflow_packaging", None)
    import workflow_packaging

    return workflow_packaging


SOURCE_TYPE = "custom_python_source"

SOURCE_CODE = ("def produce_frame(context):\n"
               "    import dda_frames\n"
               "    payload = context.get(\"payload_json\") or {}\n"
               "    return dda_frames.load_image(payload[\"image_url\"])\n")

PREFIXES = "s3://plant-images/\nhttps://mes.local/"

#: Binding-point entry keys the pre-feature packager could produce
#: (camera-registry-sync / aravis-camera-input): never
#: pythonSourceBinding.
PRE_FEATURE_POINT_KEYS = {"nodeId", "nodeType", "parameters", "slots",
                          "bindingHint", "adapterBinding", "csiSensorBinding",
                          "aravisBinding"}


def _definition(source_node):
    """A minimal valid definition: one source node feeding one capture."""
    return {
        "schemaVersion": 1,
        "nodes": [
            source_node,
            {"id": "cap0", "type": "capture",
             "position": {"x": 0.0, "y": 200.0},
             "parameters": {"output_path": "/out/0"}},
        ],
        "connections": [{
            "id": "c0",
            "from": {"node": source_node["id"], "port": "out"},
            "to": {"node": "cap0", "port": "in"},
        }],
    }


def _packaging_pipeline(packaging, definition):
    """Drive the packager's pure binding-point pipeline exactly as the
    handler does: gather camera + python source nodes, then per device
    architecture build binding points and the packaged document text.
    Returns (camera_nodes, source_nodes, per-arch results)."""
    parse_result = parse(json.dumps(definition))
    assert parse_result.ok, parse_result.error
    graph = parse_result.graph

    camera_nodes = packaging.gather_camera_input_nodes(graph, set())
    source_nodes = packaging.gather_python_source_nodes(graph)
    hints = packaging.binding_hints_from_definition(definition)
    catalog = resolve_catalog([])
    descriptors_by_id = {d.type_id: d for d in catalog}
    context = CompileContext(workflow_id="wf-t33", workflow_version="1")

    results = {}
    for arch in DEVICE_ARCHITECTURES:
        compiled = compile_workflow(graph, arch, context, simulation=False,
                                    catalog=catalog)
        assert not isinstance(compiled, list), (
            "compilation failed on {}: {}".format(arch, compiled))
        compiled_dict = compiled.to_dict()
        points = packaging.build_binding_points(
            camera_nodes + source_nodes, compiled_dict, arch, hints,
            descriptors_by_id)
        results[arch] = (compiled, points,
                         packaging.compiled_document_json(compiled, points))
    return camera_nodes, source_nodes, results


def assert_no_python_source_key(value):
    """No pythonSourceBinding key anywhere in the document tree."""
    if isinstance(value, dict):
        assert "pythonSourceBinding" not in value
        for child in value.values():
            assert_no_python_source_key(child)
    elif isinstance(value, list):
        for child in value:
            assert_no_python_source_key(child)


# ---------------------------------------------------------------------------
# Example tests: pythonSourceBinding point shape (9.1, 9.2)
# ---------------------------------------------------------------------------

def test_python_source_binding_point_shape(packaging):
    """The pythonSourceBinding point carries the node id, node type, and
    the node's allowed_uri_prefixes value with empty slots — and omits
    code/requirements (they ship as artifact files)."""
    definition = _definition({
        "id": "src1", "type": SOURCE_TYPE,
        "position": {"x": 0.0, "y": 0.0},
        "parameters": {"code": SOURCE_CODE,
                       "allowed_uri_prefixes": PREFIXES},
    })
    _, source_nodes, results = _packaging_pipeline(packaging, definition)
    assert [n.id for n in source_nodes] == ["src1"]

    for arch, (compiled, points, packaged_text) in results.items():
        # Exact entry shape on every device architecture — by full-dict
        # equality, so code/requirements can never ride along.
        assert points == [{
            "nodeId": "src1",
            "nodeType": SOURCE_TYPE,
            "pythonSourceBinding": True,
            "parameters": {"allowed_uri_prefixes": PREFIXES},
            "slots": [],
        }], "unexpected binding points on {}".format(arch)

        # The packaged document carries exactly that bindingPoints
        # section, canonically serialized alongside the compiler output.
        document = json.loads(packaged_text)
        assert document["bindingPoints"] == points
        expected_doc = compiled.to_dict()
        expected_doc["bindingPoints"] = points
        assert packaged_text == json.dumps(
            expected_doc, sort_keys=True, indent=2, ensure_ascii=True)


def test_python_source_binding_point_default_prefixes(packaging):
    """A node without an explicit allowed_uri_prefixes value renders the
    declared default (empty string = unrestricted) into the point."""
    definition = _definition({
        "id": "srcD", "type": SOURCE_TYPE,
        "position": {"x": 0.0, "y": 0.0},
        "parameters": {"code": SOURCE_CODE},
    })
    _, _, results = _packaging_pipeline(packaging, definition)
    for arch, (_, points, _) in results.items():
        assert points == [{
            "nodeId": "srcD",
            "nodeType": SOURCE_TYPE,
            "pythonSourceBinding": True,
            "parameters": {"allowed_uri_prefixes": ""},
            "slots": [],
        }], "unexpected binding points on {}".format(arch)


def test_camera_and_source_points_coexist(packaging):
    """A workflow with both a Camera_Input_Node and a Custom Python
    source emits the camera point unchanged plus the source point —
    camera entries never gain the pythonSourceBinding marker."""
    definition = {
        "schemaVersion": 1,
        "nodes": [
            {"id": "cam0", "type": "icam_source",
             "position": {"x": 0.0, "y": 0.0},
             "parameters": {"device": "/dev/video0"}},
            {"id": "cap0", "type": "capture",
             "position": {"x": 0.0, "y": 200.0},
             "parameters": {"output_path": "/out/0"}},
            {"id": "src1", "type": SOURCE_TYPE,
             "position": {"x": 100.0, "y": 0.0},
             "parameters": {"code": SOURCE_CODE,
                            "allowed_uri_prefixes": PREFIXES}},
            {"id": "cap1", "type": "capture",
             "position": {"x": 100.0, "y": 200.0},
             "parameters": {"output_path": "/out/1"}},
        ],
        "connections": [
            {"id": "c0", "from": {"node": "cam0", "port": "out"},
             "to": {"node": "cap0", "port": "in"}},
            {"id": "c1", "from": {"node": "src1", "port": "out"},
             "to": {"node": "cap1", "port": "in"}},
        ],
    }
    camera_nodes, source_nodes, results = _packaging_pipeline(
        packaging, definition)
    assert [n.id for n in camera_nodes] == ["cam0"]
    assert [n.id for n in source_nodes] == ["src1"]

    for arch, (_, points, _) in results.items():
        assert [p["nodeId"] for p in points] == ["cam0", "src1"]
        camera_point, source_point = points
        assert "pythonSourceBinding" not in camera_point
        assert camera_point["nodeType"] == "icam_source"
        assert source_point["pythonSourceBinding"] is True
        assert source_point["parameters"] == {
            "allowed_uri_prefixes": PREFIXES}
        assert source_point["slots"] == []


# ---------------------------------------------------------------------------
# Preservation tests: source-free documents are byte-identical (11.5)
# ---------------------------------------------------------------------------

def test_source_free_plain_document_is_compiler_serialization(packaging):
    """A source-free, cameraless document's packaged text IS the
    compiler's own serialization — no bindingPoints section and no
    pythonSourceBinding key anywhere (pre-feature oracle by
    construction: the only packaging changes are gated on the
    custom_python_source type id)."""
    definition = _definition({
        "id": "fold0", "type": "folder_source",
        "position": {"x": 0.0, "y": 0.0},
        "parameters": {"location": "/data/images/0"},
    })
    camera_nodes, source_nodes, results = _packaging_pipeline(
        packaging, definition)
    assert camera_nodes == []
    assert source_nodes == []

    for arch, (compiled, points, packaged_text) in results.items():
        assert points == []
        assert packaged_text == compiled.to_json(), (
            "source-free output diverged from the compiler "
            "serialization on {}".format(arch))
        document = json.loads(packaged_text)
        assert "bindingPoints" not in document
        assert_no_python_source_key(document)


def test_source_free_camera_document_is_pre_feature_output(packaging):
    """A source-free camera document's packaged text equals the
    pre-feature output: the compiler serialization plus only the
    pre-existing bindingPoints section, every point restricted to
    pre-feature keys (never pythonSourceBinding)."""
    definition = _definition({
        "id": "cam0", "type": "icam_source",
        "position": {"x": 0.0, "y": 0.0},
        "parameters": {"device": "/dev/video7"},
    })
    camera_nodes, source_nodes, results = _packaging_pipeline(
        packaging, definition)
    assert [n.id for n in camera_nodes] == ["cam0"]
    assert source_nodes == []

    for arch, (compiled, points, packaged_text) in results.items():
        # Pre-feature reconstruction: the source gather adds nothing, so
        # the points ARE the pre-feature camera points.
        assert [p["nodeId"] for p in points] == ["cam0"]
        for point in points:
            assert set(point) <= PRE_FEATURE_POINT_KEYS
        expected_doc = compiled.to_dict()
        expected_doc["bindingPoints"] = points
        assert packaged_text == json.dumps(
            expected_doc, sort_keys=True, indent=2, ensure_ascii=True), (
            "camera output diverged from pre-feature serialization "
            "on {}".format(arch))
        assert_no_python_source_key(json.loads(packaged_text))


def test_source_free_gather_is_pre_feature_gather(packaging):
    """On a source-free graph, gather_custom_python_nodes returns
    exactly the pre-feature set (custom_python and
    custom_python_preprocess nodes only, in graph order)."""
    graph = WorkflowGraph(nodes=[
        Node(id="pre0", type="custom_python_preprocess",
             position=Position(0.0, 0.0),
             parameters={"code": "def process_frame(f, m):\n    return f",
                         "requirements": "numpy"}),
        Node(id="inf0", type="model_inference",
             position=Position(0.0, 0.0), parameters={}),
        Node(id="py0", type="custom_python",
             position=Position(0.0, 0.0),
             parameters={"code": "def handle(f, m):\n    return m"}),
    ], connections=[])

    gathered = packaging.gather_custom_python_nodes(graph)
    # Pre-feature oracle: the two pre-existing Custom Python types only.
    assert gathered == [
        {"node_id": "pre0",
         "code": "def process_frame(f, m):\n    return f",
         "requirements": "numpy"},
        {"node_id": "py0",
         "code": "def handle(f, m):\n    return m",
         "requirements": ""},
    ]
    assert packaging.gather_python_source_nodes(graph) == []
