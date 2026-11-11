"""Unit tests for compiling the ``s3_upload`` output node.

On device architectures the node is executor-level and opaque: the
compiler terminates the GStreamer branch feeding its VideoFrames input
in the same synthetic frame-capture sink chain bedrock_inference uses
(``videoconvert ! capsfilter I420 ! jpegenc ! multifilesink
location={work_dir}/bedrock_frame_<feeder>.jpg``) and emits an
``s3_upload`` executor binding carrying the node's parameters plus
``capturePaths["in"]``. A source fanning out to one upload and to a
Custom Python node feeding a second upload yields a tee and two distinct
capture files. In simulation the node resolves to its recording stub.

Preservation: the committed golden ``golden_s3_upload_preservation.json``
holds the compiled documents the PINNED (pre-feature) compiler produced
for representative legacy workflows on every device architecture. The
current compiler must reproduce them byte-for-byte, except that every
``emlpython`` element now carries ``wall-clock-limit-sec`` (10, the
default): launch strings are unchanged because the executor's
Python_Bridge drops the element's arguments when it rewrites it.
Regenerate the golden only after a deliberate, reviewed compiler change:

    cd edge-cv-portal/backend/layers/workflow_core && \\
        PYTHONPATH=python python3 -c \\
        "from tests.test_compiler_s3_upload import _write_golden; _write_golden()"
"""

from __future__ import annotations

import copy
import json
import os

from workflow_core.catalog import DEVICE_ARCHITECTURES
from workflow_core.compiler import CompiledPipelineDocument, compile
from workflow_core.serializer import (
    Connection,
    Node,
    PortEndpoint,
    Position,
    WorkflowGraph,
)
from workflow_core.validator import validate

ARCH = "arm64_jp7"
_GOLDEN_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "golden_s3_upload_preservation.json",
)
_CAPTURE_CHAIN = ["videoconvert", "capsfilter", "jpegenc", "multifilesink"]


def _node(node_id, type_id, **parameters):
    return Node(id=node_id, type=type_id, position=Position(0.0, 0.0),
                parameters=parameters)


def _connect(cid, source, target):
    return Connection(
        id=cid,
        source=PortEndpoint(node=source[0], port=source[1]),
        target=PortEndpoint(node=target[0], port=target[1]),
    )


def _graph(nodes, connections):
    return WorkflowGraph(nodes=nodes, connections=connections)


def _compile_ok(graph, arch=ARCH, simulation=False):
    document = compile(graph, arch, simulation=simulation)
    assert isinstance(document, CompiledPipelineDocument), document
    return document


def _all_elements(document):
    return [e for s in document.segments for e in s["elements"]]


def _bindings(document, kind="s3_upload"):
    return [b for b in document.executor_bindings if b["binding"] == kind]


def _capture_sinks(document):
    return [e for e in _all_elements(document) if e["factory"] == "multifilesink"]


def _upload(node_id, **extra):
    parameters = {
        "destination": "s3://example-bucket/{trigger.payload_json.body.image_key}",
    }
    parameters.update(extra)
    return _node(node_id, "s3_upload", **parameters)


def single_upload_graph():
    """trigger -> camera.activation; camera -> upload."""
    return _graph(
        nodes=[
            _node("trigger", "mqtt_subscribe", topic="plant/line1/evaluate",
                  greengrass=True, qos=1),
            _node("camera", "aravis_camera_source", camera_id="cam-01"),
            _upload("upload"),
        ],
        connections=[
            _connect("c1", ("trigger", "out"), ("camera", "activation")),
            _connect("c2", ("camera", "out"), ("upload", "in")),
        ],
    )


