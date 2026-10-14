"""Property test — feeder frame-capture sharing across inference bindings.

**Feature: edge-vlm-image-inference, Property 2: Feeder capture files are
shared, one sink per feeder**

*For any* valid workflow definition where one video source feeds input
ports of multiple ``llm_inference`` and/or ``bedrock_inference`` nodes,
the compiled document SHALL contain exactly one capture sink chain
(``videoconvert ! jpegenc ! multifilesink``) for that feeder, and every
consuming binding's ``capturePaths`` entry for the fed port SHALL
reference that feeder's single capture file path.

**Validates: Requirements 1.3**

The compiler is pure over the definition JSON (parse -> compile), so the
property is exercised directly against the canonical ``workflow_core``
layer with no AWS involvement. Workflows are generated with one or two
``folder_source`` feeders; the first feeder always fans out to at least
two frame-consuming inference nodes (the property's premise), consumers
mix both binding kinds, and Bedrock nodes optionally wire their
``reference`` port to the same feeder — sharing must hold across ports
and across binding kinds alike.
"""

from __future__ import annotations

import json
from collections import Counter

from hypothesis import given, settings
from hypothesis import strategies as st

from workflow_core.compiler import CompileContext
from workflow_core.compiler import compile as compile_workflow
from workflow_core.serializer import parse as parse_definition

#: A vLLM-capable device architecture on which BOTH llm_inference and
#: bedrock_inference resolve to executor bindings with frame capture.
VLLM_ARCH = "arm64_jp6"

WORK_DIR_PREFIX = "{work_dir}/"


# ---------------------------------------------------------------------------
# Workflow generation: F feeders (1..2), each feeding >=1 consumer;
# feeder 0 always feeds >=2 consumers (the shared-feeder premise).
# Each consumer is an llm_inference or bedrock_inference node with its
# own mqtt_publish sink; a bedrock consumer may additionally wire its
# ``reference`` port to the same feeder.
# ---------------------------------------------------------------------------


@st.composite
def shared_feeder_workflows(draw):
    """Returns ``(definition_json, consumers_by_feeder)`` where
    ``consumers_by_feeder`` maps each feeder node id to the list of
    ``(node_id, node_type, fed_ports)`` tuples it feeds."""
    feeder_count = draw(st.integers(min_value=1, max_value=2))
    feeder_ids = ["src{0}".format(i) for i in range(feeder_count)]

    # Consumers per feeder: feeder 0 gets 2..3 (must be shared); any
    # second feeder gets 1..3.
    consumer_counts = [draw(st.integers(min_value=2, max_value=3))]
    for _ in feeder_ids[1:]:
        consumer_counts.append(draw(st.integers(min_value=1, max_value=3)))

    nodes = [
        {
            "id": feeder_id,
            "type": "folder_source",
            "position": {"x": 0, "y": 100 * index},
            "parameters": {"location": "/aws_dda/images"},
        }
        for index, feeder_id in enumerate(feeder_ids)
    ]
    connections = []
    consumers_by_feeder = {feeder_id: [] for feeder_id in feeder_ids}
    serial = 0

    def next_id(prefix):
        nonlocal serial
        serial += 1
        return "{0}{1}".format(prefix, serial)

    for feeder_id, count in zip(feeder_ids, consumer_counts):
        for _ in range(count):
            kind = draw(st.sampled_from(["llm_inference", "bedrock_inference"]))
            node_id = next_id("infer")
            if kind == "llm_inference":
                parameters = {
                    "modelName": "qwen2-vl-2b",
                    "prompt_template": "Describe the part.",
                }
                fed_ports = ["in"]
            else:
                parameters = {"prompt": "Compare the part to the reference."}
                fed_ports = ["in"]
                if draw(st.booleans()):
                    fed_ports.append("reference")
            nodes.append({
                "id": node_id,
                "type": kind,
                "position": {"x": 200, "y": 100 * serial},
                "parameters": parameters,
            })
            for port in fed_ports:
                connections.append({
                    "id": next_id("c"),
                    "from": {"node": feeder_id, "port": "out"},
                    "to": {"node": node_id, "port": port},
                })
            sink_id = next_id("mq")
            nodes.append({
                "id": sink_id,
                "type": "mqtt_publish",
                "position": {"x": 400, "y": 100 * serial},
                "parameters": {"broker_host": "broker.local",
                               "topic": "dda/out"},
            })
            connections.append({
                "id": next_id("c"),
                "from": {"node": node_id, "port": "out"},
                "to": {"node": sink_id, "port": "in"},
            })
            consumers_by_feeder[feeder_id].append((node_id, kind, fed_ports))

    definition_json = json.dumps({
        "schemaVersion": 1,
        "nodes": nodes,
        "connections": connections,
    })
    return definition_json, consumers_by_feeder


