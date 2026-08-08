"""
Property-based test for llm-free compilation identity (task 1.4).

**Feature: edge-vlm-image-inference, Property 3: Compilation identity for
llm-free workflows**

*For any* valid workflow definition containing no ``llm_inference`` node,
the compiled per-architecture pipeline documents SHALL be identical with
and without this feature's compiler change.

**Validates: Requirements 1.5**

Pre-feature oracle (the practical formulation used by prior
additive-identity suites, e.g. test_property_aravis_free_packaging_identity):
the feature's only compiler changes are gated on the
``llm_inference`` executor binding — collecting ``llm_node_ids`` (empty on
an llm-free graph, so the frame-capture plan input is exactly the
pre-feature ``bedrock_node_ids`` set) and a ``capturePaths`` emission
branch that never fires without an llm binding. The pre-feature output is
therefore pinned structurally:

1. No executor binding other than ``bedrock_inference`` carries a
   ``capturePaths`` key — anywhere in the document tree.
2. Segments are unchanged: every synthetic frame-capture sink chain
   (``videoconvert ! jpegenc ! multifilesink``, elements with
   ``nodeId: null``) is attributable to a bedrock node — its
   ``multifilesink`` location appears among the bedrock bindings'
   ``capturePaths`` values — and bedrock-free documents contain no
   ``multifilesink`` at all (the user-facing ``capture`` node compiles to
   ``jpegenc ! emlcapture``, never ``multifilesink``, so any capture sink
   chain is synthetic by construction).

Generators (reusing the established definition-generator pattern of the
packaging/compiler property tests): 1..3 source->capture chains headed by
folder_source or icam_source, optionally an opaque ``bedrock_inference``
node whose ``in``/``reference`` ports are fed by any (possibly shared)
subset of those sources, feeding an mqtt_publish output. Never an
``llm_inference`` node.

Hypothesis runs under the conftest profile (no hardcoded max_examples).
"""
import json

from hypothesis import given, settings
from hypothesis import strategies as st

from workflow_core.serializer import parse
from workflow_core.compiler import compile as compile_workflow, CompileContext
from workflow_core.catalog import DEVICE_ARCHITECTURES
from workflow_core.catalog.custom import resolve_catalog


# ---------------------------------------------------------------------------
# Generators: llm-free definitions (sources, captures, optional bedrock)
# ---------------------------------------------------------------------------

_device_paths = st.integers(min_value=0, max_value=63).map(
    lambda n: "/dev/video{}".format(n))


@st.composite
def _llm_free_definitions(draw):
    """A valid definition of 1..3 source->capture chains, optionally with
    one bedrock_inference node fed by any of the sources — and never an
    llm_inference node."""
    chain_count = draw(st.integers(min_value=1, max_value=3))
    nodes, connections = [], []
    source_ids = []
    for i in range(chain_count):
        if draw(st.booleans()):
            source = {"id": "cam{}".format(i), "type": "icam_source",
                      "position": {"x": 100.0 * i, "y": 0.0},
                      "parameters": {"device": draw(_device_paths)}}
        else:
            source = {"id": "src{}".format(i), "type": "folder_source",
                      "position": {"x": 100.0 * i, "y": 0.0},
                      "parameters": {"location": "/data/images/{}".format(i)}}
        nodes.append(source)
        source_ids.append(source["id"])
        nodes.append({"id": "cap{}".format(i), "type": "capture",
                      "position": {"x": 100.0 * i, "y": 200.0},
                      "parameters": {"output_path": "/out/{}".format(i)}})
        connections.append({
            "id": "c{}".format(i),
            "from": {"node": source["id"], "port": "out"},
            "to": {"node": "cap{}".format(i), "port": "in"},
        })

    if draw(st.booleans()):
        # An opaque bedrock node: "in" fed by one of the sources,
        # "reference" optionally fed (possibly by the SAME feeder — the
        # shared path_for(feeder) case), output to mqtt_publish.
        nodes.append({"id": "bedrock", "type": "bedrock_inference",
                      "position": {"x": 400.0, "y": 100.0},
                      "parameters": {}})
        in_feeder = source_ids[draw(
            st.integers(min_value=0, max_value=len(source_ids) - 1))]
        connections.append({
            "id": "cb-in",
            "from": {"node": in_feeder, "port": "out"},
            "to": {"node": "bedrock", "port": "in"},
        })
        ref_choice = draw(
            st.integers(min_value=-1, max_value=len(source_ids) - 1))
        if ref_choice >= 0:
            connections.append({
                "id": "cb-ref",
                "from": {"node": source_ids[ref_choice], "port": "out"},
                "to": {"node": "bedrock", "port": "reference"},
            })
        nodes.append({"id": "mqtt", "type": "mqtt_publish",
                      "position": {"x": 600.0, "y": 100.0},
                      "parameters": {"topic": "results",
                                     "broker_host": "localhost"}})
        connections.append({
            "id": "cb-out",
            "from": {"node": "bedrock", "port": "out"},
            "to": {"node": "mqtt", "port": "in"},
        })

    return {"schemaVersion": 1, "nodes": nodes, "connections": connections}


