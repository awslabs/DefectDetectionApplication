"""
Property-based test for binding-hint transparency (workflow_core).

**Feature: camera-registry-sync, Property 12: Binding hints are transparent to validation and compilation**

*For any* valid workflow definition and any binding hints attached to
its Camera_Input_Nodes, validating and compiling the hinted definition
produces results equivalent to validating and compiling the same
definition with the hints stripped.

**Validates: Requirements 7.5, 11.5**

The hint is advisory metadata inside the workflow definition JSON
(``nodes[].data.cameraBindingHint``, recorded by the Workflow_Builder
camera picker). The design requires the validator and compiler to
ignore unknown node data keys so the workflow stays device-portable
(7.5) and existing definitions are untouched (11.5). The pipeline under
test is the definition-level one every portal consumer uses:
``workflow_core.serializer.parse`` -> ``workflow_core.validator.validate``
-> ``workflow_core.compiler.compile`` (per device architecture).

Generators: 1-3 camera_source -> capture chains with generated device
paths and optional gain/exposure values; hints with unicode/whitespace
names attached to a non-empty subset of the camera nodes.
"""
import copy
import json

from hypothesis import given, settings
from hypothesis import strategies as st

from workflow_core.serializer import parse
from workflow_core.validator import validate
from workflow_core.compiler import compile as compile_workflow, CompileContext
from workflow_core.catalog import DEVICE_ARCHITECTURES

# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------

_device_paths = st.integers(min_value=0, max_value=63).map(
    lambda n: "/dev/video{}".format(n))

# Whitespace/unicode-heavy hint text: transparency must hold for any
# advisory content the Workflow_Builder records.
_hint_text = st.text(
    alphabet=st.characters(codec="utf-8", categories=("L", "N", "P", "Zs", "S")),
    min_size=1,
    max_size=24,
)

_hints = st.fixed_dictionaries(
    {"cameraSourceId": _hint_text},
    optional={
        "cameraName": _hint_text,
        "sourceDeviceId": _hint_text,
    },
)


@st.composite
def _definitions_with_hints(draw):
    """A valid definition (camera_source -> capture chains) plus hints
    for a non-empty subset of its Camera_Input_Nodes."""
    chain_count = draw(st.integers(min_value=1, max_value=3))
    nodes, connections = [], []
    for i in range(chain_count):
        parameters = {"device": draw(_device_paths)}
        nodes.append({
            "id": "cam{}".format(i), "type": "icam_source",
            "position": {"x": 100.0 * i, "y": 0.0},
            "parameters": parameters,
        })
        nodes.append({
            "id": "cap{}".format(i), "type": "capture",
            "position": {"x": 100.0 * i, "y": 200.0},
            "parameters": {"output_path": "/out/{}".format(i)},
        })
        connections.append({
            "id": "c{}".format(i),
            "from": {"node": "cam{}".format(i), "port": "out"},
            "to": {"node": "cap{}".format(i), "port": "in"},
        })

    hinted_cameras = draw(st.sets(
        st.integers(min_value=0, max_value=chain_count - 1), min_size=1))
    hints = {"cam{}".format(i): draw(_hints) for i in sorted(hinted_cameras)}

    definition = {"schemaVersion": 1, "nodes": nodes,
                  "connections": connections}
    return definition, hints


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _apply_hints(definition, hints):
    """The hinted equivalent: ``data.cameraBindingHint`` on hinted nodes."""
    hinted = copy.deepcopy(definition)
    for node in hinted["nodes"]:
        if node["id"] in hints:
            node["data"] = {"cameraBindingHint": copy.deepcopy(hints[node["id"]])}
    return hinted


def _comparable(compiled):
    """Compile output as plain data: a document dict or an error list."""
    if isinstance(compiled, list):
        return [error.to_dict() for error in compiled]
    return compiled.to_dict()


# ---------------------------------------------------------------------------
# Property 12
# ---------------------------------------------------------------------------

@settings(deadline=None)
@given(_definitions_with_hints())
def test_binding_hints_are_transparent_to_validation_and_compilation(case):
    definition, hints = case
    hinted = _apply_hints(definition, hints)

    stripped_parse = parse(json.dumps(definition))
    hinted_parse = parse(json.dumps(hinted))

    # The generator only emits valid definitions.
    assert stripped_parse.ok, stripped_parse.error
    # Transparency starts at parse: the hinted definition must be
    # accepted and yield the same graph as its hint-stripped equivalent.
    assert hinted_parse.ok, (
        "hinted definition failed to parse: {}".format(hinted_parse.error))
    assert hinted_parse.graph.is_equivalent_to(stripped_parse.graph)

    # Validation findings are identical.
    stripped_findings = [f.to_dict() for f in validate(stripped_parse.graph)]
    hinted_findings = [f.to_dict() for f in validate(hinted_parse.graph)]
    assert hinted_findings == stripped_findings

    # Compilation output is identical on every device architecture.
    context = CompileContext(workflow_id="wf-hint-transparency",
                             workflow_version="1")
    for arch in DEVICE_ARCHITECTURES:
        stripped_compiled = compile_workflow(stripped_parse.graph, arch,
                                             context)
        hinted_compiled = compile_workflow(hinted_parse.graph, arch, context)
        assert _comparable(hinted_compiled) == _comparable(stripped_compiled), (
            "compilation diverged on {}".format(arch))
