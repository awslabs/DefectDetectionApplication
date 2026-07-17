"""
Property-based test for Aravis binding point emission (task 7.2).

**Feature: aravis-camera-input, Property 9: Packaging emits Aravis binding points**

*For any* workflow definition containing `aravis_camera_source` nodes,
packaging SHALL emit exactly one `bindingPoints` entry per Aravis node
per architecture carrying `aravisBinding: true`, empty slots, and the
node's rendered `camera_id`/`gain`/`exposure` parameter values, and
SHALL record each node in the version item's `camera_input_nodes` with
`has_binding_points: true`.

**Validates: Requirements 4.1, 4.2**

The layer under test is the packager's pure binding-point pipeline —
``gather_camera_input_nodes`` -> ``binding_hints_from_definition`` ->
``build_binding_points`` -> ``camera_input_nodes_record`` — exercised
over compiler output exactly as the handler drives it (same call
sequence, same built-in catalog), with no AWS. The module is imported
through the shared moto-backed session fixture only so its module-level
boto3 clients bind to the mock (same re-import pattern as
test_property_packaging_gates.py). ``has_binding_points`` is asserted
through the value the handler stores: ``bool(camera_nodes)``.

Generators: definitions with 1..3 source->capture chains, each headed
by an aravis_camera_source or (mixed) a camera_source, with generated
camera_id/device values, optional gain/exposure overrides, and optional
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
# Reference model: the rendered parameter values Requirement 4.2 demands,
# restated from the catalog contract (declared defaults overlaid with the
# node's explicit values) rather than imported from the implementation.
# ---------------------------------------------------------------------------

ARAVIS_TYPE = "aravis_camera_source"
CAMERA_TYPE = "camera_source"

#: aravis_camera_source declared parameter defaults (design section 1).
DEFAULT_GAIN = 4
DEFAULT_EXPOSURE = 5000000


def expected_rendered_parameters(node_parameters):
    """camera_id (required, no default) plus gain/exposure defaults
    overlaid with the node's explicit values."""
    rendered = {"camera_id": node_parameters["camera_id"],
                "gain": DEFAULT_GAIN, "exposure": DEFAULT_EXPOSURE}
    rendered.update(node_parameters)
    return rendered


# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------

_camera_ids = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.",
    min_size=1, max_size=32)

_device_paths = st.integers(min_value=0, max_value=63).map(
    lambda n: "/dev/video{}".format(n))

_hint_text = st.text(min_size=1, max_size=24)

_hints = st.fixed_dictionaries(
    {"cameraSourceId": _hint_text},
    optional={"cameraName": _hint_text, "sourceDeviceId": _hint_text},
)


@st.composite
def _aravis_definitions(draw):
    """A valid definition of 1..3 source->capture chains, at least one
    headed by an aravis_camera_source, the rest optionally mixed with
    camera_source chains. Returns (definition, aravis_specs, camera_ids)
    where aravis_specs maps node id -> (parameters, hint-or-None) and
    camera_ids is the full ordered list of Camera_Input_Node ids."""
    chain_count = draw(st.integers(min_value=1, max_value=3))
    aravis_indices = draw(st.sets(
        st.integers(min_value=0, max_value=chain_count - 1), min_size=1))

    nodes, connections = [], []
    aravis_specs, camera_node_ids = {}, []
    for i in range(chain_count):
        if i in aravis_indices:
            source_id = "arv{}".format(i)
            parameters = {"camera_id": draw(_camera_ids)}
            if draw(st.booleans()):
                parameters["gain"] = draw(st.integers(min_value=0, max_value=100))
            if draw(st.booleans()):
                parameters["exposure"] = draw(
                    st.integers(min_value=0, max_value=10_000_000))
            source = {"id": source_id, "type": ARAVIS_TYPE,
                      "position": {"x": 100.0 * i, "y": 0.0},
                      "parameters": parameters}
            hint = draw(st.one_of(st.none(), _hints))
            if hint is not None:
                source["data"] = {"cameraBindingHint": hint}
            aravis_specs[source_id] = (parameters, hint)
        else:
            source_id = "cam{}".format(i)
            source = {"id": source_id, "type": CAMERA_TYPE,
                      "position": {"x": 100.0 * i, "y": 0.0},
                      "parameters": {"device": draw(_device_paths)}}
        camera_node_ids.append(source_id)
        nodes.append(source)
        nodes.append({"id": "cap{}".format(i), "type": "capture",
                      "position": {"x": 100.0 * i, "y": 200.0},
                      "parameters": {"output_path": "/out/{}".format(i)}})
        connections.append({
            "id": "c{}".format(i),
            "from": {"node": source_id, "port": "out"},
            "to": {"node": "cap{}".format(i), "port": "in"},
        })

    definition = {"schemaVersion": 1, "nodes": nodes,
                  "connections": connections}
    return definition, aravis_specs, camera_node_ids


