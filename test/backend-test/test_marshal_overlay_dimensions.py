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
"""Property-based test for the Marshal's overlay reference + overlay dimensions.

Feature: object-detection-visualization, Property 6: Detection captures always get
an overlay reference of source dimensions.

For any detection capture with an empty anomaly mask (including the zero-object
sentinel), two things must hold:

  (a) ``_generate_capture_meta_data`` includes an Overlay_Image
      Auxiliary_Output_Reference in ``ret["deviceFleetAuxiliaryOutputs"]`` -- an
      entry whose ``observedContentType == "overlay.jpg"``; and
  (b) the overlay produced by ``_generate_detection_overlay(input_image,
      detections)`` has the SAME dimensions (``.shape``) as the source input
      image (an unannotated copy when there are no valid boxes).

Importing ``marshal_for_capture_template.py`` requires the Triton Python-backend
module (``triton_python_backend_utils``), which is stubbed in ``sys.modules``
before the module is loaded. Unlike the pure-metadata tests, this test exercises
the real overlay-drawing path (``image.copy()`` + ``cv2.rectangle`` +
``cv2.putText``), so the REAL ``cv2`` is imported -- it must produce a
correctly-shaped array for (b) to be meaningful. ``resolve_class_label`` resolves
for real via ``PYTHONPATH=src/backend``.
"""
import importlib.util
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


