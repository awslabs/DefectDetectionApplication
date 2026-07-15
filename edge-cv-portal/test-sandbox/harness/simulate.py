"""``HARNESS_MODE=simulate``: single-plugin Plugin_Simulator runs.

Runs the same sandbox image as the workflow test harness, but instead
of executing a Compiled Pipeline Document it exercises exactly one
custom-node plugin element against sample input frames (Custom Node
Designer, Requirements 7.2, 7.3, 7.6):

1. Stages the plugin's x86_64 ``.so`` from the Plugin_Library copy under
   the run's S3 prefix into the task's plugin scan directory (prepended
   to ``GST_PLUGIN_PATH`` before GStreamer initializes).
2. Stages the sample input frames exactly like the test-run dataset
   staging (sequential JPEG frame set) and uploads each staged input
   frame under the run's prefix so the UI can render input/output side
   by side (7.3).
3. Renders a single-plugin pipeline
   ``multifilesrc ! jpegparse ! jpegdec ! videoconvert !
   <element> <declared-params> ! videoconvert ! jpegenc ! appsink``
   and executes it via ``Gst.parse_launch`` like the test harness. The
   appsink is the frame capture + metadata tap: every output frame is
   uploaded and its result record ``{frameIndex, inputRef, outputRef,
   metadata}`` is flushed to S3 incrementally, so a mid-run failure
   retains everything produced before it (7.2, 7.3).
4. Abnormal plugin termination is contained to the task: bus errors and
   the plugin's stderr output (captured around pipeline execution) are
   recorded in the flushed results document; a hard crash kills only
   this Fargate task and the state machine's catch marks the run failed
   while the incrementally flushed partial results survive (7.6).

Environment contract (set by the RunSandbox state of the node-designer
simulator state machine — see the design's Plugin_Simulator section;
the state machine itself is task 8.2):

    SIMULATION_RUN_ID    The SimulationRuns item this run belongs to
    ARTIFACTS_BUCKET     Portal artifacts bucket
    DATASET_S3_PREFIX    Prefix holding the sample input frames (a
                         Test_Dataset copy or uploaded frames staged by
                         the Prepare step)
    RESULTS_S3_KEY       Key the simulation results document is flushed
                         to; frame images are staged under the sibling
                         ``frames/`` prefix
    PLUGIN_S3_KEY        Key of the plugin's x86_64 .so artifact (staged
                         under the run's prefix by the Prepare step)
    ELEMENT_FACTORY      GStreamer element factory name the plugin
                         provides (from the Plugin_Record)
    ELEMENT_PARAMETERS   JSON object {parameter: value} of declared
                         parameter values for this run (7.4 re-runs pass
                         changed values); optional, defaults to {}
    PLUGIN_SCAN_DIR      Optional override of the per-task plugin scan
                         directory (defaults to a run-local directory)
    PIPELINE_TIMEOUT_SEC Optional watchdog override (default 270 s; the
                         state machine enforces the 5-minute limit, 7.7)

The task role only has access to the run's S3 prefix — no Plugin_Library
write path, no other Use_Case data (7.2; enforced by task 8.2's stack).

Exit code 0 = the pipeline completed; the results document status is
``completed``. Exit code 1 = the run failed; the flushed document
carries the error (message + captured plugin error output) and every
frame result produced before the failure.
"""

import copy
import json
import logging
import os
import re
import sys
import tempfile
from typing import Any, Callable, Dict, List, Optional, Set

from . import dataset as dataset_module
from .harness import (
    download_dataset,
    make_flush,
    missing_element_factory,
    require_env,
    s3_client,
)

logger = logging.getLogger("sandbox-harness")

#: Watchdog for a stalled pipeline. Kept under the state machine's
#: 5-minute execution timeout (7.7, task 8.2) so the harness can flush
#: the timeout failure itself before the task is stopped.
PIPELINE_TIMEOUT_SEC = int(os.environ.get("PIPELINE_TIMEOUT_SEC", "270"))

