#
#  Copyright 2025 Amazon Web Services, Inc.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
"""Property test for the Marshal detection count and top-confidence reporting.

# Feature: object-detection-visualization, Property 3: Detection count and top confidence are reported
# Validates: Requirements 1.4, 1.5

For any detection payload, the decoded inf_result ``Detection_count`` equals the
number of VALID detections (entries carrying a 4-element ``bounding_box``; the
zero-object sentinel yields 0), and for payloads with >= 1 valid object the
inf_result ``Confidence`` equals the max confidence among valid detected objects
(0.0 when none).

Importing ``marshal_for_capture_template.py`` requires ``triton_python_backend_utils``
(the Triton Python-backend module, not installed in the test env) and ``cv2``.
``_generate_capture_meta_data`` uses neither (it only touches numpy, json,
base64, os and the pure ``resolve_class_label``), so both are stubbed in
``sys.modules`` before the module is loaded -- mirroring the sibling
``test_lfv_detection_tensor_set.py`` approach.
"""
import base64
import importlib.util
import json
import os
import sys
import types

import numpy as np
import pytest
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
    """Load ``marshal_for_capture_template`` with triton + cv2 stubbed,
    returning the module object."""
    pb_utils_stub = types.ModuleType("triton_python_backend_utils")
    pb_utils_stub.triton_string_to_numpy = lambda s: np.float32
    sys.modules["triton_python_backend_utils"] = pb_utils_stub

    # cv2 is imported at module top but unused by _generate_capture_meta_data.
    # Prefer the real cv2 when importable so we never leave a stub in
    # ``sys.modules`` that would leak into sibling tests needing the genuine
    # library (e.g. the overlay-drawing tests); only stub when cv2 is absent.
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


def _make_marshal_instance(module):
    """Build a TritonPythonModel instance with only the attributes read by
    ``_generate_capture_meta_data``, bypassing ``initialize`` (which needs a
    real Triton model config)."""
    instance = module.TritonPythonModel.__new__(module.TritonPythonModel)
    instance.model_name = "test_model"
    instance.model_version = "1"
    return instance


def _capture_meta_data():
    return {
        "capture_id": "cap-123",
        "workflow_id": "wf-456",
        "capture_folder": "/tmp/captures/wf-456",
        "event_id": "cap-123",
        "device_fleet_name": "fleet-A",
    }


def _decode_inf_result(ret):
    """Extract and decode the base64 'json' auxiliary output holding the
    inference-result summary."""
    for aux in ret["deviceFleetAuxiliaryOutputs"]:
        if aux.get("observedContentType") == "json" and aux.get("encoding") == "BASE64":
            return json.loads(base64.b64decode(aux["data"]).decode())
    raise AssertionError("no inf_result json aux output found")


def _run_marshal(instance, payload):
    return instance._generate_capture_meta_data(
        capture_meta_data=_capture_meta_data(),
        inference_output=np.uint8(1),
        time_str="2025-01-01T00:00:00",
        inference_confidence=np.float32(0.5),
        inference_mask=np.zeros((4, 4, 3)),  # empty mask -> _has_anomaly_mask False
        inference_anomalies=payload,
        inference_score=np.float32(0.5),
        input_image=np.zeros((4, 4, 3)),
    )


# --- Strategies -------------------------------------------------------------

_confidence = st.floats(
    min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False
)


def _valid_entry():
    """A valid detection: a 4-element bounding_box + random confidence."""
    return st.fixed_dictionaries(
        {
            "bounding_box": st.lists(
                st.floats(min_value=0, max_value=200, allow_nan=False, allow_infinity=False),
                min_size=4,
                max_size=4,
            ),
            "class": st.builds(str, st.integers(min_value=0, max_value=90)),
            "class_label": st.just(""),
            "confidence": _confidence,
        }
    )


def _invalid_entry():
    """An entry that must NOT count: a bounding_box whose length != 4 (incl.
    empty). Still carries a 'bounding_box' key so the payload is classified as
    a detection, and a random confidence to prove it is excluded from the max."""
    return st.fixed_dictionaries(
        {
            "bounding_box": st.lists(
                st.floats(min_value=0, max_value=200, allow_nan=False, allow_infinity=False),
                min_size=0,
                max_size=6,
            ).filter(lambda b: len(b) != 4),
            "class": st.just(""),
            "class_label": st.just(""),
            "confidence": _confidence,
        }
    )


_SENTINEL = [
    {
        "bounding_box": [],
        "class": "",
        "class_label": "",
        "confidence": 0.0,
        "no_objects": True,
    }
]


def _detection_payloads():
    """Mix of valid + invalid entries (non-empty so it classifies as a
    detection), plus the explicit zero-object sentinel."""
    mixed = st.lists(
        st.one_of(_valid_entry(), _invalid_entry()), min_size=1, max_size=12
    )
    return st.one_of(mixed, st.just(_SENTINEL))


def _expected(payload):
    valid = [
        e
        for e in payload
        if isinstance(e.get("bounding_box"), list) and len(e["bounding_box"]) == 4
    ]
    count = len(valid)
    conf = max((float(e.get("confidence", 0.0)) for e in valid), default=0.0)
    return count, conf


# --- Property test ----------------------------------------------------------

# Feature: object-detection-visualization, Property 3: Detection count and top confidence are reported
# Validates: Requirements 1.4, 1.5
@settings(max_examples=200)
@given(payload=_detection_payloads())
def test_detection_count_and_top_confidence_are_reported(payload):
    module = _load_marshal_template()
    instance = _make_marshal_instance(module)

    ret = _run_marshal(instance, payload)
    inf_result = _decode_inf_result(ret)

    expected_count, expected_conf = _expected(payload)

    assert inf_result["Detection_count"] == expected_count
    if expected_count >= 1:
        assert inf_result["Confidence"] == pytest.approx(expected_conf)
    else:
        assert inf_result["Confidence"] == pytest.approx(0.0)


def test_zero_object_sentinel_reports_zero_count_and_zero_confidence():
    """Explicit edge: the zero-object sentinel yields count 0 and 0.0 confidence."""
    module = _load_marshal_template()
    instance = _make_marshal_instance(module)

    ret = _run_marshal(instance, _SENTINEL)
    inf_result = _decode_inf_result(ret)

    assert inf_result["Detection_count"] == 0
    assert inf_result["Confidence"] == pytest.approx(0.0)


def test_top_confidence_ignores_invalid_boxes():
    """A high-confidence invalid box (wrong length) must not raise the reported
    max confidence, and must not be counted."""
    module = _load_marshal_template()
    instance = _make_marshal_instance(module)

    payload = [
        {"bounding_box": [1, 2, 3, 4], "class": "1", "class_label": "", "confidence": 0.4},
        {"bounding_box": [1, 2, 3], "class": "2", "class_label": "", "confidence": 0.99},
        {"bounding_box": [5, 6, 7, 8], "class": "3", "class_label": "", "confidence": 0.7},
    ]
    ret = _run_marshal(instance, payload)
    inf_result = _decode_inf_result(ret)

    assert inf_result["Detection_count"] == 2
    assert inf_result["Confidence"] == pytest.approx(0.7)
