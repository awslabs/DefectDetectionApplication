# Copyright 2026 Amazon Web Services, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Property tests for the Bedrock processor extensions
(detection-guided-bedrock-inspection task 7.5, Properties 3, 4, 7).

- **Property 3: Crop containment** — every Detection_Crop rectangle,
  after margin expansion, is fully contained in the captured frame
  bounds and non-empty; a degenerate box yields a RECORDED error, not
  a zero-size crop.
- **Property 4: No-fallback invariant** — a Bedrock_Binding configured
  with ``reference_payload_path`` either sends exactly two images
  (crop/frame + payload reference) or records an error; it NEVER
  silently sends one.
- **Property 7: Additive-only compatibility** — with no new parameters
  configured, the bytes sent to Bedrock, the metadata keys, and the
  output-binding execution order are byte/order-identical to the
  pre-feature engine. The pre-feature oracle is implemented BY
  CONSTRUCTION: ``run_context=None`` is literally the pre-change code
  path shape (whose byte-identity to the pre-feature processor the
  existing ``test_bedrock_single_image_preservation`` suite pins), so
  invoking the SAME bindings once through that shape and once with a
  fully-populated RunContext and byte-comparing requests/metadata —
  mirroring the aravis-free identity test pattern — proves the feature
  is a no-op when unconfigured.

