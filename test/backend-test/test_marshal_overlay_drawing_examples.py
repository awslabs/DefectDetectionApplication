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
"""Example + edge tests for the Marshal detection overlay path.

# Feature: object-detection-visualization (tasks 3.4 / 2.5 -- example/edge tests,
# NOT a numbered Property test)

These tests exercise the real overlay-drawing path in
``marshal_for_capture_template.py``:

- Example (Requirement 3.4): a single labeled box is drawn with the resolved
  ``class_label`` + confidence onto the overlay. Exact pixels/text are brittle to
  assert, so we verify (1) the output shape equals the input shape and (2) the
  output differs from the untouched input copy (drawing actually occurred).
- Example (one-object end-to-end metadata, Requirement 3.4): a single-object
  detection payload run through ``_generate_capture_meta_data`` yields
  ``Inference result == "Detection"``, ``Detection_count == 1``, capture
  ``Confidence`` equal to the object confidence, a ``Detections_Block`` with one
  entry carrying ``class_index``/``class_label``, and an ``overlay.jpg`` auxiliary
  reference.
- Edge (Requirement 2.5): the zero-object sentinel produces an UNANNOTATED copy of
  the source (nothing drawn, same shape), and ``_generate_capture_meta_data`` for
  the sentinel yields ``Inference result == "Detection"``, ``Detection_count == 0``
  and an ``overlay.jpg`` reference.

``_generate_detection_overlay`` uses REAL cv2 (``rectangle`` / ``putText`` /
``copy``), which is available on this runner, so cv2 is NOT stubbed. Only the
Triton Python-backend module (``triton_python_backend_utils``) is stubbed in
``sys.modules`` before importing the marshal template, following the established
``test_marshal_detection_typing.py`` pattern. The instance is built via
``TritonPythonModel.__new__`` (bypassing ``initialize``) with just the attributes
``_generate_capture_meta_data`` reads. Run with::

    PYTHONPATH=src/backend python3 -m pytest \
        test/backend-test/test_marshal_overlay_drawing_examples.py -v
"""
import base64
import importlib.util
import json
import os
import sys
import types

import numpy as np
import pytest

_MARSHAL_TEMPLATE_PATH = os.path.join(
    os.getcwd(),
    "src",
    "backend",
    "dda_triton",
    "resources_for_copy",
    "marshal_for_capture_template.py",
)


