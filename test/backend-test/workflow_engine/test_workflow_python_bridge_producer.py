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
"""Example tests for the Python_Bridge producer mode: entry-point
errors, producer limits and their environment overrides, helper
availability inside a producer, and traceback propagation
(Requirements 3.2, 3.11, 3.12, 6.1, 6.2, 6.3, 6.4, 6.5).

Real handler subprocesses throughout, following the
``test_workflow_python_bridge`` patterns.
"""
import os
import time
from types import SimpleNamespace

import pytest

from workflow_engine.python_bridge import (
    DEFAULT_MEMORY_LIMIT_BYTES,
    DEFAULT_PRODUCER_MEMORY_LIMIT_BYTES,
    DEFAULT_PRODUCER_WALL_CLOCK_LIMIT_SEC,
    DEFAULT_WALL_CLOCK_LIMIT_SEC,
    PRODUCER_MEMORY_LIMIT_ENV,
    PRODUCER_WALL_CLOCK_ENV,
    CustomPythonBridge,
    CustomPythonNodeError,
    _fed_frame_caps,
    build_producer_bridge,
    producer_memory_limit_bytes,
    producer_wall_clock_limit_sec,
)

NODE_ID = "srcnode"


def write_handler(tmp_path, code, name="handler.py"):
    path = os.path.join(str(tmp_path), name)
    with open(path, "w") as f:
        f.write(code)
    return path


def make_bridge(handler_path, **kwargs):
    kwargs.setdefault("wall_clock_limit_sec", 30.0)
    return CustomPythonBridge(NODE_ID, handler_path, **kwargs)


# ---------------------------------------------------------------------------
# Entry-point handling (Requirements 3.2, 3.11)
# ---------------------------------------------------------------------------


class TestProducerEntryPoint:
    def test_handler_without_produce_frame_names_the_entry_point(
        self, tmp_path
    ):
        handler = "def handle(frame, metadata):\n    return frame, {}\n"
        bridge = make_bridge(write_handler(tmp_path, handler))
        try:
            with pytest.raises(CustomPythonNodeError) as excinfo:
                bridge.produce_frame({})
        finally:
            bridge.stop()
        assert excinfo.value.node_id == NODE_ID
        assert "produce_frame" in str(excinfo.value)

    def test_startup_error_for_entry_point_free_handler_lists_produce_frame(
        self, tmp_path
    ):
        bridge = make_bridge(write_handler(tmp_path, "x = 1\n"))
        try:
            with pytest.raises(CustomPythonNodeError) as excinfo:
                bridge.produce_frame({})
        finally:
            bridge.stop()
        message = str(excinfo.value)
        assert "produce_frame" in message
        assert "process_frame" in message
        assert "handle" in message

    def test_only_produce_frame_is_invoked_for_a_produce_request(
        self, tmp_path
    ):
        # All three entry points defined: process_frame/handle record
        # any invocation to a file that must stay absent (Req 3.11).
        handler = (
            "import os\n"
            "_MARK = os.path.join(\n"
            "    os.path.dirname(os.path.abspath(__file__)),\n"
            "    'per_frame_invoked')\n"
            "def _mark():\n"
            "    open(_MARK, 'w').close()\n"
            "def process_frame(frame, metadata):\n"
            "    _mark()\n"
            "    return frame\n"
            "def handle(frame, metadata):\n"
            "    _mark()\n"
            "    return frame, {}\n"
            "def produce_frame(context):\n"
            "    return {'data': b'\\x07', 'width': 1, 'height': 1,\n"
            "            'format': 'GRAY8'}\n"
        )
        bridge = make_bridge(write_handler(tmp_path, handler))
        try:
            frame, width, height, fmt, _ = bridge.produce_frame({})
        finally:
            bridge.stop()
        assert (frame, width, height, fmt) == (b"\x07", 1, 1, "GRAY8")
        assert not os.path.exists(
            os.path.join(str(tmp_path), "per_frame_invoked")
        )

    def test_per_frame_request_to_produce_only_handler_names_the_contract(
        self, tmp_path
    ):
        handler = (
            "def produce_frame(context):\n"
            "    return {'data': b'\\x00', 'width': 1, 'height': 1,\n"
            "            'format': 'GRAY8'}\n"
        )
        bridge = make_bridge(write_handler(tmp_path, handler))
        try:
            with pytest.raises(CustomPythonNodeError) as excinfo:
                bridge.process_frame(b"frame")
        finally:
            bridge.stop()
        message = str(excinfo.value)
        assert "process_frame" in message
        assert "handle" in message


# ---------------------------------------------------------------------------
# Runtime environment inside the producer (Requirement 3.12)
# ---------------------------------------------------------------------------


