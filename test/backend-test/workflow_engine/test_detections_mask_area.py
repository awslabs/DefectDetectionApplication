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
#
"""rfdetr-seg-support task 10.9 (Requirements 10.5, 17.3): ``mask_area`` in
the run metadata's Detection_List (10.5d, DD14).

``detections.build_detection_list`` / ``_normalize_entry`` pass ``mask_area``
through only when the raw entry carries a finite number: an entry without it
yields exactly the six normalised keys plus ``id`` (today's YOLO shape); a
non-finite, non-numeric or boolean ``mask_area`` is not copied; ``mask_rle``
never reaches the run metadata.
"""
import math

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from workflow_engine import detections

SEVEN_KEYS = {"id", "label", "confidence", "x_min", "y_min", "x_max", "y_max"}
INSTANCE = {
    "class_index": "0", "class_label": "scratch", "bounding_box": [3, 2, 8, 5],
    "confidence": 0.9, "mask_rle": "27 4 8 4 8 4 8 4 125", "mask_area": 1234,
}
PLAIN = {"class_index": "7", "class_label": "truck", "bounding_box": [1, 2, 3, 4], "confidence": 0.8}


def _one(entry):
    result = detections.build_detection_list([entry], "left_to_right")
    assert len(result) == 1
    return result[0]


def test_mask_area_passes_through_as_a_number():
    entry = _one(INSTANCE)
    assert set(entry) == SEVEN_KEYS | {"mask_area"}
    assert entry["mask_area"] == 1234
    assert isinstance(entry["mask_area"], int)
    assert entry["label"] == "scratch" and entry["confidence"] == 0.9
    assert (entry["x_min"], entry["y_min"], entry["x_max"], entry["y_max"]) == (3.0, 2.0, 8.0, 5.0)


def test_float_mask_area_passes_through():
    assert _one(dict(INSTANCE, mask_area=12.5))["mask_area"] == 12.5


def test_absent_stays_absent():
    entry = _one(PLAIN)
    assert set(entry) == SEVEN_KEYS
    assert "mask_area" not in entry
    truncated = dict(INSTANCE)
    del truncated["mask_rle"]
    del truncated["mask_area"]
    truncated["mask_truncated"] = True
    assert set(_one(truncated)) == SEVEN_KEYS


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf"), "1234", None, True, False, [1], {}])
def test_non_finite_non_numeric_or_boolean_mask_area_is_not_copied(value):
    entry = _one(dict(INSTANCE, mask_area=value))
    assert "mask_area" not in entry
    assert set(entry) == SEVEN_KEYS


def test_mask_rle_never_reaches_the_run_metadata():
    entry = _one(INSTANCE)
    assert "mask_rle" not in entry and "mask_truncated" not in entry and "class_index" not in entry
    truncated = dict(INSTANCE, mask_truncated=True)
    assert "mask_truncated" not in _one(truncated)


def test_mixed_list_keeps_order_and_per_entry_keys():
    result = detections.build_detection_list([PLAIN, INSTANCE, dict(INSTANCE, mask_area="x")], "left_to_right")
    assert [set(e) == SEVEN_KEYS | {"mask_area"} for e in result] == [False, True, False]
    assert [e["label"] for e in result] == ["truck", "scratch", "scratch"]


@settings(max_examples=50, deadline=None)
@given(st.one_of(st.integers(min_value=0, max_value=10**9),
                 st.floats(min_value=0, max_value=1e9, allow_nan=False, allow_infinity=False)))
def test_any_finite_number_passes_through_unchanged(area):
    entry = _one(dict(INSTANCE, mask_area=area))
    assert entry["mask_area"] == area and math.isfinite(entry["mask_area"])