def fan_out_graph():
    """The two-upload shape: the camera fans out to a raw upload and to a
    frames-in/frames-out Custom Python node feeding an annotated upload;
    both uploads are gated by a dotted trigger-payload condition."""
    condition = 'trigger.payload_json.body.stage == "final"'
    return _graph(
        nodes=[
            _node("trigger", "mqtt_subscribe", topic="plant/line1/evaluate",
                  greengrass=True, qos=1, concurrency_policy="drop",
                  queue_depth=1),
            _node("camera", "aravis_camera_source", camera_id="cam-01"),
            _upload("upload_raw", condition=condition),
            _node("evaluate", "custom_python",
                  code="def process_frame(frame, metadata):\n    return frame",
                  input_port_type="VideoFrames", output_port_type="VideoFrames",
                  wall_clock_limit_s=18),
            _upload("upload_annotated", condition=condition,
                    strip_suffix=".jpg", append_suffix=".annotated.jpg"),
        ],
        connections=[
            _connect("c1", ("trigger", "out"), ("camera", "activation")),
            _connect("c2", ("camera", "out"), ("upload_raw", "in")),
            _connect("c3", ("camera", "out"), ("evaluate", "in")),
            _connect("c4", ("evaluate", "out"), ("upload_annotated", "in")),
        ],
    )


class TestDeviceCompilation:
    def test_input_branch_terminates_in_a_capture_sink(self):
        document = _compile_ok(single_upload_graph())
        camera_segment = [
            s for s in document.segments
            if any(e["nodeId"] == "camera" for e in s["elements"])
        ]
        assert len(camera_segment) == 1
        tail = [e["factory"] for e in camera_segment[0]["elements"]][-4:]
        assert tail == _CAPTURE_CHAIN
        sinks = _capture_sinks(document)
        assert len(sinks) == 1
        assert sinks[0]["args"]["location"] == "{work_dir}/bedrock_frame_camera.jpg"
        assert sinks[0]["nodeId"] is None
        capsfilter = [e for e in _all_elements(document)
                      if e["factory"] == "capsfilter" and e["nodeId"] is None]
        assert capsfilter[0]["args"] == {"caps": "video/x-raw,format=I420"}

    def test_binding_carries_parameters_and_capture_path(self):
        document = _compile_ok(single_upload_graph())
        (binding,) = _bindings(document)
        assert binding["nodeId"] == "upload"
        assert binding["capturePaths"] == {"in": "{work_dir}/bedrock_frame_camera.jpg"}
        assert binding["upstreamNodeIds"] == ["camera"]
        assert binding["downstreamNodeIds"] == []
        assert binding["parameters"] == {
            "destination": "s3://example-bucket/{trigger.payload_json.body.image_key}",
            "content_type": "image/jpeg",
            "strip_suffix": None,
            "append_suffix": None,
            "condition": None,
            "timeout_s": 10,
            "retries": 1,
            "region": None,
            # bedrock-inspection-chain defaults (declared-default emission).
            "source": "frame",
            "presign_get_expiry_s": 0,
            "expected_bucket_owner": None,
        }
        # No element is tagged with the upload node: it is executor-level.
        assert not [e for e in _all_elements(document) if e["nodeId"] == "upload"]

    def test_plugin_dependencies_include_boto3_runtime(self):
        document = _compile_ok(single_upload_graph())
        assert "python:boto3" in document.plugin_dependencies

    def test_fan_out_yields_a_tee_two_capture_files_and_two_bindings(self):
        document = _compile_ok(fan_out_graph())
        factories = [e["factory"] for e in _all_elements(document)]
        assert "tee" in factories
        sinks = _capture_sinks(document)
        locations = sorted(s["args"]["location"] for s in sinks)
        assert locations == [
            "{work_dir}/bedrock_frame_camera.jpg",
            "{work_dir}/bedrock_frame_evaluate.jpg",
        ]
        by_id = {b["nodeId"]: b for b in _bindings(document)}
        assert set(by_id) == {"upload_raw", "upload_annotated"}
        assert by_id["upload_raw"]["capturePaths"] == {
            "in": "{work_dir}/bedrock_frame_camera.jpg"}
        assert by_id["upload_annotated"]["capturePaths"] == {
            "in": "{work_dir}/bedrock_frame_evaluate.jpg"}
        assert by_id["upload_annotated"]["parameters"]["strip_suffix"] == ".jpg"
        assert by_id["upload_annotated"]["parameters"]["append_suffix"] == ".annotated.jpg"
        for binding in by_id.values():
            assert binding["parameters"]["condition"] == \
                'trigger.payload_json.body.stage == "final"'
        # The Custom Python node's emlpython element carries the configured
        # wall clock; the executor's bridge reads it before rewriting.
        (emlpython,) = [e for e in _all_elements(document)
                        if e["factory"] == "emlpython"]
        assert emlpython["nodeId"] == "evaluate"
        assert emlpython["args"] == {
            "handler-path": "python/evaluate/handler.py",
            "wall-clock-limit-sec": 18,
        }

    def test_fan_out_graph_compiles_on_every_device_architecture(self):
        for arch in DEVICE_ARCHITECTURES:
            document = _compile_ok(fan_out_graph(), arch=arch)
            assert len(_bindings(document)) == 2, arch
            assert len(_capture_sinks(document)) == 2, arch

    def test_fan_out_graph_validates_with_zero_errors(self):
        findings = validate(fan_out_graph())
        errors = [f for f in findings if f.severity == "error"]
        assert errors == [], errors

    def test_default_wall_clock_is_compiled_as_the_integer_default(self):
        graph = _graph(
            nodes=[
                _node("src", "folder_source", location="/data/images"),
                _node("inf", "model_inference", modelName="widget-anomaly-v3"),
                _node("py", "custom_python", code="def handle(b, m):\n    return b, m"),
                _node("mqtt", "mqtt_publish", broker_host="broker.local",
                      topic="plant/line1/result"),
            ],
            connections=[
                _connect("c1", ("src", "out"), ("inf", "in")),
                _connect("c2", ("inf", "out"), ("py", "in")),
                _connect("c3", ("py", "out"), ("mqtt", "in")),
            ],
        )
        document = _compile_ok(graph)
        (emlpython,) = [e for e in _all_elements(document)
                        if e["factory"] == "emlpython"]
        assert emlpython["args"]["wall-clock-limit-sec"] == 10


