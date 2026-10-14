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
"""Property test for the Inspection's Annotated_Image
(imts-triple-inspection-hmi task 1.4, Property 19).

**Feature: imts-triple-inspection-hmi, Property 19: Annotated frame
renders exactly the answer's valid Defect_Object boxes**

*For any* crop bytes and any Bedrock answer text (a valid ``objects``
list, an invalid one, mixed valid and invalid entries, fenced or
prose-wrapped JSON, or no ``objects`` key at all):

- the ``annotated`` artifact is persisted if and only if the answer
  yields a parseable ``objects`` list;
- every rendered box is the clamped intersection of its Defect_Object's
  ``bounding_box`` with the crop bounds;
- entries whose box is missing, malformed, or empty after clamping are
  skipped without affecting the valid entries;
- the persisted ``original`` artifact bytes equal the exact crop sent to
  Bedrock;
- and the existing ``is_anomalous``/``confidence`` parsing behavior for
  the same answer text is byte-identical to today, including its failure
  behavior.

**Validates: Requirements 4.4, 4.12**

No network — an injected fake invoker returns the generated answer text.
Synthetic crop arrays (real JPEGs on disk) keep the crop path decodable.
"""
import json
import math
import os
import shutil
import tempfile

import cv2
import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

import workflow_engine_test_utils  # noqa: F401 - sets COMPONENT_WORK_PATH

from workflow_engine.output_bindings import (
    ANNOTATED_FRAME_ARTIFACT_TEMPLATE,
    ORIGINAL_FRAME_ARTIFACT_TEMPLATE,
    CROP_JPEG_QUALITY,
    BedrockInferenceError,
    BedrockInferenceProcessor,
    RunContext,
    clamp_defect_box,
    draw_defect_objects,
    extract_defect_objects,
    parse_bedrock_answer,
    sanitize_node_id_for_artifact,
)

NODE_ID = "bedrock1"
FRAME_NAME = "triple_frame_cam.jpg"
CAPTURE_ID = "cap-19"
FRAME_WIDTH, FRAME_HEIGHT = 64, 48
#: A comfortably in-bounds detection, so the crop path always succeeds
#: and the answer text is the only variable of the property.
DETECTION = {
    "id": "aa11bb22", "label": "blue box", "confidence": 0.9,
    "x_min": 4.0, "y_min": 4.0, "x_max": 60.0, "y_max": 44.0,
}


# ---------------------------------------------------------------------------
# Harness (mirrors test_property_bedrock_inspection)
# ---------------------------------------------------------------------------

class RecordingInvoker:
    """Injectable Bedrock invoker returning a scripted answer and
    recording the images it was sent."""

    def __init__(self, answer):
        self.answer = answer
        self.calls = []

    def __call__(self, model, prompt, images, region, max_tokens):
        self.calls.append({"images": list(images)})
        return self.answer


def make_binding(parameters):
    return {
        "nodeId": NODE_ID,
        "binding": "bedrock_inference",
        "parameters": dict(parameters),
        "upstreamNodeIds": ["cam"],
        "downstreamNodeIds": ["mqtt"],
        "capturePaths": {
            "in": "{work_dir}/" + FRAME_NAME, "reference": None},
    }


def write_frame(work_dir):
    """A real synthetic JPEG frame (gradient, so crops decode)."""
    frame = np.zeros((FRAME_HEIGHT, FRAME_WIDTH, 3), dtype=np.uint8)
    frame[:, :, 0] = np.linspace(0, 255, FRAME_WIDTH, dtype=np.uint8)[None, :]
    frame[:, :, 1] = np.linspace(0, 255, FRAME_HEIGHT, dtype=np.uint8)[:, None]
    frame[:, :, 2] = 127
    path = os.path.join(work_dir, FRAME_NAME)
    assert cv2.imwrite(path, frame)
    return path


def synthetic_crop(width, height):
    """A synthetic crop array with distinguishable pixels (so a drawn box
    is always observable against the background)."""
    array = np.zeros((height, width, 3), dtype=np.uint8)
    array[:, :, 0] = np.linspace(10, 240, width, dtype=np.uint8)[None, :]
    array[:, :, 1] = np.linspace(20, 200, height, dtype=np.uint8)[:, None]
    array[:, :, 2] = 80
    return array


