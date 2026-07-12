#  Copyright  Amazon Web Services, Inc.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
"""Property-based test for the Marshal's Detections_Block emission.

Covers the object-detection-visualization design's Property 4 (the
Detections_Block is emitted and retains index plus label), guarding Requirements
1.6, 3.1 and 3.5.

``TritonPythonModel._generate_capture_meta_data`` (in
``marshal_for_capture_template.py``) is exercised directly. For any detection
capture -- a reused ``anomalies`` payload that is a non-empty list of entries
carrying a ``bounding_box`` -- the method must append a base64-encoded
``json_with_base64_encoding`` entry to ``deviceFleetAuxiliaryOutputs`` that
decodes to ``{"detections": {"0": {...}, ...}}``, where EVERY entry retains the
original numeric ``class_index`` and a non-empty human-readable ``class_label``
alongside its ``bounding_box`` and ``confidence``. Only valid (4-element box)
detections appear; the zero-object sentinel yields an empty detections map.

Importing ``marshal_for_capture_template.py`` requires the Triton Python-backend
module (``triton_python_backend_utils``) and ``cv2``, neither of which is needed
by ``_generate_capture_meta_data`` for a detection capture (no overlay is
encoded here -- that happens in ``execute``). Following the sibling
``test_lfv_detection_tensor_set.py`` pattern, both are stubbed in ``sys.modules``
before the module is loaded. ``resolve_class_label`` is imported from the REAL
``lyra_science_processing_utils.utils.class_label_map`` (available via
``PYTHONPATH=src/backend``), so the missing-label fallback resolves against the
real COCO map.
"""
import base64
import importlib.util
import json
import os
import sys
import types

import numpy as np
from hypothesis import given, settings
from hypothesis import strategies as st

_MARSHAL_TEMPLATE_PATH = os.path.join(
    os.getcwd(),
    "src",
    "backend",
    "dda_triton",
    "resources_for_copy",
    "marshal_for_capture_template.py",
)


