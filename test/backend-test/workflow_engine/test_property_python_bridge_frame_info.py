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
"""Property-based test for frame info delivery through a real
:class:`CustomPythonBridge` subprocess.

Covers:

- **Feature: custom-python-frames, Property 6: Frame info delivery** —
  Validates: Requirements 3.9, 5.6

One long-lived bridge subprocess is shared across all hypothesis examples
(module-scoped fixture): spawning a subprocess per example would dominate
the run with numpy/cv2 import time.
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

#: Records what the handler observes during the invocation: the frame
#: info delivered in ``metadata["frame"]`` (Requirement 3.9) and the
#: triple reported by ``dda_frames.frame_info()`` (Requirement 5.6).
#: Both snapshots travel back to the bridge caller in the response
#: metadata; returning None passes the frame bytes through.
FRAME_INFO_HANDLER = """\
import dda_frames


def process_frame(frame, metadata):
    metadata["observed_metadata_frame"] = dict(metadata["frame"])
    metadata["observed_frame_info"] = dda_frames.frame_info()
    return None
"""


@pytest.fixture(scope="module")
def frame_info_bridge(tmp_path_factory):
    handler_dir = tmp_path_factory.mktemp("prop6_frame_info")
    handler_path = os.path.join(str(handler_dir), "handler.py")
    with open(handler_path, "w") as f:
        f.write(FRAME_INFO_HANDLER)
    bridge = CustomPythonBridge(
        "prop6-frame-info",
        handler_path,
        wall_clock_limit_sec=WALL_CLOCK_LIMIT_SEC,
    )
    yield bridge
    bridge.stop()


# ---------------------------------------------------------------------------
# Feature: custom-python-frames, Property 6: Frame info delivery
#
# For any frame width, height, and supported Pixel_Format dispatched by the
# bridge, the handler observes that exact triple both in metadata["frame"]
# and from dda_frames.frame_info() during the invocation.
#
# **Validates: Requirements 3.9, 5.6**
# ---------------------------------------------------------------------------


@given(
    width=st.integers(min_value=1, max_value=32),
    height=st.integers(min_value=1, max_value=32),
    fmt=st.sampled_from(sorted(FORMAT_CHANNELS)),
)
def test_property_6_frame_info_delivery(frame_info_bridge, width, height, fmt):
    """**Feature: custom-python-frames, Property 6: Frame info delivery**

    **Validates: Requirements 3.9, 5.6**
    """
    frame = bytes(width * height * FORMAT_CHANNELS[fmt])
    expected = {"width": width, "height": height, "format": fmt}

    out_frame, out_meta = frame_info_bridge.process_frame(
        frame, width=width, height=height, frame_format=fmt
    )

    assert out_frame == frame
    # Requirement 3.9: the caps triple is delivered inside metadata["frame"].
    assert out_meta["observed_metadata_frame"] == expected
    # Requirement 5.6: dda_frames.frame_info() reports the same triple
    # while the handler invocation is in progress.
    assert out_meta["observed_frame_info"] == expected