def decode_jpeg(data):
    return cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)


def artifact_path(work_dir, template):
    return os.path.join(work_dir, template.format(
        capture_id=CAPTURE_ID,
        safe_node_id=sanitize_node_id_for_artifact(NODE_ID),
    ))


def expected_clamped_box(bounding_box, width, height):
    """The spec's reference clamp: the intersection of the box with the
    ``width`` x ``height`` image bounds, as an integer pixel rectangle
    (mins floored, maxes ceiled), or ``None`` when the box is missing,
    malformed, or empty after clamping (Requirement 4.12)."""
    if not isinstance(bounding_box, dict):
        return None
    values = []
    for key in ("x_min", "y_min", "x_max", "y_max"):
        value = bounding_box.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        value = float(value)
        if math.isnan(value) or math.isinf(value):
            return None
        values.append(value)
    x_min, y_min, x_max, y_max = values
    x0 = int(math.floor(min(max(x_min, 0.0), float(width))))
    y0 = int(math.floor(min(max(y_min, 0.0), float(height))))
    x1 = int(math.ceil(min(max(x_max, 0.0), float(width))))
    y1 = int(math.ceil(min(max(y_max, 0.0), float(height))))
    if x1 <= x0 or y1 <= y0:
        return None
    return x0, y0, x1, y1


def renderable_entries(objects, width, height):
    """The entries of ``objects`` whose box survives clamping — the ones
    an Annotated_Image must render, in answer order."""
    return [
        entry for entry in objects
        if isinstance(entry, dict)
        and expected_clamped_box(entry.get("bounding_box"), width, height)
        is not None
    ]


# ---------------------------------------------------------------------------
# Generators: Defect_Object entries and Bedrock answer texts
# ---------------------------------------------------------------------------

#: Box coordinates deliberately reach outside the crop and invert, so
#: clamping, out-of-frame boxes, and empty-after-clamping boxes are all
#: exercised.
_coordinate = st.floats(
    min_value=-40.0, max_value=140.0,
    allow_nan=False, allow_infinity=False,
)
_qc = st.sampled_from(["OK", "NOK", "ok", "nok", "", None])
_name = st.sampled_from(["scratch", "dent", "blue plate", "", None])
#: JSON-safe filler text (no braces / no backticks, so wrapping the
#: answer in prose or a fenced block stays well-formed).
_safe_text = st.text(alphabet="abcXYZ 019_-", max_size=6)


@st.composite
def _valid_entry(draw):
    """A Defect_Object with a numeric ``bounding_box`` — in bounds,
    partly out of bounds, fully out of bounds, or inverted."""
    return {
        "name": draw(_name),
        "qc": draw(_qc),
        "bounding_box": {
            "x_min": draw(_coordinate), "y_min": draw(_coordinate),
            "x_max": draw(_coordinate), "y_max": draw(_coordinate),
        },
    }


@st.composite
def _invalid_entry(draw):
    """An entry the Annotated_Image must skip: no box, a non-dict box, a
    box missing a key, or non-numeric coordinates — plus non-dict
    entries entirely."""
    kind = draw(st.sampled_from([
        "no_box", "box_none", "box_list", "box_string", "missing_key",
        "string_coords", "bool_coords", "not_a_dict",
    ]))
    if kind == "no_box":
        return {"name": draw(_name), "qc": draw(_qc)}
    if kind == "box_none":
        return {"name": draw(_name), "bounding_box": None}
    if kind == "box_list":
        return {"name": draw(_name), "bounding_box": [0, 0, 10, 10]}
    if kind == "box_string":
        return {"name": draw(_name), "bounding_box": draw(_safe_text)}
    if kind == "missing_key":
        box = {"x_min": 1.0, "y_min": 2.0, "x_max": 20.0, "y_max": 30.0}
        del box[draw(st.sampled_from(list(box)))]
        return {"name": draw(_name), "bounding_box": box}
    if kind == "string_coords":
        return {"name": draw(_name), "bounding_box": {
            "x_min": "1", "y_min": "2", "x_max": "20", "y_max": "30"}}
    if kind == "bool_coords":
        return {"name": draw(_name), "bounding_box": {
            "x_min": False, "y_min": True, "x_max": True, "y_max": True}}
    return draw(st.one_of(
        st.none(), _safe_text, st.integers(min_value=-5, max_value=5),
        st.lists(st.integers(min_value=0, max_value=3), max_size=2),
    ))


