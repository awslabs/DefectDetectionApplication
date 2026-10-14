# Copyright 2025 Amazon Web Services, Inc.
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
"""Property test for the Python_Runner's resolution of mapping returns
from ``produce_frame``, through real handler subprocesses.

- **Feature: custom-python-source, Property 6: Mapping returns
  round-trip without channel conversion** — Validates: Requirements
  3.6, 3.7
"""
import base64
import os

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from workflow_engine.python_bridge import CustomPythonBridge

WALL_CLOCK_LIMIT_SEC = 30.0

FORMAT_CHANNELS = {"RGB": 3, "RGBA": 4, "GRAY8": 1}

#: Returns {"array": ..., "format": ...} built from the context.
MAPPING_ARRAY_HANDLER = """\
def produce_frame(context):
    array = np.array(
        context["pixels"], dtype=np.uint8
    ).reshape(tuple(context["shape"]))
    return {"array": array, "format": context["format"]}
"""

#: Returns {"data": ..., "width": ..., "height": ..., "format": ...}
#: with the raw bytes decoded from the context.
MAPPING_DATA_HANDLER = """\
import base64


def produce_frame(context):
    return {
        "data": base64.b64decode(context["data_b64"]),
        "width": context["width"],
        "height": context["height"],
        "format": context["format"],
    }
"""


def _make_bridge(tmp_path_factory, name, handler_source):
    handler_dir = tmp_path_factory.mktemp("prop6_" + name)
    handler_path = os.path.join(str(handler_dir), "handler.py")
    with open(handler_path, "w") as f:
        f.write(handler_source)
    return CustomPythonBridge(
        "prop6-" + name, handler_path,
        wall_clock_limit_sec=WALL_CLOCK_LIMIT_SEC,
    )


@pytest.fixture(scope="module")
def array_mapping_bridge(tmp_path_factory):
    bridge = _make_bridge(tmp_path_factory, "array", MAPPING_ARRAY_HANDLER)
    yield bridge
    bridge.stop()


@pytest.fixture(scope="module")
def data_mapping_bridge(tmp_path_factory):
    bridge = _make_bridge(tmp_path_factory, "data", MAPPING_DATA_HANDLER)
    yield bridge
    bridge.stop()


# ---------------------------------------------------------------------------
# Feature: custom-python-source, Property 6: Mapping returns round-trip
# without channel conversion
#
# For any uint8 array paired with a supported Pixel_Format matching its
# channel count, a {"array", "format"} return resolves to exactly the array's
# bytes (no channel conversion) under the stated format with dims from the
# shape; and for any consistent {"data", "width", "height", "format"}
# mapping, resolution passes the raw bytes, dims, and format through
# unchanged.
#
# **Validates: Requirements 3.6, 3.7**
# ---------------------------------------------------------------------------


@settings(deadline=None)
@given(
    fmt=st.sampled_from(sorted(FORMAT_CHANNELS)),
    height=st.integers(min_value=1, max_value=8),
    width=st.integers(min_value=1, max_value=8),
    data=st.data(),
)
def test_property_6_mapping_returns_round_trip_without_conversion(
    array_mapping_bridge, data_mapping_bridge, fmt, height, width, data
):
    """**Feature: custom-python-source, Property 6: Mapping returns
    round-trip without channel conversion**

    **Validates: Requirements 3.6, 3.7**
    """
    channels = FORMAT_CHANNELS[fmt]
    count = height * width * channels
    pixels = data.draw(
        st.lists(st.integers(min_value=0, max_value=255),
                 min_size=count, max_size=count)
    )
    shape = (height, width) if channels == 1 else (height, width, channels)
    array = np.array(pixels, dtype=np.uint8).reshape(shape)
    raw = array.tobytes()

    # {"array", "format"}: exactly the array's bytes, no channel
    # conversion, dims from the shape (Req 3.6).
    frame, out_width, out_height, out_fmt, _ = (
        array_mapping_bridge.produce_frame(
            {"shape": list(shape), "pixels": pixels, "format": fmt}
        )
    )
    assert (out_width, out_height, out_fmt) == (width, height, fmt)
    assert frame == raw

    # {"data", "width", "height", "format"}: raw bytes, dims, and format
    # pass through unchanged (Req 3.7).
    frame, out_width, out_height, out_fmt, _ = (
        data_mapping_bridge.produce_frame(
            {
                "data_b64": base64.b64encode(raw).decode("ascii"),
                "width": width,
                "height": height,
                "format": fmt,
            }
        )
    )
    assert (out_width, out_height, out_fmt) == (width, height, fmt)
    assert frame == raw
