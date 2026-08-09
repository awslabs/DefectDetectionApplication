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
"""Property test for the Python_Runner's resolution of bare NumPy array
returns from ``produce_frame``, through a real handler subprocess.

The handler rebuilds the array from pixel values carried in the
Trigger_Context, so the test controls the exact array the producer
returns and computes the expected Produced_Frame independently.

- **Feature: custom-python-source, Property 5: NumPy array returns
  resolve with the declared format, dims, and channel order** —
  Validates: Requirements 3.3, 3.4, 3.5
"""
import os

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from workflow_engine.python_bridge import CustomPythonBridge

WALL_CLOCK_LIMIT_SEC = 30.0

#: Rebuilds the uint8 array described by the context and returns it bare.
ARRAY_HANDLER = """\
def produce_frame(context):
    return np.array(
        context["pixels"], dtype=np.uint8
    ).reshape(tuple(context["shape"]))
"""


@pytest.fixture(scope="module")
def array_bridge(tmp_path_factory):
    handler_dir = tmp_path_factory.mktemp("prop5_producer")
    handler_path = os.path.join(str(handler_dir), "handler.py")
    with open(handler_path, "w") as f:
        f.write(ARRAY_HANDLER)
    bridge = CustomPythonBridge(
        "prop5-source", handler_path,
        wall_clock_limit_sec=WALL_CLOCK_LIMIT_SEC,
    )
    yield bridge
    bridge.stop()


@st.composite
def produced_arrays(draw):
    """A uint8 array that is 2-D, 3-D x 3-channel, or 3-D x 4-channel."""
    height = draw(st.integers(min_value=1, max_value=8))
    width = draw(st.integers(min_value=1, max_value=8))
    channels = draw(st.sampled_from([None, 3, 4]))
    shape = (height, width) if channels is None else (height, width, channels)
    count = height * width * (channels or 1)
    pixels = draw(
        st.lists(st.integers(min_value=0, max_value=255),
                 min_size=count, max_size=count)
    )
    return np.array(pixels, dtype=np.uint8).reshape(shape)


# ---------------------------------------------------------------------------
# Feature: custom-python-source, Property 5: NumPy array returns resolve with
# the declared format, dims, and channel order
#
# For any uint8 NumPy array that is 2-D, 3-D with three channels, or 3-D with
# four channels, the Python_Runner resolves it as a Produced_Frame whose
# width/height come from the array's shape, whose Pixel_Format is GRAY8, RGB,
# or RGBA respectively, and whose bytes are the array's bytes with
# BGR(A)->RGB(A) channel order converted for the 3- and 4-channel cases and
# untouched for the 2-D case.
#
# **Validates: Requirements 3.3, 3.4, 3.5**
# ---------------------------------------------------------------------------


@settings(deadline=None)
@given(array=produced_arrays())
def test_property_5_array_returns_resolve_format_dims_and_channel_order(
    array_bridge, array
):
    """**Feature: custom-python-source, Property 5: NumPy array returns
    resolve with the declared format, dims, and channel order**

    **Validates: Requirements 3.3, 3.4, 3.5**
    """
    context = {
        "shape": list(array.shape),
        "pixels": [int(v) for v in array.reshape(-1)],
    }
    frame, width, height, fmt, _meta = array_bridge.produce_frame(context)

    # Dims come from the shape.
    assert (height, width) == array.shape[:2]

    if array.ndim == 2:
        # 2-D: GRAY8, bytes untouched (Req 3.3).
        assert fmt == "GRAY8"
        assert frame == array.tobytes()
    elif array.shape[2] == 3:
        # 3-channel: OpenCV BGR converted to RGB (Req 3.4).
        assert fmt == "RGB"
        assert frame == np.ascontiguousarray(array[:, :, ::-1]).tobytes()
    else:
        # 4-channel: BGRA converted to RGBA (Req 3.5).
        assert fmt == "RGBA"
        assert frame == np.ascontiguousarray(
            array[:, :, [2, 1, 0, 3]]
        ).tobytes()
