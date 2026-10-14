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
"""Property-based test for the legacy ``handle`` raw-bytes contract
through a real :class:`CustomPythonBridge` subprocess.

Covers:

- **Feature: custom-python-frames, Property 2: Legacy handle contract is
  preserved** — Validates: Requirements 3.6, 3.9

One long-lived bridge subprocess is shared across all hypothesis examples
(module-scoped fixture): spawning a subprocess per example would dominate
the run with interpreter startup time.
"""
import os

import pytest
from hypothesis import given
from hypothesis import strategies as st

from workflow_engine.python_bridge import CustomPythonBridge

#: Generous per-frame limit: the first frame of each subprocess pays the
#: interpreter startup cost.
WALL_CLOCK_LIMIT_SEC = 30.0

#: Legacy handle-only handler. It proves the received frame bytes are the
#: dispatched bytes by returning them reversed (so the caller can invert
#: the transformation), and proves the received metadata (including the
#: injected ``frame`` info key, Requirement 3.9) round-trips by returning
#: it with one marker key added (Requirement 3.6).
LEGACY_HANDLE_HANDLER = """\
def handle(frame_bytes, metadata):
    out_meta = dict(metadata)
    out_meta["handled"] = True
    return frame_bytes[::-1], out_meta
"""


@pytest.fixture(scope="module")
def legacy_handle_bridge(tmp_path_factory):
    handler_dir = tmp_path_factory.mktemp("prop2_legacy_handle")
    handler_path = os.path.join(str(handler_dir), "handler.py")
    with open(handler_path, "w") as f:
        f.write(LEGACY_HANDLE_HANDLER)
    bridge = CustomPythonBridge(
        "prop2-legacy-handle",
        handler_path,
        wall_clock_limit_sec=WALL_CLOCK_LIMIT_SEC,
    )
    yield bridge
    bridge.stop()


# Metadata travels to the handler subprocess and back as JSON, so the
# generated dicts are constrained to JSON-round-trip-stable values. The
# top-level key "frame" is reserved for the info the runner injects
# (Requirement 3.9) and is excluded from generation; "handled" is the
# handler's own marker and the expected-value computation mirrors the
# handler, so it needs no exclusion.
_JSON_VALUES = st.recursive(
    st.none()
    | st.booleans()
    | st.integers(min_value=-(2 ** 53), max_value=2 ** 53)
    | st.floats(allow_nan=False, allow_infinity=False)
    | st.text(max_size=20),
    lambda children: st.lists(children, max_size=3)
    | st.dictionaries(st.text(max_size=10), children, max_size=3),
    max_leaves=10,
)

_METADATA = st.dictionaries(
    st.text(max_size=10).filter(lambda key: key != "frame"),
    _JSON_VALUES,
    max_size=4,
)

# The handle path is format-agnostic (only process_frame handlers restrict
# the Pixel_Format, Requirement 3.5), so unsupported formats are included
# to pin the pre-existing behavior.
_FORMATS = st.sampled_from(["RGB", "BGR", "RGBA", "GRAY8", "NV12", "I420"])


# ---------------------------------------------------------------------------
# Feature: custom-python-frames, Property 2: Legacy handle contract is
# preserved
#
# For any frame bytes and metadata dict, a handler defining only
# handle(frame_bytes, metadata) invoked through the CustomPythonBridge
# receives the frame bytes unchanged, receives the metadata enriched with
# the frame info key, and its returned (frame_bytes, metadata) round-trips
# back to the bridge caller exactly as under the pre-existing contract.
#
# **Validates: Requirements 3.6, 3.9**
# ---------------------------------------------------------------------------


@given(
    frame=st.binary(max_size=256),
    metadata=_METADATA,
    width=st.integers(min_value=1, max_value=64),
    height=st.integers(min_value=1, max_value=64),
    fmt=_FORMATS,
)
def test_property_2_legacy_handle_contract_preserved(
    legacy_handle_bridge, frame, metadata, width, height, fmt
):
    """**Feature: custom-python-frames, Property 2: Legacy handle
    contract is preserved**

    **Validates: Requirements 3.6, 3.9**
    """
    out_frame, out_meta = legacy_handle_bridge.process_frame(
        frame, metadata=metadata, width=width, height=height,
        frame_format=fmt,
    )

    # The handler reversed the bytes it received; the output equalling the
    # reversed input proves both that the handler received the dispatched
    # bytes unchanged and that its returned frame bytes round-tripped to
    # the bridge caller (Requirement 3.6).
    assert out_frame == frame[::-1]

    # The handler returned its received metadata plus a marker key: the
    # caller-supplied entries survive unchanged, the runner-injected
    # "frame" info key carries the dispatched caps triple (Requirement
    # 3.9), and the handler's own addition round-trips (Requirement 3.6).
    expected_meta = dict(metadata)
    expected_meta["frame"] = {"width": width, "height": height,
                              "format": fmt}
    expected_meta["handled"] = True
    assert out_meta == expected_meta
