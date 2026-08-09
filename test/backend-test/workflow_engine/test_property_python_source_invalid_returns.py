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
"""Property test for the Python_Runner's rejection of invalid
``produce_frame`` return values, through a real handler subprocess.

The handler materializes the invalid return value described by the
Trigger_Context, so one handler covers the whole invalid-return domain.
Every rejection kills the handler subprocess (matching the per-frame
error contract), so each example pays a subprocess restart — the
deadline is disabled accordingly.

- **Feature: custom-python-source, Property 7: Invalid producer returns
  are rejected with a node-identifying error** — Validates:
  Requirements 3.8, 3.9, 3.10
"""
import base64
import os

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from workflow_engine.python_bridge import (
    CustomPythonBridge,
    CustomPythonNodeError,
)

NODE_ID = "prop7-source"
WALL_CLOCK_LIMIT_SEC = 30.0

#: Materializes the invalid return described by the context.
INVALID_RETURN_HANDLER = """\
import base64


def produce_frame(context):
    kind = context["kind"]
    if kind == "none":
        return None
    if kind == "scalar":
        return context["value"]
    if kind == "array":
        return np.zeros(tuple(context["shape"]), dtype=context["dtype"])
    mapping = dict(context["mapping"])
    if "data_b64" in mapping:
        mapping["data"] = base64.b64decode(mapping.pop("data_b64"))
    return mapping
"""


@pytest.fixture(scope="module")
def invalid_bridge(tmp_path_factory):
    handler_dir = tmp_path_factory.mktemp("prop7_producer")
    handler_path = os.path.join(str(handler_dir), "handler.py")
    with open(handler_path, "w") as f:
        f.write(INVALID_RETURN_HANDLER)
    bridge = CustomPythonBridge(
        NODE_ID, handler_path, wall_clock_limit_sec=WALL_CLOCK_LIMIT_SEC,
    )
    yield bridge
    bridge.stop()


def _b64(raw):
    return base64.b64encode(raw).decode("ascii")


_dims = st.integers(min_value=1, max_value=6)

#: A consistent data mapping needs width * height * channels bytes; an
#: inconsistent one carries any other byte count.
_supported_formats = st.sampled_from(["RGB", "RGBA", "GRAY8"])
_unsupported_formats = st.sampled_from(["BGR", "BGRA", "YUY2", "gray8", ""])

_CHANNELS = {"RGB": 3, "RGBA": 4, "GRAY8": 1}


@st.composite
def _inconsistent_length_mapping(draw):
    fmt = draw(_supported_formats)
    width = draw(_dims)
    height = draw(_dims)
    expected = width * height * _CHANNELS[fmt]
    length = draw(
        st.integers(min_value=0, max_value=expected * 2).filter(
            lambda n: n != expected
        )
    )
    return {
        "kind": "mapping",
        "mapping": {
            "data_b64": _b64(b"\x00" * length),
            "width": width,
            "height": height,
            "format": fmt,
        },
    }


@st.composite
def _unsupported_format_mapping(draw):
    fmt = draw(_unsupported_formats)
    width = draw(_dims)
    height = draw(_dims)
    return {
        "kind": "mapping",
        "mapping": {
            "data_b64": _b64(b"\x00" * (width * height)),
            "width": width,
            "height": height,
            "format": fmt,
        },
    }


_missing_key_mappings = st.sampled_from(
    [
        {},  # nothing at all
        {"data_b64": _b64(b"\x00"), "width": 1, "height": 1},  # no format
        {"data_b64": _b64(b"\x00"), "format": "GRAY8"},  # no dims
        {"width": 1, "height": 1, "format": "GRAY8"},  # no data
        {"metadata": {"a": 1}},  # only metadata
    ]
).map(lambda mapping: {"kind": "mapping", "mapping": mapping})

_invalid_specs = st.one_of(
    # None (Req 3.8)
    st.just({"kind": "none"}),
    # non-mapping scalars (Req 3.9)
    st.one_of(
        st.integers(min_value=-100, max_value=100),
        st.text(max_size=10),
        st.lists(st.integers(min_value=0, max_value=9), max_size=3),
    ).map(lambda value: {"kind": "scalar", "value": value}),
    # unsupported dtype (Req 3.9)
    st.builds(
        lambda h, w: {"kind": "array", "shape": [h, w], "dtype": "float32"},
        _dims, _dims,
    ),
    # unsupported dimensionality (Req 3.9)
    st.builds(
        lambda n: {"kind": "array", "shape": [n], "dtype": "uint8"}, _dims
    ),
    st.builds(
        lambda h, w, c, d: {
            "kind": "array", "shape": [h, w, c, d], "dtype": "uint8"
        },
        _dims, _dims, _dims, _dims,
    ),
    # unsupported channel counts (Req 3.9)
    st.builds(
        lambda h, w, c: {"kind": "array", "shape": [h, w, c],
                         "dtype": "uint8"},
        _dims, _dims, st.sampled_from([1, 2, 5, 6]),
    ),
    # mappings missing required keys (Req 3.9)
    _missing_key_mappings,
    # unsupported declared formats (Req 3.10)
    _unsupported_format_mapping(),
    # byte length inconsistent with the declared dims (Req 3.10)
    _inconsistent_length_mapping(),
)


# ---------------------------------------------------------------------------
# Feature: custom-python-source, Property 7: Invalid producer returns are
# rejected with a node-identifying error
#
# For any produce_frame return value outside the accepted shapes — None,
# arrays of unsupported dtype/dimensionality/channel count, non-mapping
# scalars, mappings missing required keys, mappings declaring a Pixel_Format
# outside {RGB, RGBA, GRAY8}, or mappings whose byte length is inconsistent
# with the declared dims — the run fails with an error identifying the node
# and describing the defect, and no Produced_Frame is resolved.
#
# **Validates: Requirements 3.8, 3.9, 3.10**
# ---------------------------------------------------------------------------


@settings(deadline=None)
@given(spec=_invalid_specs)
def test_property_7_invalid_returns_rejected_with_node_identifying_error(
    invalid_bridge, spec
):
    """**Feature: custom-python-source, Property 7: Invalid producer
    returns are rejected with a node-identifying error**

    **Validates: Requirements 3.8, 3.9, 3.10**
    """
    with pytest.raises(CustomPythonNodeError) as excinfo:
        invalid_bridge.produce_frame(spec)

    # The error identifies the node ...
    assert excinfo.value.node_id == NODE_ID
    message = str(excinfo.value)
    assert NODE_ID in message
    # ... and describes the defect against the produce_frame contract.
    assert "produce_frame" in message
    if spec["kind"] == "none":
        assert "a source must produce a frame" in message
