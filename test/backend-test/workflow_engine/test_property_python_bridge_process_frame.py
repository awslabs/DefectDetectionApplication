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
"""Property-based test for the ``process_frame`` handler contract through
a real :class:`CustomPythonBridge` subprocess.

Covers:

- **Feature: custom-python-frames, Property 4: process_frame contract
  round trip** — Validates: Requirements 3.1, 3.2, 3.3

One long-lived bridge subprocess per handler variant is shared across all
hypothesis examples (module-scoped fixtures): spawning a subprocess per
example would dominate the run with numpy/cv2 import time.
"""
import os

import pytest
from hypothesis import given
from hypothesis import strategies as st

from workflow_engine.python_bridge import CustomPythonBridge

FORMAT_CHANNELS = {"RGB": 3, "BGR": 3, "RGBA": 4, "GRAY8": 1}

#: Generous per-frame limit: the first frame of each subprocess pays the
#: numpy/cv2 import cost.
WALL_CLOCK_LIMIT_SEC = 30.0

#: Identity: asserts the received array's shape/dtype match the caps
#: (Requirement 3.1) and returns the array unchanged (Requirement 3.2).
IDENTITY_HANDLER = """\
FORMAT_CHANNELS = {"RGB": 3, "BGR": 3, "RGBA": 4, "GRAY8": 1}


def process_frame(frame, metadata):
    info = metadata["frame"]
    channels = FORMAT_CHANNELS[info["format"]]
    if channels == 1:
        expected_shape = (info["height"], info["width"])
    else:
        expected_shape = (info["height"], info["width"], channels)
    assert frame.shape == expected_shape, (
        "shape %r != expected %r" % (frame.shape, expected_shape)
    )
    assert frame.dtype == np.uint8, "dtype %r != uint8" % (frame.dtype,)
    return frame
"""

#: None return: the input frame passes through unchanged (Requirement 3.3).
NONE_HANDLER = """\
def process_frame(frame, metadata):
    return None
"""

#: Deterministic transformation: bitwise inversion of every pixel
#: (Requirement 3.2 — pixels transformed, padding/byte length preserved).
INVERT_HANDLER = """\
def process_frame(frame, metadata):
    return np.bitwise_not(frame)
"""


def _make_bridge(tmp_path_factory, name, handler_source):
    handler_dir = tmp_path_factory.mktemp("prop4_" + name)
    handler_path = os.path.join(str(handler_dir), "handler.py")
    with open(handler_path, "w") as f:
        f.write(handler_source)
    return CustomPythonBridge(
        "prop4-" + name,
        handler_path,
        wall_clock_limit_sec=WALL_CLOCK_LIMIT_SEC,
    )


@pytest.fixture(scope="module")
def identity_bridge(tmp_path_factory):
    bridge = _make_bridge(tmp_path_factory, "identity", IDENTITY_HANDLER)
    yield bridge
    bridge.stop()


@pytest.fixture(scope="module")
def none_bridge(tmp_path_factory):
    bridge = _make_bridge(tmp_path_factory, "none", NONE_HANDLER)
    yield bridge
    bridge.stop()


@pytest.fixture(scope="module")
def invert_bridge(tmp_path_factory):
    bridge = _make_bridge(tmp_path_factory, "invert", INVERT_HANDLER)
    yield bridge
    bridge.stop()


# ---------------------------------------------------------------------------
# Feature: custom-python-frames, Property 4: process_frame contract round trip
#
# For any frame dimensions, supported Pixel_Format, pixel content, and row
# padding, a process_frame handler invoked through the CustomPythonBridge
# receives a NumPy uint8 array of the shape implied by the caps (rows x
# columns x channels; 2-D for GRAY8); returning that array unchanged emits
# output bytes equal to the input bytes (padding included); returning None
# emits the input bytes unchanged; and returning a deterministic
# transformation (bitwise inversion) emits bytes whose pixel regions are the
# transformed pixels while padding bytes and total byte length are preserved.
#
# **Validates: Requirements 3.1, 3.2, 3.3**
# ---------------------------------------------------------------------------


@given(
    width=st.integers(min_value=1, max_value=16),
    height=st.integers(min_value=1, max_value=16),
    fmt=st.sampled_from(sorted(FORMAT_CHANNELS)),
    padding=st.integers(min_value=0, max_value=8),
    data=st.data(),
)
def test_property_4_process_frame_contract_round_trip(
    identity_bridge, none_bridge, invert_bridge,
    width, height, fmt, padding, data,
):
    """**Feature: custom-python-frames, Property 4: process_frame
    contract round trip**

    **Validates: Requirements 3.1, 3.2, 3.3**
    """
    channels = FORMAT_CHANNELS[fmt]
    row_bytes = width * channels
    pixels = data.draw(
        st.binary(min_size=row_bytes * height, max_size=row_bytes * height)
    )
    pad = data.draw(
        st.binary(min_size=padding * height, max_size=padding * height)
    )
    frame = b"".join(
        pixels[row * row_bytes:(row + 1) * row_bytes]
        + pad[row * padding:(row + 1) * padding]
        for row in range(height)
    )

    # Identity: the handler asserts the received shape/dtype match the
    # caps (Req 3.1) and the output bytes equal the input bytes, padding
    # included (Req 3.2).
    out_frame, _ = identity_bridge.process_frame(
        frame, width=width, height=height, frame_format=fmt
    )
    assert out_frame == frame

    # None return: the input frame passes through unchanged (Req 3.3).
    out_frame, _ = none_bridge.process_frame(
        frame, width=width, height=height, frame_format=fmt
    )
    assert out_frame == frame

    # Bitwise inversion: pixel regions are inverted, padding bytes and
    # total byte length are preserved (Req 3.2).
    stride = row_bytes + padding
    expected = b"".join(
        bytes(255 - b for b in pixels[row * row_bytes:(row + 1) * row_bytes])
        + pad[row * padding:(row + 1) * padding]
        for row in range(height)
    )
    out_frame, _ = invert_bridge.process_frame(
        frame, width=width, height=height, frame_format=fmt
    )
    assert len(out_frame) == stride * height == len(frame)
    assert out_frame == expected
