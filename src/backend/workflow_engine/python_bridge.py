#
#  Copyright 2025 Amazon Web Services, Inc.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#

"""Custom_Python_Node bridge — the ``emlpython`` element (Requirement 9.8).

The compiled document maps a ``custom_python`` node to a single
``emlpython`` element carrying ``handler-path`` (the node's
``python/{nodeId}/handler.py`` inside the Workflow_Component artifacts).
There is no compiled GStreamer plugin behind that factory; instead the
executor manages the bridge itself, exactly as the design prescribes:

- **Launch-string rewrite** (pure functions): every ``emlpython`` element
  is replaced by an ``appsink name=py_in_{nodeId}`` / ``appsrc
  name=py_out_{nodeId}`` pair, splitting the segment so the rendered
  string stays in the dialect ``Gst.parse_launch`` accepts. Both
  synthetic elements keep the custom node's ``nodeId`` so bus errors map
  back to the node (Requirement 9.7).
- **Subprocess isolation**: the user's ``handler.py`` runs in a separate
  Python process spawned with a bounded address space
  (``RLIMIT_AS``) and a per-frame wall-clock limit enforced by the
  bridge. User code never runs inside the LocalServer process.
- **Framed stdin/stdout protocol**: each message is a 4-byte big-endian
  header length, a UTF-8 JSON header, then ``header["frameSize"]`` raw
  frame bytes. Executor -> handler headers carry ``nodeId``, ``width``,
  ``height``, ``format`` and ``metadata``; handler -> executor headers
  carry ``status`` (``ok``/``error``), ``metadata`` and the transformed
  frame. The handler contract is ``handle(frame_bytes, metadata) ->
  (frame_bytes, metadata)``.
- **Failure containment**: non-zero exit, wall-clock timeout, memory
  exhaustion, handler exceptions, and protocol violations all raise
  :class:`CustomPythonNodeError` carrying the ``node_id`` — the executor
  records the run as failed with that node identified and nothing else
  is affected (Requirements 9.8, 13.7).

The protocol/subprocess/limit logic below is dependency-free and unit
tested with real subprocesses; only :func:`run_bridged_pipeline` touches
GStreamer (lazily imported), mirroring the patterns of
``GstPipelineManager.run_pipeline``.
"""

import json
import logging
import os
import re
import select
import struct
import subprocess
import sys
import threading
import time
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

logger = logging.getLogger(__name__)

#: Factory name the compiler emits for custom_python nodes.
BRIDGE_FACTORY = "emlpython"
#: Element argument carrying the handler path (relative to the
#: Workflow_Component artifact directory).
HANDLER_PATH_ARG = "handler-path"

#: Per-frame wall-clock limit for one handler invocation.
DEFAULT_WALL_CLOCK_LIMIT_SEC = 10.0
#: Address-space bound for the handler subprocess (RLIMIT_AS).
DEFAULT_MEMORY_LIMIT_BYTES = 512 * 1024 * 1024

#: Protocol sanity bounds — anything past these is a protocol violation.
MAX_HEADER_BYTES = 1 << 20
MAX_FRAME_BYTES = 1 << 30

_HEADER_LEN = struct.Struct(">I")

_NAME_SAFE_RE = re.compile(r"[^A-Za-z0-9_-]")


def _safe_name(node_id: str) -> str:
    return _NAME_SAFE_RE.sub("_", str(node_id))


def sink_name(node_id: str) -> str:
    """Name of the executor-managed appsink feeding the node's subprocess."""
    return "py_in_{0}".format(_safe_name(node_id))


def src_name(node_id: str) -> str:
    """Name of the executor-managed appsrc fed by the node's subprocess."""
    return "py_out_{0}".format(_safe_name(node_id))


class CustomPythonNodeError(Exception):
    """A Custom_Python_Node failure, identified by its node id.

    ``node_id`` lets the executor set ``failing_node_id`` directly —
    the failure fails only that workflow run (Requirements 9.8, 13.7).
    """

    def __init__(self, node_id: Optional[str], message: str) -> None:
        self.node_id = node_id
        super().__init__(
            "Custom Python node '{0}': {1}".format(node_id, message)
        )


# ---------------------------------------------------------------------------
# Framed protocol (shared by the bridge; the runner embeds its own copy)
# ---------------------------------------------------------------------------