# ---------------------------------------------------------------------------
# Document inspection helpers
# ---------------------------------------------------------------------------


def compile_document(definition_json):
    parse_result = parse_definition(definition_json)
    assert parse_result.ok, (
        "definition failed to parse: {0}".format(parse_result.error))
    compiled = compile_workflow(
        parse_result.graph,
        VLLM_ARCH,
        CompileContext(workflow_id="wf-feeder-sharing", workflow_version="1"),
        simulation=False,
    )
    assert not isinstance(compiled, list), (
        "expected a compiled document, got errors: "
        "{0}".format([error.to_dict() for error in compiled]))
    return compiled.to_dict()


def capture_sink_locations(document):
    """Every ``multifilesink`` location in the document's segments, in
    order, asserting each terminates a full synthetic capture chain
    (``videoconvert ! jpegenc ! multifilesink``)."""
    locations = []
    for segment in document["segments"]:
        elements = segment["elements"]
        for index, element in enumerate(elements):
            if element["factory"] != "multifilesink":
                continue
            location = element["args"]["location"]
            assert location.startswith(WORK_DIR_PREFIX), (
                "capture sink location {0!r} is not "
                "{{work_dir}}-rooted".format(location))
            assert index >= 2, (
                "multifilesink at segment start — no capture chain")
            assert elements[index - 1]["factory"] == "jpegenc", (
                "capture sink for {0!r} not preceded by jpegenc".format(
                    location))
            assert elements[index - 2]["factory"] == "videoconvert", (
                "capture sink for {0!r} not preceded by videoconvert".format(
                    location))
            locations.append(location)
    return locations


def bindings_by_node(document):
    return {binding["nodeId"]: binding
            for binding in document["executorBindings"]}


# ---------------------------------------------------------------------------
# Property 2: Feeder capture files are shared, one sink per feeder
# ---------------------------------------------------------------------------


class TestFeederCaptureSharing:
    """**Feature: edge-vlm-image-inference, Property 2: Feeder capture
    files are shared, one sink per feeder**

    **Validates: Requirements 1.3**
    """

    @settings(deadline=None)
    @given(workflow=shared_feeder_workflows())
    def test_one_capture_sink_per_feeder_shared_by_all_consumers(self, workflow):
        """For any workflow where one video source feeds input ports of
        multiple ``llm_inference`` and/or ``bedrock_inference`` nodes,
        the compiled document contains exactly one capture sink chain
        per feeder, and every consuming binding's ``capturePaths`` entry
        for a fed port references its feeder's single capture path."""
        definition_json, consumers_by_feeder = workflow
        document = compile_document(definition_json)
        bindings = bindings_by_node(document)

        # Every consuming binding's fed ports reference exactly one
        # shared path per feeder; unfed bedrock reference ports are None.
        path_by_feeder = {}
        for feeder_id, consumers in consumers_by_feeder.items():
            feeder_paths = set()
            for node_id, kind, fed_ports in consumers:
                binding = bindings[node_id]
                assert binding["binding"] == kind
                capture_paths = binding["capturePaths"]
                for port in fed_ports:
                    path = capture_paths[port]
                    assert isinstance(path, str) and path.startswith(
                        WORK_DIR_PREFIX), (
                        "fed port {0}.{1} has no {{work_dir}}-rooted "
                        "capture path: {2!r}".format(node_id, port, path))
                    feeder_paths.add(path)
                if kind == "bedrock_inference" and "reference" not in fed_ports:
                    assert capture_paths["reference"] is None, (
                        "unfed reference port of {0} should map to "
                        "None".format(node_id))
            assert len(feeder_paths) == 1, (
                "feeder {0} serves {1} consumers but its consumers "
                "reference {2} distinct capture paths: {3!r}".format(
                    feeder_id, len(consumers), len(feeder_paths),
                    sorted(feeder_paths)))
            path_by_feeder[feeder_id] = feeder_paths.pop()

        # Distinct feeders never share a capture file.
        assert len(set(path_by_feeder.values())) == len(path_by_feeder), (
            "distinct feeders share a capture path: "
            "{0!r}".format(path_by_feeder))

        # Exactly one capture sink chain per feeder, each writing that
        # feeder's single shared path — no duplicates, no strays.
        locations = capture_sink_locations(document)
        assert Counter(locations) == Counter(path_by_feeder.values()), (
            "capture sink chains {0!r} do not match one-per-feeder "
            "paths {1!r}".format(sorted(locations),
                                 sorted(path_by_feeder.values())))