#: Mixed valid / invalid ``objects`` lists, including the empty list (a
#: clean part) — JSON-serializable throughout.
_json_objects = st.lists(
    st.one_of(_valid_entry(), _invalid_entry()), max_size=5)

#: Additionally NaN/infinite coordinates, which only the pure draw path
#: can be handed (json.dumps would emit non-standard literals).
_raw_objects = st.lists(
    st.one_of(
        _valid_entry(),
        _invalid_entry(),
        st.fixed_dictionaries({"bounding_box": st.fixed_dictionaries({
            "x_min": st.sampled_from(
                [float("nan"), float("inf"), -float("inf"), 0.0]),
            "y_min": st.just(0.0),
            "x_max": st.sampled_from([float("nan"), float("inf"), 10.0]),
            "y_max": st.just(10.0),
        })}),
    ),
    max_size=5,
)


@st.composite
def _answers(draw):
    """``(answer_text, expected_objects)`` where ``expected_objects`` is
    the ``objects`` list the answer carries by construction (``None``
    when it carries none): fenced, prose-wrapped, plain,
    objects-less, non-list ``objects``, and wholly non-JSON variants."""
    kind = draw(st.sampled_from([
        "plain", "fenced_json", "fenced_bare", "prose",
        "no_objects", "non_list_objects", "garbage",
    ]))
    if kind == "garbage":
        return draw(st.sampled_from([
            "", "The part looks fine.", "objects: none",
            "no json here at all", "```\nstill no json\n```",
        ])), None

    body = {}
    if draw(st.booleans()):
        # A verdict alongside the objects, so the existing
        # is_anomalous/confidence parse is exercised in both directions.
        body["is_anomalous"] = draw(st.booleans())
        body["confidence"] = draw(st.floats(
            min_value=0.0, max_value=1.0,
            allow_nan=False, allow_infinity=False))
    expected = None
    if kind == "non_list_objects":
        body["objects"] = {"count": draw(st.integers(0, 3))}
    elif kind != "no_objects":
        expected = draw(_json_objects)
        body["objects"] = expected
    if not body:
        body["note"] = draw(_safe_text)

    payload = json.dumps(body)
    if kind == "fenced_json":
        text = "Result below.\n```json\n{0}\n```\n".format(payload)
    elif kind == "fenced_bare":
        text = "```\n{0}\n```".format(payload)
    elif kind == "prose":
        text = "Here is the inspection result:\n{0}\nEnd of report.".format(
            payload)
    else:
        text = payload
    return text, expected


# ---------------------------------------------------------------------------
# Property 19: the boxes an Annotated_Image renders
# ---------------------------------------------------------------------------

@settings(max_examples=100, deadline=None)
@given(objects=_raw_objects, width=st.integers(8, 96),
       height=st.integers(8, 96))