def write_message(stream, header: Dict[str, Any], frame: bytes = b"") -> None:
    """Write one framed message: length-prefixed JSON header + frame bytes."""
    header = dict(header)
    header["frameSize"] = len(frame)
    raw = json.dumps(header).encode("utf-8")
    stream.write(_HEADER_LEN.pack(len(raw)))
    stream.write(raw)
    if frame:
        stream.write(frame)
    stream.flush()


class ProtocolViolation(Exception):
    """The peer wrote something that is not a valid protocol message."""


def decode_header(raw: bytes) -> Dict[str, Any]:
    """Parse and sanity-check a JSON header, or raise ProtocolViolation."""
    try:
        header = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise ProtocolViolation("invalid JSON header: {0}".format(e))
    if not isinstance(header, dict):
        raise ProtocolViolation("header is not a JSON object")
    frame_size = header.get("frameSize", 0)
    if not isinstance(frame_size, int) or not 0 <= frame_size <= MAX_FRAME_BYTES:
        raise ProtocolViolation(
            "invalid frameSize {0!r}".format(frame_size)
        )
    return header


# ---------------------------------------------------------------------------
# Runner script executed inside the subprocess (self-contained on purpose:
# the subprocess must not depend on LocalServer being importable)
# ---------------------------------------------------------------------------

#: The handler contract: ``handler.py`` defines
#: ``handle(frame_bytes, metadata) -> (frame_bytes, metadata)``.
#: Returning ``None`` for the frame passes the input frame through.
#: stdout belongs to the protocol — handlers must not print to it.
RUNNER_SOURCE = r'''
import importlib.util
import json
import os
import struct
import sys
import traceback


def _read_exact(stream, n):
    data = b""
    while len(data) < n:
        chunk = stream.read(n - len(data))
        if not chunk:
            return None
        data += chunk
    return data


def _write(stream, header, frame):
    header = dict(header)
    header["frameSize"] = len(frame)
    raw = json.dumps(header).encode("utf-8")
    stream.write(struct.pack(">I", len(raw)))
    stream.write(raw)
    if frame:
        stream.write(frame)
    stream.flush()


def main():
    handler_path = sys.argv[1]
    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer
    # Let the handler import siblings shipped in its python/{nodeId}/ dir.
    sys.path.insert(0, os.path.dirname(os.path.abspath(handler_path)))
    try:
        spec = importlib.util.spec_from_file_location(
            "dda_custom_python_handler", handler_path
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        handle = getattr(module, "handle", None)
        if not callable(handle):
            raise TypeError(
                "handler.py does not define a callable "
                "handle(frame_bytes, metadata)"
            )
    except BaseException:
        _write(stdout, {"status": "error",
                        "error": traceback.format_exc(limit=20)}, b"")
        sys.exit(2)

    while True:
        prefix = _read_exact(stdin, 4)
        if prefix is None:
            return  # clean shutdown: executor closed our stdin
        (header_len,) = struct.unpack(">I", prefix)
        raw_header = _read_exact(stdin, header_len)
        if raw_header is None:
            return
        header = json.loads(raw_header.decode("utf-8"))
        frame = _read_exact(stdin, int(header.get("frameSize", 0))) or b""
        try:
            result = handle(frame, header.get("metadata") or {})
            if isinstance(result, tuple):
                out_frame, out_meta = result
            else:
                out_frame, out_meta = result, {}
            if out_frame is None:
                out_frame = frame
            if not isinstance(out_frame, (bytes, bytearray)):
                raise TypeError(
                    "handle() must return frame bytes, got "
                    + type(out_frame).__name__
                )
            _write(stdout, {"status": "ok", "metadata": out_meta or {}},
                   bytes(out_frame))
        except BaseException:
            _write(stdout, {"status": "error",
                            "error": traceback.format_exc(limit=20)}, b"")
            sys.exit(1)


main()
'''


def _memory_limit_preexec(limit_bytes: Optional[int]):
    """A preexec_fn applying RLIMIT_AS in the child, or None when
    unlimited/unsupported (non-POSIX)."""
    if not limit_bytes:
        return None
    try:
        import resource  # noqa: F401 - POSIX only
    except ImportError:  # pragma: no cover - non-POSIX
        return None

    def _apply():
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (limit_bytes, limit_bytes))

    return _apply


class _Timeout(Exception):
    pass


class _PeerClosed(Exception):
    pass


# ---------------------------------------------------------------------------
# The bridge: one subprocess per Custom_Python_Node per run
# ---------------------------------------------------------------------------