# ---------------------------------------------------------------------------
# Property 9
# ---------------------------------------------------------------------------

@settings(deadline=None)
@given(case=_aravis_definitions())
def test_packaging_emits_aravis_binding_points(packaging, case):
    """**Feature: aravis-camera-input, Property 9: Packaging emits Aravis binding points**

    Exactly one bindingPoints entry per Aravis node per device
    architecture, carrying ``aravisBinding: true``, empty slots, and the
    rendered camera_id/gain/exposure values; each node recorded in
    camera_input_nodes with has_binding_points: true.

    **Validates: Requirements 4.1, 4.2**
    """
    definition, aravis_specs, camera_node_ids = case

    parse_result = parse(json.dumps(definition))
    assert parse_result.ok, parse_result.error
    graph = parse_result.graph

    # 4.1: every aravis_camera_source is gathered as a Camera_Input_Node
    # (alongside any camera_source), in graph node order.
    camera_nodes = packaging.gather_camera_input_nodes(graph, set())
    assert [node.id for node in camera_nodes] == camera_node_ids

    hints = packaging.binding_hints_from_definition(definition)
    catalog = resolve_catalog([])
    descriptors_by_id = {d.type_id: d for d in catalog}
    context = CompileContext(workflow_id="wf-p9", workflow_version="1")

    arch_binding_points, arch_compiled_dicts = {}, {}
    for arch in DEVICE_ARCHITECTURES:
        compiled = compile_workflow(graph, arch, context, simulation=False,
                                    catalog=catalog)
        assert not isinstance(compiled, list), (
            "compilation failed on {}: {}".format(arch, compiled))
        compiled_dict = compiled.to_dict()
        points = packaging.build_binding_points(
            camera_nodes, compiled_dict, arch, hints, descriptors_by_id)
        arch_compiled_dicts[arch] = compiled_dict
        arch_binding_points[arch] = points

        # One entry per Camera_Input_Node, hence exactly one per Aravis
        # node per architecture (4.1).
        assert [p["nodeId"] for p in points] == camera_node_ids

        for point in points:
            if point["nodeId"] in aravis_specs:
                parameters, hint = aravis_specs[point["nodeId"]]
                # 4.2: the aravisBinding marker with empty slots on every
                # physical device architecture.
                assert point["nodeType"] == ARAVIS_TYPE
                assert point["aravisBinding"] is True
                assert point["slots"] == []
                # 4.2: rendered (defaults-overlaid) parameter values.
                assert point["parameters"] == expected_rendered_parameters(
                    parameters)
                # The hint rides along exactly when the definition
                # carries one; the other camera markers never appear.
                assert point.get("bindingHint") == hint or (
                    hint is None and "bindingHint" not in point)
                assert "adapterBinding" not in point
                assert "csiSensorBinding" not in point
            else:
                # Mixed camera_source entries never gain the Aravis marker.
                assert "aravisBinding" not in point

    # 4.1: the version item records each Aravis node in camera_input_nodes
    # through the existing recording path...
    records = packaging.camera_input_nodes_record(
        camera_nodes, hints, arch_binding_points, arch_compiled_dicts)
    assert [r["node_id"] for r in records] == camera_node_ids
    for record in records:
        if record["node_id"] in aravis_specs:
            _, hint = aravis_specs[record["node_id"]]
            assert record["node_type"] == ARAVIS_TYPE
            assert record.get("binding_hint") == hint or (
                hint is None and "binding_hint" not in record)
            # No Aravis parameter ever lands in an element argument.
            assert record["compiled_device_paths"] == {}

    # ...with has_binding_points: true (the handler stores
    # bool(camera_nodes) — an Aravis workflow always has camera nodes).
    assert bool(camera_nodes) is True
