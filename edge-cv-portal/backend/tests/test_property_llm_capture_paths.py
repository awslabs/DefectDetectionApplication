"""Property test — llm_inference frame-capture path emission.

**Feature: edge-vlm-image-inference, Property 1: Fed ports get capture
paths, unfed ports get None**

For any valid workflow definition containing ``llm_inference`` nodes,
compiling for a vLLM-capable architecture SHALL emit ``capturePaths`` on
every ``llm_inference`` binding such that the ``in`` entry is a
``{work_dir}``-rooted path if and only if the node's ``in`` port is
(transitively) fed by a GStreamer video source, and ``None`` otherwise;
and for every emitted path some segment terminates a feeder branch with a
frame-capture sink chain (``videoconvert -> jpegenc -> multifilesink``)
whose ``multifilesink`` location equals that path.

**Validates: Requirements 1.1, 1.2**

The compile path is pure over the definition JSON (parse -> compile), so
the property is exercised directly against the ``workflow_core`` layer
with no AWS or portal handler involvement (the conftest puts the layer on
sys.path).

Generator notes (smart constraints): each ``llm_inference`` node is drawn
in one of three wiring modes covering both sides of the iff —

- ``direct``: a GStreamer video source (``folder_source``) feeds ``in``
  directly (fed);
- ``via_transform``: the source feeds ``in`` through a GStreamer
  pass-through transform (``crop``), so the port is *transitively* fed
  and the feeder is the transform element (fed);
- ``unfed``: ``in`` is fed only through an opaque ``bedrock_inference``
  node (InferenceMeta coerces to VideoFrames, so the graph is valid, but
  frames terminate at the bedrock capture sinks and never reach the llm
  node — no GStreamer feeder).

Every llm node emits into its own ``mqtt_publish`` sink so the workflow
always passes validation (input node present, all nodes reachable).
"""

from __future__ import annotations

import json

from hypothesis import given, settings
from hypothesis import strategies as st

from workflow_core.compiler import CompileContext
from workflow_core.compiler import compile as compile_workflow
from workflow_core.serializer import parse as parse_definition

#: A vLLM-capable architecture (llm_inference maps only on these).
VLLM_ARCH = "arm64_jp6"

#: The three wiring modes for an llm node's ``in`` port. ``direct`` and
#: ``via_transform`` are the fed side of the iff; ``unfed`` is the
#: unfed side (the only GStreamer frames upstream terminate at an opaque
#: bedrock node).
FED_MODES = ("direct", "via_transform")
ALL_MODES = FED_MODES + ("unfed",)


# ---------------------------------------------------------------------------
# Definition builder
# ---------------------------------------------------------------------------


def build_definition(modes):
    """A valid Workflow_Definition with one llm_inference node per mode,
    wired per the mode's fed/unfed shape. Returns (definition_json,
    {llm_node_id: mode})."""
    nodes = []
    connections = []
    llm_modes = {}

    def add_node(node_id, node_type, parameters):
        nodes.append({
            "id": node_id,
            "type": node_type,
            "position": {"x": len(nodes) * 100, "y": 0},
            "parameters": parameters,
        })

    def connect(source, source_port, target, target_port):
        connections.append({
            "id": "c{0}".format(len(connections)),
            "from": {"node": source, "port": source_port},
            "to": {"node": target, "port": target_port},
        })

    for index, mode in enumerate(modes):
        src_id = "src{0}".format(index)
        llm_id = "llm{0}".format(index)
        mq_id = "mq{0}".format(index)
        llm_modes[llm_id] = mode

        add_node(src_id, "folder_source", {"location": "/aws_dda/images"})
        add_node(llm_id, "llm_inference", {
            "modelName": "qwen2-vl-2b",
            "prompt_template": "Describe the part.",
        })
        add_node(mq_id, "mqtt_publish", {
            "broker_host": "broker.local",
            "topic": "dda/out/{0}".format(index),
        })

        if mode == "direct":
            connect(src_id, "out", llm_id, "in")
        elif mode == "via_transform":
            crop_id = "crop{0}".format(index)
            add_node(crop_id, "crop", {})
            connect(src_id, "out", crop_id, "in")
            connect(crop_id, "out", llm_id, "in")
        else:  # unfed: frames stop at an opaque bedrock node upstream
            bed_id = "bed{0}".format(index)
            add_node(bed_id, "bedrock_inference", {"prompt": "Compare."})
            connect(src_id, "out", bed_id, "in")
            connect(src_id, "out", bed_id, "reference")
            connect(bed_id, "out", llm_id, "in")

        connect(llm_id, "out", mq_id, "in")

    definition = {
        "schemaVersion": 1,
        "nodes": nodes,
        "connections": connections,
    }
    return json.dumps(definition), llm_modes