def test_property_19_draws_exactly_the_clamped_valid_boxes(
        objects, width, height):
    """**Feature: imts-triple-inspection-hmi, Property 19: Annotated
    frame renders exactly the answer's valid Defect_Object boxes**

    For any ``objects`` list (valid, invalid, and mixed entries) and any
    crop size, the Annotated_Image draw renders exactly one rectangle
    per entry whose ``bounding_box`` clamps to a non-empty intersection
    with the crop bounds, in answer order — every rendered rectangle
    being that clamped intersection (it contains the real intersection,
    stays inside the crop, and is non-empty), and every entry with a
    missing, malformed, or empty-after-clamping box being skipped.

    **Validates: Requirements 4.4, 4.12**
    """
    frame = synthetic_crop(width, height)
    drawn = draw_defect_objects(frame, objects)

    expected = [
        expected_clamped_box(entry["bounding_box"], width, height)
        for entry in renderable_entries(objects, width, height)
    ]
    assert drawn == expected, (
        "the Annotated_Image did not render exactly the clamped valid "
        "Defect_Object boxes")
    for entry, box in zip(renderable_entries(objects, width, height), drawn):
        x0, y0, x1, y1 = box
        # Inside the crop bounds and non-empty.
        assert 0 <= x0 < x1 <= width
        assert 0 <= y0 < y1 <= height
        # Contains the real-valued intersection of the box with the crop.
        bounding_box = entry["bounding_box"]
        assert x0 <= min(max(float(bounding_box["x_min"]), 0.0), width)
        assert y0 <= min(max(float(bounding_box["y_min"]), 0.0), height)
        assert x1 >= min(max(float(bounding_box["x_max"]), 0.0), width)
        assert y1 >= min(max(float(bounding_box["y_max"]), 0.0), height)
        # The same rectangle the clamp helper reports for that entry.
        assert clamp_defect_box(bounding_box, width, height) == box


@settings(max_examples=100, deadline=None)
@given(objects=_raw_objects, width=st.integers(8, 96),
       height=st.integers(8, 96))
def test_property_19_skipped_entries_never_affect_valid_ones(
        objects, width, height):
    """**Feature: imts-triple-inspection-hmi, Property 19: Annotated
    frame renders exactly the answer's valid Defect_Object boxes**

    Entries whose box is missing, malformed, or empty after clamping are
    skipped WITHOUT affecting the valid entries: rendering the mixed
    list and rendering only its renderable entries produce pixel-identical
    Annotated_Images and the identical rectangle list.

    **Validates: Requirements 4.12**
    """
    mixed_frame = synthetic_crop(width, height)
    valid_frame = synthetic_crop(width, height)

    mixed_drawn = draw_defect_objects(mixed_frame, objects)
    valid_drawn = draw_defect_objects(
        valid_frame, renderable_entries(objects, width, height))

    assert mixed_drawn == valid_drawn
    assert np.array_equal(mixed_frame, valid_frame), (
        "a skipped Defect_Object changed the rendering of the valid ones")


# ---------------------------------------------------------------------------
# Property 19: the persisted artifacts
# ---------------------------------------------------------------------------

@settings(max_examples=100, deadline=None)
@given(answer=_answers())
def test_property_19_annotated_persisted_iff_the_answer_yields_objects(
        answer):
    """**Feature: imts-triple-inspection-hmi, Property 19: Annotated
    frame renders exactly the answer's valid Defect_Object boxes**

    For any answer text — fenced, prose-wrapped, plain, objects-less,
    non-list ``objects``, or wholly non-JSON — the crop path persists the
    ``annotated`` artifact if and only if the answer yields a parseable
    ``objects`` list; the artifact decodes at the crop's own dimensions
    with exactly the answer's valid boxes drawn (an empty ``objects: []``
    persisting the crop unchanged with zero boxes); and the ``original``
    artifact bytes always equal the exact crop sent to Bedrock.

    **Validates: Requirements 4.4, 4.12**
    """
    answer_text, expected_objects = answer
    work_dir = tempfile.mkdtemp(prefix="triple-annotated-")
    try:
        write_frame(work_dir)
        invoker = RecordingInvoker(answer_text)
        run_context = RunContext(
            tag_values={"detections": [DETECTION]},
            output_dir=work_dir, capture_id=CAPTURE_ID)

        # Freeform mode: the answer format never fails, so the persist
        # decision is a pure function of the answer's objects list.
        BedrockInferenceProcessor(invoker=invoker).process(
            {"schemaVersion": 1, "executorBindings": [make_binding({
                "crop_detection_index": 0, "anomaly_mode": False,
            })]},
            {}, work_dir, run_context=run_context)

        assert len(invoker.calls) == 1
        crop_bytes = invoker.calls[0]["images"][0][1]

        # The Original_Image is the exact crop sent to Bedrock.
        original_path = artifact_path(
            work_dir, ORIGINAL_FRAME_ARTIFACT_TEMPLATE)
        assert os.path.exists(original_path)
        with open(original_path, "rb") as original_file:
            assert original_file.read() == crop_bytes

        # The Annotated_Image exists exactly when the answer yields a
        # parseable objects list.
        annotated_path = artifact_path(
            work_dir, ANNOTATED_FRAME_ARTIFACT_TEMPLATE)
        assert extract_defect_objects(answer_text) == expected_objects
        assert os.path.exists(annotated_path) == (expected_objects is not None)
        if expected_objects is None:
            return

        crop = decode_jpeg(crop_bytes)
        height, width = crop.shape[:2]
        with open(annotated_path, "rb") as annotated_file:
            annotated_bytes = annotated_file.read()
        annotated = decode_jpeg(annotated_bytes)
        assert annotated is not None
        assert annotated.shape == crop.shape

        # Exactly the answer's valid boxes were rendered onto the crop.
        reference = crop.copy()
        drawn = draw_defect_objects(reference, expected_objects)
        assert drawn == [
            expected_clamped_box(entry["bounding_box"], width, height)
            for entry in renderable_entries(expected_objects, width, height)
        ]
        ok, encoded = cv2.imencode(
            ".jpg", reference,
            [int(cv2.IMWRITE_JPEG_QUALITY), CROP_JPEG_QUALITY])
        assert ok
        assert annotated_bytes == bytes(encoded.tobytes()), (
            "the Annotated_Image is not the crop with exactly the "
            "answer's valid Defect_Object boxes drawn")
        if not expected_objects:
            # A clean part: the crop, unchanged, with zero boxes.
            assert np.array_equal(reference, crop)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