def bedrock_fed_graph(annotated_source="annotated_image"):
    """The inspection chain (bedrock-inspection-chain): trigger -> camera ->
    bedrock check -> two uploads fed by the check -> one publish fed by
    both uploads' result ports."""
    return _graph(
        nodes=[
            _node("trigger", "mqtt_subscribe", topic="plant/line1/evaluate",
                  greengrass=True, qos=1),
            _node("camera", "aravis_camera_source", camera_id="cam-01"),
            _node("check", "bedrock_inference", answer_format="inspection",
                  on_error="record"),
            _upload("upload_raw", source="frame"),
            _upload("upload_annotated", source=annotated_source,
                    strip_suffix=".jpg", append_suffix=".annotated.jpg"),
            _node("mqtt", "mqtt_publish", topic="plant/line1/result",
                  greengrass=True, payload_format="json",
                  payload_template='{"key": "{s3_upload.upload_raw.key}"}'),
        ],
        connections=[
            _connect("c1", ("trigger", "out"), ("camera", "activation")),
            _connect("c2", ("camera", "out"), ("check", "in")),
            _connect("c3", ("check", "out"), ("upload_raw", "in")),
            _connect("c4", ("check", "out"), ("upload_annotated", "in")),
            _connect("c5", ("upload_raw", "out"), ("mqtt", "in")),
            _connect("c6", ("upload_annotated", "out"), ("mqtt", "in")),
        ],
    )