class CustomPythonBridge:
    """Runs one node's ``handler.py`` in a limited subprocess and pumps
    frames through the framed stdin/stdout protocol.

    Every failure mode (missing handler, non-zero exit, wall-clock
    timeout, memory exhaustion, handler exception, protocol violation)
    raises :class:`CustomPythonNodeError` naming the node, and leaves the
    subprocess terminated.
    """

    def __init__(
        self,
        node_id: str,
        handler_path: str,
        wall_clock_limit_sec: float = DEFAULT_WALL_CLOCK_LIMIT_SEC,
        memory_limit_bytes: Optional[int] = DEFAULT_MEMORY_LIMIT_BYTES,
        python_executable: Optional[str] = None,
    ) -> None:
        self.node_id = node_id
        self.sink_name = sink_name(node_id)
        self.src_name = src_name(node_id)
        self._handler_path = handler_path
        self._wall_clock_limit_sec = wall_clock_limit_sec
        self._memory_limit_bytes = memory_limit_bytes
        self._python_executable = python_executable or sys.executable
        self._process: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()

    # -- lifecycle ------------------------------------------------------

    def start(self) -> None:
        """Spawn the handler subprocess (idempotent)."""
        with self._lock:
            self._start_locked()

    def _start_locked(self) -> None:
        if self._process is not None and self._process.poll() is None:
            return
        if not self._handler_path or not os.path.isfile(self._handler_path):
            raise CustomPythonNodeError(
                self.node_id,
                "handler not found at '{0}'".format(self._handler_path),
            )
        # PYTHONHOME (when set, e.g. for Triton's embedded interpreter
        # lookup) targets a different interpreter layout and would break
        # the spawned CPython's own bootstrap — the handler subprocess
        # runs the executor's interpreter with its default home.
        env = dict(os.environ)
        env.pop("PYTHONHOME", None)
        self._process = subprocess.Popen(
            [self._python_executable, "-c", RUNNER_SOURCE, self._handler_path],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            preexec_fn=_memory_limit_preexec(self._memory_limit_bytes),
            close_fds=True,
            env=env,
        )
        logger.info(
            "Custom Python node '%s': handler subprocess %s started "
            "(wall-clock %.1fs/frame, memory %s bytes)",
            self.node_id,
            self._process.pid,
            self._wall_clock_limit_sec,
            self._memory_limit_bytes,
        )

    def stop(self) -> None:
        """Terminate the subprocess (idempotent, never raises)."""
        with self._lock:
            process, self._process = self._process, None
        if process is None:
            return
        try:
            if process.stdin:
                process.stdin.close()  # EOF -> runner exits cleanly
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        except Exception:  # noqa: BLE001 - best-effort teardown
            logger.exception(
                "Custom Python node '%s': error stopping subprocess",
                self.node_id,
            )
        finally:
            for stream in (process.stdout, process.stderr):
                try:
                    if stream:
                        stream.close()
                except Exception:  # noqa: BLE001
                    pass

    # -- frame processing -----------------------------------------------

    def process_frame(
        self,
        frame: bytes,
        metadata: Optional[Dict[str, Any]] = None,
        width: Optional[int] = None,
        height: Optional[int] = None,
        frame_format: Optional[str] = None,
    ) -> Tuple[bytes, Dict[str, Any]]:
        """One handler invocation: send the frame, return the handler's
        ``(frame_bytes, metadata)`` within the wall-clock limit."""
        with self._lock:
            self._start_locked()
            deadline = time.monotonic() + self._wall_clock_limit_sec
            header = {
                "nodeId": self.node_id,
                "width": width,
                "height": height,
                "format": frame_format,
                "metadata": metadata or {},
            }
            try:
                write_message(self._process.stdin, header, frame)
            except (BrokenPipeError, OSError):
                raise self._death_error("exited while receiving a frame")
            try:
                response, out_frame = self._read_response(deadline)
            except _Timeout:
                self._kill_locked()
                raise CustomPythonNodeError(
                    self.node_id,
                    "exceeded the {0:g}s wall-clock limit for one "
                    "frame".format(self._wall_clock_limit_sec),
                )
            except _PeerClosed:
                raise self._death_error("exited without answering a frame")
            except ProtocolViolation as e:
                self._kill_locked()
                raise CustomPythonNodeError(
                    self.node_id, "protocol violation: {0}".format(e)
                )
            if response.get("status") != "ok":
                error = str(
                    response.get("error") or "handler reported an error"
                ).strip()
                self._kill_locked()
                raise CustomPythonNodeError(
                    self.node_id, "handler failed: {0}".format(error)
                )
            return out_frame, response.get("metadata") or {}

    # -- internals ------------------------------------------------------

    def _read_response(self, deadline: float) -> Tuple[Dict[str, Any], bytes]:
        prefix = self._read_exact(4, deadline)
        (header_len,) = _HEADER_LEN.unpack(prefix)
        if header_len > MAX_HEADER_BYTES:
            raise ProtocolViolation(
                "header length {0} exceeds the {1}-byte bound".format(
                    header_len, MAX_HEADER_BYTES
                )
            )
        header = decode_header(self._read_exact(header_len, deadline))
        frame = self._read_exact(int(header.get("frameSize", 0)), deadline)
        return header, frame

    def _read_exact(self, n: int, deadline: float) -> bytes:
        """Read exactly n bytes from the subprocess stdout before the
        deadline, or raise _Timeout/_PeerClosed."""
        if n == 0:
            return b""
        fd = self._process.stdout.fileno()
        buf = bytearray()
        while len(buf) < n:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _Timeout()
            ready, _, _ = select.select([fd], [], [], min(remaining, 0.5))
            if not ready:
                continue
            chunk = os.read(fd, n - len(buf))
            if not chunk:
                raise _PeerClosed()
            buf.extend(chunk)
        return bytes(buf)

    def _kill_locked(self) -> None:
        process, self._process = self._process, None
        if process is None:
            return
        try:
            process.kill()
            process.wait(timeout=2)
        except Exception:  # noqa: BLE001 - best-effort
            pass
        finally:
            for stream in (process.stdin, process.stdout, process.stderr):
                try:
                    if stream:
                        stream.close()
                except Exception:  # noqa: BLE001
                    pass

    def _death_error(self, what: str) -> CustomPythonNodeError:
        """Build the node error for a subprocess that died on its own,
        folding in the exit code and a stderr tail."""
        process, self._process = self._process, None
        returncode = None
        stderr_tail = ""
        if process is not None:
            try:
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
                returncode = process.returncode
                if process.stderr:
                    stderr_tail = (
                        process.stderr.read() or b""
                    ).decode("utf-8", "replace")[-2000:].strip()
            except Exception:  # noqa: BLE001 - best-effort diagnostics
                pass
            finally:
                for stream in (process.stdin, process.stdout, process.stderr):
                    try:
                        if stream:
                            stream.close()
                    except Exception:  # noqa: BLE001
                        pass
        message = "subprocess {0} (exit code {1})".format(what, returncode)
        if returncode is not None and returncode < 0:
            message += " — killed by signal, possibly over the memory limit"
        if stderr_tail:
            message += ": {0}".format(stderr_tail)
        return CustomPythonNodeError(self.node_id, message)


