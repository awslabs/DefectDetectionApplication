"""Sandbox harness runtime: S3 I/O and GStreamer pipeline execution.

Environment (set by the RunSandbox state of the test-runner state
machine — edge-cv-portal/infrastructure/lib/test-runner-stack.ts):

    TEST_RUN_ID               The TestRuns item this run belongs to
    WORKFLOW_ID               Workflow under test
    USECASE_ID                Owning Use_Case
    ARTIFACTS_BUCKET          Portal artifacts bucket
    DATASET_S3_PREFIX         Prefix holding the Test_Dataset objects
    RESULTS_S3_KEY            Key the results document is flushed to
    COMPILED_DOCUMENT_S3_KEY  Key of the Compiled Pipeline Document
    SIMULATED_INFERENCE       JSON {"is_anomalous": bool, "confidence": 0..1}
                              — the user-configured outcome injected for
                              model inference nodes that stay stubbed
                              (i.e. have no staged model)
    STAGED_MODELS             JSON [{nodeId, modelName, s3Key}, ...] — the
                              Triton model artifacts the portal staged
                              under the run's prefix; each is unpacked
                              into /aws_dda/dda_triton/triton_model_repo
                              and the node's stub is rewritten into a
                              real emltriton element (harness/model_staging.py)
    TEST_RUNS_TABLE           TestRuns table (status is finalized by the
                              CollectResults/record_failure steps, not here)
    WORKFLOWS_S3_PREFIX       Portal workflow key prefix (default "workflows")

Exit code 0 = pipeline and bindings ran without a node failure; the
CollectResults step then finalizes the run from the flushed results.
Exit code 1 = a node failed (or the harness could not run at all); the
state machine's catch marks the run failed while the incrementally
flushed results retain everything produced before the failure and
identify the failing node (Requirements 12.7, 12.10).
"""

