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
"""Property test for the Python_Bridge ``produce`` operation's
invocation contract, through a real handler subprocess.

The instrumented handler appends one JSON line per invocation to a
record file in its own directory, so the test observes both the
invocation count and the exact ``context`` argument independently of
the protocol response.

- **Feature: custom-python-source, Property 4: The Frame_Producer is
  invoked exactly once with the run's Trigger_Context** — Validates:
  Requirements 3.1
"""
import json
import os

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from workflow_engine.python_bridge import CustomPythonBridge

#: Generous limit: the first produce of the subprocess pays import cost.
WALL_CLOCK_LIMIT_SEC = 30.0

#: Records every invocation (with the observed context) to a JSONL file
#: next to the handler, then returns a minimal valid Produced_Frame.
RECORDING_HANDLER = """\
import json
import os

_RECORD = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "invocations.jsonl"
)


def produce_frame(context):
    with open(_RECORD, "a") as f:
        f.write(json.dumps(context) + "\\n")
    return {"data": b"\\x00", "width": 1, "height": 1, "format": "GRAY8"}
"""


@pytest.fixture(scope="module")
def recording_bridge(tmp_path_factory):
    handler_dir = tmp_path_factory.mktemp("prop4_producer")
    handler_path = os.path.join(str(handler_dir), "handler.py")
    with open(handler_path, "w") as f:
        f.write(RECORDING_HANDLER)
    bridge = CustomPythonBridge(
        "prop4-source", handler_path,
        wall_clock_limit_sec=WALL_CLOCK_LIMIT_SEC,
    )
    yield bridge, os.path.join(str(handler_dir), "invocations.jsonl")
    bridge.stop()


def read_invocations(record_path):
    if not os.path.exists(record_path):
        return []
    with open(record_path) as f:
        return [json.loads(line) for line in f if line.strip()]


#: JSON-representable Trigger_Context values (the context crosses the
#: framed protocol as JSON).
_json_values = st.recursive(
    st.none()
    | st.booleans()
    | st.integers(min_value=-(2 ** 31), max_value=2 ** 31)
    | st.floats(allow_nan=False, allow_infinity=False)
    | st.text(max_size=20),
    lambda children: st.lists(children, max_size=3)
    | st.dictionaries(st.text(max_size=10), children, max_size=3),
    max_leaves=10,
)

_contexts = st.dictionaries(st.text(max_size=10), _json_values, max_size=5)


# ---------------------------------------------------------------------------
# Feature: custom-python-source, Property 4: The Frame_Producer is invoked
# exactly once with the run's Trigger_Context
#
# For any JSON-representable Trigger_Context, a produce request through the
# Python_Bridge invokes the handler's produce_frame exactly once, and the
# context argument it observes equals the Trigger_Context sent.
#
# **Validates: Requirements 3.1**
# ---------------------------------------------------------------------------


@settings(deadline=None)
@given(context=_contexts)
def test_property_4_producer_invoked_exactly_once_with_the_context(
    recording_bridge, context
):
    """**Feature: custom-python-source, Property 4: The Frame_Producer
    is invoked exactly once with the run's Trigger_Context**

    **Validates: Requirements 3.1**
    """
    bridge, record_path = recording_bridge
    before = read_invocations(record_path)

    frame, width, height, fmt, _meta = bridge.produce_frame(context)
    assert (frame, width, height, fmt) == (b"\x00", 1, 1, "GRAY8")

    after = read_invocations(record_path)
    # Exactly one new invocation, and it observed the context sent.
    assert len(after) == len(before) + 1
    assert after[-1] == context
