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

import json
import logging
import os
import re
import sys
import tempfile
from typing import Any, Dict, List, Optional, Tuple

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

    try:
        ret = pipeline.set_state(Gst.State.PLAYING)
        if ret == Gst.StateChangeReturn.FAILURE:
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
        pipeline.set_state(Gst.State.NULL)

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

    # 0. Triton model staging: the STAGED_MODELS manifest lists the
    #    model artifacts the portal copied under the run's prefix for
    #    every model_inference node. Each zip is unpacked into the
    #    (initially empty) Triton model repository, then the nodes'
    #    simulation stubs are rewritten into real emltriton elements so
    #    the pipeline performs actual CPU inference — the model name
    #    emltriton requests matches the staged repository entry exactly
    #    (see model_staging). Runs without a manifest keep the
    #    simulated-inference stubs (pre-staging behavior).
    staged_entries = model_staging.parse_staged_models(
        os.environ.get("STAGED_MODELS"))
    if staged_entries:
        logger.info("Staging %d Triton model artifact(s) into %s ...",
                    len({e["modelName"] for e in staged_entries}),
                    model_staging.model_repo_dir())
        try:
            staged_by_node = model_staging.download_and_stage(
                s3, bucket, staged_entries,
                model_staging.model_repo_dir(),
                os.path.join(workdir, "models"))
        except model_staging.ModelStagingError as error:
            # A missing/corrupt model artifact fails the run with the
            # owning model_inference node identified (12.10).
            if error.node_id and error.node_id in store.node_ids:
                store.set_error(error.node_id, str(error),
                                code=model_staging.MODEL_STAGING_FAILED,
                                flush=False)
            else:
                store.add_run_error(str(error),
                                    code=model_staging.MODEL_STAGING_FAILED,
                                    flush=False)
            store.skip_remaining()
            logger.error("Model staging failed: %s", error)
            return 1
        realized = model_staging.realize_inference_elements(
            document, staged_by_node)
        logger.info("Staged %d model(s); running real CPU inference for "
                    "node(s) %s", len(set(staged_by_node.values())), realized)

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

    gst_nodes = renderer.gst_node_ids(document)
    name_map = renderer.element_name_map(document)
    sim_sources = renderer.sim_appsrc_names(document)

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

    # 2. Render exactly as LocalServer does and execute (12.5).
    launch_string = renderer.render_launch_string(document)
    logger.info("Launch string: %s", launch_string)
    store.set_statuses(gst_nodes, STATUS_RUNNING)

    try:
        tag_values, error = run_gst_pipeline(launch_string, sim_sources, store)
    except Exception as parse_error:
        # Gst.parse_launch raises GLib.Error('no element "X"') when a
        # required plugin is not installed - expected in the sandbox for
        # proprietary DDA plugins. Map the factory back to its owning
        # node so the failure is legible per-node; anything else
        # propagates to the generic harness-failure handler.
        factory = missing_element_factory(str(parse_error))
        if factory is None:
            raise
        failing_node = record_missing_element(document, store, factory)
        logger.error("Element factory %r unavailable in the sandbox "
                     "(node %s)", factory, failing_node)
        return 1

    if error:
        failing_node = renderer.node_id_for_element(
            name_map, error.get("element") or "")
        if failing_node is None and gst_nodes:
            # Synthetic/unknown element: attribute to the first pipeline
            # node so the failure still identifies a node (12.10).
            failing_node = gst_nodes[0]
        message = error["message"]
        if error.get("element"):
            message = "{0} (element {1})".format(message, error["element"])
        if failing_node is not None:
            store.set_error(failing_node, message,
                            code="PIPELINE_EXECUTION_ERROR", flush=False)
        store.skip_remaining()
        logger.error("Pipeline failed at node %s: %s", failing_node, message)
        return 1

    # 3. Simulated inference injection: model inference nodes are stubbed
    #    in simulation (no emltriton in the sandbox, device-compiled
    #    models), so the user-configured outcome from SIMULATED_INFERENCE
    #    becomes the inference metadata driving downstream executor
    #    bindings — filters, conditionals, and output recorders — and is
    #    recorded as stub activity on each stubbed node (12.6).
    sim_inference_nodes = renderer.sim_inference_node_ids(document)
    if sim_inference_nodes:
        simulated = simulated_inference_from_env()
        tag_values = dict(tag_values)
        tag_values["is_anomalous"] = simulated["is_anomalous"]
        tag_values["confidence"] = simulated["confidence"]
        logger.info("Injecting simulated inference outcome %s for node(s) %s",
                    simulated, sim_inference_nodes)
        for node_id in sim_inference_nodes:
            store.add_stub_activity(node_id, {
                "type": "simulated_inference",
                "isAnomalous": simulated["is_anomalous"],
                "confidence": simulated["confidence"],
                "note": SIMULATED_INFERENCE_NOTE,
            }, flush=False)

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
    sys.exit(main())
