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
"""Property-based test for ``process_frame`` contract violations through
a real :class:`CustomPythonBridge` subprocess.

Covers:

- **Feature: custom-python-frames, Property 5: process_frame contract
  violations fail the node identifiably** — Validates: Requirements 3.4, 3.5

A single module-scoped bridge with one violation-dispatching handler is
shared across all hypothesis examples. Every contract violation kills the
handler subprocess (the runner exits after reporting ``status: error`` and
the bridge kills it besides), but ``CustomPythonBridge.process_frame``
restarts the subprocess on the next invocation, so the bridge object stays
reusable — each example pays one subprocess start. One violation kind is
drawn per example to keep that to a single restart, and dims stay small.
"""
import os
import string

import pytest
from hypothesis import given
from hypothesis import strategies as st

from workflow_engine.python_bridge import (
    CustomPythonBridge,
    CustomPythonNodeError,
)

FORMAT_CHANNELS = {"RGB": 3, "BGR": 3, "RGBA": 4, "GRAY8": 1}

NODE_ID = "prop5-violations"

#: Generous per-frame limit: every example restarts the subprocess, so
#: every frame pays the numpy/cv2 import cost.
WALL_CLOCK_LIMIT_SEC = 30.0

#: One handler covering every handler-side violation kind, selected by
#: the caller through the request metadata (the unsupported-format kind
#: is runner-side: the handler is never invoked for it).
VIOLATION_HANDLER = """\
def process_frame(frame, metadata):
    kind = metadata["violation"]
    if kind == "wrong_shape":
        return np.zeros((frame.shape[0] + 1,) + frame.shape[1:],
                        dtype=np.uint8)
    if kind == "wrong_dtype":
        return frame.astype(np.uint16)
    if kind == "non_array":
        return "not-an-array"
    raise AssertionError("unexpected violation kind %r" % (kind,))
"""

VIOLATION_KINDS = (
    "wrong_shape",
    "wrong_dtype",
    "non_array",
    "unsupported_format",
)

#: Format strings outside the supported set (Requirement 3.5): realistic
#: GStreamer video formats plus arbitrary short uppercase-ish strings.
UNSUPPORTED_FORMATS = st.one_of(
    st.sampled_from(["NV12", "I420", "YUY2", "UYVY", "BGRA", "GRAY16_LE"]),
    st.text(
        alphabet=string.ascii_uppercase + string.digits + "_",
        min_size=0,
        max_size=8,
    ).filter(lambda s: s not in FORMAT_CHANNELS),
)


@pytest.fixture(scope="module")
def violation_bridge(tmp_path_factory):
    handler_dir = tmp_path_factory.mktemp("prop5")
    handler_path = os.path.join(str(handler_dir), "handler.py")
    with open(handler_path, "w") as f:
        f.write(VIOLATION_HANDLER)
    bridge = CustomPythonBridge(
        NODE_ID,
        handler_path,
        wall_clock_limit_sec=WALL_CLOCK_LIMIT_SEC,
    )
    yield bridge
    bridge.stop()


# ---------------------------------------------------------------------------
# Feature: custom-python-frames, Property 5: process_frame contract
# violations fail the node identifiably
#
# For any frame and any non-compliant process_frame outcome — a returned
# array of different shape, a returned array of different dtype, a
# non-array non-None return, or an input frame whose Pixel_Format is
# outside the supported set — the CustomPythonBridge raises
# CustomPythonNodeError carrying the node id and a message describing the
# mismatch or the unsupported format.
#
# **Validates: Requirements 3.4, 3.5**
# ---------------------------------------------------------------------------


@given(
    width=st.integers(min_value=1, max_value=8),
    height=st.integers(min_value=1, max_value=8),
    violation=st.sampled_from(VIOLATION_KINDS),
    data=st.data(),
)
def test_property_5_contract_violations_fail_the_node_identifiably(
    violation_bridge, width, height, violation, data,
):
    """**Feature: custom-python-frames, Property 5: process_frame
    contract violations fail the node identifiably**

    **Validates: Requirements 3.4, 3.5**
    """
    if violation == "unsupported_format":
        fmt = data.draw(UNSUPPORTED_FORMATS)
        channels = 1  # frame content is irrelevant: rejected before decode
    else:
        fmt = data.draw(st.sampled_from(sorted(FORMAT_CHANNELS)))
        channels = FORMAT_CHANNELS[fmt]
    size = width * height * channels
    frame = data.draw(st.binary(min_size=size, max_size=size))

    with pytest.raises(CustomPythonNodeError) as excinfo:
        violation_bridge.process_frame(
            frame,
            metadata={"violation": violation},
            width=width,
            height=height,
            frame_format=fmt,
        )

    # The error carries the node id (Requirements 3.4, 3.5).
    assert excinfo.value.node_id == NODE_ID
    message = str(excinfo.value)
    assert NODE_ID in message

    if channels == 1 and violation != "unsupported_format":
        expected_shape = (height, width)
    else:
        expected_shape = (height, width, channels)

    if violation == "wrong_shape":
        # Message describes the shape mismatch (Requirement 3.4).
        assert "expected shape {0} and dtype uint8".format(
            expected_shape
        ) in message
        returned_shape = (height + 1, width) if channels == 1 else (
            height + 1, width, channels
        )
        assert "returned an array of shape {0}".format(
            returned_shape
        ) in message
    elif violation == "wrong_dtype":
        # Message describes the dtype mismatch (Requirement 3.4).
        assert "dtype uint16" in message
        assert "expected shape {0} and dtype uint8".format(
            expected_shape
        ) in message
    elif violation == "non_array":
        # Message describes the non-array return (Requirement 3.4).
        assert "must return None or a NumPy array" in message
        assert "got str" in message
    else:  # unsupported_format
        # Message names the unsupported format (Requirement 3.5).
        assert "unsupported frame format" in message
        assert repr(fmt) in message