class TestBedrockFedUploads:
    def test_uploads_behind_the_bedrock_node_share_its_capture_path(self):
        # The feeder search of each upload stops at the opaque Bedrock
        # node, so the plan hands both uploads the Bedrock node's own
        # ``in`` capture path: one sink, one file, three readers.
        document = _compile_ok(bedrock_fed_graph())
        sinks = _capture_sinks(document)
        assert [s["args"]["location"] for s in sinks] == \
            ["{work_dir}/bedrock_frame_camera.jpg"]
        (check,) = [b for b in document.executor_bindings
                    if b["binding"] == "bedrock_inference"]
        assert check["capturePaths"] == {
            "in": "{work_dir}/bedrock_frame_camera.jpg", "reference": None}
        by_id = {b["nodeId"]: b for b in _bindings(document)}
        assert set(by_id) == {"upload_raw", "upload_annotated"}
        for binding in by_id.values():
            assert binding["capturePaths"] == {
                "in": "{work_dir}/bedrock_frame_camera.jpg"}
            assert binding["upstreamNodeIds"] == ["check"]
            assert binding["downstreamNodeIds"] == ["mqtt"]
        assert by_id["upload_raw"]["parameters"]["source"] == "frame"
        assert by_id["upload_annotated"]["parameters"]["source"] == "annotated_image"

    def test_input_image_source_gets_the_same_path(self):
        document = _compile_ok(bedrock_fed_graph(annotated_source="input_image"))
        by_id = {b["nodeId"]: b for b in _bindings(document)}
        assert by_id["upload_annotated"]["capturePaths"] == {
            "in": "{work_dir}/bedrock_frame_camera.jpg"}

    def test_publish_fed_by_two_result_ports_lists_both_uploads_upstream(self):
        document = _compile_ok(bedrock_fed_graph())
        (mqtt,) = [b for b in document.executor_bindings
                   if b["binding"] == "mqtt_publish"]
        assert mqtt["upstreamNodeIds"] == ["upload_raw", "upload_annotated"]
        assert mqtt["parameters"]["payload_format"] == "json"
        # The result port carries no frames: no element follows an upload.
        assert len(_capture_sinks(document)) == 1

    def test_chain_compiles_on_every_device_architecture_and_validates(self):
        for arch in DEVICE_ARCHITECTURES:
            document = _compile_ok(bedrock_fed_graph(), arch=arch)
            assert len(_bindings(document)) == 2, arch
        errors = [f for f in validate(bedrock_fed_graph()) if f.severity == "error"]
        assert errors == [], errors


class TestSimulationCompilation:
    def test_simulation_resolves_the_recording_stub_without_captures(self):
        document = _compile_ok(single_upload_graph(), arch="x86_64", simulation=True)
        assert _bindings(document) == []
        (stub,) = _bindings(document, "recording_s3_upload")
        assert stub["nodeId"] == "upload"
        assert "capturePaths" not in stub
        assert _capture_sinks(document) == []


# ---------------------------------------------------------------------------
# Preservation against the pinned (pre-feature) compiler's documents
# ---------------------------------------------------------------------------

def _legacy_workflows():
    """Representative pre-feature workflows (no s3_upload node): the
    classic fixtures of test_compiler_compile.py / test_compiler_bedrock.py
    plus a bridged Custom Python graph and a subscribe-triggered graph."""
    folder_capture = _graph(
        nodes=[_node("src", "folder_source", location="/data/images"),
               _node("cap", "capture", output_path="/out")],
        connections=[_connect("c1", ("src", "out"), ("cap", "in"))],
    )
    icam_inference_mqtt = _graph(
        nodes=[_node("cam", "icam_source", device="/dev/video0"),
               _node("rot", "rotate", method="clockwise"),
               _node("inf", "model_inference", modelName="widget-anomaly-v3"),
               _node("mqtt", "mqtt_publish", broker_host="10.0.0.12",
                     topic="factory/line1")],
        connections=[_connect("c1", ("cam", "out"), ("rot", "in")),
                     _connect("c2", ("rot", "out"), ("inf", "in")),
                     _connect("c3", ("inf", "out"), ("mqtt", "in"))],
    )
    fan_out_captures = _graph(
        nodes=[_node("src", "folder_source", location="/data/images"),
               _node("rot", "rotate", method="clockwise"),
               _node("cap1", "capture", output_path="/out1"),
               _node("cap2", "capture", output_path="/out2")],
        connections=[_connect("c1", ("src", "out"), ("rot", "in")),
                     _connect("c2", ("rot", "out"), ("cap1", "in")),
                     _connect("c3", ("src", "out"), ("cap2", "in"))],
    )
    custom_python_mqtt = _graph(
        nodes=[_node("src", "folder_source", location="/data/images"),
               _node("inf", "model_inference", modelName="widget-anomaly-v3"),
               _node("py", "custom_python",
                     code="def handle(b, m):\n    return b, m"),
               _node("pre", "custom_python_preprocess",
                     code="def process_frame(f, m):\n    return f"),
               _node("mqtt", "mqtt_publish", broker_host="10.0.0.12",
                     topic="factory/line1")],
        connections=[_connect("c1", ("src", "out"), ("pre", "in")),
                     _connect("c2", ("pre", "out"), ("inf", "in")),
                     _connect("c3", ("inf", "out"), ("py", "in")),
                     _connect("c4", ("py", "out"), ("mqtt", "in"))],
    )
    bedrock_two_source = _graph(
        nodes=[_node("cam", "icam_source"),
               _node("ref", "folder_source", location="/aws_dda/ref/golden.jpg"),
               _node("bedrock", "bedrock_inference"),
               _node("mqtt", "mqtt_publish", broker_host="10.0.0.12",
                     topic="factory/line1")],
        connections=[_connect("c1", ("cam", "out"), ("bedrock", "in")),
                     _connect("c2", ("ref", "out"), ("bedrock", "reference")),
                     _connect("c3", ("bedrock", "out"), ("mqtt", "in"))],
    )
    triggered_filter_dio = _graph(
        nodes=[_node("trigger", "mqtt_subscribe", topic="plant/line1/evaluate",
                     greengrass=True, qos=1),
               _node("camera", "aravis_camera_source", camera_id="cam-01"),
               _node("inf", "model_inference", modelName="widget-anomaly-v3"),
               _node("flt", "inference_filter", condition="is_anomalous == true"),
               _node("dio", "digital_output", pin=7, condition="is_anomalous == true")],
        connections=[_connect("c1", ("trigger", "out"), ("camera", "activation")),
                     _connect("c2", ("camera", "out"), ("inf", "in")),
                     _connect("c3", ("inf", "out"), ("flt", "in")),
                     _connect("c4", ("flt", "out"), ("dio", "in"))],
    )
    return [
        ("folder_capture", folder_capture),
        ("icam_inference_mqtt", icam_inference_mqtt),
        ("fan_out_captures", fan_out_captures),
        ("custom_python_mqtt", custom_python_mqtt),
        ("bedrock_two_source", bedrock_two_source),
        ("triggered_filter_dio", triggered_filter_dio),
    ]


