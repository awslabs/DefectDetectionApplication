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
"""Example tests for the frame-based Custom Python handler contract:
entry-point dispatch, pre-imported cv2/np bindings, device/stdlib/sibling
imports, and the injected ``dda_frames`` helper module.

All tests run real handler subprocesses through
:class:`CustomPythonBridge`, mirroring ``test_workflow_python_bridge.py``.

Covers Requirements 3.7, 3.8, 4.1, 4.2, 4.3, 4.4, 4.5, 5.1, 5.7.
"""
import os

import pytest

from workflow_engine.python_bridge import (
    CustomPythonBridge,
    CustomPythonNodeError,
)

NODE_ID = "pyframes"

#: Generous per-frame limit: the first frame of a subprocess pays the
#: numpy/cv2 import cost.
WALL_CLOCK_LIMIT_SEC = 30.0

# A 2x2 BGR frame (12 bytes, no row padding) used by the dispatch tests.
WIDTH, HEIGHT, FORMAT = 2, 2, "BGR"
FRAME = bytes(range(WIDTH * HEIGHT * 3))
INVERTED_FRAME = bytes(255 - b for b in FRAME)


def write_handler(tmp_path, code, name="handler.py"):
    path = os.path.join(str(tmp_path), name)
    with open(path, "w") as f:
        f.write(code)
    return path


def make_bridge(handler_path, **kwargs):
    kwargs.setdefault("wall_clock_limit_sec", WALL_CLOCK_LIMIT_SEC)
    return CustomPythonBridge(NODE_ID, handler_path, **kwargs)


# ---------------------------------------------------------------------------
# Entry-point dispatch (Requirements 3.7, 3.8)
# ---------------------------------------------------------------------------


BOTH_ENTRY_POINTS_HANDLER = """\
def process_frame(frame, metadata):
    metadata["entry"] = "process_frame"
    return np.bitwise_not(frame)


def handle(frame_bytes, metadata):
    return b"HANDLE-MUST-NOT-WIN", {"entry": "handle"}
"""


class TestEntryPointDispatch:
    def test_process_frame_wins_when_both_are_defined(self, tmp_path):
        """When ``handler.py`` defines both entry points, ``process_frame``
        is invoked and ``handle`` is ignored (Requirement 3.7)."""
        bridge = make_bridge(
            write_handler(tmp_path, BOTH_ENTRY_POINTS_HANDLER)
        )
        try:
            out_frame, out_meta = bridge.process_frame(
                FRAME, width=WIDTH, height=HEIGHT, frame_format=FORMAT
            )
        finally:
            bridge.stop()
        assert out_frame == INVERTED_FRAME
        assert out_frame != b"HANDLE-MUST-NOT-WIN"
        assert out_meta["entry"] == "process_frame"

    def test_neither_entry_point_names_both_in_the_error(self, tmp_path):
        """A handler defining neither entry point fails the run with a
        ``CustomPythonNodeError`` naming ``process_frame`` and ``handle``
        (Requirement 3.8)."""
        bridge = make_bridge(write_handler(tmp_path, "x = 1\n"))
        with pytest.raises(CustomPythonNodeError) as excinfo:
            bridge.process_frame(
                FRAME, width=WIDTH, height=HEIGHT, frame_format=FORMAT
            )
        assert excinfo.value.node_id == NODE_ID
        message = str(excinfo.value)
        assert "process_frame" in message
        assert "handle" in message


# ---------------------------------------------------------------------------
# Pre-imported cv2 / np / numpy bindings (Requirements 4.1, 4.2, 4.3)
# ---------------------------------------------------------------------------


#: Uses cv2, np, and numpy at module top level and per frame without a
#: single import statement.
PREIMPORTED_HANDLER = """\
CV2_VERSION = cv2.__version__
NUMPY_VERSION = numpy.__version__
ONES = np.ones((1,), dtype=np.uint8)


def process_frame(frame, metadata):
    metadata["cv2_version"] = CV2_VERSION
    metadata["numpy_version"] = NUMPY_VERSION
    inverted = cv2.bitwise_not(frame)
    assert isinstance(inverted, np.ndarray)
    return inverted
"""


