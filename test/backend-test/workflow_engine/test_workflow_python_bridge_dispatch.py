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
"""Example tests for handler entry-point dispatch, pre-imports, and
library imports through a real :class:`CustomPythonBridge` subprocess
(custom-python-frames Requirements 3.7, 3.8, 4.1-4.5, 5.1, 5.7).

Sibling of ``test_workflow_python_bridge.py``, following its patterns:
real handler subprocesses over the framed stdin/stdout protocol, one
bridge per test with a generous wall-clock limit (the first frame pays
the numpy/cv2 import cost).
"""
import os

import pytest

from workflow_engine.python_bridge import (
    CustomPythonBridge,
    CustomPythonNodeError,
)

NODE_ID = "pynode"

#: Generous per-frame limit: the first frame of each subprocess pays the
#: numpy/cv2 import cost.
WALL_CLOCK_LIMIT_SEC = 30.0


def write_file(tmp_path, code, name="handler.py"):
    path = os.path.join(str(tmp_path), name)
    with open(path, "w") as f:
        f.write(code)
    return path


def make_bridge(handler_path, **kwargs):
    kwargs.setdefault("wall_clock_limit_sec", WALL_CLOCK_LIMIT_SEC)
    return CustomPythonBridge(NODE_ID, handler_path, **kwargs)


# A 2x2 RGB frame (12 bytes) for tests dispatching through the
# process_frame path, which needs valid caps.
RGB_FRAME = bytes(range(12))
RGB_CAPS = {"width": 2, "height": 2, "frame_format": "RGB"}


# ---------------------------------------------------------------------------
# Entry-point dispatch (Requirements 3.7, 3.8)
# ---------------------------------------------------------------------------


class TestEntryPointDispatch:
    def test_process_frame_wins_when_both_entry_points_defined(
        self, tmp_path
    ):
        # process_frame passes the frame through (None return) and tags
        # the metadata; handle would reverse the bytes and tag
        # differently. The output proves process_frame ran and handle
        # was ignored (Requirement 3.7).
        handler = (
            "def process_frame(frame, metadata):\n"
            "    metadata['entry'] = 'process_frame'\n"
            "    return None\n"
            "\n"
            "def handle(frame_bytes, metadata):\n"
            "    metadata['entry'] = 'handle'\n"
            "    return frame_bytes[::-1], metadata\n"
        )
        bridge = make_bridge(write_file(tmp_path, handler))
        try:
            out_frame, out_meta = bridge.process_frame(RGB_FRAME, **RGB_CAPS)
        finally:
            bridge.stop()
        assert out_frame == RGB_FRAME
        assert out_meta["entry"] == "process_frame"

    def test_neither_entry_point_names_both_and_the_node(self, tmp_path):
        # A module that loads fine but defines no entry point fails the
        # run with an error naming both accepted entry points
        # (Requirement 3.8).
        bridge = make_bridge(write_file(tmp_path, "x = 1\n"))
        try:
            with pytest.raises(CustomPythonNodeError) as excinfo:
                bridge.process_frame(b"frame")
        finally:
            bridge.stop()
        assert excinfo.value.node_id == NODE_ID
        message = str(excinfo.value)
        assert "process_frame" in message
        assert "handle" in message

    def test_non_callable_entry_points_name_both_and_the_node(
        self, tmp_path
    ):
        # Bound but non-callable names do not count as entry points
        # (Requirement 3.8).
        handler = "process_frame = 1\nhandle = 'nope'\n"
        bridge = make_bridge(write_file(tmp_path, handler))
        try:
            with pytest.raises(CustomPythonNodeError) as excinfo:
                bridge.process_frame(b"frame")
        finally:
            bridge.stop()
        assert excinfo.value.node_id == NODE_ID
        message = str(excinfo.value)
        assert "process_frame" in message
        assert "handle" in message


# ---------------------------------------------------------------------------
# Pre-imported cv2/np and device library imports (Requirements 4.1-4.5)
# ---------------------------------------------------------------------------