def _load_marshal_template():
    """Load ``marshal_for_capture_template`` with the triton backend module and
    ``cv2`` stubbed in ``sys.modules`` (neither is used by the code path under
    test), returning the module object. ``resolve_class_label`` still imports
    from the real first-party package via PYTHONPATH=src/backend."""
    pb_utils_stub = types.ModuleType("triton_python_backend_utils")
    # The detection metadata path does not touch pb_utils; a bare module stub is
    # sufficient to satisfy the top-level import.
    sys.modules["triton_python_backend_utils"] = pb_utils_stub

    # Prefer the real cv2 when it is importable so we never pollute
    # ``sys.modules`` with a stub that would leak into sibling tests that need
    # the genuine library (e.g. the overlay-drawing tests). Only fall back to a
    # bare stub when cv2 is unavailable on the runner; the code path under test
    # does not touch cv2 either way.
    try:
        import cv2  # noqa: F401
    except ImportError:  # pragma: no cover - depends on runner
        sys.modules["cv2"] = types.ModuleType("cv2")

    spec = importlib.util.spec_from_file_location(
        "marshal_for_capture_template", _MARSHAL_TEMPLATE_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_MARSHAL_MODULE = _load_marshal_template()
TritonPythonModel = _MARSHAL_MODULE.TritonPythonModel


def _make_marshal_instance():
    """Build a ``TritonPythonModel`` instance with only the attributes that
    ``_generate_capture_meta_data`` reads, bypassing ``initialize`` (which parses
    a real Triton model config)."""
    instance = TritonPythonModel.__new__(TritonPythonModel)
    instance.model_name = "test_model"
    instance.model_version = "1"
    return instance


def _call_generate(instance, inference_anomalies):
    """Invoke ``_generate_capture_meta_data`` with the fixed sibling-test calling
    convention, varying only the detection payload."""
    capture_meta_data = {
        "capture_id": "test_capture",
        "capture_folder": "/tmp/captures",
        "workflow_id": "wf",
        "event_id": "test_capture",
        "device_fleet_name": "fleet",
    }
    return instance._generate_capture_meta_data(
        capture_meta_data=capture_meta_data,
        inference_output=np.uint8(1),
        time_str="2025-01-01T00:00:00",
        inference_confidence=np.float32(0.5),
        inference_mask=np.zeros((4, 4, 3)),
        inference_anomalies=inference_anomalies,
        inference_score=np.float32(0.5),
        input_image=np.zeros((4, 4, 3)),
    )


def _extract_detections_block(ret):
    """Find and decode the base64 ``json_with_base64_encoding`` auxiliary output,
    returning the parsed object (expected: ``{"detections": {...}}``) or ``None``
    when no such entry exists."""
    for aux in ret.get("deviceFleetAuxiliaryOutputs", []):
        if aux.get("observedContentType") == "json_with_base64_encoding":
            decoded = base64.b64decode(aux["data"]).decode()
            return json.loads(decoded)
    return None


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# A single detection object: a numeric ``class`` index string, a 4-element
# bounding_box, a confidence, and an optional embedded class_label. The
# class_label is deliberately sometimes present (non-empty), sometimes empty,
# and sometimes absent, to exercise both the payload-provided label and the
# missing-label fallback (re-resolve via COCO, then class-index string).
_coordinate = st.integers(min_value=0, max_value=3)
_class_index = st.integers(min_value=0, max_value=120).map(str)
_confidence = st.floats(
    min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False
)
_label_mode = st.sampled_from(["present", "empty", "absent"])


@st.composite
def _detection_object(draw):
    obj = {
        "bounding_box": [draw(_coordinate) for _ in range(4)],
        "class": draw(_class_index),
        "confidence": draw(_confidence),
    }
    mode = draw(_label_mode)
    if mode == "present":
        obj["class_label"] = draw(st.text(min_size=1, max_size=12))
    elif mode == "empty":
        obj["class_label"] = ""
    # "absent": leave class_label out entirely.
    return obj


_detection_payload = st.lists(_detection_object(), min_size=1, max_size=8)


# Feature: object-detection-visualization, Property 4: The Detections_Block is emitted and retains index plus label
# Validates: Requirements 1.6, 3.1, 3.5
@settings(max_examples=200)
@given(detections=_detection_payload)
def test_detections_block_emitted_retains_index_and_label(detections):
    """For any detection capture with valid boxes, the Detections_Block is
    present, has one entry per valid box, and every entry retains the original
    class_index and a non-empty human-readable class_label."""
    instance = _make_marshal_instance()
    ret = _call_generate(instance, detections)

    block = _extract_detections_block(ret)

    # Block present and correctly shaped.
    assert block is not None, "Detections_Block was not emitted"
    assert set(block.keys()) == {"detections"}
    det_map = block["detections"]
    assert isinstance(det_map, dict)

    # Every input object has a valid 4-element box, so the number of block
    # entries equals the number of valid detections fed in.
    valid = [d for d in detections if len(d["bounding_box"]) == 4]
    assert len(det_map) == len(valid)

    # Keys are the contiguous re-indexed strings "0","1",... in input order.
    assert set(det_map.keys()) == {str(i) for i in range(len(valid))}

    for i, original in enumerate(valid):
        entry = det_map[str(i)]
        # Every entry has both class_index and class_label keys.
        assert "class_index" in entry
        assert "class_label" in entry
        assert "bounding_box" in entry
        assert "confidence" in entry

        # class_index is retained: equals the original index fed in.
        assert entry["class_index"] == str(original["class"])
        assert entry["class_index"] != ""

        # class_label is a non-empty human-readable string.
        assert isinstance(entry["class_label"], str)
        assert entry["class_label"] != ""

        # bounding_box is retained unchanged.
        assert entry["bounding_box"] == original["bounding_box"]


# Feature: object-detection-visualization, Property 4: The Detections_Block is emitted and retains index plus label
# Validates: Requirements 1.6, 3.1, 3.5
@settings(max_examples=100)
@given(
    class_index=st.integers(min_value=0, max_value=120).map(str),
    label=st.text(min_size=1, max_size=12),
    confidence=_confidence,
)
def test_detections_block_uses_payload_label_when_present(
    class_index, label, confidence
):
    """When the payload embeds a non-empty class_label, the block preserves it
    verbatim while still retaining the original class_index."""
    instance = _make_marshal_instance()
    detections = [
        {
            "bounding_box": [1, 2, 3, 4],
            "class": class_index,
            "class_label": label,
            "confidence": confidence,
        }
    ]
    block = _extract_detections_block(_call_generate(instance, detections))

    assert block is not None
    entry = block["detections"]["0"]
    assert entry["class_index"] == class_index
    assert entry["class_label"] == label


# Feature: object-detection-visualization, Property 4: The Detections_Block is emitted and retains index plus label
# Validates: Requirements 1.6, 3.1, 3.5
def test_zero_object_sentinel_yields_empty_detections_map():
    """The zero-object sentinel is a detection capture: the block is still
    emitted, but with an empty detections map (the sentinel's empty bounding_box
    is filtered out)."""
    instance = _make_marshal_instance()
    sentinel = [
        {
            "bounding_box": [],
            "class": "",
            "class_label": "",
            "confidence": 0.0,
            "no_objects": True,
        }
    ]
    block = _extract_detections_block(_call_generate(instance, sentinel))

    assert block is not None, "Detections_Block must be emitted for the sentinel"
    assert block == {"detections": {}}


# Feature: object-detection-visualization, Property 4: The Detections_Block is emitted and retains index plus label
# Validates: Requirements 1.6, 3.1, 3.5
def test_missing_label_falls_back_to_coco_or_index_string():
    """When class_label is absent, the block re-resolves a non-empty label
    (COCO name for a known index, index string otherwise)."""
    instance = _make_marshal_instance()
    detections = [
        # Index 16 is a known COCO class ("dog"); index 999 is unknown.
        {"bounding_box": [0, 0, 2, 2], "class": "16", "confidence": 0.9},
        {"bounding_box": [1, 1, 3, 3], "class": "999", "confidence": 0.4},
    ]
    block = _extract_detections_block(_call_generate(instance, detections))

    assert block is not None
    det_map = block["detections"]
    assert det_map["0"]["class_index"] == "16"
    assert det_map["0"]["class_label"] != ""
    assert det_map["1"]["class_index"] == "999"
    # Unknown index falls back to the index string.
    assert det_map["1"]["class_label"] == "999"