#: Name of the appsink acting as the frame capture + metadata tap.
CAPTURE_SINK_NAME = "sim_capture"

#: Staged frame object names under the run's ``frames/`` prefix.
INPUT_FRAME_PATTERN = "input_%05d.jpg"
OUTPUT_FRAME_PATTERN = "output_%05d.jpg"

#: Cap on the captured plugin stderr included in the results error.
ERROR_OUTPUT_LIMIT = 8000

#: Run statuses in the simulation results document.
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"

#: Error codes recorded in the results document.
INVALID_ELEMENT = "INVALID_ELEMENT"
EMPTY_DATASET = "EMPTY_DATASET"
ELEMENT_NOT_AVAILABLE = "ELEMENT_NOT_AVAILABLE"
PIPELINE_EXECUTION_ERROR = "PIPELINE_EXECUTION_ERROR"
SIMULATION_TIMEOUT = "SIMULATION_TIMEOUT"
HARNESS_ERROR = "HARNESS_ERROR"

#: Launch-safe element factory / parameter names (GStreamer identifiers:
#: letters, digits, underscores, dashes). Anything else is refused before
#: it can reach Gst.parse_launch (the values are user-influenced).
_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")

#: Characters in a rendered argument value that require launch quoting
#: (pipeline syntax, quoting, and the launch parser's escape character).
_UNSAFE_VALUE_PATTERN = re.compile(r"[\s!\"'()=,;\\]")


# ---------------------------------------------------------------------------
# Pure helpers: env parsing, launch-string assembly, result-record shaping
# (unit-testable without GStreamer or AWS — mirrored on harness.renderer)
# ---------------------------------------------------------------------------

def parse_element_parameters(raw: Optional[str]) -> Dict[str, Any]:
    """The declared parameter values from the ELEMENT_PARAMETERS env
    JSON. Only scalar values (str/int/float/bool) are kept — element
    properties are scalars in launch syntax; non-scalar entries are
    dropped with a warning (the start endpoint validates the shape, so
    this is defensive only). Absent/malformed input yields {}."""
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError:
        logger.warning("Malformed ELEMENT_PARAMETERS %r; using no parameters",
                       raw)
        return {}
    if not isinstance(parsed, dict):
        logger.warning("ELEMENT_PARAMETERS is not an object; using no "
                       "parameters")
        return {}
    values: Dict[str, Any] = {}
    for name, value in parsed.items():
        if isinstance(value, (str, int, float, bool)):
            values[str(name)] = value
        else:
            logger.warning("Dropping non-scalar ELEMENT_PARAMETERS entry %r",
                           name)
    return values


def invalid_identifier(element_factory: str,
                       parameters: Dict[str, Any]) -> Optional[str]:
    """A message describing the first launch-unsafe element factory or
    parameter name, or None when everything is a plain GStreamer
    identifier. Refusing these before ``Gst.parse_launch`` keeps
    user-influenced strings from injecting pipeline syntax."""
    if not _NAME_PATTERN.match(element_factory or ""):
        return ("Element factory name {0!r} is not a valid GStreamer "
                "element name".format(element_factory))
    for name in parameters:
        if not _NAME_PATTERN.match(name):
            return ("Parameter name {0!r} is not a valid GStreamer "
                    "property name".format(name))
    return None


def render_argument_value(value: Any) -> str:
    """One launch-string argument value: bools lower-cased like
    harness.renderer.render_value; strings that contain launch syntax
    characters (whitespace, ``!``, quotes, ``=`` ...) are double-quoted
    with backslash escaping — the quoting ``Gst.parse_launch``
    understands — so declared parameter values cannot break the
    single-plugin pipeline apart."""
    if isinstance(value, bool):
        return "true" if value else "false"
    text = str(value)
    if isinstance(value, str) and (not text or _UNSAFE_VALUE_PATTERN.search(text)):
        escaped = text.replace("\\", "\\\\").replace('"', '\\"')
        return '"{0}"'.format(escaped)
    return text