def _load_marshal_module():
    """Load ``marshal_for_capture_template`` with ONLY triton stubbed.

    cv2 is intentionally NOT stubbed: the overlay-drawing path under test relies
    on real ``cv2.rectangle`` / ``cv2.putText`` / ``ndarray.copy`` behavior, and
    cv2 is available on this runner."""
    pb_utils_stub = types.ModuleType("triton_python_backend_utils")
    pb_utils_stub.Tensor = object
    pb_utils_stub.triton_string_to_numpy = lambda s: np.float32
    sys.modules["triton_python_backend_utils"] = pb_utils_stub

    spec = importlib.util.spec_from_file_location(
        "marshal_for_capture_template_overlay_examples", _MARSHAL_TEMPLATE_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_MARSHAL_MODULE = _load_marshal_module()
TritonPythonModel = _MARSHAL_MODULE.TritonPythonModel

_ZERO_OBJECT_SENTINEL = [
    {"bounding_box": [], "class": "", "class_label": "", "confidence": 0.0, "no_objects": True}
]


def _make_marshal_instance():
    """Bare instance carrying only the attributes ``_generate_capture_meta_data``
    reads (used to fill ``eventMetadata``), bypassing ``initialize``."""
    instance = TritonPythonModel.__new__(TritonPythonModel)
    instance.model_name = "m"
    instance.model_version = "1"
    return instance


def _extract_aux(ret, content_type):
    """Decode a base64 auxiliary output by observedContentType."""
    for aux in ret["deviceFleetAuxiliaryOutputs"]:
        if aux.get("observedContentType") == content_type:
            return json.loads(base64.b64decode(aux["data"]).decode())
    raise AssertionError(f"no '{content_type}' aux output found")


def _overlay_ref_present(ret):
    return any(
        aux.get("observedContentType") == "overlay.jpg"
        for aux in ret["deviceFleetAuxiliaryOutputs"]
    )


# ---------------------------------------------------------------------------
# Example (Requirement 3.4): a single labeled box is drawn on the overlay.
# ---------------------------------------------------------------------------

# Feature: object-detection-visualization (task 3.4)
def test_single_box_is_drawn_on_overlay():
    instance = _make_marshal_instance()
    image = np.zeros((100, 120, 3), dtype=np.uint8)
    detection = {
        "bounding_box": [10, 20, 80, 70],  # 4-element box inside the image
        "class": "17",
        "class_label": "dog",
        "confidence": 0.83,
    }

    overlay = instance._generate_detection_overlay(image, [detection])

    # (1) The overlay preserves the source image dimensions.
    assert overlay.shape == image.shape
    # (2) Something was actually drawn: the overlay differs from the untouched
    # input for a case with a valid box.
    assert not np.array_equal(overlay, image)


# ---------------------------------------------------------------------------
# Example (Requirement 3.4): one-object end-to-end capture metadata.
# ---------------------------------------------------------------------------

# Feature: object-detection-visualization (task 3.4)
def test_single_object_capture_metadata_end_to_end():
    instance = _make_marshal_instance()
    object_confidence = 0.83
    detection_payload = [
        {
            "bounding_box": [12, 40, 100, 90],
            "class": "17",
            "class_label": "dog",
            "confidence": object_confidence,
        }
    ]
    # Empty mask so the detection classification alone drives typing/overlay.
    inference_mask = np.zeros((100, 120, 3), dtype=np.uint8)
    input_image = np.zeros((100, 120, 3), dtype=np.uint8)
    capture_meta_data = {
        "capture_id": "cap-1",
        "workflow_id": "wf-1",
        "capture_folder": "/tmp/captures/wf-1",
        "event_id": "cap-1",
        "device_fleet_name": "fleet-1",
    }

    ret = instance._generate_capture_meta_data(
        capture_meta_data=capture_meta_data,
        inference_output=np.uint8(1),
        time_str="2025-01-01T00:00:00",
        inference_confidence=np.float32(object_confidence),
        inference_mask=inference_mask,
        inference_anomalies=detection_payload,
        inference_score=np.float32(object_confidence),
        input_image=input_image,
    )

    inf_result = _extract_aux(ret, "json")
    assert inf_result["Inference result"] == "Detection"
    assert inf_result["Detection_count"] == 1
    assert inf_result["Confidence"] == pytest.approx(object_confidence, rel=1e-6)

    detections_block = _extract_aux(ret, "json_with_base64_encoding")["detections"]
    assert len(detections_block) == 1
    entry = detections_block["0"]
    assert entry["class_index"] == "17"
    assert entry["class_label"] == "dog"

    # An overlay.jpg auxiliary reference is present for the detection capture.
    assert _overlay_ref_present(ret)


# ---------------------------------------------------------------------------
# Edge (Requirement 2.5): zero-object sentinel produces an unannotated overlay
# and still types the capture as a Detection with count 0 and an overlay ref.
# ---------------------------------------------------------------------------

# Feature: object-detection-visualization (task 2.5)
def test_zero_object_sentinel_overlay_is_unannotated():
    instance = _make_marshal_instance()
    image = np.zeros((100, 120, 3), dtype=np.uint8)

    overlay = instance._generate_detection_overlay(image, _ZERO_OBJECT_SENTINEL)

    # Same shape as the source, and nothing drawn (an unannotated copy).
    assert overlay.shape == image.shape
    assert np.array_equal(overlay, image)


# Feature: object-detection-visualization (task 2.5)
def test_zero_object_sentinel_capture_metadata():
    instance = _make_marshal_instance()
    inference_mask = np.zeros((100, 120, 3), dtype=np.uint8)
    input_image = np.zeros((100, 120, 3), dtype=np.uint8)
    capture_meta_data = {
        "capture_id": "cap-0",
        "workflow_id": "wf-0",
        "capture_folder": "/tmp/captures/wf-0",
        "event_id": "cap-0",
        "device_fleet_name": "fleet-0",
    }

    ret = instance._generate_capture_meta_data(
        capture_meta_data=capture_meta_data,
        inference_output=np.uint8(1),
        time_str="2025-01-01T00:00:00",
        inference_confidence=np.float32(0.0),
        inference_mask=inference_mask,
        inference_anomalies=_ZERO_OBJECT_SENTINEL,
        inference_score=np.float32(0.0),
        input_image=input_image,
    )

    inf_result = _extract_aux(ret, "json")
    assert inf_result["Inference result"] == "Detection"
    assert inf_result["Detection_count"] == 0
    # Overlay ref present even for the zero-object case.
    assert _overlay_ref_present(ret)