# ---------------------------------------------------------------------------
# Compiled-document rewrite (pure functions, no GStreamer)
# ---------------------------------------------------------------------------


class BridgeSpec(NamedTuple):
    """One emlpython element found in the compiled document."""

    node_id: str
    handler_path: str  # relative to the component artifact directory
    sink_name: str
    src_name: str


def bridge_specs(document: Dict) -> List[BridgeSpec]:
    """Every emlpython element in the document, in render order."""
    specs = []
    for segment in document.get("segments", []):
        for element in segment.get("elements", []):
            if element.get("factory") != BRIDGE_FACTORY:
                continue
            node_id = element.get("nodeId")
            specs.append(
                BridgeSpec(
                    node_id=node_id,
                    handler_path=element.get("args", {}).get(
                        HANDLER_PATH_ARG
                    ),
                    sink_name=sink_name(node_id),
                    src_name=src_name(node_id),
                )
            )
    return specs


def _appsink_element(node_id: str) -> Dict:
    return {
        "nodeId": node_id,
        "factory": "appsink",
        "args": {
            "name": sink_name(node_id),
            "emit-signals": True,
            "sync": False,
            "max-buffers": 1,
        },
    }


def _appsrc_element(node_id: str) -> Dict:
    return {
        "nodeId": node_id,
        "factory": "appsrc",
        "args": {
            "name": src_name(node_id),
            "is-live": True,
            "format": "time",
            "block": True,
        },
    }


