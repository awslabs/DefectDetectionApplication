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
"""Property-based test for the Marshal's payload discriminator.

Feature: object-detection-visualization
Design Property 1: Detection payloads are distinguished solely by a bounding_box
field. For any reused ``anomalies`` payload, the Marshal classifies the capture
as a Detection_Result IF AND ONLY IF the payload is a non-empty list whose first
entry is a dict carrying a ``bounding_box`` field (including the zero-object
sentinel), and otherwise treats it as an anomaly payload.

Target under test: ``TritonPythonModel._is_detection_list`` -- a ``@staticmethod``
in ``marshal_for_capture_template.py`` that returns True iff the payload is a
non-empty list whose first element is a dict containing a ``bounding_box`` key.

Import handling: ``marshal_for_capture_template.py`` imports
``triton_python_backend_utils`` and ``cv2`` at module top and also imports
``resolve_class_label`` from ``lyra_science_processing_utils`` (real, resolved via
``PYTHONPATH=src/backend``). ``triton_python_backend_utils`` is not installed in
the unit-test environment (and ``cv2`` may be absent on some runners), so we
inject lightweight ``sys.modules`` stubs for those native/backend deps BEFORE
loading the module -- mirroring ``test_lfv_detection_tensor_set.py``. Because
``_is_detection_list`` is a staticmethod that uses only builtins, import-time
stubbing is sufficient: we invoke it as
``TritonPythonModel._is_detection_list(payload)`` on the real class object.
"""
import importlib.util
import os
import sys
import types

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