class TestPreImportsAndLibraryImports:
    def test_handler_uses_cv2_and_np_without_import_statements(
        self, tmp_path
    ):
        # cv2 and np are bound in the handler module's namespace before
        # its code executes (Requirements 4.1, 4.2): no import
        # statements anywhere in the handler.
        handler = (
            "CV2_VERSION = cv2.__version__\n"  # top-level use, no import
            "\n"
            "def process_frame(frame, metadata):\n"
            "    metadata['cv2_version'] = CV2_VERSION\n"
            "    metadata['numpy_is_np'] = numpy is np\n"
            "    return np.bitwise_not(frame)\n"
        )
        bridge = make_bridge(write_file(tmp_path, handler))
        try:
            out_frame, out_meta = bridge.process_frame(RGB_FRAME, **RGB_CAPS)
        finally:
            bridge.stop()
        assert out_frame == bytes(255 - b for b in RGB_FRAME)
        assert out_meta["cv2_version"]
        assert out_meta["numpy_is_np"] is True

    def test_handler_imports_stdlib_and_sibling_module(self, tmp_path):
        # A standard import statement resolves stdlib modules through
        # the device interpreter (Requirement 4.4) and modules shipped
        # beside handler.py in the node's artifact directory
        # (Requirement 4.5).
        write_file(
            tmp_path,
            "def marker():\n    return 'sibling-ok'\n",
            name="sibling_helper.py",
        )
        handler = (
            "import base64\n"
            "import sibling_helper\n"
            "\n"
            "def handle(frame_bytes, metadata):\n"
            "    out = dict(metadata)\n"
            "    out['encoded'] = base64.b64encode(frame_bytes)"
            ".decode('ascii')\n"
            "    out['sibling'] = sibling_helper.marker()\n"
            "    return frame_bytes, out\n"
        )
        bridge = make_bridge(write_file(tmp_path, handler))
        try:
            out_frame, out_meta = bridge.process_frame(b"abc")
        finally:
            bridge.stop()
        assert out_frame == b"abc"
        assert out_meta["encoded"] == "YWJj"
        assert out_meta["sibling"] == "sibling-ok"

    def test_blocked_cv2_import_still_runs_a_handle_only_handler(
        self, tmp_path
    ):
        # The runner prepends the handler's directory to sys.path, so an
        # import-raising cv2.py stub beside handler.py blocks the cv2
        # pre-import inside this subprocess. The binding is left absent
        # and a handle-only handler that never references cv2 runs
        # unaffected (Requirement 4.3).
        write_file(
            tmp_path,
            "raise ImportError('cv2 blocked for this test')\n",
            name="cv2.py",
        )
        handler = (
            "def handle(frame_bytes, metadata):\n"
            "    out = dict(metadata)\n"
            "    out['cv2_bound'] = 'cv2' in globals()\n"
            "    return frame_bytes, out\n"
        )
        bridge = make_bridge(write_file(tmp_path, handler))
        try:
            out_frame, out_meta = bridge.process_frame(b"frame")
        finally:
            bridge.stop()
        assert out_frame == b"frame"
        assert out_meta["cv2_bound"] is False


# ---------------------------------------------------------------------------
# dda_frames availability (Requirements 5.1, 5.7)
# ---------------------------------------------------------------------------


class TestDdaFramesImport:
    def test_import_dda_frames_with_only_handler_py_shipped(self, tmp_path):
        # `import dda_frames` resolves without the node shipping any
        # additional files (Requirement 5.1); it works from a
        # handle-based handler just as from a process_frame one
        # (Requirement 5.7).
        handler = (
            "import dda_frames\n"
            "\n"
            "def handle(frame_bytes, metadata):\n"
            "    out = dict(metadata)\n"
            "    out['formats'] = sorted(dda_frames.FORMAT_CHANNELS)\n"
            "    out['info'] = dda_frames.frame_info()\n"
            "    return frame_bytes, out\n"
        )
        handler_path = write_file(tmp_path, handler)
        assert os.listdir(str(tmp_path)) == ["handler.py"]
        bridge = make_bridge(handler_path)
        try:
            out_frame, out_meta = bridge.process_frame(
                b"frame", width=4, height=2, frame_format="GRAY8"
            )
        finally:
            bridge.stop()
        assert out_frame == b"frame"
        assert out_meta["formats"] == ["BGR", "GRAY8", "RGB", "RGBA"]
        assert out_meta["info"] == {
            "width": 4, "height": 2, "format": "GRAY8",
        }

    def test_import_dda_frames_from_a_process_frame_handler(self, tmp_path):
        # The same helper module serves process_frame handlers
        # (Requirement 5.7): to_bytes(frame) of the received array
        # equals the dispatched (unpadded) frame bytes.
        handler = (
            "import dda_frames\n"
            "\n"
            "def process_frame(frame, metadata):\n"
            "    metadata['roundtrip'] = "
            "dda_frames.to_bytes(frame) == bytes(range(12))\n"
            "    return None\n"
        )
        bridge = make_bridge(write_file(tmp_path, handler))
        try:
            out_frame, out_meta = bridge.process_frame(RGB_FRAME, **RGB_CAPS)
        finally:
            bridge.stop()
        assert out_frame == RGB_FRAME
        assert out_meta["roundtrip"] is True