def _load_marshal_module():
    """Load ``marshal_for_capture_template`` with only triton stubbed.

    The REAL ``cv2`` is used so the overlay-drawing path
    (``_generate_detection_overlay``) actually copies the source image and draws
    boxes, letting assertion (b) verify true output dimensions."""
    pb_utils_stub = types.ModuleType("triton_python_backend_utils")
    pb_utils_stub.Tensor = object
    pb_utils_stub.triton_string_to_numpy = lambda s: np.float32
    sys.modules["triton_python_backend_utils"] = pb_utils_stub

    # Intentionally do NOT stub cv2: the real library must draw the overlay.
    import cv2  # noqa: F401  (import for real; asserts availability at load time)

    spec = importlib.util.spec_from_file_location(
        "marshal_for_capture_template_overlay_under_test", _MARSHAL_TEMPLATE_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_MARSHAL_MODULE = _load_marshal_module()
TritonPythonModel = _MARSHAL_MODULE.TritonPythonModel


def _make_marshal_instance():
    """Bare instance carrying only the attributes ``_generate_capture_meta_data``
    reads (used solely to fill ``eventMetadata``), bypassing ``initialize``."""
    instance = TritonPythonModel.__new__(TritonPythonModel)
    instance.model_name = "m"
    instance.model_version = "1"
    return instance


def _has_overlay_ref(ret):
    """True iff an overlay.jpg Auxiliary_Output_Reference is present."""
    return any(
        aux.get("observedContentType") == "overlay.jpg"
        for aux in ret["deviceFleetAuxiliaryOutputs"]
    )


# ---- Detection-payload + source-image generators ----------------------------

_ZERO_OBJECT_SENTINEL = [
    {"bounding_box": [], "class": "", "class_label": "", "confidence": 0.0, "no_objects": True}
]

# Boxes are constrained to small pixel coords compatible with the tiny source
# images generated below; the overlay code clamps to image bounds regardless.
_box = st.lists(st.integers(min_value=0, max_value=40), min_size=4, max_size=4)
_class_index = st.one_of(
    st.integers(min_value=0, max_value=90).map(str),
    st.text(min_size=0, max_size=5),
)
_class_label = st.one_of(st.just(""), st.text(min_size=0, max_size=12))
_confidence = st.floats(
    min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False
)

_detection_entry = st.fixed_dictionaries(
    {
        "bounding_box": _box,
        "class": _class_index,
        "class_label": _class_label,
        "confidence": _confidence,
    }
)

# A detection payload: a non-empty list of real detections (varying count) OR
# the zero-object sentinel. Both are recognized by _is_detection_list.
_detection_payload = st.one_of(
    st.lists(_detection_entry, min_size=1, max_size=6),
    st.just(_ZERO_OBJECT_SENTINEL),
)

_np_float32 = st.floats(
    min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False
).map(np.float32)

# Random small source-image shapes: H, W in [2..40], 3 channels, uint8.
_dim = st.integers(min_value=2, max_value=40)


# Feature: object-detection-visualization, Property 6: Detection captures always get an overlay reference of source dimensions
# Validates: Requirements 2.1, 2.3, 2.4, 2.5
@settings(max_examples=25)
@given(
    inference_anomalies=_detection_payload,
    height=_dim,
    width=_dim,
    output_flag=st.integers(min_value=0, max_value=1),
    inference_confidence=_np_float32,
    inference_score=_np_float32,
)
def test_detection_capture_gets_overlay_ref_of_source_dimensions(
    inference_anomalies, height, width, output_flag, inference_confidence, inference_score
):
    instance = _make_marshal_instance()
    # All-zero mask -> _has_anomaly_mask() is False, so the overlay ref decision
    # is driven solely by detection classification (empty-mask detection case).
    inference_mask = np.zeros((height, width, 3), dtype=np.uint8)
    input_image = np.zeros((height, width, 3), dtype=np.uint8)
    capture_meta_data = {
        "capture_id": "cap-1",
        "workflow_id": "wf-1",
        "capture_folder": "/tmp/captures/wf-1",
        "event_id": "cap-1",
        "device_fleet_name": "fleet-1",
    }

    # Precondition: this payload is indeed classified as a detection.
    assert TritonPythonModel._is_detection_list(inference_anomalies)

    ret = instance._generate_capture_meta_data(
        capture_meta_data=capture_meta_data,
        inference_output=np.uint8(output_flag),
        time_str="2025-01-01T00:00:00",
        inference_confidence=inference_confidence,
        inference_mask=inference_mask,
        inference_anomalies=inference_anomalies,
        inference_score=inference_score,
        input_image=input_image,
    )

    # (a) An overlay.jpg auxiliary-output reference must be present for the
    # empty-mask detection capture, including the zero-object sentinel.
    assert _has_overlay_ref(ret), (
        "detection capture with empty mask must emit an overlay.jpg reference"
    )

    # (b) The generated overlay must have the SAME dimensions as the source
    # image (unannotated copy when there are no valid boxes).
    overlay = instance._generate_detection_overlay(input_image, inference_anomalies)
    assert overlay.shape == input_image.shape, (
        f"overlay shape {overlay.shape} != source shape {input_image.shape}"
    )


# Feature: object-detection-visualization, Property 6: Detection captures always get an overlay reference of source dimensions
# Validates: Requirements 2.1, 2.3, 2.4, 2.5
def test_zero_object_sentinel_overlay_is_unannotated_source_copy():
    """Deterministic edge case: the zero-object sentinel yields an overlay ref
    and an overlay whose dimensions equal the (unannotated) source image."""
    instance = _make_marshal_instance()
    input_image = np.zeros((16, 24, 3), dtype=np.uint8)
    ret = instance._generate_capture_meta_data(
        capture_meta_data={
            "capture_id": "cap-0",
            "workflow_id": "wf-0",
            "capture_folder": "/tmp/captures/wf-0",
            "event_id": "cap-0",
            "device_fleet_name": "fleet-0",
        },
        inference_output=np.uint8(1),
        time_str="2025-01-01T00:00:00",
        inference_confidence=np.float32(0.0),
        inference_mask=np.zeros((16, 24, 3), dtype=np.uint8),
        inference_anomalies=_ZERO_OBJECT_SENTINEL,
        inference_score=np.float32(0.0),
        input_image=input_image,
    )
    assert _has_overlay_ref(ret)

    overlay = instance._generate_detection_overlay(input_image, _ZERO_OBJECT_SENTINEL)
    assert overlay.shape == input_image.shape
    # No valid boxes -> unannotated copy: identical pixels to the source.
    assert np.array_equal(overlay, input_image)