class TestPreImportedBindings:
    def test_handler_uses_cv2_and_np_without_import_statements(
        self, tmp_path
    ):
        """cv2, np, and numpy are bound in the handler module namespace
        before the handler code executes (Requirements 4.1, 4.2)."""
        bridge = make_bridge(write_handler(tmp_path, PREIMPORTED_HANDLER))
        try:
            out_frame, out_meta = bridge.process_frame(
                FRAME, width=WIDTH, height=HEIGHT, frame_format=FORMAT
            )
        finally:
            bridge.stop()
        assert out_frame == INVERTED_FRAME
        assert out_meta["cv2_version"]
        assert out_meta["numpy_version"]

    def test_blocked_cv2_still_runs_a_handle_only_handler(
        self, tmp_path, monkeypatch
    ):
        """With cv2 unimportable in the subprocess (an import-raising stub
        on PYTHONPATH), a handler that does not reference cv2 still loads
        and runs (Requirement 4.3)."""
        stub_dir = tmp_path / "stubs"
        stub_dir.mkdir()
        (stub_dir / "cv2.py").write_text(
            'raise ImportError("cv2 blocked for this test")\n'
        )
        existing = os.environ.get("PYTHONPATH", "")
        monkeypatch.setenv(
            "PYTHONPATH",
            str(stub_dir) + (os.pathsep + existing if existing else ""),
        )
        handler = (
            "def handle(frame_bytes, metadata):\n"
            "    try:\n"
            "        import cv2  # noqa: F401\n"
            "        blocked = False\n"
            "    except ImportError:\n"
            "        blocked = True\n"
            "    return frame_bytes[::-1], {'cv2_blocked': blocked}\n"
        )
        handler_dir = tmp_path / "node"
        handler_dir.mkdir()
        bridge = make_bridge(write_handler(handler_dir, handler))
        try:
            out_frame, out_meta = bridge.process_frame(b"abc")
        finally:
            bridge.stop()
        assert out_frame == b"cba"
        # The stub really did block cv2 inside the subprocess.
        assert out_meta["cv2_blocked"] is True


# ---------------------------------------------------------------------------
# Device-interpreter and sibling-module imports (Requirements 4.4, 4.5)
# ---------------------------------------------------------------------------


SIBLING_MODULE = """\
def reverse(data):
    return data[::-1]
"""

IMPORTING_HANDLER = """\
import base64

import frame_ops


def handle(frame_bytes, metadata):
    encoded = base64.b64encode(frame_bytes).decode("ascii")
    return frame_ops.reverse(frame_bytes), {"b64": encoded}
"""


class TestHandlerImports:
    def test_stdlib_and_sibling_module_imports_resolve(self, tmp_path):
        """A standard import resolves libraries of the device interpreter
        (stdlib ``base64``, Requirement 4.4) and modules shipped beside
        ``handler.py`` in the node's artifact directory (``frame_ops``,
        Requirement 4.5)."""
        import base64

        write_handler(tmp_path, SIBLING_MODULE, name="frame_ops.py")
        bridge = make_bridge(write_handler(tmp_path, IMPORTING_HANDLER))
        try:
            out_frame, out_meta = bridge.process_frame(b"payload")
        finally:
            bridge.stop()
        assert out_frame == b"daolyap"
        assert out_meta["b64"] == base64.b64encode(b"payload").decode("ascii")


# ---------------------------------------------------------------------------
# dda_frames injection (Requirements 5.1, 5.7)
# ---------------------------------------------------------------------------


#: handle-contract handler importing dda_frames — only handler.py shipped.
DDA_FRAMES_HANDLE_HANDLER = """\
import dda_frames


def handle(frame_bytes, metadata):
    info = metadata["frame"]
    array = dda_frames.to_array(
        frame_bytes, info["width"], info["height"], info["format"]
    )
    return dda_frames.to_bytes(array), {"shape": list(array.shape)}
"""

#: process_frame-contract handler importing dda_frames.
DDA_FRAMES_PROCESS_HANDLER = """\
import dda_frames


def process_frame(frame, metadata):
    metadata["info"] = dda_frames.frame_info()
    return None
"""


class TestDdaFramesInjection:
    def test_import_dda_frames_with_only_handler_py_shipped(self, tmp_path):
        """``import dda_frames`` succeeds although the node ships only
        ``handler.py`` — the runner injects the module (Requirement 5.1);
        available under the ``handle`` contract (Requirement 5.7)."""
        assert os.listdir(str(tmp_path)) == []
        bridge = make_bridge(
            write_handler(tmp_path, DDA_FRAMES_HANDLE_HANDLER)
        )
        assert os.listdir(str(tmp_path)) == ["handler.py"]
        try:
            out_frame, out_meta = bridge.process_frame(
                FRAME, width=WIDTH, height=HEIGHT, frame_format=FORMAT
            )
        finally:
            bridge.stop()
        assert out_frame == FRAME
        assert out_meta["shape"] == [HEIGHT, WIDTH, 3]

    def test_dda_frames_available_to_process_frame_handlers(self, tmp_path):
        """The same helper module serves ``process_frame`` handlers alike
        (Requirements 5.1, 5.7)."""
        bridge = make_bridge(
            write_handler(tmp_path, DDA_FRAMES_PROCESS_HANDLER)
        )
        try:
            out_frame, out_meta = bridge.process_frame(
                FRAME, width=WIDTH, height=HEIGHT, frame_format=FORMAT
            )
        finally:
            bridge.stop()
        assert out_frame == FRAME
        assert out_meta["info"] == {
            "width": WIDTH, "height": HEIGHT, "format": FORMAT,
        }
