"""Containerized end-to-end sandbox integration tests (task 11.8).

Runs the real harness (``python3 -m harness``) inside the sandbox
container image against moto-served S3:

- A sample workflow compiled by the real ``workflow_core`` compiler
  (``simulation=True``, x86_64) executes against a small Test_Dataset
  through the real ``Gst.parse_launch`` path — Requirement 12.5.
- A never-ending pipeline with ``PIPELINE_TIMEOUT_SEC=2`` exits 1 with
  the timeout failure flushed and partial results retained —
  Requirement 12.13.
- The harness's AWS usage during a real run is S3-only (no Greengrass
  interaction in the test path) — Requirement 12.9.

Documented limitation: emltriton/CPU-Triton inference is not end-to-end
coverable here — the proprietary DDA plugin ``.so`` set and Triton model
artifacts are not available in this repository, and the slim CI image
omits the multi-gigabyte Triton stage. The sample workflow therefore
uses stock GStreamer elements (multifilesrc ! jpegparse ! jpegdec !
videoconvert ! jpegenc ! sink), which exercises the identical execution
semantics of 12.5: dataset download/staging, ``{dataset_location}``
resolution, launch-string rendering, bus-watched pipeline execution, and
incremental per-node results flushing.
"""

import json

import pytest

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Sample workflow: real compiler output with one documented substitution
# ---------------------------------------------------------------------------

def compile_sample_workflow_document():
    """Compile camera_source -> capture with the real Workflow_Compiler
    (``simulation=True``, x86_64) and return the document dict.

    The capture node's ``emlcapture`` element (proprietary DDA plugin,
    unavailable in the image — see module docstring) is substituted with
    a stock ``fakesink`` carrying the same ``nodeId``, so per-node result
    tracking for the capture node is preserved while the pipeline uses
    only stock elements.
    """
    from workflow_core.compiler import CompileContext
    from workflow_core.compiler import compile as workflow_compile
    from workflow_core.serializer.models import (
        Connection, Node, PortEndpoint, Position, WorkflowGraph)

    graph = WorkflowGraph(
        nodes=[
            Node(id="cam", type="camera_source", position=Position(0, 0),
                 parameters={}),
            Node(id="cap", type="capture", position=Position(200, 0),
                 parameters={"output_path": "/tmp/captures"}),
        ],
        connections=[
            Connection(id="c1", source=PortEndpoint("cam", "out"),
                       target=PortEndpoint("cap", "in")),
        ],
    )
    compiled = workflow_compile(
        graph, "x86_64",
        CompileContext(workflow_id="wf-integration", workflow_version="1"),
        simulation=True)
    assert not isinstance(compiled, list), \
        "sample workflow failed to compile: {0}".format(compiled)
    document = compiled.to_dict()

    substituted = 0
    for segment in document["segments"]:
        for element in segment["elements"]:
            if element["factory"] == "emlcapture":
                element["factory"] = "fakesink"
                element["args"] = {"sync": False}
                substituted += 1
    assert substituted == 1
    return document


def make_timeout_document():
    """A compiled document whose pipeline never reaches EOS: a live
    videotestsrc with no num-buffers limit feeding a sink."""
    return {
        "schemaVersion": 1,
        "workflowId": "wf-timeout",
        "workflowVersion": "1",
        "targetArch": "x86_64",
        "segments": [{
            "name": "s0",
            "from": None,
            "linkTo": None,
            "elements": [
                {"nodeId": "src", "factory": "videotestsrc",
                 "args": {"is-live": True}},
                {"nodeId": "sink", "factory": "fakesink",
                 "args": {"sync": True}},
            ],
        }],
        "executorBindings": [],
        "pluginDependencies": [],
    }


def records_by_node(results):
    return {record["nodeId"]: record for record in results["nodes"]}


# ---------------------------------------------------------------------------
# Requirement 12.5: end-to-end sample workflow against a small Test_Dataset
# ---------------------------------------------------------------------------