def rewrite_document(document: Dict) -> Dict:
    """The document with every emlpython element replaced by the
    executor-managed appsink/appsrc pair.

    The containing segment is split at the bridge: the upstream part ends
    with the appsink (keeping the segment's ``from`` tee reference) and
    the downstream part starts with the appsrc (carrying the segment's
    ``linkTo`` funnel reference when it is the last part). Rendering the
    result stays within the ``Gst.parse_launch`` dialect the executor
    already runs. Documents without emlpython elements are returned
    unchanged (same content).
    """
    rewritten = dict(document)
    segments: List[Dict] = []
    for segment in document.get("segments", []):
        segments.extend(_split_segment(segment))
    rewritten["segments"] = segments
    return rewritten


def _split_segment(segment: Dict) -> List[Dict]:
    parts: List[Dict] = []
    current = {
        key: value
        for key, value in segment.items()
        if key not in ("elements", "linkTo")
    }
    current["elements"] = []
    base_name = segment.get("name", "s")
    split_index = 0
    for element in segment.get("elements", []):
        if element.get("factory") == BRIDGE_FACTORY:
            node_id = element.get("nodeId")
            current["elements"].append(_appsink_element(node_id))
            parts.append(current)
            split_index += 1
            current = {
                "name": "{0}_py{1}".format(base_name, split_index),
                "elements": [_appsrc_element(node_id)],
            }
        else:
            current["elements"].append(element)
    if segment.get("linkTo"):
        current["linkTo"] = segment["linkTo"]
    parts.append(current)
    return parts


def build_bridges(
    specs: List[BridgeSpec],
    artifact_path: str,
    wall_clock_limit_sec: float = DEFAULT_WALL_CLOCK_LIMIT_SEC,
    memory_limit_bytes: Optional[int] = DEFAULT_MEMORY_LIMIT_BYTES,
) -> List[CustomPythonBridge]:
    """One CustomPythonBridge per spec, handler paths resolved against
    the component artifact directory."""
    bridges = []
    for spec in specs:
        if not spec.handler_path:
            raise CustomPythonNodeError(
                spec.node_id,
                "compiled document does not specify {0}".format(
                    HANDLER_PATH_ARG
                ),
            )
        bridges.append(
            CustomPythonBridge(
                node_id=spec.node_id,
                handler_path=os.path.join(artifact_path, spec.handler_path),
                wall_clock_limit_sec=wall_clock_limit_sec,
                memory_limit_bytes=memory_limit_bytes,
            )
        )
    return bridges


# ---------------------------------------------------------------------------
# GStreamer wiring (lazy gi import; mirrors GstPipelineManager.run_pipeline)
# ---------------------------------------------------------------------------