class TestProducerEnvironment:
    def test_np_is_bound_and_dda_frames_imports_in_a_producer(
        self, tmp_path
    ):
        handler = (
            "import dda_frames\n"
            "def produce_frame(context):\n"
            "    array = np.full((2, 3), 9, dtype=np.uint8)\n"
            "    data = dda_frames.to_bytes(array)\n"
            "    return {'data': data, 'width': 3, 'height': 2,\n"
            "            'format': 'GRAY8'}\n"
        )
        bridge = make_bridge(write_handler(tmp_path, handler))
        try:
            frame, width, height, fmt, _ = bridge.produce_frame({})
        finally:
            bridge.stop()
        assert (width, height, fmt) == (3, 2, "GRAY8")
        assert frame == b"\x09" * 6

    def test_fetched_sources_are_recorded_on_the_bridge(self, tmp_path):
        source = os.path.join(str(tmp_path), "payload.bin")
        with open(source, "wb") as f:
            f.write(b"\x01\x02")
        handler = (
            "import dda_frames\n"
            "def produce_frame(context):\n"
            "    data = dda_frames.load_bytes(context['src'])\n"
            "    return {'data': data, 'width': 2, 'height': 1,\n"
            "            'format': 'GRAY8'}\n"
        )
        bridge = make_bridge(write_handler(tmp_path, handler))
        try:
            frame, _, _, _, _ = bridge.produce_frame({"src": source})
        finally:
            bridge.stop()
        assert frame == b"\x01\x02"
        assert bridge.fetched_sources == [source]

    def test_denied_prefix_fetch_fails_the_produce_naming_the_source(
        self, tmp_path
    ):
        source = os.path.join(str(tmp_path), "payload.bin")
        with open(source, "wb") as f:
            f.write(b"\x01\x02")
        handler = (
            "import dda_frames\n"
            "def produce_frame(context):\n"
            "    data = dda_frames.load_bytes(context['src'])\n"
            "    return {'data': data, 'width': 2, 'height': 1,\n"
            "            'format': 'GRAY8'}\n"
        )
        bridge = make_bridge(write_handler(tmp_path, handler))
        try:
            with pytest.raises(CustomPythonNodeError) as excinfo:
                bridge.produce_frame(
                    {"src": source}, allowed_uri_prefixes=("s3://",)
                )
        finally:
            bridge.stop()
        message = str(excinfo.value)
        assert excinfo.value.node_id == NODE_ID
        assert source in message
        assert "outside the node's allowed" in message

    def test_producer_metadata_travels_back(self, tmp_path):
        handler = (
            "def produce_frame(context):\n"
            "    return {'data': b'\\x00', 'width': 1, 'height': 1,\n"
            "            'format': 'GRAY8',\n"
            "            'metadata': {'part_id': 'XYZ'}}\n"
        )
        bridge = make_bridge(write_handler(tmp_path, handler))
        try:
            _, _, _, _, metadata = bridge.produce_frame({})
        finally:
            bridge.stop()
        assert metadata == {"part_id": "XYZ"}


# ---------------------------------------------------------------------------
# Producer limits (Requirements 6.1, 6.2, 6.3, 6.4, 6.5)
# ---------------------------------------------------------------------------


SLEEPING_PRODUCER = (
    "import time\n"
    "def produce_frame(context):\n"
    "    time.sleep(30)\n"
)