def render_element_invocation(element_factory: str,
                              parameters: Dict[str, Any]) -> str:
    """``<element> <declared-params>`` — the plugin element with its
    declared parameter values in insertion order."""
    parts = [element_factory]
    for name, value in parameters.items():
        parts.append("{0}={1}".format(name, render_argument_value(value)))
    return " ".join(parts)


def render_simulation_launch(element_factory: str, parameters: Dict[str, Any],
                             dataset_location: str,
                             capture_name: str = CAPTURE_SINK_NAME) -> str:
    """The complete single-plugin launch string (design's Plugin_Simulator
    RunSandbox step): ``multifilesrc ! decode ! <element> <declared-params>
    ! frame capture + metadata tap``. The decode chain matches the
    test-run simulation source chain (jpegparse ! jpegdec over the staged
    sequential JPEG frame set); the appsink is the capture/metadata tap
    the runtime drains per frame (7.2, 7.3)."""
    return " ! ".join([
        "multifilesrc location={0}".format(
            render_argument_value(dataset_location)),
        "jpegparse",
        "jpegdec",
        "videoconvert",
        render_element_invocation(element_factory, parameters),
        "videoconvert",
        "jpegenc",
        "appsink name={0} emit-signals=true sync=false".format(capture_name),
    ])


def run_prefix(results_key: str) -> str:
    """The run's S3 prefix, derived from the results key the same way
    the test harness stages node outputs next to its results document."""
    return results_key.rsplit("/", 1)[0] + "/" if "/" in results_key else ""


def input_frame_key(results_key: str, index: int) -> str:
    """S3 key of the staged input frame image for ``index``."""
    return "{0}frames/{1}".format(run_prefix(results_key),
                                  INPUT_FRAME_PATTERN % index)


def output_frame_key(results_key: str, index: int) -> str:
    """S3 key of the captured output frame image for ``index``."""
    return "{0}frames/{1}".format(run_prefix(results_key),
                                  OUTPUT_FRAME_PATTERN % index)