def compile_definition(definition_json):
    """Parse + compile for the vLLM-capable arch; assert success and
    return the compiled document as a plain dict."""
    parse_result = parse_definition(definition_json)
    assert parse_result.ok, "definition failed to parse: {0}".format(
        parse_result.error)

    compiled = compile_workflow(
        parse_result.graph,
        VLLM_ARCH,
        CompileContext(workflow_id="wf-llm-capture", workflow_version="1"),
        simulation=False,
    )
    assert not isinstance(compiled, list), (
        "expected a compiled document, got errors: "
        "{0}".format([error.to_dict() for error in compiled]))
    return compiled.to_dict()


def capture_sink_locations(document):
    """Every ``multifilesink`` location in the document's segments that
    terminates a synthetic frame-capture chain (immediately preceded by
    ``videoconvert -> jpegenc``)."""
    locations = []
    for segment in document["segments"]:
        elements = segment["elements"]
        for position, element in enumerate(elements):
            if element["factory"] != "multifilesink":
                continue
            if (
                position >= 2
                and elements[position - 1]["factory"] == "jpegenc"
                and elements[position - 2]["factory"] == "videoconvert"
            ):
                locations.append(element["args"]["location"])
    return locations


def llm_bindings_by_node(document):
    return {
        binding["nodeId"]: binding
        for binding in document["executorBindings"]
        if binding["binding"] == "llm_inference"
    }


# ---------------------------------------------------------------------------
# Property 1: Fed ports get capture paths, unfed ports get None
# ---------------------------------------------------------------------------


class TestLlmCapturePathEmission:
    """**Feature: edge-vlm-image-inference, Property 1: Fed ports get
    capture paths, unfed ports get None**

    **Validates: Requirements 1.1, 1.2**
    """

    @settings(deadline=None)
    @given(modes=st.lists(st.sampled_from(ALL_MODES), min_size=1, max_size=4))
    def test_capture_path_iff_fed(self, modes):
        """``capturePaths.in`` is a ``{work_dir}``-rooted path iff the llm
        node's ``in`` port is (transitively) fed by a GStreamer video
        source, ``None`` otherwise; and every emitted path names a
        matching frame-capture ``multifilesink`` location in the compiled
        segments."""
        definition_json, llm_modes = build_definition(modes)
        document = compile_definition(definition_json)

        bindings = llm_bindings_by_node(document)
        assert set(bindings) == set(llm_modes), (
            "expected one llm_inference binding per llm node: "
            "{0} != {1}".format(sorted(bindings), sorted(llm_modes)))

        sink_locations = capture_sink_locations(document)

        for llm_id, mode in llm_modes.items():
            binding = bindings[llm_id]
            assert "capturePaths" in binding, (
                "llm binding '{0}' carries no capturePaths".format(llm_id))
            capture_paths = binding["capturePaths"]
            assert set(capture_paths) == {"in"}, (
                "llm binding '{0}' capturePaths must map exactly the 'in' "
                "port, got {1!r}".format(llm_id, capture_paths))

            path = capture_paths["in"]
            if mode == "unfed":
                # Requirement 1.2: unfed port -> None.
                assert path is None, (
                    "llm binding '{0}' (unfed 'in' port) must map to None, "
                    "got {1!r}".format(llm_id, path))
            else:
                # Requirement 1.1: fed port -> {work_dir}-rooted path with
                # a matching frame-capture sink chain in the segments.
                assert isinstance(path, str) and path.startswith("{work_dir}/"), (
                    "llm binding '{0}' (fed 'in' port, mode {1}) must map "
                    "to a {{work_dir}}-rooted path, got {2!r}".format(
                        llm_id, mode, path))
                assert path in sink_locations, (
                    "no videoconvert->jpegenc->multifilesink capture chain "
                    "with location {0!r} in the compiled segments (found: "
                    "{1!r})".format(path, sink_locations))

    # -- Deterministic anchors for the two sides of the iff ---------------

    def test_directly_fed_llm_gets_path_and_sink(self):
        """One folder_source feeding one llm node: the binding maps 'in'
        to a {work_dir} path with a matching capture sink."""
        definition_json, _ = build_definition(["direct"])
        document = compile_definition(definition_json)
        binding = llm_bindings_by_node(document)["llm0"]
        path = binding["capturePaths"]["in"]
        assert path is not None and path.startswith("{work_dir}/")
        assert path in capture_sink_locations(document)

    def test_unfed_llm_gets_none(self):
        """An llm node fed only through an opaque bedrock node: the
        binding maps 'in' to None."""
        definition_json, _ = build_definition(["unfed"])
        document = compile_definition(definition_json)
        binding = llm_bindings_by_node(document)["llm0"]
        assert binding["capturePaths"]["in"] is None
