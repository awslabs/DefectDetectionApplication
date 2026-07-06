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
"""Property-based test for the Marshal's detection-result typing.

Feature: object-detection-visualization, Property 2: Detection captures receive a
distinct inference-result type.

For any capture classified as a Detection_Result, ``_generate_capture_meta_data``
must set the Capture_Metadata ``Inference result`` to the detection-specific value
(``"Detection"``) and never to the anomaly-classification value (``"Anomaly"`` /
``"Normal"``). The ``inf_result`` is base64-encoded inside
``ret["deviceFleetAuxiliaryOutputs"]`` with ``observedContentType == "json"`` --
decode it to assert. The detection form must likewise surface in
``deviceGroundTruthData[0]``'s ``anomaly-label-detected-metadata`` ``class-name``.

Importing ``marshal_for_capture_template.py`` requires the Triton Python-backend
module (``triton_python_backend_utils``) and ``cv2``, neither of which is needed by
``_generate_capture_meta_data`` (it only uses numpy via ``_has_anomaly_mask``,
json, base64, os and the shared label resolver). Both are stubbed in
``sys.modules`` before the module is loaded, following the established
``test_lfv_detection_tensor_set.py`` pattern. ``resolve_class_label`` resolves for
real via ``PYTHONPATH=src/backend``.
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


def _load_marshal_module():
    """Load ``marshal_for_capture_template`` with triton + cv2 stubbed.

    ``_generate_capture_meta_data`` never touches ``pb_utils`` or ``cv2``, so
    stubbing them at import time is sufficient to exercise the real method."""
    pb_utils_stub = types.ModuleType("triton_python_backend_utils")
    pb_utils_stub.Tensor = object
    pb_utils_stub.triton_string_to_numpy = lambda s: np.float32
    sys.modules["triton_python_backend_utils"] = pb_utils_stub

    # Prefer the real cv2 when importable so no stub is left in ``sys.modules``
    # to leak into sibling tests that need the genuine library; only stub when
    # cv2 is unavailable (the code path under test does not use cv2).
    try:
        import cv2  # noqa: F401
    except ImportError:  # pragma: no cover - depends on runner
        sys.modules.setdefault("cv2", types.ModuleType("cv2"))

    spec = importlib.util.spec_from_file_location(
        "marshal_for_capture_template_under_test", _MARSHAL_TEMPLATE_PATH
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


def _extract_inf_result(ret):
    """Decode the base64 ``json`` auxiliary output holding the inference result."""
    for aux in ret["deviceFleetAuxiliaryOutputs"]:
        if aux.get("observedContentType") == "json":
            decoded = base64.b64decode(aux["data"]).decode()
            return json.loads(decoded)
    raise AssertionError("no 'json' inference-result aux output found")


# ---- Detection-payload generators -------------------------------------------

_ZERO_OBJECT_SENTINEL = [
    {"bounding_box": [], "class": "", "class_label": "", "confidence": 0.0, "no_objects": True}
]

_box = st.lists(st.integers(min_value=0, max_value=640), min_size=4, max_size=4)
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

# A detection payload: either a non-empty list of real detections (varying count)
# or the zero-object sentinel. Both are recognized by _is_detection_list.
_detection_payload = st.one_of(
    st.lists(_detection_entry, min_size=1, max_size=6),
    st.just(_ZERO_OBJECT_SENTINEL),
)

_np_float32 = st.floats(
    min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False
).map(np.float32)


# Feature: object-detection-visualization, Property 2: Detection captures receive a distinct inference-result type
# Validates: Requirements 1.2, 1.3
@settings(max_examples=200)
@given(
    inference_anomalies=_detection_payload,
    # inference_output is randomized over falsy/truthy: detection typing must win
    # regardless, so the result is "Detection" and never "Anomaly"/"Normal".
    output_flag=st.integers(min_value=0, max_value=1),
    inference_confidence=_np_float32,
    inference_score=_np_float32,
)
def test_detection_captures_are_typed_detection(
    inference_anomalies, output_flag, inference_confidence, inference_score
):
    instance = _make_marshal_instance()
    # Zero mask so _has_anomaly_mask() is False -> detection classification is the
    # sole driver of the overlay/typing decisions.
    inference_mask = np.zeros((8, 8, 3), dtype=np.uint8)
    input_image = np.zeros((8, 8, 3), dtype=np.uint8)
    capture_meta_data = {
        "capture_id": "cap-1",
        "workflow_id": "wf-1",
        "capture_folder": "/tmp/captures/wf-1",
        "event_id": "cap-1",
        "device_fleet_name": "fleet-1",
    }

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

    # Precondition: this payload is indeed classified as a detection.
    assert TritonPythonModel._is_detection_list(inference_anomalies)

    inf_result = _extract_inf_result(ret)
    assert inf_result["Inference result"] == "Detection"
    assert inf_result["Inference result"] not in ("Anomaly", "Normal")

    # The ground-truth class-name must also carry the detection form, never the
    # anomaly-classification wording.
    class_name = ret["deviceGroundTruthData"][0][
        "anomaly-label-detected-metadata"
    ]["class-name"]
    assert class_name == "Detection"
    assert class_name not in ("Anomaly", "Normal")


# Feature: object-detection-visualization, Property 2: Detection captures receive a distinct inference-result type
# Validates: Requirements 1.2, 1.3
def test_zero_object_sentinel_is_typed_detection():
    """Deterministic edge case: the zero-object sentinel is typed 'Detection'."""
    instance = _make_marshal_instance()
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
        inference_mask=np.zeros((8, 8, 3), dtype=np.uint8),
        inference_anomalies=_ZERO_OBJECT_SENTINEL,
        inference_score=np.float32(0.0),
        input_image=np.zeros((8, 8, 3), dtype=np.uint8),
    )
    inf_result = _extract_inf_result(ret)
    assert inf_result["Inference result"] == "Detection"
    assert (
        ret["deviceGroundTruthData"][0]["anomaly-label-detected-metadata"][
            "class-name"
        ]
        == "Detection"
    )
