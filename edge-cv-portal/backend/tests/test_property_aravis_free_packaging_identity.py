"""
Property-based test for Aravis-free packaging identity (task 7.3).

**Feature: aravis-camera-input, Property 10: Aravis-free packaging identity**

*For any* workflow definition containing no `aravis_camera_source`
node, the packaged compiled document SHALL be byte-identical to the
pre-feature packaging output for the same definition.

**Validates: Requirements 4.3**

Pre-feature oracle: the only packaging behavior the feature changed is
gated on the new `aravis_camera_source` type id (the gather predicate
and the ``aravisBinding`` branch of ``build_binding_points``), so the
pre-feature output over an Aravis-free definition is reconstructed by
running the same pure pipeline with the pre-feature gather rule —
Camera_Input_Nodes are ``camera_source`` (or camera-backed custom)
nodes only — and asserting the current output equals it byte-for-byte.
The structure is additionally pinned the way the camera-registry-sync
snapshot tests (test_workflow_packaging_binding_points.py) pinned
theirs: a cameraless document IS the compiler's own serialization
(``compiled.to_json()``), a camera document is that serialization plus
only the pre-existing bindingPoints section, and no ``aravisBinding``
key appears anywhere in any produced document.

The module is imported through the shared moto-backed session fixture
only so its module-level boto3 clients bind to the mock (same re-import
pattern as test_property_packaging_gates.py).

Generators: 1..3 source->capture chains headed by folder_source or
camera_source (any mix, including zero camera nodes), with generated
device paths, optional gain/exposure overrides, and optional
cameraBindingHint node data.
"""
import json
import sys

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from workflow_core.serializer import parse
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


# ---------------------------------------------------------------------------
# Generators: Aravis-free definitions
# ---------------------------------------------------------------------------

_device_paths = st.integers(min_value=0, max_value=63).map(
    lambda n: "/dev/video{}".format(n))

_hint_text = st.text(min_size=1, max_size=24)

_hints = st.fixed_dictionaries(
    {"cameraSourceId": _hint_text},
    optional={"cameraName": _hint_text, "sourceDeviceId": _hint_text},
)


@st.composite
def _aravis_free_definitions(draw):
    """A valid definition of 1..3 source->capture chains headed by
    folder_source or icam_source — never aravis_camera_source."""
    chain_count = draw(st.integers(min_value=1, max_value=3))
    nodes, connections = [], []
    for i in range(chain_count):
        if draw(st.booleans()):
            parameters = {"device": draw(_device_paths)}
            source = {"id": "cam{}".format(i), "type": "icam_source",
                      "position": {"x": 100.0 * i, "y": 0.0},
                      "parameters": parameters}
            if draw(st.booleans()):
                source["data"] = {"cameraBindingHint": draw(_hints)}
        else:
            source = {"id": "src{}".format(i), "type": "folder_source",
                      "position": {"x": 100.0 * i, "y": 0.0},
                      "parameters": {"location": "/data/images/{}".format(i)}}
        nodes.append(source)
        nodes.append({"id": "cap{}".format(i), "type": "capture",
                      "position": {"x": 100.0 * i, "y": 200.0},
                      "parameters": {"output_path": "/out/{}".format(i)}})
        connections.append({
            "id": "c{}".format(i),
            "from": {"node": source["id"], "port": "out"},
            "to": {"node": "cap{}".format(i), "port": "in"},
        })
    return {"schemaVersion": 1, "nodes": nodes, "connections": connections}


# ---------------------------------------------------------------------------
# Pre-feature oracle
# ---------------------------------------------------------------------------

#: Binding-point entry keys the pre-feature packager could produce
#: (camera-registry-sync): never aravisBinding.
PRE_FEATURE_POINT_KEYS = {"nodeId", "nodeType", "parameters", "slots",
                          "bindingHint", "adapterBinding", "csiSensorBinding"}


def pre_feature_camera_nodes(graph):
    """The gather rule for the built-in camera inputs in play here:
    icam_source nodes only (no custom camera-backed types — resolved_items
    is empty)."""
    return [node for node in graph.nodes if node.type == "icam_source"]


def assert_no_aravis_key(value):
    """No aravisBinding key anywhere in the document tree."""
    if isinstance(value, dict):
        assert "aravisBinding" not in value
        for child in value.values():
            assert_no_aravis_key(child)
    elif isinstance(value, list):
        for child in value:
            assert_no_aravis_key(child)


# ---------------------------------------------------------------------------
# Property 10
# ---------------------------------------------------------------------------

@settings(deadline=None)
@given(definition=_aravis_free_definitions())
def test_aravis_free_packaging_is_byte_identical_to_pre_feature_output(
        packaging, definition):
    """**Feature: aravis-camera-input, Property 10: Aravis-free packaging identity**

    For any Aravis-free definition, ``compiled_document_json`` over the
    current pipeline is byte-equal to the pre-feature serialization of
    the same definition on every device architecture.

    **Validates: Requirements 4.3**
    """
    parse_result = parse(json.dumps(definition))
    assert parse_result.ok, parse_result.error
    graph = parse_result.graph

    # The feature's gather gate adds nothing on an Aravis-free graph:
    # the gathered Camera_Input_Nodes ARE the pre-feature set.
    camera_nodes = packaging.gather_camera_input_nodes(graph, set())
    expected_nodes = pre_feature_camera_nodes(graph)
    assert [n.id for n in camera_nodes] == [n.id for n in expected_nodes]

    hints = packaging.binding_hints_from_definition(definition)
    catalog = resolve_catalog([])
    descriptors_by_id = {d.type_id: d for d in catalog}
    context = CompileContext(workflow_id="wf-p10", workflow_version="1")

    for arch in DEVICE_ARCHITECTURES:
        compiled = compile_workflow(graph, arch, context, simulation=False,
                                    catalog=catalog)
        assert not isinstance(compiled, list), (
            "compilation failed on {}: {}".format(arch, compiled))
        compiled_dict = compiled.to_dict()

        binding_points = packaging.build_binding_points(
            camera_nodes, compiled_dict, arch, hints, descriptors_by_id)
        packaged_text = packaging.compiled_document_json(
            compiled, binding_points)

        # Pre-feature reconstruction: same pure pipeline driven by the
        # pre-feature gather rule (the type-id gate is the only change).
        pre_feature_points = packaging.build_binding_points(
            expected_nodes, compiled_dict, arch, hints, descriptors_by_id)
        pre_feature_text = packaging.compiled_document_json(
            compiled, pre_feature_points)

        # 4.3: byte-identical output.
        assert packaged_text == pre_feature_text

        # Structure pinning, as the camera-registry-sync snapshots did:
        document = json.loads(packaged_text)
        assert_no_aravis_key(document)
        if not camera_nodes:
            # Cameraless: byte-identical to the compiler's own
            # serialization — exactly the pre-feature packager output.
            assert packaged_text == compiled.to_json()
            assert "bindingPoints" not in document
        else:
            # Camera workflow: the compiler serialization plus only the
            # pre-existing bindingPoints section, canonically serialized.
            expected_doc = compiled.to_dict()
            expected_doc["bindingPoints"] = binding_points
            assert packaged_text == json.dumps(
                expected_doc, sort_keys=True, indent=2, ensure_ascii=True)
            for point in document["bindingPoints"]:
                assert set(point) <= PRE_FEATURE_POINT_KEYS