def test_e2e_sample_workflow_runs_against_test_dataset(sandbox_run):
    """The containerized harness executes a compiled sample workflow
    against a small Test_Dataset and reports per-node completion
    (Requirement 12.5)."""
    sandbox_run.upload_document(compile_sample_workflow_document())
    sandbox_run.upload_dataset_jpegs(count=3)

    exit_code, logs = sandbox_run.run_harness()
    assert exit_code == 0, "harness exited {0}; logs:\n{1}".format(
        exit_code, logs)

    results = sandbox_run.fetch_results()
    records = records_by_node(results)
    assert set(records) == {"cam", "cap"}
    assert records["cam"]["status"] == "completed"
    assert records["cap"]["status"] == "completed"
    assert records["cam"]["error"] is None
    assert records["cap"]["error"] is None

    # The dataset-fed source node records the Test_Dataset substitution
    # as stub activity: the {dataset_location} placeholder was resolved
    # and all three staged frames were fed (12.5 execution semantics).
    activities = records["cam"]["stubActivity"]
    dataset_feeds = [entry for entry in activities
                     if entry.get("type") == "dataset_source"]
    assert len(dataset_feeds) == 1
    assert dataset_feeds[0]["frameCount"] == 3
    assert "frame_%05d.jpg" in dataset_feeds[0]["datasetLocation"]

    # The rendered launch string resolved the placeholder (visible in the
    # harness log) — no unresolved {dataset_location} reached GStreamer.
    assert "{dataset_location}" not in logs.split("Launch string:")[-1]


# ---------------------------------------------------------------------------
# Requirement 12.13: shortened timeout terminates the run as failed
# ---------------------------------------------------------------------------

def test_timeout_with_shortened_limit_fails_run_and_retains_partial_results(
        sandbox_run):
    """With PIPELINE_TIMEOUT_SEC=2 a never-ending pipeline is terminated:
    the harness exits 1, the timeout failure is flushed with the failing
    node identified, and partial per-node results are retained
    (Requirement 12.13)."""
    sandbox_run.upload_document(make_timeout_document())
    sandbox_run.upload_dataset_jpegs(count=1)

    exit_code, logs = sandbox_run.run_harness(
        extra_env={"PIPELINE_TIMEOUT_SEC": "2"}, timeout=120)
    assert exit_code == 1, "harness exited {0}; logs:\n{1}".format(
        exit_code, logs)

    results = sandbox_run.fetch_results()
    records = records_by_node(results)
    # The timeout names no pipeline element, so it is reported as a
    # run-level (unattributed) error rather than being blamed on the
    # source node (Requirements 12.13, 12.15).
    assert set(records) == {"src", "sink", None}

    # Timeout recorded as a run-level failure with a timeout indication
    # naming the shortened limit.
    run_error = records[None]
    assert run_error["status"] == "failed"
    assert run_error["error"] is not None
    assert "timed out after 2s" in run_error["error"]["message"]

    # Partial results produced before termination are retained: both
    # pipeline nodes are present and marked skipped, not lost or
    # mislabeled as the failing node.
    assert records["src"]["status"] == "skipped"
    assert records["src"]["error"] is None
    assert records["sink"]["status"] == "skipped"
    assert records["sink"]["error"] is None


# ---------------------------------------------------------------------------
# Requirement 12.9: no Greengrass interaction in the test path (behavioral)
# ---------------------------------------------------------------------------

#: Wrapper entrypoint: records every boto3 service the harness creates a
#: client for during a real run, then reports them on a marker line.
_SERVICE_RECORDER = """\
import boto3, json, sys
recorded = []
original_client = boto3.client
def recording_client(service_name, *args, **kwargs):
    recorded.append(str(service_name))
    return original_client(service_name, *args, **kwargs)
boto3.client = recording_client
from harness.harness import main
code = main()
print("BOTO3_SERVICES=" + json.dumps(sorted(set(recorded))))
sys.exit(code)
"""


def test_harness_run_uses_only_s3_no_greengrass(sandbox_run):
    """During a full containerized run the harness talks to S3 and to no
    other AWS service — no Greengrass client, no deployment, no artifact
    delivered to any device (Requirement 12.9)."""
    sandbox_run.upload_document(compile_sample_workflow_document())
    sandbox_run.upload_dataset_jpegs(count=2)

    exit_code, logs = sandbox_run.run_harness(
        command=["python3", "-c", _SERVICE_RECORDER])
    assert exit_code == 0, "harness exited {0}; logs:\n{1}".format(
        exit_code, logs)

    marker_lines = [line for line in logs.splitlines()
                    if line.startswith("BOTO3_SERVICES=")]
    assert marker_lines, "service recorder marker missing; logs:\n" + logs
    services = json.loads(marker_lines[-1][len("BOTO3_SERVICES="):])
    assert services == ["s3"], \
        "harness contacted non-S3 AWS services: {0}".format(services)