@settings(max_examples=100, deadline=None)
@given(answer=_answers())
def test_property_19_verdict_parsing_behavior_unchanged(answer):
    """**Feature: imts-triple-inspection-hmi, Property 19: Annotated
    frame renders exactly the answer's valid Defect_Object boxes**

    The additive Annotated_Image never disturbs the existing
    ``is_anomalous``/``confidence`` parse: for the same answer text the
    anomaly-mode outcome still matches ``parse_bedrock_answer`` exactly —
    including its failure behavior (an unparseable answer still raises
    ``BedrockInferenceError`` naming the node, with no Annotated_Image
    persisted) — and the run metadata shape stays byte-identical to
    today's (no ``objects`` merged anywhere).

    **Validates: Requirements 4.4, 4.12**
    """
    answer_text, _expected_objects = answer
    try:
        oracle = parse_bedrock_answer(answer_text)
    except ValueError:
        oracle = None

    work_dir = tempfile.mkdtemp(prefix="triple-annotated-parse-")
    try:
        write_frame(work_dir)
        processor = BedrockInferenceProcessor(
            invoker=RecordingInvoker(answer_text))
        document = {
            "schemaVersion": 1,
            "executorBindings": [make_binding({"crop_detection_index": 0})],
        }
        run_context = RunContext(
            tag_values={"detections": [DETECTION]},
            output_dir=work_dir, capture_id=CAPTURE_ID)
        annotated_path = artifact_path(
            work_dir, ANNOTATED_FRAME_ARTIFACT_TEMPLATE)

        if oracle is None:
            with pytest.raises(BedrockInferenceError) as excinfo:
                processor.process(
                    document, {}, work_dir, run_context=run_context)
            assert excinfo.value.node_id == NODE_ID
            assert not os.path.exists(annotated_path)
            return

        metadata = processor.process(
            document, {}, work_dir, run_context=run_context)

        assert metadata["is_anomalous"] == oracle["is_anomalous"]
        assert metadata["confidence"] == oracle["confidence"]
        assert metadata["bedrock_text"] == answer_text
        assert set(metadata) == {
            "is_anomalous", "confidence", "bedrock_text", "bedrock"}
        entry = metadata["bedrock"][NODE_ID]
        assert set(entry) == {
            "text", "detection_id", "is_anomalous", "confidence"}
        assert entry["is_anomalous"] == oracle["is_anomalous"]
        assert entry["confidence"] == oracle["confidence"]
        assert entry["detection_id"] == DETECTION["id"]
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