No network — injected fake invokers/clients throughout. The suite's
registered hypothesis profile (``engine-fast`` / ``ci``, see
``conftest.py``) governs example counts.
"""
import base64
import itertools
import os
import shutil
import tempfile

import numpy as np
import cv2
from hypothesis import given, settings
from hypothesis import strategies as st

import workflow_engine_test_utils  # noqa: F401 - sets COMPONENT_WORK_PATH

from workflow_engine.node_status import STATUS_FAILURE, NodeStatusCollector
from workflow_engine.output_bindings import (
    BedrockInferenceProcessor,
    OutputBindingProcessor,
    RunContext,
    compute_crop_box,
)

NODE_ID = "bedrock1"
FRAME_NAME = "bedrock_frame_cam.jpg"
CAPTURE_ID = "cap-1"
FRAME_WIDTH, FRAME_HEIGHT = 48, 32


# ---------------------------------------------------------------------------
# Harness (mirrors test_bedrock_detection_crop / _payload_reference)
# ---------------------------------------------------------------------------

class RecordingInvoker:
    """Injectable Bedrock invoker recording every call byte-for-byte."""

    def __init__(self, answer='{"is_anomalous": true, "confidence": 0.9}'):
        self.answer = answer
        self.calls = []

    def __call__(self, model, prompt, images, region, max_tokens):
        self.calls.append({
            "model": model,
            "prompt": prompt,
            "images": list(images),
            "region": region,
            "max_tokens": max_tokens,
        })
        return self.answer


def make_binding(node_id=NODE_ID, parameters=None, capture_paths=None):
    return {
        "nodeId": node_id,
        "binding": "bedrock_inference",
        "parameters": dict(parameters or {}),
        "upstreamNodeIds": ["cam"],
        "downstreamNodeIds": ["mqtt"],
        "capturePaths": (
            {"in": "{work_dir}/" + FRAME_NAME, "reference": None}
            if capture_paths is None else capture_paths
        ),
    }


def make_document(bindings):
    return {"schemaVersion": 1, "executorBindings": list(bindings)}


def write_frame(work_dir, width=FRAME_WIDTH, height=FRAME_HEIGHT,
                name=FRAME_NAME):
    """A real JPEG frame (gradient, so crops are decodable and sized)."""
    frame = np.zeros((height, width, 3), dtype=np.uint8)
    frame[:, :, 0] = np.linspace(0, 255, width, dtype=np.uint8)[None, :]
    frame[:, :, 1] = np.linspace(0, 255, height, dtype=np.uint8)[:, None]
    path = os.path.join(work_dir, name)
    assert cv2.imwrite(path, frame)
    return path


def decode_jpeg(data):
    return cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)


def encode_png():
    """A small real PNG the reference validator accepts."""
    array = np.arange(48, dtype=np.uint8).reshape(4, 4, 3)
    ok, encoded = cv2.imencode(".png", array)
    assert ok
    return encoded.tobytes()


PNG_BYTES = encode_png()
PNG_BASE64 = base64.b64encode(PNG_BYTES).decode("ascii")


# ---------------------------------------------------------------------------
# Property 3: Crop containment — the pure crop math
# ---------------------------------------------------------------------------

#: Marshal boxes are JSON floats from real model output: finite, and
#: intentionally allowed to fall outside / invert within the frame so
#: clamping and degeneracy are both exercised.
_box_coordinates = st.floats(
    min_value=-512.0, max_value=4608.0,
    allow_nan=False, allow_infinity=False,
)
_frame_dims = st.integers(min_value=1, max_value=4096)
_margins = st.floats(
    min_value=0.0, max_value=150.0, allow_nan=False, allow_infinity=False)
_source_dims = st.one_of(
    st.none(),
    st.tuples(
        st.floats(min_value=1.0, max_value=8192.0,
                  allow_nan=False, allow_infinity=False),
        st.floats(min_value=1.0, max_value=8192.0,
                  allow_nan=False, allow_infinity=False),
    ),
)


@given(
    box=st.tuples(_box_coordinates, _box_coordinates,
                  _box_coordinates, _box_coordinates),
    frame_width=_frame_dims,
    frame_height=_frame_dims,
    margin=_margins,
    source_dimensions=_source_dims,
)
def test_property_3_crop_box_contained_and_non_empty_or_none(
        box, frame_width, frame_height, margin, source_dimensions):
    """**Feature: detection-guided-bedrock-inspection, Property 3: Crop
    containment**

    For any detection box (including inverted, zero-area, and fully
    out-of-frame ones), frame dimensions, margin percentage, and
    source-dimension scaling input, ``compute_crop_box`` returns either
    ``None`` (degenerate — the caller records an error) or an integer
    rectangle fully contained in the frame bounds with strictly positive
    area — never a zero-size or out-of-bounds crop.

    **Validates: Requirements 2.2, 2.3**
    """
    result = compute_crop_box(
        box, frame_width, frame_height, margin, source_dimensions)
    if result is None:
        return
    x0, y0, x1, y1 = result
    assert all(isinstance(value, int) for value in result)
    assert 0 <= x0 < x1 <= frame_width
    assert 0 <= y0 < y1 <= frame_height


@given(
    box=st.tuples(_box_coordinates, _box_coordinates,
                  _box_coordinates, _box_coordinates),
    frame_width=_frame_dims,
    frame_height=_frame_dims,
    margin=_margins,
)
def test_property_3_margin_expansion_never_escapes_the_frame(
        box, frame_width, frame_height, margin):
    """**Feature: detection-guided-bedrock-inspection, Property 3: Crop
    containment**

    Margin expansion only ever grows the crop rectangle — and the grown
    rectangle is still clamped inside the frame: the margined crop
    contains the margin-free crop and both stay within bounds.

    **Validates: Requirements 2.2, 2.3**
    """
    plain = compute_crop_box(box, frame_width, frame_height, 0)
    margined = compute_crop_box(box, frame_width, frame_height, margin)
    if plain is None:
        # A degenerate box stays degenerate: margin is a percentage of
        # the box's own (non-positive) dimensions.
        return
    assert margined is not None
    px0, py0, px1, py1 = plain
    mx0, my0, mx1, my1 = margined
    assert mx0 <= px0 and my0 <= py0 and mx1 >= px1 and my1 >= py1
    assert 0 <= mx0 < mx1 <= frame_width
    assert 0 <= my0 < my1 <= frame_height


# ---------------------------------------------------------------------------
# Property 3: Crop containment — the full processor crop path
# ---------------------------------------------------------------------------

_crop_path_x = st.floats(
    min_value=-64.0, max_value=128.0, allow_nan=False, allow_infinity=False)
_crop_path_y = st.floats(
    min_value=-48.0, max_value=96.0, allow_nan=False, allow_infinity=False)


@given(
    x_a=_crop_path_x, x_b=_crop_path_x,
    y_a=_crop_path_y, y_b=_crop_path_y,
    margin=st.integers(min_value=0, max_value=100),
)
@settings(deadline=None)
def test_property_3_processor_sends_contained_crop_or_records_error(
        x_a, x_b, y_a, y_b, margin):
    """**Feature: detection-guided-bedrock-inspection, Property 3: Crop
    containment**

    For any detection box (well-formed, inverted, or out-of-frame) and
    any margin, a ``crop_detection_index`` binding either sends the
    invoker exactly one "Input image" that decodes to a non-empty crop
    no larger than the captured frame, or records an error at
    ``bedrock.{nodeId}.error`` without invoking Bedrock — exactly one of
    the two, never a zero-size crop and never a raise.

    **Validates: Requirements 2.2, 2.3**
    """
    detection = {
        "id": "d1e2f3a4", "label": "blue box", "confidence": 0.9,
        "x_min": x_a, "y_min": y_a, "x_max": x_b, "y_max": y_b,
    }
    work_dir = tempfile.mkdtemp(prefix="bedrock-property3-")
    try:
        write_frame(work_dir)
        invoker = RecordingInvoker()
        processor = BedrockInferenceProcessor(invoker=invoker)
        binding = make_binding(parameters={
            "crop_detection_index": 0, "crop_margin_percent": margin,
        })
        run_context = RunContext(
            tag_values={"detections": [detection]},
            output_dir=work_dir, capture_id=CAPTURE_ID)

        metadata = processor.process(
            make_document([binding]), {}, work_dir, run_context=run_context)

        entry = metadata["bedrock"][NODE_ID]
        if invoker.calls:
            # Success leg: exactly one call, one contained non-empty crop.
            assert len(invoker.calls) == 1
            images = invoker.calls[0]["images"]
            assert [label for label, _ in images] == ["Input image"]
            crop = decode_jpeg(images[0][1])
            assert crop is not None
            height, width = crop.shape[:2]
            assert 1 <= width <= FRAME_WIDTH
            assert 1 <= height <= FRAME_HEIGHT
            assert entry["detection_id"] == detection["id"]
            assert "error" not in entry
        else:
            # Degenerate leg: a recorded error outcome, never a raise,
            # never a zero-size crop (Requirement 2.4 gating).
            assert "error" in entry
            assert NODE_ID in entry["error"]
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Property 4: No-fallback invariant
# ---------------------------------------------------------------------------

@st.composite
def _reference_scenarios(draw):
    """One Payload_Reference configuration: ``(parameters, payload_json,
    expect_success)`` spanning every outcome class — resolvable data-URL
    / bare-base64 / nested-list values, unresolvable paths, prefix
    denial, non-image bytes, and missing trigger payloads."""
    kind = draw(st.sampled_from([
        "data_url", "bare_base64", "nested_list",
        "unresolvable_path", "prefix_denied", "non_image",
        "missing_payload", "non_string_value",
    ]))
    parameters = {}
    if kind == "data_url":
        payload = {"image": "data:image/png;base64," + PNG_BASE64}
        parameters["reference_payload_path"] = "image"
        return parameters, payload, True
    if kind == "bare_base64":
        payload = {"reference": PNG_BASE64}
        parameters["reference_payload_path"] = "reference"
        return parameters, payload, True
    if kind == "nested_list":
        index = draw(st.integers(min_value=0, max_value=2))
        refs = [{"id": "ref-{0}".format(i), "image": PNG_BASE64}
                for i in range(3)]
        parameters["reference_payload_path"] = \
            "refs.{0}.image".format(index)
        return parameters, {"refs": refs}, True
    if kind == "unresolvable_path":
        parameters["reference_payload_path"] = draw(st.sampled_from(
            ["refs.5.image", "refs.0.picture", "no.such.path"]))
        return parameters, {"refs": [{"image": PNG_BASE64}]}, False
    if kind == "prefix_denied":
        parameters["reference_payload_path"] = "image"
        parameters["allowed_uri_prefixes"] = \
            "https://trusted.example/\ns3://allowed-bucket/"
        return parameters, {"image": "s3://untrusted-bucket/ref.png"}, False
    if kind == "non_image":
        payload = {"image": base64.b64encode(
            b"definitely not an image").decode("ascii")}
        parameters["reference_payload_path"] = "image"
        return parameters, payload, False
    if kind == "missing_payload":
        parameters["reference_payload_path"] = "refs.0.image"
        return parameters, None, False
    # non_string_value: the path resolves to a dict, not image data.
    parameters["reference_payload_path"] = "refs.0"
    return parameters, {"refs": [{"image": PNG_BASE64}]}, False


@given(scenario=_reference_scenarios(), with_crop=st.booleans())
@settings(deadline=None)
def test_property_4_two_images_or_recorded_error_never_one(
        scenario, with_crop):
    """**Feature: detection-guided-bedrock-inspection, Property 4:
    No-fallback invariant**

    For any ``reference_payload_path`` configuration and any trigger
    payload (resolvable or not, allowed or denied, image or garbage,
    present or absent) — with or without a Detection_Crop on the same
    binding — the binding either invokes Bedrock exactly once with
    exactly two images (["Input image", "Reference image"]) or records
    an error at ``bedrock.{nodeId}.error`` with ZERO invocations. It
    never silently sends a single image.

    **Validates: Requirements 3.5**
    """
    parameters, payload_json, expect_success = scenario
    if with_crop:
        parameters = dict(parameters)
        parameters["crop_detection_index"] = 0
    detection = {
        "id": "aa11bb22", "label": "blue box", "confidence": 0.9,
        "x_min": 4.0, "y_min": 4.0, "x_max": 28.0, "y_max": 24.0,
    }
    work_dir = tempfile.mkdtemp(prefix="bedrock-property4-")
    try:
        write_frame(work_dir)
        invoker = RecordingInvoker()
        processor = BedrockInferenceProcessor(invoker=invoker)
        run_context = RunContext(
            tag_values={
                "detections": [detection],
                "trigger": {"payload_json": payload_json},
            },
            output_dir=work_dir, capture_id=CAPTURE_ID)

        metadata = processor.process(
            make_document([make_binding(parameters=parameters)]),
            {}, work_dir, run_context=run_context)

        entry = metadata["bedrock"][NODE_ID]
        if invoker.calls:
            assert len(invoker.calls) == 1
            labels = [label for label, _ in invoker.calls[0]["images"]]
            assert labels == ["Input image", "Reference image"], (
                "NO-FALLBACK VIOLATION: a reference_payload_path binding "
                "invoked Bedrock with {0!r}".format(labels))
            assert "error" not in entry
        else:
            assert "error" in entry, (
                "the binding neither invoked Bedrock nor recorded an error")
        assert bool(invoker.calls) == expect_success
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Property 7: Additive-only compatibility — Bedrock requests + metadata
# ---------------------------------------------------------------------------

_node_id_pools = st.permutations(["bedrock1", "bedrock_2", "n-42"])
_models = st.sampled_from(
    ["us.amazon.nova-lite-v1:0", "anthropic.claude-3-haiku-20240307-v1:0"])
_prompts = st.text(min_size=1, max_size=40)
_regions = st.sampled_from(["us-east-1", "eu-central-1"])
_max_tokens = st.integers(min_value=1, max_value=4096)
_frame_bytes = st.binary(min_size=1, max_size=64)
_tag_values = st.dictionaries(
    st.text(alphabet="abcdefghij_", min_size=1, max_size=8),
    st.one_of(st.booleans(), st.integers(), st.text(max_size=10)),
    max_size=3,
).filter(lambda d: not (
    {"is_anomalous", "confidence", "bedrock", "bedrock_text",
     "detections", "detection_count", "trigger"} & set(d)))


@st.composite
def _legacy_binding_sets(draw):
    """1-3 ``bedrock_inference`` bindings with ONLY pre-feature
    parameters (model/prompt/region/max_tokens; optionally a fed
    reference port) plus the frame files to write: ``(bindings,
    files)``."""
    count = draw(st.integers(min_value=1, max_value=3))
    node_ids = draw(_node_id_pools)[:count]
    bindings, files = [], {}
    for position, node_id in enumerate(node_ids):
        in_name = "in_{0}.jpg".format(position)
        files[in_name] = draw(_frame_bytes)
        capture_paths = {"in": "{work_dir}/" + in_name, "reference": None}
        if draw(st.booleans()):
            ref_name = "ref_{0}.jpg".format(position)
            files[ref_name] = draw(_frame_bytes)
            capture_paths["reference"] = "{work_dir}/" + ref_name
        bindings.append({
            "nodeId": node_id,
            "binding": "bedrock_inference",
            "parameters": {
                "model": draw(_models),
                "prompt": draw(_prompts),
                "region": draw(_regions),
                "max_tokens": draw(_max_tokens),
            },
            "upstreamNodeIds": ["cam"],
            "downstreamNodeIds": ["mqtt"],
            "capturePaths": capture_paths,
        })
    return bindings, files


@st.composite
def _detection_lists(draw):
    size = draw(st.integers(min_value=0, max_value=4))
    return [
        {
            "id": "{0:08x}".format(0xA0 + i),
            "label": draw(st.sampled_from(["blue box", "plate"])),
            "confidence": draw(st.floats(
                min_value=0.0, max_value=1.0, allow_nan=False)),
            "x_min": 1.0 * i, "y_min": 2.0 * i,
            "x_max": 10.0 + i, "y_max": 12.0 + i,
        }
        for i in range(size)
    ]


@given(
    binding_set=_legacy_binding_sets(),
    tag_values=_tag_values,
    detections=_detection_lists(),
)
@settings(deadline=None)
def test_property_7_requests_and_metadata_identical_without_new_params(
        binding_set, tag_values, detections):
    """**Feature: detection-guided-bedrock-inspection, Property 7:
    Additive-only compatibility**

    For any set of ``bedrock_inference`` bindings carrying ONLY
    pre-feature parameters, running the processor through the
    pre-feature code path shape (``run_context=None`` — the oracle by
    construction) and through a fully-populated RunContext (detections
    merged, trigger payload present, node-status collector attached)
    produces byte-identical invoker calls (model, prompt, image bytes,
    region, max_tokens — the bytes sent to Bedrock) and identical
    returned metadata (flat keys included), with no node marked failed.

    **Validates: Requirements 7.1, 7.3**
    """
    bindings, files = binding_set
    work_dir = tempfile.mkdtemp(prefix="bedrock-property7-")
    try:
        for name, payload in files.items():
            with open(os.path.join(work_dir, name), "wb") as f:
                f.write(payload)
        document = make_document(bindings)

        oracle_invoker = RecordingInvoker()
        oracle_metadata = BedrockInferenceProcessor(
            invoker=oracle_invoker,
        ).process(document, dict(tag_values), work_dir, run_context=None)

        node_ids = [binding["nodeId"] for binding in bindings]
        collector = NodeStatusCollector(extra_node_ids=node_ids)
        run_context = RunContext(
            tag_values={
                **tag_values,
                "detections": detections,
                "detection_count": len(detections),
                "trigger": {"payload_json": {
                    "refs": [{"id": "plate-A", "image": PNG_BASE64}]}},
            },
            output_dir=work_dir,
            capture_id=CAPTURE_ID,
            graph_document={"nodes": [], "connections": []},
            node_status=collector,
        )
        feature_invoker = RecordingInvoker()
        feature_metadata = BedrockInferenceProcessor(
            invoker=feature_invoker,
        ).process(
            document, dict(tag_values), work_dir, run_context=run_context)

        assert feature_invoker.calls == oracle_invoker.calls, (
            "ADDITIVE-ONLY REGRESSION (Property 7): the bytes sent to "
            "Bedrock changed with an unconfigured feature")
        assert feature_metadata == oracle_metadata, (
            "ADDITIVE-ONLY REGRESSION (Property 7): the run metadata "
            "changed with an unconfigured feature")
        failed = [node_id for node_id in node_ids
                  if collector.status_of(node_id) == STATUS_FAILURE]
        assert failed == [], (
            "ADDITIVE-ONLY REGRESSION (Property 7): legacy binding(s) "
            "{0!r} were marked failed".format(failed))
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Property 7: Additive-only compatibility — output-binding execution order
# ---------------------------------------------------------------------------

#: Pre-feature-expressible conditions over the flat keys only.
_flat_conditions = st.sampled_from([
    None, "true", "false", "is_anomalous", "confidence >= 0.5",
    "is_anomalous == true && confidence >= 0.5",
])

_output_kinds = st.sampled_from(
    ["mqtt_publish", "opcua_write", "digital_output"])


@st.composite
def _flat_output_documents(draw):
    """0-8 runner-backed output bindings whose parameters (topics,
    payload templates, values, conditions) reference flat keys only —
    everything a pre-feature workflow could express."""
    bindings = []
    tokens = itertools.count(100)
    for position in range(draw(st.integers(min_value=0, max_value=8))):
        kind = draw(_output_kinds)
        token = next(tokens)
        node_id = "n{0}".format(position)
        if kind == "mqtt_publish":
            parameters = {
                "broker_host": "b",
                "topic": str(token),
                "payload_template":
                    "anomaly={is_anomalous} confidence={confidence}",
            }
        elif kind == "opcua_write":
            parameters = {
                "endpoint": "opc.tcp://plc.local:4840",
                "node_id": str(token),
                "value": "{confidence}",
            }
        else:
            parameters = {
                "pin": token, "signal_type": "high", "pulse_width_ms": 10,
            }
        condition = draw(_flat_conditions)
        if condition is not None:
            parameters["condition"] = condition
        bindings.append({
            "nodeId": node_id,
            "binding": kind,
            "parameters": parameters,
            "upstreamNodeIds": [],
            "downstreamNodeIds": [],
        })
    return {
        "schemaVersion": 1,
        "workflowId": "wf-1",
        "workflowVersion": "3",
        "targetArch": "aarch64-jp5",
        "segments": [],
        "executorBindings": bindings,
        "pluginDependencies": [],
    }


class _RecordingClients:
    """Injectable fake clients appending full call tuples to ONE ordered
    log, so cross-runner ordering AND rendered content are observable."""

    def __init__(self):
        self.log = []

    def mqtt(self, *args):
        self.log.append(("mqtt_publish",) + args)

    def opcua(self, *args):
        self.log.append(("opcua_write",) + args)

    def dio(self, *args):
        self.log.append(("digital_output",) + args)


@given(
    document=_flat_output_documents(),
    is_anomalous=st.booleans(),
    confidence=st.sampled_from([0.0, 0.3, 0.5, 0.7, 1.0]),
    detections=_detection_lists(),
)
@settings(deadline=None)
def test_property_7_output_binding_order_unchanged_by_additive_keys(
        document, is_anomalous, confidence, detections):
    """**Feature: detection-guided-bedrock-inspection, Property 7:
    Additive-only compatibility**

    For any pre-feature-expressible output-binding document (templates
    and conditions over the flat keys only), running the output
    processor over the pre-feature metadata (the oracle by
    construction) and over the same metadata extended with the
    feature's additive keys (``detections``, ``detection_count``, the
    nested ``bedrock`` per-node verdicts) produces the exact same
    ordered runner invocation sequence with the exact same rendered
    arguments.

    **Validates: Requirements 7.1, 7.3**
    """
    base_metadata = {"is_anomalous": is_anomalous, "confidence": confidence}
    additive_metadata = {
        **base_metadata,
        "detections": detections,
        "detection_count": len(detections),
        "bedrock": {"bedrock1": {
            "is_anomalous": is_anomalous, "confidence": confidence,
            "text": "verdict", "detection_id": "aa11bb22",
        }},
    }

    oracle_clients = _RecordingClients()
    OutputBindingProcessor(
        dio_actuator=oracle_clients.dio,
        mqtt_publisher=oracle_clients.mqtt,
        opcua_writer=oracle_clients.opcua,
    )(None, document, dict(base_metadata))

    feature_clients = _RecordingClients()
    OutputBindingProcessor(
        dio_actuator=feature_clients.dio,
        mqtt_publisher=feature_clients.mqtt,
        opcua_writer=feature_clients.opcua,
    )(None, document, dict(additive_metadata))

    assert feature_clients.log == oracle_clients.log, (
        "ADDITIVE-ONLY REGRESSION (Property 7): the output-binding "
        "execution order or rendered content changed under the additive "
        "metadata keys")