import copy
import json
import logging
import os
import re
import sys
import tempfile
import threading
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import bindings as bindings_module
from . import dataset as dataset_module
from . import model_staging
from . import renderer
from .results import (
    STATUS_COMPLETED,
    STATUS_RUNNING,
    ResultsStore,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("sandbox-harness")

#: Watchdog for a stalled pipeline (no EOS/ERROR). Step Functions stops
#: the task at 10 minutes regardless (12.13); quitting earlier lets the
#: harness flush the failing state itself.
PIPELINE_TIMEOUT_SEC = int(os.environ.get("PIPELINE_TIMEOUT_SEC", "540"))

#: Extra allowance the thread-based hard watchdog grants beyond
#: PIPELINE_TIMEOUT_SEC before force-killing the process. The GLib
#: watchdog above only dispatches once the main loop runs; when the main
#: thread wedges inside GStreamer/Triton C code (e.g. a state change
#: that never returns), only this thread timer can still act.
HARD_WATCHDOG_GRACE_SEC = int(os.environ.get("HARD_WATCHDOG_GRACE_SEC", "30"))

#: Bounded wait, in seconds, spent draining the pipeline bus for the
#: terminal GST_MESSAGE_ERROR after a synchronous PLAYING state-change
#: failure. GStreamer posts the real element's error (e.g. a staged
#: emltriton/CPU-Triton model that fails to load) on the bus even though
#: the GLib main loop never ran, so a short bounded wait lets the harness
#: surface the failing element and its backend detail instead of the
#: opaque "failed to change state to PLAYING" (Requirements 12.14, 12.15).
STATE_CHANGE_ERROR_DRAIN_SEC = int(
    os.environ.get("STATE_CHANGE_ERROR_DRAIN_SEC", "5"))


class _ParseFailure(Exception):
    """Internal sentinel: a ``Gst.parse_launch`` ``no element "X"``
    failure was already attributed to its owning node and flushed, so the
    caller should return exit code 1. Lets the initial pipeline run and
    the model-load fallback re-run share one parse-error handler."""


def exit_now(code: int) -> None:
    """Terminate the container immediately, bypassing interpreter
    finalization.

    An in-process Triton started by emltriton can leave non-daemon
    threads and C-level atexit work that block a normal ``sys.exit``
    indefinitely; the task then idles until the Step Functions
    10-minute timeout, whose generic message displaces the per-node
    error already flushed to S3 (observed on Fargate). All results are
    flushed before this is called, so skipping finalization loses
    nothing.
    """
    sys.stdout.flush()
    sys.stderr.flush()
    logging.shutdown()
    os._exit(code)


def make_hard_watchdog(store: ResultsStore,
                       exit_fn: Callable[[int], None] = exit_now,
                       timeout_sec: Optional[int] = None) -> Callable[[], None]:
    """The last-resort watchdog body: flush an explicit timeout error and
    kill the process.

    Runs on a plain thread timer, so it fires even when the main thread
    never reaches the GLib main loop. Separated from the timer wiring for
    unit testing.
    """
    seconds = PIPELINE_TIMEOUT_SEC if timeout_sec is None else timeout_sec

    def hard_watchdog() -> None:
        message = ("Pipeline wedged: no completion within {0}s and the "
                   "pipeline watchdog could not run (main thread blocked "
                   "in a GStreamer state change or element startup)"
                   .format(seconds))
        logger.error(message)
        try:
            store.add_run_error(message, code="PIPELINE_EXECUTION_TIMEOUT",
                                flush=False)
            store.skip_remaining()
        except Exception:  # noqa: BLE001 — the exit must still happen
            logger.exception("Hard watchdog could not flush results")
        exit_fn(1)

    return hard_watchdog

#: Fallback simulated inference outcome when SIMULATED_INFERENCE is
#: absent or malformed (matches the start endpoint's default).
DEFAULT_SIMULATED_INFERENCE = {"is_anomalous": False, "confidence": 0.9}


def parse_simulated_inference(raw: Optional[str]) -> Dict[str, Any]:
    """The simulated inference outcome from the SIMULATED_INFERENCE env
    JSON, falling back to :data:`DEFAULT_SIMULATED_INFERENCE` field by
    field on absent/malformed input (the start endpoint validates the
    shape, so this is defensive only)."""
    values = dict(DEFAULT_SIMULATED_INFERENCE)
    if not raw:
        return values
    try:
        parsed = json.loads(raw)
    except ValueError:
        logger.warning("Malformed SIMULATED_INFERENCE %r; using defaults", raw)
        return values
    if not isinstance(parsed, dict):
        logger.warning("SIMULATED_INFERENCE is not an object; using defaults")
        return values
    is_anomalous = parsed.get("is_anomalous")
    if isinstance(is_anomalous, bool):
        values["is_anomalous"] = is_anomalous
    confidence = parsed.get("confidence")
    if (isinstance(confidence, (int, float)) and not isinstance(confidence, bool)
            and 0.0 <= confidence <= 1.0):
        values["confidence"] = float(confidence)
    return values


def simulated_inference_from_env() -> Dict[str, Any]:
    return parse_simulated_inference(os.environ.get("SIMULATED_INFERENCE"))


#: Note recorded with every simulated-inference stub activity entry.
SIMULATED_INFERENCE_NOTE = (
    "Simulated: the model was not executed in the cloud sandbox; this "
    "configured outcome was injected"
)

#: Note recorded on a simulated-inference stub activity entry when the
#: simulated outcome was a *fallback* — the model was staged/attempted
#: but could not be staged or loaded on CPU Triton, so the configured
#: outcome was injected instead of failing the run (Requirements 12.16,
#: 12.17). The specific cause is carried in the entry's fallbackReason.
SIMULATED_INFERENCE_FALLBACK_NOTE = (
    "Simulated (fallback): the model could not be staged or loaded in the "
    "cloud sandbox, so this configured outcome was injected instead of "
    "failing the test run"
)


def parse_staging_fallbacks(raw: Optional[str]) -> Dict[str, str]:
    """``{nodeId: reason}`` from the STAGING_FALLBACKS env JSON.

    The portal's best-effort staging (task 11.11) omits nodes it could
    not stage from STAGED_MODELS and lists them here as
    ``[{nodeId, modelName, reason}, ...]``. Those nodes keep their
    ``sim_inference_<id>`` stub and run with the injected simulated
    outcome; the harness records each as ``inferenceMode: "simulated"``
    with this ``reason`` as the fallbackReason (Requirements 12.16,
    12.17, 12.18). Absent/malformed input yields ``{}`` — the run then
    behaves like a pre-11.11 run (no attributed staging fallbacks)."""
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError:
        logger.warning("Malformed STAGING_FALLBACKS %r; ignoring", raw)
        return {}
    if not isinstance(parsed, list):
        logger.warning("STAGING_FALLBACKS is not a list; ignoring")
        return {}
    fallbacks: Dict[str, str] = {}
    for entry in parsed:
        if isinstance(entry, dict) and entry.get("nodeId"):
            reason = entry.get("reason") or "Model could not be staged"
            fallbacks[str(entry["nodeId"])] = str(reason)
        else:
            logger.warning("Dropping malformed STAGING_FALLBACKS entry %r",
                           entry)
    return fallbacks

#: Note recorded with every custom-node pass-through stub activity entry
#: (custom-node-designer Requirement 12.2): the Custom_Node_Type has no
#: x86_64 Plugin_Artifact, so the compile step substituted a recording
#: stub that passes input frames through unchanged.
CUSTOM_NODE_STUB_NOTE = (
    "Simulated: this custom node has no x86_64 build; a pass-through "
    "stub recorded the frames the node would have consumed and passed "
    "them through unchanged"
)

#: Manifest written by the compile step next to the compiled document,
#: listing the staged custom x86_64 Plugin_Artifacts to download into the
#: task's plugin scan path plus the stubbed Custom_Node_Type ids
#: (custom-node-designer 12.1, 12.2).
CUSTOM_PLUGINS_MANIFEST_NAME = "custom_plugins.json"

#: Shape used when the manifest is absent (runs without custom nodes).
EMPTY_CUSTOM_PLUGINS_MANIFEST = {"plugins": [], "stubbedNodeTypeIds": []}


# ---------------------------------------------------------------------------
# S3 helpers
# ---------------------------------------------------------------------------

def s3_client():
    import boto3
    return boto3.client("s3")


def download_compiled_document(s3, bucket: str, key: str) -> Dict:
    response = s3.get_object(Bucket=bucket, Key=key)
    return json.loads(response["Body"].read().decode("utf-8"))


def download_dataset(s3, bucket: str, prefix: str, target_dir: str) -> Dict[str, str]:
    """Download every Test_Dataset object under ``prefix``; returns
    ``{filename: local_path}``."""
    os.makedirs(target_dir, exist_ok=True)
    files: Dict[str, str] = {}
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for entry in page.get("Contents", []):
            key = entry["Key"]
            name = key[len(prefix):].lstrip("/")
            if not name or name.endswith("/"):
                continue
            local_path = os.path.join(target_dir, name.replace("/", "__"))
            s3.download_file(bucket, key, local_path)
            files[name] = local_path
    return files


def custom_plugins_manifest_key(results_key: str) -> str:
    """The custom-plugins manifest lives next to the results document:
    .../test-runs/{test_run_id}/custom_plugins.json"""
    prefix = results_key.rsplit("/", 1)[0] if "/" in results_key else ""
    return (prefix + "/" if prefix else "") + CUSTOM_PLUGINS_MANIFEST_NAME


def parse_custom_plugins_manifest(document: Any) -> Dict:
    """The ``{plugins, stubbedNodeTypeIds}`` manifest shape from a parsed
    document; absent/malformed input yields the empty manifest (the
    compile step writes the manifest, so this is defensive only)."""
    if not isinstance(document, dict):
        return dict(EMPTY_CUSTOM_PLUGINS_MANIFEST)
    plugins = document.get("plugins")
    stubbed = document.get("stubbedNodeTypeIds")
    return {
        "plugins": [p for p in plugins if isinstance(p, dict)]
        if isinstance(plugins, list) else [],
        "stubbedNodeTypeIds": [s for s in stubbed if isinstance(s, str)]
        if isinstance(stubbed, list) else [],
    }


def load_custom_plugins_manifest(s3, bucket: str, results_key: str) -> Dict:
    """Read the custom-plugins manifest the compile step staged next to
    the compiled document; a missing manifest (runs without custom nodes,
    or a compile step predating custom-node support) yields the empty
    manifest (custom-node-designer 12.1)."""
    from botocore.exceptions import ClientError

    key = custom_plugins_manifest_key(results_key)
    try:
        response = s3.get_object(Bucket=bucket, Key=key)
        document = json.loads(response["Body"].read().decode("utf-8"))
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") not in ("NoSuchKey", "404"):
            raise
        return dict(EMPTY_CUSTOM_PLUGINS_MANIFEST)
    except (ValueError, UnicodeDecodeError) as e:
        logger.warning("Malformed custom plugins manifest %s: %s", key, str(e))
        return dict(EMPTY_CUSTOM_PLUGINS_MANIFEST)
    return parse_custom_plugins_manifest(document)


def plugin_scan_dir(workdir: str) -> str:
    """The task's plugin scan directory: PLUGIN_SCAN_DIR when set (tests,
    stack overrides), otherwise a run-local directory under the task's
    workdir — per-task by construction on Fargate."""
    return os.environ.get("PLUGIN_SCAN_DIR") or os.path.join(workdir, "plugins")


def extend_plugin_path(existing: Optional[str], scan_dir: str) -> str:
    """The GST_PLUGIN_PATH value with the task's plugin scan directory
    prepended (staged custom plugins found first; the image's DDA plugin
    set stays available)."""
    if not existing:
        return scan_dir
    parts = existing.split(os.pathsep)
    if scan_dir in parts:
        return existing
    return scan_dir + os.pathsep + existing


def stage_custom_plugins(s3, bucket: str, entries: List[Dict],
                         scan_dir: str) -> List[str]:
    """Download the staged custom x86_64 Plugin_Artifacts into the task's
    plugin scan directory (before GStreamer initializes, so the registry
    scan finds them — custom-node-designer 12.1). Returns the staged
    paths."""
    os.makedirs(scan_dir, exist_ok=True)
    staged: List[str] = []
    for entry in entries:
        key = entry.get("s3Key")
        if not key:
            continue
        filename = entry.get("fileName") or os.path.basename(key) or "plugin.so"
        if not filename.endswith(".so"):
            filename += ".so"
        target = os.path.join(scan_dir, filename)
        s3.download_file(bucket, key, target)
        staged.append(target)
    return staged


def make_flush(s3, bucket: str, key: str):
    """Incremental results flush: every call rewrites the full document
    to S3 so the latest snapshot always survives a mid-run failure
    (12.7, 12.10)."""
    def flush(document: Dict) -> None:
        s3.put_object(
            Bucket=bucket,
            Key=key,
            Body=json.dumps(document, indent=2).encode("utf-8"),
            ContentType="application/json",
        )
    return flush


def upload_node_output(s3, bucket: str, results_key: str,
                       node_id: str, payload: Dict) -> str:
    """Stage a node's output metadata next to the results document and
    return its S3 key (the outputs S3 ref shape from design)."""
    prefix = results_key.rsplit("/", 1)[0] if "/" in results_key else ""
    key = "{0}outputs/{1}.json".format(prefix + "/" if prefix else "", node_id)
    s3.put_object(
        Bucket=bucket,
        Key=key,
        Body=json.dumps(payload, indent=2).encode("utf-8"),
        ContentType="application/json",
    )
    return key


# ---------------------------------------------------------------------------
# Missing-element diagnosis (Gst.parse_launch 'no element "X"' failures)
# ---------------------------------------------------------------------------

#: ``Gst.parse_launch`` raises a GLib.Error whose message contains
#: ``no element "<factory>"`` when a required plugin is not installed -
#: in the sandbox image that is expected for the proprietary DDA plugins.
MISSING_ELEMENT_PATTERN = re.compile(r'no element "([^"]+)"')

#: Error code recorded when a pipeline element factory is unavailable.
ELEMENT_NOT_AVAILABLE = "ELEMENT_NOT_AVAILABLE"


def missing_element_factory(message: str) -> Optional[str]:
    """The factory name from a ``no element "X"`` parse failure, or None
    when the message describes a different failure."""
    match = MISSING_ELEMENT_PATTERN.search(message or "")
    return match.group(1) if match else None


def missing_element_message(factory: str) -> str:
    """User-facing per-node explanation of an unavailable element."""
    return ("The '{0}' element required by this node is not available in "
            "the cloud test sandbox (proprietary DDA plugins are not "
            "installed in the sandbox image). This node can only run on "
            "a device.".format(factory))


def missing_element_run_message(factory: str) -> str:
    """Run-level variant for a factory that maps to no workflow node
    (synthetic linking elements such as tee/queue)."""
    return ("The '{0}' element required by the pipeline is not available "
            "in the cloud test sandbox (proprietary DDA plugins are not "
            "installed in the sandbox image). This workflow can only run "
            "on a device.".format(factory))


def record_missing_element(document: Dict, store: ResultsStore,
                           factory: str) -> Optional[str]:
    """Attribute a ``no element "<factory>"`` parse failure to the owning
    node via the compiled document's segments (elements carry nodeId +
    factory) and record a per-node error on it; a factory owned by no
    node (synthetic tee/queue) records a run-level error with nodeId
    null instead. Remaining nodes are skipped; the flushed partial
    results are retained (12.10). Returns the failing nodeId or None."""
    owners = renderer.nodes_with_factory(document, factory)
    node_id = owners[0] if owners else None
    if node_id is not None:
        store.set_error(node_id, missing_element_message(factory),
                        code=ELEMENT_NOT_AVAILABLE, flush=False)
    else:
        store.add_run_error(missing_element_run_message(factory),
                            code=ELEMENT_NOT_AVAILABLE, flush=False)
    store.skip_remaining()
    return node_id


# ---------------------------------------------------------------------------
# GStreamer execution (mirrors GstPipelineManager.run_pipeline:
# parse_launch + bus watch + GLib loop + watchdog + tag parsing)
# ---------------------------------------------------------------------------

def run_gst_pipeline(launch_string: str, sim_sources: List[Tuple[str, str]],
                     store: ResultsStore) -> Tuple[Dict[str, Any], Optional[Dict]]:
    """Execute the rendered launch string exactly as LocalServer does.

    Returns ``(tag_values, error)`` where error is
    ``{"element": <bus source element name>, "message": ...}`` or None.
    ``sim_sources`` are the simulation appsrc stubs (hardware event
    inputs); the harness closes them immediately — no GPIO is polled —
    and records the substitution as stub activity (12.6).
    """
    import gi
    gi.require_version("Gst", "1.0")
    from gi.repository import Gst, GLib

    Gst.init(None)
    tag_values: Dict[str, Any] = {}
    pipeline_error: Dict[str, Any] = {}

    pipeline = Gst.parse_launch(launch_string)
    loop = GLib.MainLoop()

    def on_message(bus, message):
        if message.type == Gst.MessageType.ERROR:
            error, debug = message.parse_error()
            source = message.src.get_name() if message.src else "unknown"
            logger.error("Pipeline ERROR - %s : %s", source, error.message)
            pipeline_error["element"] = source
            pipeline_error["message"] = "Pipeline failed with: {0}.{1}".format(
                error.message, " " + debug if debug else "")
            loop.quit()
        elif message.type == Gst.MessageType.EOS:
            logger.info("End of stream")
            loop.quit()
        elif message.type == Gst.MessageType.TAG:
            # Same eminfer tag names GstPipelineManager.parse_msg reads.
            taglist = message.parse_tag()
            for tag in ("is_anomalous", "confidence"):
                value = taglist.get_value_index(tag, 0)
                if value is not None:
                    tag_values[tag] = value

    bus = pipeline.get_bus()
    bus.add_signal_watch()
    bus.connect("message", on_message)

    def watchdog():
        if loop.is_running():
            pipeline_error.setdefault("element", None)
            pipeline_error.setdefault(
                "message",
                "Pipeline timed out after {0}s without completing "
                "(no EOS/ERROR received)".format(PIPELINE_TIMEOUT_SEC))
            loop.quit()
        return False

    watchdog_id = GLib.timeout_add_seconds(PIPELINE_TIMEOUT_SEC, watchdog)

    # Last-resort hard watchdog on a plain thread: the GLib watchdog above
    # only dispatches once loop.run() executes; a state change that blocks
    # forever (e.g. emltriton waiting on an embedded Triton that cannot
    # come up) wedges the main thread before that, and only a thread timer
    # can still flush a diagnosis and end the task ahead of the opaque
    # Step Functions 10-minute timeout.
    hard_timer = threading.Timer(
        PIPELINE_TIMEOUT_SEC + HARD_WATCHDOG_GRACE_SEC,
        make_hard_watchdog(store))
    hard_timer.daemon = True
    hard_timer.start()

    try:
        ret = pipeline.set_state(Gst.State.PLAYING)
        if ret == Gst.StateChangeReturn.FAILURE:
            # Synchronous failure: the GLib loop never ran, so on_message
            # never fired. Drain the bus for the terminal GST_MESSAGE_ERROR
            # the failing element already posted (bounded wait) so the
            # reported error carries the real element name and its backend
            # detail (e.g. a staged emltriton/CPU-Triton model that fails
            # to load) instead of the opaque "failed to change state to
            # PLAYING". Mirrors the device executor's detail-capturing path
            # in src/backend/workflow_engine/python_bridge.py; the returned
            # shape matches the async on_message path so execute() maps the
            # element back to its owning node identically (12.14, 12.15).
            err_msg = bus.timed_pop_filtered(
                STATE_CHANGE_ERROR_DRAIN_SEC * Gst.SECOND,
                Gst.MessageType.ERROR)
            if err_msg is not None:
                error, debug = err_msg.parse_error()
                source = err_msg.src.get_name() if err_msg.src else "unknown"
                logger.error("Pipeline state-change ERROR - %s : %s",
                             source, error.message)
                return tag_values, {
                    "element": source,
                    "message": "Pipeline failed with: {0}.{1}".format(
                        error.message, " " + debug if debug else ""),
                }
            # No error arrived within the bounded wait: fall back to the
            # generic message with no attributable element.
            return tag_values, {"element": None,
                                "message": "Pipeline failed to change state "
                                           "to PLAYING"}

        # Close the simulation event-source stubs: nothing is polled from
        # GPIO; the stub substitution itself is the recorded activity.
        for element_name, node_id in sim_sources:
            element = pipeline.get_by_name(element_name)
            if element is not None:
                element.emit("end-of-stream")
            store.add_stub_activity(node_id, {
                "type": "sim_event_source",
                "element": element_name,
                "note": "Simulated: event input stubbed with an appsrc; "
                        "no hardware input was polled",
            })

        loop.run()
    finally:
        try:
            GLib.source_remove(watchdog_id)
        except Exception:
            pass
        # The hard timer stays armed through this teardown on purpose: a
        # NULL-transition that never returns must also end in a flushed
        # diagnosis rather than the opaque task timeout.
        pipeline.set_state(Gst.State.NULL)
        hard_timer.cancel()

    return tag_values, (pipeline_error or None)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError("Missing required environment variable " + name)
    return value


def main() -> int:
    test_run_id = require_env("TEST_RUN_ID")
    bucket = require_env("ARTIFACTS_BUCKET")
    dataset_prefix = require_env("DATASET_S3_PREFIX")
    results_key = require_env("RESULTS_S3_KEY")
    compiled_key = require_env("COMPILED_DOCUMENT_S3_KEY")
    logger.info("Test run %s: workflow=%s usecase=%s", test_run_id,
                os.environ.get("WORKFLOW_ID"), os.environ.get("USECASE_ID"))

    s3 = s3_client()
    flush = make_flush(s3, bucket, results_key)

    document = download_compiled_document(s3, bucket, compiled_key)
    node_ids = renderer.all_node_ids(document)
    store = ResultsStore(node_ids, flush)
    store.flush()  # results document exists before anything can fail

    try:
        return execute(s3, bucket, results_key, dataset_prefix, document, store)
    except Exception as error:  # flush the failure before the task dies
        logger.exception("Harness failure")
        pending = [n for n in store.node_ids
                   if store.record(n)["status"] in ("pending", "running")]
        if pending:
            store.set_error(pending[0], str(error), code="HARNESS_ERROR",
                            flush=False)
        store.skip_remaining()
        return 1


def execute(s3, bucket: str, results_key: str, dataset_prefix: str,
            document: Dict, store: ResultsStore) -> int:
    workdir = tempfile.mkdtemp(prefix="test-run-")

    # -1. Custom plugin staging (custom-node-designer 12.1): the compile
    #     step staged each used Custom_Node_Type's x86_64 Plugin_Artifact
    #     under the run's prefix and listed it in custom_plugins.json.
    #     Download them into the task's plugin scan directory and prepend
    #     it to GST_PLUGIN_PATH before GStreamer initializes, so the
    #     registry scan finds the custom elements and the pipeline
    #     executes them. Runs without custom nodes have no manifest.
    manifest = load_custom_plugins_manifest(s3, bucket, results_key)
    if manifest["plugins"]:
        scan_dir = plugin_scan_dir(workdir)
        staged_plugins = stage_custom_plugins(s3, bucket,
                                              manifest["plugins"], scan_dir)
        os.environ["GST_PLUGIN_PATH"] = extend_plugin_path(
            os.environ.get("GST_PLUGIN_PATH"), scan_dir)
        logger.info("Staged %d custom plugin(s) into scan dir %s: %s",
                    len(staged_plugins), scan_dir, staged_plugins)

    # 0. Best-effort Triton model staging (Requirements 12.16, 12.17).
    #
    #    Model availability is never a precondition for a test run: the
    #    contract surfaced in the test panel is "the model is not executed
    #    in cloud tests; the configured outcome is injected". Staging is
    #    best-effort on both sides:
    #
    #    * STAGING_FALLBACKS lists nodes the portal could not stage (task
    #      11.11); they were omitted from STAGED_MODELS, keep their
    #      sim_inference stub, and run simulated with the reason surfaced
    #      as the node's fallbackReason (12.18).
    #    * STAGED_MODELS lists the artifacts the portal staged under the
    #      run's prefix. Each zip is unpacked into the (initially empty)
    #      Triton model repository and the node's stub is rewritten into a
    #      real emltriton element for CPU inference. A zip that cannot be
    #      unpacked is NON-FATAL: the node keeps its stub and joins the
    #      simulated fallbacks (it is never realized into emltriton).
    #
    #    A third fallback — a staged emltriton element that fails to
    #    load/serve its model at PLAYING — is handled after the pipeline
    #    run below (model-load fallback). ``fallback_reason_by_node`` maps
    #    nodeId -> the reason a model inference node ran simulated.
    fallback_reason_by_node: Dict[str, str] = parse_staging_fallbacks(
        os.environ.get("STAGING_FALLBACKS"))
    staged_by_node: Dict[str, str] = {}
    #: Deep copy taken immediately before realize_inference_elements so a
    #: model-load fallback can restore the reverted nodes' original stubs.
    pre_realize_document: Optional[Dict] = None
    staged_entries = model_staging.parse_staged_models(
        os.environ.get("STAGED_MODELS"))
    if staged_entries:
        logger.info("Staging %d Triton model artifact(s) into %s ...",
                    len({e["modelName"] for e in staged_entries}),
                    model_staging.model_repo_dir())
        staged_by_node, unpack_fallbacks = \
            model_staging.download_and_stage_best_effort(
                s3, bucket, staged_entries,
                model_staging.model_repo_dir(),
                os.path.join(workdir, "models"))
        for fallback in unpack_fallbacks:
            # A model that could not be unpacked is non-fatal (12.16,
            # 12.17): the node keeps its sim_inference stub and runs
            # simulated with the staging error as its fallbackReason.
            fallback_reason_by_node[fallback["nodeId"]] = fallback["reason"]
        # A node cannot be both staged and a fallback (disjoint by
        # construction); drop any stale fallback for a staged node.
        for node_id in staged_by_node:
            fallback_reason_by_node.pop(node_id, None)
        if staged_by_node:
            pre_realize_document = copy.deepcopy(document)
            realized = model_staging.realize_inference_elements(
                document, staged_by_node)
            logger.info("Staged %d model(s); running real CPU inference for "
                        "node(s) %s",
                        len(set(staged_by_node.values())), realized)
        if unpack_fallbacks:
            logger.info("%d model(s) could not be staged; node(s) %s will "
                        "run with the injected simulated outcome",
                        len(unpack_fallbacks),
                        [f["nodeId"] for f in unpack_fallbacks])

    # 1. Dataset: download + stage, then resolve {dataset_location} so
    #    dataset-fed simulation sources read the Test_Dataset (12.5).
    files = download_dataset(s3, bucket, dataset_prefix,
                             os.path.join(workdir, "download"))
    logger.info("Downloaded %d dataset object(s)", len(files))
    dataset_location = dataset_module.stage_dataset(
        files, os.path.join(workdir, "dataset"))
    substitutions = renderer.resolve_placeholder(
        document, "dataset_location", dataset_location)
    logger.info("Resolved {dataset_location} -> %s (%d substitution(s))",
                dataset_location, substitutions)

    # Hardware sinks the CPU-only cloud sandbox cannot initialize
    # (emlcapture "Capture to File System" needs device libs and a
    # writable device path) are rewritten to a benign fakesink so the
    # simulation pipeline reaches PLAYING instead of aborting with
    # multifilesrc "not-linked" when the sink's pad stays unlinked. The
    # capture output is not consumed in a test run (Requirement 12.6);
    # record the substitution as stub activity so the report identifies
    # the node as simulated. Done before the name map is built so
    # attribution reflects the rewritten (fakesink) element name.
    stubbed_sinks = renderer.stub_hardware_sinks(document)

    gst_nodes = renderer.gst_node_ids(document)
    name_map = renderer.element_name_map(document)
    sim_sources = renderer.sim_appsrc_names(document)

    if stubbed_sinks:
        frame_count = len(dataset_module.plan_staging(list(files.keys())))
        for node_id in stubbed_sinks:
            store.add_stub_activity(node_id, {
                "type": "capture_sink_stub",
                "element": "sim_capture_" + node_id,
                "frameCount": frame_count,
                "note": "Simulated: the capture-to-filesystem sink is not "
                        "executed in the cloud sandbox; frames are routed "
                        "to a benign sink instead of device storage",
            }, flush=False)
        logger.info("Stubbed hardware sink(s) for simulation: %s",
                    stubbed_sinks)
        store.flush()

    # Record the dataset feed on every dataset-fed source node (12.6).
    if substitutions:
        frame_count = len(dataset_module.plan_staging(list(files.keys())))
        for segment in document.get("segments", []):
            for element in segment["elements"]:
                if element["factory"] == "multifilesrc" and element.get("nodeId"):
                    store.add_stub_activity(element["nodeId"], {
                        "type": "dataset_source",
                        "datasetLocation": dataset_location,
                        "frameCount": frame_count,
                        "note": "Simulated: source fed from the selected "
                                "Test_Dataset instead of camera hardware",
                    }, flush=False)
        store.flush()

    # Record the pass-through stub substitution on every stubbed
    # Custom_Node_Type node (custom-node-designer 12.2): the identity
    # element custom_stub_<nodeId> passes frames through unchanged while
    # this entry identifies the node as stubbed in the test run report.
    # Recorded before execution so a later failure retains it (12.10).
    custom_stub_nodes = renderer.custom_stub_node_ids(document)
    if custom_stub_nodes:
        frame_count = len(dataset_module.plan_staging(list(files.keys())))
        for node_id in custom_stub_nodes:
            store.add_stub_activity(node_id, {
                "type": "custom_node_stub",
                "element": "custom_stub_" + node_id,
                "frameCount": frame_count,
                "note": CUSTOM_NODE_STUB_NOTE,
            }, flush=False)
        logger.info("Custom node(s) %s stubbed with pass-through recorders",
                    custom_stub_nodes)
        store.flush()

    # 2. Render exactly as LocalServer does and execute (12.5).
    store.set_statuses(gst_nodes, STATUS_RUNNING)

    def run_once() -> Tuple[Dict[str, Any], Optional[Dict]]:
        """Render the (possibly reverted) document and run it once. A
        ``no element "X"`` parse failure is mapped to its owning node and
        surfaced as a return of exit code 1 via the ``_ParseFailure``
        sentinel so both the initial run and the fallback re-run share
        one handler."""
        launch_string = renderer.render_launch_string(document)
        logger.info("Launch string: %s", launch_string)
        try:
            return run_gst_pipeline(launch_string, sim_sources, store)
        except Exception as parse_error:  # noqa: BLE001
            factory = missing_element_factory(str(parse_error))
            if factory is None:
                raise
            failing_node = record_missing_element(document, store, factory)
            logger.error("Element factory %r unavailable in the sandbox "
                         "(node %s)", factory, failing_node)
            raise _ParseFailure()

    try:
        tag_values, error = run_once()
    except _ParseFailure:
        return 1

    # Model-load fallback (Requirements 12.16, 12.17): a first-run failure
    # with staged emltriton nodes present means a staged model could not
    # be loaded/served on CPU Triton. This is NON-FATAL — revert ALL
    # staged emltriton nodes to their sim_inference stubs, re-render, and
    # re-run the pipeline ONCE. Reverting every staged node (rather than
    # just the one that failed first) keeps the fallback bounded to a
    # single re-run even when several staged models are unusable, and is
    # robust to a second staged node failing on the retry; the reverted
    # nodes then run with the injected simulated outcome.
    #
    # The fallback fires when the failure is attributable to a staged node
    # (``failing_node in staged_by_node``) OR when it is not attributable
    # to any specific node (``failing_node is None``). The latter covers
    # the observed live failure where emltriton returns
    # STATE_CHANGE_FAILURE WITHOUT posting a bus ERROR: run_gst_pipeline
    # then returns ``{"element": None, ...}`` and nothing maps back to the
    # staged node, yet the undeployable/unloadable model must still fall
    # back to the injected simulated outcome and succeed. Only a failure
    # attributed to a DIFFERENT, non-staged node (a real node not in
    # staged_by_node — e.g. a source/sink element) is a genuine failure;
    # that skips the fallback and is reported via the 12.14/12.15 path
    # below. A model-load failure therefore never fails the run.
    if error and staged_by_node and pre_realize_document is not None:
        failing_node = renderer.node_id_for_element(
            name_map, error.get("element") or "")
        if failing_node in staged_by_node or failing_node is None:
            if error.get("element"):
                captured = "{0} (element {1})".format(
                    error["message"], error["element"])
            else:
                # Unattributable first-run failure (no bus error posted /
                # generic PLAYING message): give a clear reason rather than
                # surfacing the opaque state-change text.
                captured = ("A staged model could not be loaded on CPU "
                            "Triton (the pipeline did not reach the PLAYING "
                            "state); the configured simulated outcome was "
                            "used instead.")
            reverted = model_staging.revert_inference_elements(
                document, pre_realize_document, list(staged_by_node.keys()))
            for node_id in reverted:
                fallback_reason_by_node[node_id] = captured
            logger.warning(
                "Staged model load failed (node %s); reverting node(s) %s "
                "to simulated inference and re-running once: %s",
                failing_node, reverted, captured)
            # No staged emltriton nodes remain, and the element->node map
            # changed (emltriton -> identity), so recompute it for any
            # attribution on the re-run.
            staged_by_node = {}
            name_map = renderer.element_name_map(document)
            try:
                tag_values, error = run_once()
            except _ParseFailure:
                return 1

    if error:
        failing_node = renderer.node_id_for_element(
            name_map, error.get("element") or "")
        message = error["message"]
        if error.get("element"):
            message = "{0} (element {1})".format(message, error["element"])
        if failing_node is not None:
            # The bus error named an element that maps to a workflow node
            # (e.g. an emltriton element -> the model inference node):
            # attribute the failure to that node with the backend detail
            # in the message (12.15, 12.16).
            store.set_error(failing_node, message,
                            code="PIPELINE_EXECUTION_ERROR", flush=False)
        else:
            # No owning node could be determined — a synthetic linking
            # element (tee/queue), an unnamed element, or no bus error at
            # all. Record a run-level (unattributed) error instead of
            # defaulting onto the first/source node, which previously
            # mislabeled downstream inference/sink failures as a
            # Folder/Camera source failure (12.15).
            store.add_run_error(message, code="PIPELINE_EXECUTION_ERROR",
                                flush=False)
        store.skip_remaining()
        logger.error("Pipeline failed at node %s: %s",
                     failing_node or "<run-level>", message)
        return 1

    # 3. Simulated inference injection: model inference nodes are stubbed
    #    in simulation (no emltriton in the sandbox, device-compiled
    #    models), so the user-configured outcome from SIMULATED_INFERENCE
    #    becomes the inference metadata driving downstream executor
    #    bindings — filters, conditionals, and output recorders — and is
    #    recorded as stub activity on each stubbed node (12.6).
    #    Per-node inference-mode reporting (12.18): every stubbed node
    #    (never-staged, staging-unpack fallback, and model-load fallback)
    #    records inferenceMode "simulated"; a fallbackReason is set when
    #    the simulated outcome replaced a model that could not be staged
    #    or loaded (fallback_reason_by_node), and left None for a node
    #    that was only ever simulated (no model staged).
    sim_inference_nodes = renderer.sim_inference_node_ids(document)
    if sim_inference_nodes:
        simulated = simulated_inference_from_env()
        tag_values = dict(tag_values)
        tag_values["is_anomalous"] = simulated["is_anomalous"]
        tag_values["confidence"] = simulated["confidence"]
        logger.info("Injecting simulated inference outcome %s for node(s) %s",
                    simulated, sim_inference_nodes)
        for node_id in sim_inference_nodes:
            fallback_reason = fallback_reason_by_node.get(node_id)
            store.add_stub_activity(node_id, {
                "type": "simulated_inference",
                "isAnomalous": simulated["is_anomalous"],
                "confidence": simulated["confidence"],
                "inferenceMode": "simulated",
                "fallbackReason": fallback_reason,
                "note": (SIMULATED_INFERENCE_FALLBACK_NOTE if fallback_reason
                         else SIMULATED_INFERENCE_NOTE),
            }, flush=False)
            store.set_inference_mode(node_id, "simulated",
                                     fallback_reason=fallback_reason,
                                     flush=False)

    # 4. Success: pipeline nodes completed; attach inference metadata
    #    outputs (S3 refs) to the emltriton nodes (12.7; in simulation
    #    there are none — the stubbed nodes carry stubActivity instead).
    for node_id in renderer.nodes_with_factory(document, "emltriton"):
        payload = {"type": "inference_metadata", "tags": tag_values}
        key = upload_node_output(s3, bucket, results_key, node_id, payload)
        store.add_output(node_id, {
            "type": "inference_metadata",
            "s3Key": key,
            "tags": tag_values,
        }, flush=False)
        # A surviving emltriton element executed the staged CPU model
        # (a model-load fallback would have reverted it to a stub) —
        # record the node as a real inference run (12.18).
        store.set_inference_mode(node_id, "real", flush=False)
    store.set_statuses(gst_nodes, STATUS_COMPLETED)

    # 5. Executor bindings as recording stubs, flushed per node (12.6).
    bindings_module.execute_bindings(
        document.get("executorBindings", []), tag_values, store)

    store.flush()
    if store.has_failure():
        return 1
    logger.info("Test run pipeline completed")
    return 0


if __name__ == "__main__":
    # exit_now, not sys.exit: results are already flushed, and leftover
    # emltriton/Triton threads must not hold the task open until the
    # 10-minute timeout displaces the flushed per-node error.
    exit_now(main())