def frame_record(frame_index: int, input_ref: Optional[str],
                 output_ref: Optional[str],
                 metadata: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """One per-frame result record — the ``{frameIndex, inputRef,
    outputRef, metadata}`` shape the design's RunSandbox step flushes
    and the UI renders side by side (7.3)."""
    return {
        "frameIndex": frame_index,
        "inputRef": input_ref,
        "outputRef": output_ref,
        "metadata": metadata if metadata is not None else {},
    }


def frame_metadata(pts: Optional[int], duration: Optional[int], size: int,
                   caps: Optional[str],
                   tags: Dict[str, Any]) -> Dict[str, Any]:
    """The metadata tap payload for one captured output frame: buffer
    timing, size, negotiated caps, and the tags the plugin emitted up to
    this frame (7.3)."""
    return {
        "ptsNs": pts,
        "durationNs": duration,
        "bytes": size,
        "caps": caps,
        "tags": dict(tags),
    }


def missing_frame_records(frame_count: int, produced_indexes: Set[int],
                          input_refs: List[str]) -> List[Dict[str, Any]]:
    """Backfill records for input frames the pipeline completed without
    producing an output frame for (an element may legitimately drop
    frames), so a completed run's results cover every input frame with
    the input reference retained and a null output reference."""
    records = []
    for index in range(frame_count):
        if index not in produced_indexes:
            records.append(frame_record(
                index,
                input_refs[index] if index < len(input_refs) else None,
                None,
                {"note": "No output frame was produced for this input frame"},
            ))
    return records


def extend_plugin_path(existing: Optional[str], scan_dir: str) -> str:
    """The GST_PLUGIN_PATH value with the task's plugin scan directory
    prepended (staged plugin found first; the image's DDA plugin set
    stays available)."""
    if not existing:
        return scan_dir
    parts = existing.split(os.pathsep)
    if scan_dir in parts:
        return existing
    return scan_dir + os.pathsep + existing


def error_output_tail(text: str, limit: int = ERROR_OUTPUT_LIMIT) -> str:
    """The tail of the captured plugin stderr that gets recorded in the
    results error (bounded so the results document stays small)."""
    return text[-limit:] if len(text) > limit else text


# ---------------------------------------------------------------------------
# Results store (incremental flush, mirroring harness.results.ResultsStore)
# ---------------------------------------------------------------------------

class SimulationResultsStore:
    """The simulation results document with incremental flushing.

    Document shape::

        {"element": ..., "parameters": {...}, "status": running|completed|
         failed, "frameCount": int|None, "frames": [{frameIndex, inputRef,
         outputRef, metadata}, ...], "error": {"code", "message",
         "errorOutput"}|None}

    Every mutation flushes the complete document through the injected
    flush callable (an S3 put in the container), so a mid-run plugin
    failure retains every frame result produced before it (7.2, 7.6).
    """

    def __init__(self, element_factory: str, parameters: Dict[str, Any],
                 flush: Optional[Callable[[Dict], None]] = None):
        self._flush = flush or (lambda document: None)
        self._document: Dict[str, Any] = {
            "element": element_factory,
            "parameters": dict(parameters),
            "status": STATUS_RUNNING,
            "frameCount": None,
            "frames": [],
            "error": None,
        }

    # -- document ---------------------------------------------------------

    def to_document(self) -> Dict[str, Any]:
        """The full document (frames ordered by frameIndex; deep-copied
        so flushed snapshots cannot be mutated afterwards)."""
        document = copy.deepcopy(self._document)
        document["frames"].sort(key=lambda record: record["frameIndex"])
        return document

    def flush(self) -> None:
        self._flush(self.to_document())

    @property
    def frames(self) -> List[Dict[str, Any]]:
        return list(self._document["frames"])

    @property
    def produced_indexes(self) -> Set[int]:
        return {record["frameIndex"] for record in self._document["frames"]}

    # -- mutations (each flushes) ------------------------------------------

    def set_frame_count(self, count: int, flush: bool = True) -> None:
        self._document["frameCount"] = count
        if flush:
            self.flush()

    def add_frame(self, record: Dict[str, Any], flush: bool = True) -> None:
        """Append one per-frame result and flush — the incremental
        per-frame S3 write (7.2, 7.3)."""
        self._document["frames"].append(record)
        if flush:
            self.flush()

    def set_completed(self, flush: bool = True) -> None:
        self._document["status"] = STATUS_COMPLETED
        if flush:
            self.flush()

    def set_error(self, message: str, code: Optional[str] = None,
                  error_output: Optional[str] = None,
                  flush: bool = True) -> None:
        """Mark the run failed with the error description and the
        plugin's captured error output; produced frame results are
        retained (7.6)."""
        self._document["status"] = STATUS_FAILED
        self._document["error"] = {
            "code": code,
            "message": message,
            "errorOutput": error_output_tail(error_output or ""),
        }
        if flush:
            self.flush()

    def has_failure(self) -> bool:
        return self._document["status"] == STATUS_FAILED


# ---------------------------------------------------------------------------
# Runtime: plugin staging, stderr capture, GStreamer execution
# ---------------------------------------------------------------------------

def plugin_scan_dir(workdir: str) -> str:
    """The task's plugin scan directory: PLUGIN_SCAN_DIR when set (tests,
    stack overrides), otherwise a run-local directory under the task's
    workdir — per-task by construction on Fargate."""
    return os.environ.get("PLUGIN_SCAN_DIR") or os.path.join(workdir, "plugins")


def stage_plugin(s3, bucket: str, plugin_key: str, scan_dir: str) -> str:
    """Download the plugin ``.so`` into the task's plugin scan directory
    (before GStreamer initializes, so the registry scan finds it).
    Returns the staged path."""
    os.makedirs(scan_dir, exist_ok=True)
    filename = os.path.basename(plugin_key) or "plugin.so"
    if not filename.endswith(".so"):
        filename += ".so"
    target = os.path.join(scan_dir, filename)
    s3.download_file(bucket, plugin_key, target)
    return target


def upload_frame(s3, bucket: str, key: str, data: bytes) -> None:
    s3.put_object(Bucket=bucket, Key=key, Body=data, ContentType="image/jpeg")


class redirect_stderr_fd:
    """Capture fd 2 into a file around pipeline execution — GStreamer
    and the plugin's native code write warnings/errors there — and
    replay the captured output to the real stderr afterwards so the
    CloudWatch task log retains it (7.6)."""

    def __init__(self, path: str):
        self._path = path
        self._saved: Optional[int] = None
        self._file = None

    def __enter__(self):
        sys.stderr.flush()
        self._saved = os.dup(2)
        self._file = open(self._path, "ab")
        os.dup2(self._file.fileno(), 2)
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        sys.stderr.flush()
        os.dup2(self._saved, 2)
        os.close(self._saved)
        self._file.close()
        try:
            with open(self._path, "rb") as handle:
                data = handle.read()
            if data:
                os.write(2, data)
        except OSError:
            pass
        return False


def read_captured_stderr(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return ""


def taglist_to_dict(taglist) -> Dict[str, Any]:
    """A Gst.TagList as a JSON-serializable dict (non-serializable tag
    values stringified)."""
    result: Dict[str, Any] = {}
    for index in range(taglist.n_tags()):
        name = taglist.nth_tag_name(index)
        value = taglist.get_value_index(name, 0)
        try:
            json.dumps(value)
            result[name] = value
        except (TypeError, ValueError):
            result[name] = str(value)
    return result


def run_simulation_pipeline(launch_string: str,
                            on_frame: Callable[[int, bytes, Dict], None],
                            stderr_path: str,
                            capture_name: str = CAPTURE_SINK_NAME,
                            ) -> Optional[Dict[str, Any]]:
    """Execute the single-plugin launch string like the test harness
    (parse_launch + bus watch + GLib loop + watchdog), draining the
    capture appsink per frame through ``on_frame(index, jpeg_bytes,
    metadata)``.

    Returns ``{"code", "message", "element"}`` on a bus error/timeout or
    None on EOS. ``Gst.parse_launch`` failures (GLib.Error) propagate to
    the caller with the plugin's stderr captured in ``stderr_path``.
    """
    import gi
    gi.require_version("Gst", "1.0")
    from gi.repository import Gst, GLib

    pipeline_error: Dict[str, Any] = {}
    tags: Dict[str, Any] = {}
    state = {"index": 0}

    with redirect_stderr_fd(stderr_path):
        Gst.init(None)
        pipeline = Gst.parse_launch(launch_string)
        loop = GLib.MainLoop()

        def on_message(bus, message):
            if message.type == Gst.MessageType.ERROR:
                error, debug = message.parse_error()
                source = message.src.get_name() if message.src else "unknown"
                logger.error("Pipeline ERROR - %s : %s", source, error.message)
                pipeline_error["code"] = PIPELINE_EXECUTION_ERROR
                pipeline_error["element"] = source
                pipeline_error["message"] = (
                    "Pipeline failed with: {0}.{1}".format(
                        error.message, " " + debug if debug else ""))
                loop.quit()
            elif message.type == Gst.MessageType.EOS:
                logger.info("End of stream")
                loop.quit()
            elif message.type == Gst.MessageType.TAG:
                tags.update(taglist_to_dict(message.parse_tag()))

        def on_sample(sink):
            sample = sink.emit("pull-sample")
            if sample is None:
                return Gst.FlowReturn.OK
            buffer = sample.get_buffer()
            ok, mapinfo = buffer.map(Gst.MapFlags.READ)
            data = bytes(mapinfo.data) if ok else b""
            if ok:
                buffer.unmap(mapinfo)
            caps = sample.get_caps()
            pts = buffer.pts if buffer.pts != Gst.CLOCK_TIME_NONE else None
            duration = (buffer.duration
                        if buffer.duration != Gst.CLOCK_TIME_NONE else None)
            index = state["index"]
            state["index"] += 1
            metadata = frame_metadata(
                pts, duration, len(data),
                caps.to_string() if caps is not None else None, tags)
            try:
                on_frame(index, data, metadata)
            except Exception:
                logger.exception("Failed to record output frame %d", index)
                return Gst.FlowReturn.ERROR
            return Gst.FlowReturn.OK

        bus = pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", on_message)
        sink = pipeline.get_by_name(capture_name)
        if sink is not None:
            sink.connect("new-sample", on_sample)

        def watchdog():
            if loop.is_running():
                pipeline_error.setdefault("code", SIMULATION_TIMEOUT)
                pipeline_error.setdefault("element", None)
                pipeline_error.setdefault(
                    "message",
                    "Simulation run timed out after {0}s without completing "
                    "(no EOS/ERROR received)".format(PIPELINE_TIMEOUT_SEC))
                loop.quit()
            return False

        watchdog_id = GLib.timeout_add_seconds(PIPELINE_TIMEOUT_SEC, watchdog)

        try:
            ret = pipeline.set_state(Gst.State.PLAYING)
            if ret == Gst.StateChangeReturn.FAILURE:
                return {"code": PIPELINE_EXECUTION_ERROR, "element": None,
                        "message": "Pipeline failed to change state to "
                                   "PLAYING"}
            loop.run()
        finally:
            try:
                GLib.source_remove(watchdog_id)
            except Exception:
                pass
            pipeline.set_state(Gst.State.NULL)

    return pipeline_error or None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def element_unavailable_message(factory: str, element_factory: str,
                                plugin_path: str) -> str:
    """User-facing explanation of a ``no element "X"`` parse failure in a
    simulation run: the plugin's own element missing means the staged
    ``.so`` did not load/register; any other factory names a decode/
    capture element missing from the sandbox image."""
    if factory == element_factory:
        return ("The '{0}' element was not found after staging the plugin "
                "({1}) into the sandbox's plugin scan path — the plugin "
                "failed to load or does not register that element; see the "
                "captured error output".format(factory, plugin_path))
    return ("The '{0}' element required by the simulation pipeline is not "
            "available in the sandbox image".format(factory))


def main() -> int:
    run_id = require_env("SIMULATION_RUN_ID")
    bucket = require_env("ARTIFACTS_BUCKET")
    dataset_prefix = require_env("DATASET_S3_PREFIX")
    results_key = require_env("RESULTS_S3_KEY")
    plugin_key = require_env("PLUGIN_S3_KEY")
    element_factory = require_env("ELEMENT_FACTORY")
    parameters = parse_element_parameters(os.environ.get("ELEMENT_PARAMETERS"))
    logger.info("Simulation run %s: element=%s plugin=%s", run_id,
                element_factory, plugin_key)

    s3 = s3_client()
    flush = make_flush(s3, bucket, results_key)
    store = SimulationResultsStore(element_factory, parameters, flush)
    store.flush()  # results document exists before anything can fail

    invalid = invalid_identifier(element_factory, parameters)
    if invalid:
        store.set_error(invalid, code=INVALID_ELEMENT)
        logger.error("Refusing launch-unsafe input: %s", invalid)
        return 1

    try:
        return execute(s3, bucket, results_key, dataset_prefix, plugin_key,
                       element_factory, parameters, store)
    except Exception as error:  # flush the failure before the task dies
        logger.exception("Simulation harness failure")
        store.set_error(str(error), code=HARNESS_ERROR)
        return 1


def execute(s3, bucket: str, results_key: str, dataset_prefix: str,
            plugin_key: str, element_factory: str,
            parameters: Dict[str, Any],
            store: SimulationResultsStore) -> int:
    workdir = tempfile.mkdtemp(prefix="simulate-run-")

    # 1. Stage the plugin .so into the task's plugin scan directory and
    #    prepend it to GST_PLUGIN_PATH before GStreamer initializes (7.2).
    scan_dir = plugin_scan_dir(workdir)
    plugin_path = stage_plugin(s3, bucket, plugin_key, scan_dir)
    os.environ["GST_PLUGIN_PATH"] = extend_plugin_path(
        os.environ.get("GST_PLUGIN_PATH"), scan_dir)
    logger.info("Staged plugin %s into scan dir %s", plugin_path, scan_dir)

    # 2. Stage the sample input frames like the test-run dataset staging.
    files = download_dataset(s3, bucket, dataset_prefix,
                             os.path.join(workdir, "download"))
    logger.info("Downloaded %d sample input object(s)", len(files))
    staging_dir = os.path.join(workdir, "dataset")
    try:
        dataset_location = dataset_module.stage_dataset(files, staging_dir)
    except ValueError as error:
        store.set_error(str(error), code=EMPTY_DATASET)
        return 1
    plan = dataset_module.plan_staging(list(files.keys()))
    frame_count = len(plan)
    store.set_frame_count(frame_count, flush=False)

    # 3. Upload the staged input frames so every result record's inputRef
    #    resolves to a renderable image under the run's prefix (7.3).
    input_refs: List[str] = []
    for index, (_source, staged_name, _convert) in enumerate(plan):
        key = input_frame_key(results_key, index)
        with open(os.path.join(staging_dir, staged_name), "rb") as handle:
            upload_frame(s3, bucket, key, handle.read())
        input_refs.append(key)
    store.flush()

    # 4. Render the single-plugin pipeline and execute it; every captured
    #    output frame is uploaded and its result record flushed
    #    incrementally (7.2, 7.3).
    launch_string = render_simulation_launch(element_factory, parameters,
                                             dataset_location)
    logger.info("Simulation launch string: %s", launch_string)

    def on_frame(index: int, jpeg_bytes: bytes,
                 metadata: Dict[str, Any]) -> None:
        output_key = output_frame_key(results_key, index)
        upload_frame(s3, bucket, output_key, jpeg_bytes)
        input_ref = input_refs[index] if index < len(input_refs) else None
        store.add_frame(frame_record(index, input_ref, output_key, metadata))

    stderr_path = os.path.join(workdir, "plugin-stderr.log")
    try:
        error = run_simulation_pipeline(launch_string, on_frame, stderr_path)
    except Exception as parse_error:
        # Gst.parse_launch raises GLib.Error('no element "X"') when the
        # staged plugin failed to load/register (or a stock element is
        # missing); anything else propagates to the generic handler.
        factory = missing_element_factory(str(parse_error))
        if factory is None:
            raise
        store.set_error(
            element_unavailable_message(factory, element_factory, plugin_path),
            code=ELEMENT_NOT_AVAILABLE,
            error_output=read_captured_stderr(stderr_path))
        logger.error("Element factory %r unavailable in the simulation "
                     "pipeline", factory)
        return 1

    if error:
        # Abnormal plugin termination: the bus error plus the plugin's
        # captured stderr, recorded in the flushed results while every
        # frame produced before the failure is retained (7.6).
        message = error["message"]
        if error.get("element"):
            message = "{0} (element {1})".format(message, error["element"])
        store.set_error(message,
                        code=error.get("code") or PIPELINE_EXECUTION_ERROR,
                        error_output=read_captured_stderr(stderr_path))
        logger.error("Simulation pipeline failed: %s", message)
        return 1

    # 5. Completed: backfill records for input frames the element produced
    #    no output for, so the results cover every input frame.
    produced = store.produced_indexes
    for record in missing_frame_records(frame_count, produced, input_refs):
        store.add_frame(record, flush=False)
    store.set_completed()
    logger.info("Simulation run completed: %d/%d frame(s) produced output",
                len(produced & set(range(frame_count))), frame_count)
    return 0


if __name__ == "__main__":
    sys.exit(main())