def _current_golden_payload() -> dict:
    payload = {}
    for name, graph in _legacy_workflows():
        per_arch = {}
        for arch in DEVICE_ARCHITECTURES:
            result = compile(graph, arch)
            if isinstance(result, CompiledPipelineDocument):
                per_arch[arch] = {"status": "ok", "document": result.to_dict()}
            else:
                per_arch[arch] = {"status": "error",
                                  "errors": [e.to_dict() for e in result]}
        payload[name] = per_arch
    return payload


def _canonical_bytes(payload: dict) -> str:
    return json.dumps(payload, sort_keys=True, indent=2, ensure_ascii=True)


def _write_golden() -> None:  # pragma: no cover - deliberate manual step
    with open(_GOLDEN_PATH, "w") as handle:
        handle.write(_canonical_bytes(_current_golden_payload()) + "\n")


def _strip_wall_clock(payload: dict) -> dict:
    """The payload with ``wall-clock-limit-sec`` removed from every
    emlpython element — the only compile-output delta the feature makes
    on pre-feature workflows."""
    stripped = copy.deepcopy(payload)
    for per_arch in stripped.values():
        for outcome in per_arch.values():
            for segment in (outcome.get("document") or {}).get("segments", []):
                for element in segment.get("elements", []):
                    if element.get("factory") == "emlpython":
                        element["args"].pop("wall-clock-limit-sec", None)
    return stripped


class TestLegacyWorkflowPreservation:
    def test_documents_match_the_pinned_compiler_except_the_wall_clock_arg(self):
        assert os.path.exists(_GOLDEN_PATH), _GOLDEN_PATH
        with open(_GOLDEN_PATH) as handle:
            golden = handle.read()
        current = _current_golden_payload()
        assert _canonical_bytes(_strip_wall_clock(current)) + "\n" == golden, (
            "compiled documents of pre-feature workflows drifted from the "
            "pinned compiler's output beyond the emlpython wall-clock argument"
        )

    def test_every_emlpython_element_carries_the_default_wall_clock(self):
        seen = 0
        for per_arch in _current_golden_payload().values():
            for outcome in per_arch.values():
                for segment in outcome["document"]["segments"]:
                    for element in segment["elements"]:
                        if element["factory"] == "emlpython":
                            assert element["args"]["wall-clock-limit-sec"] == 10
                            seen += 1
        assert seen == 2 * len(DEVICE_ARCHITECTURES)

    def test_golden_has_no_wall_clock_argument(self):
        with open(_GOLDEN_PATH) as handle:
            assert "wall-clock-limit-sec" not in handle.read()
