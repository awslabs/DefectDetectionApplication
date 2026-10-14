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
"""Preservation property test: per-frame requests (no ``op`` field)
through the produce-extended runner keep today's exact semantics.

The pre-feature oracle is implemented by construction: the per-frame
contract (frame-info injection into metadata, ``handle`` raw-bytes
round trip, ``process_frame`` array round trip) is computed in the
test, and each handler runs both WITH and WITHOUT a ``produce_frame``
definition — the presence of the producer entry point must not change
any per-frame response.

- **Feature: custom-python-source, Property 11: The per-frame protocol
  is preserved** — Validates: Requirements 6.6, 11.3
"""
import os

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from workflow_engine.python_bridge import CustomPythonBridge

WALL_CLOCK_LIMIT_SEC = 30.0

FORMAT_CHANNELS = {"RGB": 3, "BGR": 3, "RGBA": 4, "GRAY8": 1}

#: Raw-bytes contract: reverses the frame and marks the metadata.
HANDLE_ECHO = """\
def handle(frame, metadata):
    out = dict(metadata)
    out["echo"] = True
    return frame[::-1], out
"""

#: Array contract: returns the received array unchanged.
PROCESS_IDENTITY = """\
def process_frame(frame, metadata):
    return frame
"""

#: A producer entry point whose presence must not change the per-frame
#: path (it is never invoked by a request without an ``op`` field).
PRODUCE_SUFFIX = """\


def produce_frame(context):
    raise AssertionError(
        "produce_frame must never run for a per-frame request"
    )
"""


def _make_bridge(tmp_path_factory, name, handler_source):
    handler_dir = tmp_path_factory.mktemp("prop11_" + name)
    handler_path = os.path.join(str(handler_dir), "handler.py")
    with open(handler_path, "w") as f:
        f.write(handler_source)
    return CustomPythonBridge(
        "prop11-" + name, handler_path,
        wall_clock_limit_sec=WALL_CLOCK_LIMIT_SEC,
    )


@pytest.fixture(scope="module")
def handle_bridges(tmp_path_factory):
    plain = _make_bridge(tmp_path_factory, "handle", HANDLE_ECHO)
    with_produce = _make_bridge(
        tmp_path_factory, "handle_p", HANDLE_ECHO + PRODUCE_SUFFIX
    )
    yield plain, with_produce
    plain.stop()
    with_produce.stop()


@pytest.fixture(scope="module")
def process_bridges(tmp_path_factory):
    plain = _make_bridge(tmp_path_factory, "process", PROCESS_IDENTITY)
    with_produce = _make_bridge(
        tmp_path_factory, "process_p", PROCESS_IDENTITY + PRODUCE_SUFFIX
    )
    yield plain, with_produce
    plain.stop()
    with_produce.stop()


#: JSON-representable metadata (metadata crosses the protocol as JSON).
_metadata = st.dictionaries(
    st.text(max_size=8),
    st.one_of(
        st.none(),
        st.booleans(),
        st.integers(min_value=-1000, max_value=1000),
        st.text(max_size=10),
    ),
    max_size=4,
)


# ---------------------------------------------------------------------------
# Feature: custom-python-source, Property 11: The per-frame protocol is
# preserved
#
# For any frame bytes, caps (width/height/format), and metadata, a per-frame
# request (no op field) through the extended runner invokes process_frame or
# handle with today's exact semantics and returns a response identical in
# shape and content to the pre-change contract — for handlers that do and do
# not also define produce_frame.
#
# **Validates: Requirements 6.6, 11.3**
# ---------------------------------------------------------------------------


@settings(deadline=None)
@given(
    frame=st.binary(min_size=0, max_size=64),
    width=st.integers(min_value=1, max_value=16),
    height=st.integers(min_value=1, max_value=16),
    fmt=st.sampled_from(sorted(FORMAT_CHANNELS)),
    metadata=_metadata,
)
def test_property_11_handle_per_frame_semantics_preserved(
    handle_bridges, frame, width, height, fmt, metadata
):
    """**Feature: custom-python-source, Property 11: The per-frame
    protocol is preserved** (``handle`` raw-bytes contract)

    **Validates: Requirements 6.6, 11.3**
    """
    plain, with_produce = handle_bridges

    # Pre-change contract, by construction: the runner injects the
    # frame info under metadata["frame"], the handler reverses the
    # bytes and adds its marker.
    expected_meta = dict(metadata)
    expected_meta["frame"] = {"width": width, "height": height,
                              "format": fmt}
    expected_meta["echo"] = True
    expected_frame = frame[::-1]

    for bridge in (plain, with_produce):
        out_frame, out_meta = bridge.process_frame(
            frame, metadata=dict(metadata),
            width=width, height=height, frame_format=fmt,
        )
        assert out_frame == expected_frame
        assert out_meta == expected_meta


@settings(deadline=None)
@given(
    width=st.integers(min_value=1, max_value=8),
    height=st.integers(min_value=1, max_value=8),
    fmt=st.sampled_from(sorted(FORMAT_CHANNELS)),
    metadata=_metadata,
    data=st.data(),
)
def test_property_11_process_frame_per_frame_semantics_preserved(
    process_bridges, width, height, fmt, metadata, data
):
    """**Feature: custom-python-source, Property 11: The per-frame
    protocol is preserved** (``process_frame`` array contract)

    **Validates: Requirements 6.6, 11.3**
    """
    plain, with_produce = process_bridges

    size = width * height * FORMAT_CHANNELS[fmt]
    frame = data.draw(st.binary(min_size=size, max_size=size))

    # Pre-change contract, by construction: identity process_frame
    # emits the input bytes unchanged; the runner injects the frame
    # info under metadata["frame"].
    expected_meta = dict(metadata)
    expected_meta["frame"] = {"width": width, "height": height,
                              "format": fmt}

    for bridge in (plain, with_produce):
        out_frame, out_meta = bridge.process_frame(
            frame, metadata=dict(metadata),
            width=width, height=height, frame_format=fmt,
        )
        assert out_frame == frame
        assert out_meta == expected_meta