class TestProducerLimits:
    def test_sleeping_producer_exceeds_the_wall_clock_limit(self, tmp_path):
        bridge = make_bridge(
            write_handler(tmp_path, SLEEPING_PRODUCER),
            wall_clock_limit_sec=0.5,
        )
        bridge.start()
        process = bridge._process
        with pytest.raises(CustomPythonNodeError) as excinfo:
            bridge.produce_frame({})
        message = str(excinfo.value)
        assert excinfo.value.node_id == NODE_ID
        # The timeout message states the limit (Req 6.4).
        assert "0.5" in message
        assert "wall-clock" in message
        # Deadline-based: the subprocess is killed, not slept out.
        deadline = time.time() + 5
        while process.poll() is None and time.time() < deadline:
            time.sleep(0.05)
        assert process.poll() is not None

    def test_wall_clock_env_var_applies_through_build_producer_bridge(
        self, tmp_path, monkeypatch
    ):
        handler_path = write_handler(tmp_path, SLEEPING_PRODUCER)
        monkeypatch.setenv(PRODUCER_WALL_CLOCK_ENV, "0.5")
        feed = SimpleNamespace(node_id=NODE_ID, handler_path="handler.py")
        bridge = build_producer_bridge(feed, str(tmp_path))
        assert bridge._handler_path == handler_path
        try:
            with pytest.raises(CustomPythonNodeError) as excinfo:
                bridge.produce_frame({})
        finally:
            bridge.stop()
        assert "0.5" in str(excinfo.value)

    def test_raising_producer_carries_the_handler_traceback(self, tmp_path):
        handler = (
            "def produce_frame(context):\n"
            "    raise ValueError('boom from producer code')\n"
        )
        bridge = make_bridge(write_handler(tmp_path, handler))
        try:
            with pytest.raises(CustomPythonNodeError) as excinfo:
                bridge.produce_frame({})
        finally:
            bridge.stop()
        message = str(excinfo.value)
        assert excinfo.value.node_id == NODE_ID
        assert "boom from producer code" in message
        assert "ValueError" in message
        assert "Traceback" in message

    def test_producer_limits_default_independently_of_per_frame_limits(
        self, monkeypatch
    ):
        monkeypatch.delenv(PRODUCER_WALL_CLOCK_ENV, raising=False)
        monkeypatch.delenv(PRODUCER_MEMORY_LIMIT_ENV, raising=False)
        assert producer_wall_clock_limit_sec() == (
            DEFAULT_PRODUCER_WALL_CLOCK_LIMIT_SEC
        )
        assert producer_memory_limit_bytes() == (
            DEFAULT_PRODUCER_MEMORY_LIMIT_BYTES
        )
        # The producer defaults are their own constants — the per-frame
        # limits stay what they were (Req 6.2, 6.3).
        assert DEFAULT_PRODUCER_WALL_CLOCK_LIMIT_SEC == 30.0
        assert DEFAULT_WALL_CLOCK_LIMIT_SEC == 10.0
        assert DEFAULT_MEMORY_LIMIT_BYTES == 512 * 1024 * 1024

    def test_env_vars_configure_the_producer_limits_only(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv(PRODUCER_WALL_CLOCK_ENV, "7.5")
        monkeypatch.setenv(
            PRODUCER_MEMORY_LIMIT_ENV, str(256 * 1024 * 1024)
        )
        assert producer_wall_clock_limit_sec() == 7.5
        assert producer_memory_limit_bytes() == 256 * 1024 * 1024

        feed = SimpleNamespace(
            node_id=NODE_ID, handler_path="python/srcnode/handler.py"
        )
        bridge = build_producer_bridge(feed, str(tmp_path))
        assert bridge._wall_clock_limit_sec == 7.5
        assert bridge._memory_limit_bytes == 256 * 1024 * 1024
        assert bridge._handler_path == os.path.join(
            str(tmp_path), "python/srcnode/handler.py"
        )
        # The per-frame limits are untouched by the producer env vars.
        per_frame = CustomPythonBridge(NODE_ID, "unused.py")
        assert per_frame._wall_clock_limit_sec == (
            DEFAULT_WALL_CLOCK_LIMIT_SEC
        )
        assert per_frame._memory_limit_bytes == DEFAULT_MEMORY_LIMIT_BYTES

    def test_invalid_env_values_fall_back_to_the_defaults(self, monkeypatch):
        monkeypatch.setenv(PRODUCER_WALL_CLOCK_ENV, "not-a-number")
        monkeypatch.setenv(PRODUCER_MEMORY_LIMIT_ENV, "-1")
        assert producer_wall_clock_limit_sec() == (
            DEFAULT_PRODUCER_WALL_CLOCK_LIMIT_SEC
        )
        assert producer_memory_limit_bytes() == (
            DEFAULT_PRODUCER_MEMORY_LIMIT_BYTES
        )

    def test_build_producer_bridge_requires_a_handler_path(self, tmp_path):
        feed = SimpleNamespace(node_id=NODE_ID, handler_path=None)
        with pytest.raises(CustomPythonNodeError) as excinfo:
            build_producer_bridge(feed, str(tmp_path))
        assert excinfo.value.node_id == NODE_ID


# ---------------------------------------------------------------------------
# Fed-appsrc caps derivation for run_bridged_pipeline (Requirement 7.4)
# ---------------------------------------------------------------------------


class TestFedFrameCaps:
    def test_explicit_format_wins(self):
        caps = _fed_frame_caps(
            "appsrc name=appsrc ! videoconvert ! fakesink",
            {"data": b"\x00" * 12, "width": 2, "height": 2,
             "format": "RGB"},
        )
        assert caps == "video/x-raw,format=RGB,width=2,height=2"

    def test_missing_format_falls_back_to_the_launch_caps_clause(self):
        caps = _fed_frame_caps(
            "appsrc name=appsrc caps=video/x-raw,format=GRAY8 ! fakesink",
            {"data": b"\x00" * 4, "width": 2, "height": 2},
        )
        assert caps == "video/x-raw,format=GRAY8,width=2,height=2"
