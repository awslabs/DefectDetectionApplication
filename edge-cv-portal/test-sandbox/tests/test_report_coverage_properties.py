"""Property test for test report coverage (task 11.4).

**Feature: workflow-manager, Property 15: Test report covers every node**

For all valid Workflow_Definitions executed by the test harness (with the
pipeline layer mocked), the per-node results report contains exactly one
entry per node in the definition, each keyed by its node identifier.

**Validates: Requirements 12.7**

The subject is the harness's results assembly: ``renderer.all_node_ids``
over the Compiled Pipeline Document seeds the :class:`ResultsStore`
(exactly as ``harness.main`` does), and the store's document must keep
covering every node through both lifecycle paths the harness drives —
the success flow and a mid-run failure (``set_error`` + ``skip_remaining``),
where every node still appears with an error/skipped/completed status.

Graphs are generated with the real workflow_core compiler
(``simulation=True``, x86_64 — the exact configuration the test-run state
machine uses) from the shared ``graph_strategy`` generators.
"""

import json
import os
import sys

# -- sys.path: harness package + workflow_core layer + its shared generators.
# The layer's python/ dir is appended rather than prepended (mirroring the
# workflow_core tests conftest): it also carries the layer's vendored
# Lambda-runtime dependencies (CPython 3.11 manylinux wheels, e.g.
# jsonschema's rpds), which must not shadow the host interpreter's own
# packages when the tests run locally.
_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_TEST_SANDBOX_DIR = os.path.dirname(_TESTS_DIR)
_PORTAL_DIR = os.path.dirname(_TEST_SANDBOX_DIR)
_WORKFLOW_CORE_DIR = os.path.join(_PORTAL_DIR, "backend", "layers", "workflow_core")
if _TEST_SANDBOX_DIR not in sys.path:
    sys.path.insert(0, _TEST_SANDBOX_DIR)
for _path in (
    os.path.join(_WORKFLOW_CORE_DIR, "python"),
    os.path.join(_WORKFLOW_CORE_DIR, "tests"),
):
    if _path not in sys.path:
        sys.path.append(_path)

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from workflow_core.catalog import ARCH_X86_64
from workflow_core.compiler import CompiledPipelineDocument, compile

from generators import graph_strategy  # workflow_core shared generators

from harness import renderer
from harness.results import (
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_RUNNING,
    STATUS_SKIPPED,
    ResultsStore,
)

#: Every record in the report carries the full per-node shape (12.7).
_RECORD_KEYS = {"nodeId", "status", "outputs", "stubActivity", "error"}


class FlushRecorder:
    """Captures every flushed report snapshot (the S3 put stand-in)."""

    def __init__(self):
        self.snapshots = []

    def __call__(self, document):
        self.snapshots.append(document)


def _compile_for_sandbox(graph):
    """Compile exactly as the test-run state machine does (x86_64,
    simulation=true) and hand back the JSON round-tripped dict the
    harness downloads from S3."""
    result = compile(graph, ARCH_X86_64, simulation=True)
    assert isinstance(result, CompiledPipelineDocument), (
        "compilation of a valid graph failed: {0}".format(result)
    )
    return json.loads(json.dumps(result.to_dict()))


def _assert_covers_every_node(report, expected_node_ids):
    """The report contains exactly one entry per workflow node, keyed by
    its node identifier, each with the full record shape."""
    record_ids = [record["nodeId"] for record in report["nodes"]]
    assert len(record_ids) == len(set(record_ids)), (
        "duplicate per-node records: {0}".format(record_ids)
    )
    assert set(record_ids) == expected_node_ids, (
        "report node set mismatch: missing {0}, extra {1}".format(
            sorted(expected_node_ids - set(record_ids)),
            sorted(set(record_ids) - expected_node_ids),
        )
    )
    for record in report["nodes"]:
        assert set(record) == _RECORD_KEYS


def _drive_success_flow(document, flush):
    """The harness's success lifecycle: initial flush, pipeline nodes
    running then completed (with outputs/stub activity), executor
    bindings recorded and completed."""
    store = ResultsStore(renderer.all_node_ids(document), flush)
    store.flush()

    gst_nodes = renderer.gst_node_ids(document)
    if gst_nodes:
        store.set_statuses(gst_nodes, STATUS_RUNNING)
    for _, node_id in renderer.sim_appsrc_names(document):
        store.add_stub_activity(node_id, {"type": "sim_event_source"})
    for node_id in renderer.nodes_with_factory(document, "emltriton"):
        store.add_output(node_id, {"type": "inference_metadata", "s3Key": "k"},
                         flush=False)
    if gst_nodes:
        store.set_statuses(gst_nodes, STATUS_COMPLETED)

    for node_id in renderer.executor_node_ids(document):
        store.add_stub_activity(node_id, {"type": "recorded_actuation"},
                                flush=False)
        store.set_status(node_id, STATUS_COMPLETED)

    store.flush()
    return store


def _drive_failure_flow(document, flush, failing_node_id):
    """The harness's mid-run failure lifecycle: initial flush, pipeline
    nodes running, one node fails, the rest are skipped (12.10)."""
    store = ResultsStore(renderer.all_node_ids(document), flush)
    store.flush()

    gst_nodes = renderer.gst_node_ids(document)
    if gst_nodes:
        store.set_statuses(gst_nodes, STATUS_RUNNING)
    store.set_error(failing_node_id, "Pipeline failed with: injected",
                    code="PIPELINE_EXECUTION_ERROR", flush=False)
    store.skip_remaining()
    return store


@given(graph=graph_strategy(), failure_choice=st.integers(min_value=0))
@settings(max_examples=100, deadline=None,
          suppress_health_check=[HealthCheck.too_slow])
def test_report_covers_every_node(graph, failure_choice):
    """**Feature: workflow-manager, Property 15: Test report covers every node**

    **Validates: Requirements 12.7**
    """
    document = _compile_for_sandbox(graph)
    expected_node_ids = {node.id for node in graph.nodes}

    # The store is seeded from the compiled document exactly as the
    # harness seeds it — that seeding must already cover the whole graph.
    node_ids = renderer.all_node_ids(document)
    assert len(node_ids) == len(set(node_ids))
    assert set(node_ids) == expected_node_ids

    # -- success path -------------------------------------------------------
    success_flush = FlushRecorder()
    success_store = _drive_success_flow(document, success_flush)
    assert success_flush.snapshots, "harness must flush before anything runs"
    for snapshot in success_flush.snapshots:
        _assert_covers_every_node(snapshot, expected_node_ids)
    final = success_flush.snapshots[-1]
    assert all(record["status"] == STATUS_COMPLETED for record in final["nodes"])
    assert not success_store.has_failure()

    # -- mid-run failure path ------------------------------------------------
    failing_node_id = node_ids[failure_choice % len(node_ids)]
    failure_flush = FlushRecorder()
    failure_store = _drive_failure_flow(document, failure_flush, failing_node_id)
    for snapshot in failure_flush.snapshots:
        _assert_covers_every_node(snapshot, expected_node_ids)
    final = failure_flush.snapshots[-1]
    statuses = {record["nodeId"]: record["status"] for record in final["nodes"]}
    assert statuses[failing_node_id] == STATUS_FAILED
    for node_id, status in statuses.items():
        assert status in (STATUS_COMPLETED, STATUS_FAILED, STATUS_SKIPPED), (
            "node '{0}' left in transient status '{1}' after failure".format(
                node_id, status)
        )
        assert status != STATUS_RUNNING
    assert failure_store.has_failure()