def run_bridged_pipeline(
    launch_string: str,
    bridges: List[CustomPythonBridge],
    latency_metrics=None,
) -> dict:
    """Run a rewritten launch string, pumping each appsink through its
    node's subprocess and into the paired appsrc.

    Mirrors ``GstPipelineManager.run_pipeline`` (bus watch capturing
    errors outside the GLib callback, one-shot watchdog, tag parsing via
    ``parse_msg``) so workflow runs with Custom_Python_Nodes inherit the
    same error capture and emltriton tag handling. Bridge failures quit
    the loop and re-raise as :class:`CustomPythonNodeError`, failing only
    this run with the node identified (Requirements 9.8, 13.7).
    """
    import gi

    gi.require_version("Gst", "1.0")
    from gi.repository import Gst, GLib

    from exceptions.api.gst_pipeline_exception import (
        PipelineExecutionException,
        PipelineSyntaxException,
    )
    from gi.repository.GLib import GError
    from gstreamer.gst_pipeline import PIPELINE_TIMEOUT_SEC, GstPipelineManager

    manager = GstPipelineManager()  # reused for its parse_msg tag parsing
    parsed_tag_values: dict = {}
    pipeline_error: dict = {}
    pipeline = None
    loop = None

    def fail(message, error=None):
        if "message" not in pipeline_error:
            pipeline_error["message"] = message
            pipeline_error["error"] = error
        if loop is not None and loop.is_running():
            loop.quit()
        return False  # one-shot when scheduled via GLib.idle_add

    def on_message(bus, message):
        acceptable = [
            Gst.MessageType.ERROR,
            Gst.MessageType.EOS,
            Gst.MessageType.TAG,
        ]
        if message.type not in acceptable:
            return
        if message.type == Gst.MessageType.ERROR:
            err, dbg = message.parse_error()
            src = message.src.get_name() if message.src else "unknown"
            logger.error("Pipeline ERROR - %s : %s", src, err.message)
            fail(
                "Pipeline failed with: {0}. {1}".format(
                    err.message, dbg if dbg else ""
                )
            )
            return
        parsed_tag_values.update(
            manager.parse_msg(message, latency_metrics=latency_metrics)
        )
        if message.type != Gst.MessageType.TAG:
            loop.quit()

    def make_on_new_sample(bridge, src_element, caps_applied):
        def on_new_sample(sink):
            sample = sink.emit("pull-sample")
            if sample is None:
                return Gst.FlowReturn.OK
            buffer = sample.get_buffer()
            ok, mapinfo = buffer.map(Gst.MapFlags.READ)
            if not ok:
                GLib.idle_add(
                    fail,
                    "could not map input buffer",
                    CustomPythonNodeError(
                        bridge.node_id, "could not map input buffer"
                    ),
                )
                return Gst.FlowReturn.ERROR
            try:
                data = bytes(mapinfo.data)
            finally:
                buffer.unmap(mapinfo)
            caps = sample.get_caps()
            width = height = frame_format = None
            if caps is not None and caps.get_size() > 0:
                structure = caps.get_structure(0)
                frame_format = structure.get_string("format")
                has_width, value = structure.get_int("width")
                width = value if has_width else None
                has_height, value = structure.get_int("height")
                height = value if has_height else None
                if not caps_applied:
                    src_element.set_property("caps", caps)
                    caps_applied.append(True)
            try:
                out_bytes, _out_meta = bridge.process_frame(
                    data,
                    metadata={},
                    width=width,
                    height=height,
                    frame_format=frame_format,
                )
            except CustomPythonNodeError as e:
                GLib.idle_add(fail, str(e), e)
                return Gst.FlowReturn.ERROR
            out_buffer = Gst.Buffer.new_wrapped(out_bytes)
            out_buffer.pts = buffer.pts
            out_buffer.dts = buffer.dts
            out_buffer.duration = buffer.duration
            src_element.emit("push-buffer", out_buffer)
            return Gst.FlowReturn.OK

        return on_new_sample

    try:
        # Start every handler subprocess first: a missing/broken handler
        # fails the run before the pipeline ever goes to PLAYING.
        for bridge in bridges:
            bridge.start()

        Gst.init(None)
        try:
            pipeline = Gst.parse_launch(launch_string)
        except GError as e:
            raise PipelineSyntaxException(str(e))
        loop = GLib.MainLoop()

        for bridge in bridges:
            sink = pipeline.get_by_name(bridge.sink_name)
            src = pipeline.get_by_name(bridge.src_name)
            if sink is None or src is None:
                raise CustomPythonNodeError(
                    bridge.node_id,
                    "bridge elements {0}/{1} missing from the "
                    "pipeline".format(bridge.sink_name, bridge.src_name),
                )
            sink.connect(
                "new-sample", make_on_new_sample(bridge, src, [])
            )
            # Propagate upstream EOS through the bridge boundary.
            sink.connect(
                "eos",
                lambda _sink, _src=src: _src.emit("end-of-stream"),
            )

        bus = pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", on_message)

        def _watchdog():
            if loop.is_running():
                fail(
                    "Pipeline timed out after {0}s without completing "
                    "(no EOS/ERROR received).".format(PIPELINE_TIMEOUT_SEC)
                )
            return False  # one-shot

        watchdog_id = GLib.timeout_add_seconds(PIPELINE_TIMEOUT_SEC, _watchdog)

        ret = pipeline.set_state(Gst.State.PLAYING)
        if ret == Gst.StateChangeReturn.FAILURE:
            raise PipelineExecutionException(
                "Pipeline failed to change state to PLAYING, "
                "check logs above this."
            )
        loop.run()
        try:
            GLib.source_remove(watchdog_id)
        except Exception:  # noqa: BLE001 - already removed
            pass
        if pipeline_error:
            if pipeline_error.get("error") is not None:
                raise pipeline_error["error"]
            raise PipelineExecutionException(pipeline_error["message"])
    finally:
        if pipeline is not None:
            pipeline.set_state(Gst.State.NULL)
        for bridge in bridges:
            bridge.stop()
    return parsed_tag_values