# ---------------------------------------------------------------------------
# Pre-feature oracle helpers
# ---------------------------------------------------------------------------

def _bindings_with_capture_paths(value, found=None):
    """Every dict anywhere in the document tree carrying a capturePaths
    key (pre-feature: bedrock_inference executor bindings only)."""
    if found is None:
        found = []
    if isinstance(value, dict):
        if "capturePaths" in value:
            found.append(value)
        for child in value.values():
            _bindings_with_capture_paths(child, found)
    elif isinstance(value, list):
        for child in value:
            _bindings_with_capture_paths(child, found)
    return found


def _synthetic_capture_sinks(document):
    """The multifilesink elements of the synthetic frame-capture chains.

    The user-facing ``capture`` node compiles to ``jpegenc ! emlcapture``
    on every architecture — ``multifilesink`` appears ONLY in synthetic
    capture chains, so this is the complete set of capture sinks.
    """
    sinks = []
    for segment in document["segments"]:
        elements = segment["elements"]
        for index, element in enumerate(elements):
            if element["factory"] != "multifilesink":
                continue
            sinks.append(element)
            # Pre-feature chain shape: videoconvert ! jpegenc !
            # multifilesink, all synthetic (nodeId null).
            chain = elements[index - 2:index + 1]
            assert [e["factory"] for e in chain] == \
                ["videoconvert", "jpegenc", "multifilesink"]
            for member in chain:
                assert member["nodeId"] is None
    return sinks


def _bedrock_capture_path_values(document):
    """All non-None capturePaths values across bedrock bindings."""
    return [path
            for binding in document["executorBindings"]
            if binding.get("binding") == "bedrock_inference"
            for path in (binding.get("capturePaths") or {}).values()
            if path is not None]


# ---------------------------------------------------------------------------
# Property 3
# ---------------------------------------------------------------------------

@settings(deadline=None)
@given(definition=_llm_free_definitions())
def test_llm_free_compilation_is_identical_to_pre_feature_output(definition):
    """**Feature: edge-vlm-image-inference, Property 3: Compilation identity
    for llm-free workflows**

    For any llm-free definition, on every device architecture the compiled
    document carries capturePaths on bedrock_inference bindings only, and
    every synthetic capture sink chain in the segments is attributable to
    a bedrock node — the pre-feature output, unchanged by the compiler
    edit.

    **Validates: Requirements 1.5**
    """
    assert not any(node["type"] == "llm_inference"
                   for node in definition["nodes"])

    parse_result = parse(json.dumps(definition))
    assert parse_result.ok, parse_result.error
    graph = parse_result.graph

    catalog = resolve_catalog([])
    context = CompileContext(workflow_id="wf-p3", workflow_version="1")

    for arch in DEVICE_ARCHITECTURES:
        compiled = compile_workflow(graph, arch, context, simulation=False,
                                    catalog=catalog)
        assert not isinstance(compiled, list), (
            "compilation failed on {}: {}".format(arch, compiled))
        document = compiled.to_dict()

        # 1. capturePaths appears on bedrock_inference bindings ONLY —
        #    no llm capturePaths key differences anywhere in the tree.
        carriers = _bindings_with_capture_paths(document)
        bedrock_bindings = [b for b in document["executorBindings"]
                            if b.get("binding") == "bedrock_inference"]
        assert carriers == bedrock_bindings, (
            "on {}: capturePaths must appear on bedrock_inference "
            "bindings only; found on {}".format(
                arch,
                [c.get("binding") or c for c in carriers]))

        # 2. Segments unchanged: every capture sink is attributable to a
        #    bedrock node — one sink per distinct bedrock capture path,
        #    and zero sinks when the definition has no bedrock node.
        sinks = _synthetic_capture_sinks(document)
        sink_locations = sorted(s["args"]["location"] for s in sinks)
        bedrock_paths = sorted(set(_bedrock_capture_path_values(document)))
        assert sink_locations == bedrock_paths, (
            "on {}: capture sinks {} must be exactly the bedrock "
            "bindings' capture paths {}".format(
                arch, sink_locations, bedrock_paths))
        if not bedrock_bindings:
            assert sinks == [], (
                "on {}: a bedrock-free document must contain no "
                "synthetic capture sink".format(arch))