def _load_marshal_module():
    """Load ``marshal_for_capture_template`` with the genuinely-missing native /
    backend deps stubbed in ``sys.modules``, returning the module object.

    Only modules that cannot be resolved in the unit-test environment are
    stubbed; ``resolve_class_label`` (first-party) still imports for real via
    ``PYTHONPATH=src/backend``. ``_is_detection_list`` never touches any of the
    stubbed deps, so the real implementation is exercised."""
    # Triton Python-backend module: never installed in the test env.
    if "triton_python_backend_utils" not in sys.modules:
        pb_utils_stub = types.ModuleType("triton_python_backend_utils")
        pb_utils_stub.Tensor = object
        pb_utils_stub.triton_string_to_numpy = lambda s: None
        sys.modules["triton_python_backend_utils"] = pb_utils_stub

    # cv2 may be missing on some runners; only stub if it is not importable so a
    # real cv2 (when present) is left untouched.
    try:
        import cv2  # noqa: F401
    except ImportError:  # pragma: no cover - depends on runner
        sys.modules["cv2"] = types.ModuleType("cv2")

    spec = importlib.util.spec_from_file_location(
        "marshal_for_capture_template_under_test", _MARSHAL_TEMPLATE_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_MARSHAL_MODULE = _load_marshal_module()
TritonPythonModel = _MARSHAL_MODULE.TritonPythonModel


def _classify(payload):
    """Invoke the discriminator exactly as the Marshal does."""
    return TritonPythonModel._is_detection_list(payload)


def _expected_is_detection(payload):
    """Reference oracle matching Property 1's spec: a non-empty list whose first
    entry is a dict containing a ``bounding_box`` key."""
    return (
        isinstance(payload, list)
        and len(payload) > 0
        and isinstance(payload[0], dict)
        and "bounding_box" in payload[0]
    )


# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------

# The zero-object sentinel emitted by the base model when a detection model
# finds nothing (Design Decision 1). It IS a detection payload.
_ZERO_OBJECT_SENTINEL = [
    {
        "bounding_box": [],
        "class": "",
        "class_label": "",
        "confidence": 0.0,
        "no_objects": True,
    }
]

_bbox = st.lists(
    st.floats(allow_nan=False, allow_infinity=False, width=32),
    min_size=0,
    max_size=6,
)


@st.composite
def _detection_dict(draw):
    """A detection-shaped dict: always carries a ``bounding_box`` key (possibly
    empty, as in the sentinel), plus assorted detection fields."""
    return {
        "bounding_box": draw(_bbox),
        "class": draw(st.text(max_size=4)),
        "class_label": draw(st.text(max_size=8)),
        "confidence": draw(
            st.floats(min_value=0.0, max_value=1.0, allow_nan=False)
        ),
    }


@st.composite
def _anomaly_dict(draw):
    """An anomaly-shaped dict: carries anomaly fields and crucially NO
    ``bounding_box`` key."""
    return {
        "name": draw(st.text(max_size=8)),
        "hex_color": draw(st.sampled_from(["#808000", "#800080", "#ffffff"])),
        "total_percentage_area": draw(
            st.floats(min_value=0.0, max_value=100.0, allow_nan=False)
        ),
    }


# Detection-shaped payloads: non-empty lists whose FIRST dict carries a
# bounding_box (mixed remaining entries allowed), plus the explicit sentinel.
_detection_payloads = st.one_of(
    st.just(list(_ZERO_OBJECT_SENTINEL)),
    st.builds(
        lambda head, tail: [head, *tail],
        _detection_dict(),
        st.lists(st.one_of(_detection_dict(), _anomaly_dict()), max_size=4),
    ),
)

# Anomaly-shaped payloads: the empty list ``[]`` (the anomaly "no anomalies"
# payload) and non-empty lists of anomaly dicts (no bounding_box field).
_anomaly_payloads = st.one_of(
    st.just([]),
    st.lists(_anomaly_dict(), min_size=1, max_size=5),
)


# ---------------------------------------------------------------------------
# Property test
# ---------------------------------------------------------------------------

# Feature: object-detection-visualization, Property 1: Detection payloads are distinguished solely by a bounding_box field
# Validates: Requirements 1.1, 5.5
@settings(max_examples=25)
@given(payload=st.one_of(_detection_payloads, _anomaly_payloads))
def test_detection_iff_first_entry_has_bounding_box(payload):
    """_is_detection_list returns True iff the payload is a non-empty list whose
    first entry is a dict with a ``bounding_box`` field, and False otherwise."""
    assert _classify(payload) == _expected_is_detection(payload)


# Feature: object-detection-visualization, Property 1: Detection payloads are distinguished solely by a bounding_box field
# Validates: Requirements 1.1, 5.5
@settings(max_examples=25)
@given(payload=_detection_payloads)
def test_detection_shaped_payloads_classify_as_detection(payload):
    """Every detection-shaped payload (including the zero-object sentinel) is
    classified as a detection."""
    assert _classify(payload) is True


# Feature: object-detection-visualization, Property 1: Detection payloads are distinguished solely by a bounding_box field
# Validates: Requirements 1.1, 5.5
@settings(max_examples=25)
@given(payload=_anomaly_payloads)
def test_anomaly_shaped_payloads_classify_as_anomaly(payload):
    """No anomaly-shaped payload (empty list or lists of anomaly dicts without a
    bounding_box) is classified as a detection."""
    assert _classify(payload) is False


# ---------------------------------------------------------------------------
# Deterministic edge cases
# ---------------------------------------------------------------------------


def test_zero_object_sentinel_is_detection():
    """The zero-object sentinel is explicitly recognized as a detection."""
    assert _classify(list(_ZERO_OBJECT_SENTINEL)) is True


def test_empty_list_is_not_detection():
    """The empty list (anomaly 'no anomalies' payload) is not a detection."""
    assert _classify([]) is False


def test_anomaly_list_is_not_detection():
    """A representative anomaly list is not a detection."""
    payload = [
        {"name": "scratch", "hex_color": "#808000", "total_percentage_area": 12.5}
    ]
    assert _classify(payload) is False


def test_detection_list_is_detection():
    """A representative single-object detection list is a detection."""
    payload = [
        {
            "bounding_box": [12, 40, 220, 310],
            "class": "17",
            "class_label": "dog",
            "confidence": 0.83,
        }
    ]
    assert _classify(payload) is True


@pytest.mark.parametrize("payload", [None, {}, "not-a-list", 42, 3.14, True])
def test_non_list_payloads_are_not_detections(payload):
    """Non-list payloads are never classified as detections."""
    assert _classify(payload) is False


def test_list_with_non_dict_first_entry_is_not_detection():
    """A non-empty list whose first entry is not a dict is not a detection."""
    assert _classify([["bounding_box"], {"bounding_box": [1, 2, 3, 4]}]) is False
