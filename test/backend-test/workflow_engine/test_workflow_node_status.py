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
"""Unit tests for per-node run-status collection (Task 4).

Covers the ``NodeStatusCollector`` in isolation — element->node mapping, a
fully-terminal map on completion and on failure, failing-node attribution,
and warning capture (Requirements 3.1, 3.2, 3.4, 3.6) — plus the executor
integration that threads the collector's sink through ``run_pipeline`` and
persists the terminal ``node_status_json`` on both the success and failure
paths, while the Pipeline_Configuration caller (no sink) is unchanged (R8.1).
"""
import json
import os
import time
from unittest.mock import patch

import pytest

from workflow_engine_test_utils import (
    DEVICE_ARCH,
    make_session_factory,
    write_artifact_set,
)

from workflow_engine import gst_plugins
from workflow_engine import pipeline_executor
from workflow_engine.models import WorkflowExecution, WorkflowRegistration
from workflow_engine.node_status import (
    NodeStatusCollector,
    STATUS_FAILURE,
    STATUS_PENDING,
    STATUS_RUNNING,
    STATUS_SUCCESS,
    STATUS_WARNING,
    TERMINAL_STATES,
)
from workflow_engine.pipeline_executor import (
    EXECUTION_STATUS_COMPLETED,
    EXECUTION_STATUS_FAILED,
    EXECUTION_STATUS_PENDING,
    WorkflowExecutor,
)

# A three-element segment: the trailing fakesink is a synthetic (nodeId=None)
# element, so only n1/n2 participate. The launch string auto-names elements
# videotestsrc0 / emltriton0 / fakesink0.
COMPILED_DOC = {
    "schemaVersion": 1,
    "workflowId": "wf-1",
    "workflowVersion": "3",
    "targetArch": DEVICE_ARCH,
    "segments": [
        {
            "name": "s0",
            "elements": [
                {"nodeId": "n1", "factory": "videotestsrc",
                 "args": {"num-buffers": 1}},
                {"nodeId": "n2", "factory": "emltriton",
                 "args": {"model": "widget-anomaly-v3"}},
                {"nodeId": None, "factory": "fakesink", "args": {}},
            ],
        }
    ],
    "executorBindings": [],
    "pluginDependencies": [],
}

# element-name -> nodeId, as rendering.element_name_map would produce it.
NAME_MAP = {"videotestsrc0": "n1", "emltriton0": "n2", "fakesink0": None}


# --- NodeStatusCollector unit tests -----------------------------------------


class TestElementToNodeMapping:
    def test_participating_nodes_are_distinct_non_none_ids(self):
        collector = NodeStatusCollector(
            {"a0": "n1", "b0": "n2", "c0": "n1", "queue0": None}
        )
        # Duplicates collapse; None (synthetic) elements do not participate.
        assert collector.participating_nodes() == {"n1", "n2"}

    def test_every_participating_node_starts_pending(self):
        collector = NodeStatusCollector(NAME_MAP)
        assert collector.status_of("n1") == STATUS_PENDING
        assert collector.status_of("n2") == STATUS_PENDING

    def test_sink_maps_element_to_node(self):
        collector = NodeStatusCollector(NAME_MAP)
        collector.sink("emltriton0", "running", None)
        assert collector.status_of("n2") == STATUS_RUNNING
        # n1 was not signalled and stays pending.
        assert collector.status_of("n1") == STATUS_PENDING

    def test_sink_ignores_unknown_and_synthetic_elements(self):
        collector = NodeStatusCollector(NAME_MAP)
        collector.sink("nonexistent7", "running", None)
        collector.sink("fakesink0", "warning", "noise")  # synthetic -> None
        assert collector.to_map() == {
            "n1": {"status": STATUS_PENDING},
            "n2": {"status": STATUS_PENDING},
        }


class TestWarningCapture:
    def test_warning_sets_status_and_retains_detail(self):
        collector = NodeStatusCollector(NAME_MAP)
        collector.sink("emltriton0", "warning", "clipping detected")
        entry = collector.to_map()["n2"]
        assert entry["status"] == STATUS_WARNING
        assert entry["detail"] == "clipping detected"

    def test_success_does_not_downgrade_a_warning(self):
        collector = NodeStatusCollector(NAME_MAP)
        collector.sink("emltriton0", "warning", "clipping detected")
        collector.mark_success_all()
        collector.finalize()
        result = collector.to_map()
        assert result["n2"]["status"] == STATUS_WARNING
        assert result["n2"]["detail"] == "clipping detected"
        # A node with no warning still resolves to success.
        assert result["n1"]["status"] == STATUS_SUCCESS

    def test_running_never_downgrades_a_warning(self):
        collector = NodeStatusCollector(NAME_MAP)
        collector.sink("emltriton0", "warning", "clipping")
        collector.sink("emltriton0", "running", None)
        assert collector.status_of("n2") == STATUS_WARNING


class TestTerminalMapOnCompletion:
    def test_completion_yields_fully_terminal_success_map(self):
        collector = NodeStatusCollector(NAME_MAP)
        collector.mark_running_all()
        collector.mark_success_all()
        collector.finalize()
        result = collector.to_map()
        assert set(result) == {"n1", "n2"}
        for entry in result.values():
            assert entry["status"] in TERMINAL_STATES
            assert entry["status"] not in (STATUS_PENDING, STATUS_RUNNING)
        assert result["n1"]["status"] == STATUS_SUCCESS
        assert result["n2"]["status"] == STATUS_SUCCESS

    def test_finalize_resolves_leftover_pending_and_running(self):
        collector = NodeStatusCollector(NAME_MAP)
        collector.sink("videotestsrc0", "running", None)  # n1 running
        # n2 left pending; no mark_success_all called.
        collector.finalize()
        result = collector.to_map()
        assert result["n1"]["status"] == STATUS_SUCCESS
        assert result["n2"]["status"] == STATUS_SUCCESS


class TestMarkPipelineSuccess:
    """Pipeline_EOS terminal marking (vllm-workflow-latency-optimization,
    Requirements 2.1, 2.2, 2.4, 2.6, 2.7)."""

    def test_running_pipeline_nodes_become_success(self):
        collector = NodeStatusCollector(NAME_MAP)
        collector.mark_running_all()
        collector.mark_pipeline_success()
        assert collector.status_of("n1") == STATUS_SUCCESS
        assert collector.status_of("n2") == STATUS_SUCCESS

    def test_pending_pipeline_nodes_are_untouched(self):
        collector = NodeStatusCollector(NAME_MAP)
        collector.sink("videotestsrc0", "running", None)  # n1 running
        collector.mark_pipeline_success()
        assert collector.status_of("n1") == STATUS_SUCCESS
        assert collector.status_of("n2") == STATUS_PENDING
        # A pending node gets no duration at EOS (R2.7).
        assert "durationMs" not in collector.to_map()["n2"]

    def test_warning_and_failure_are_retained_with_detail(self):
        collector = NodeStatusCollector(NAME_MAP)
        collector.mark_running_all()
        collector.sink("videotestsrc0", "warning", "flaky source")
        collector.mark_failure("n2", "emltriton failed")
        collector.mark_pipeline_success()
        entries = collector.to_map()
        assert entries["n1"]["status"] == STATUS_WARNING
        assert entries["n1"]["detail"] == "flaky source"
        assert entries["n2"]["status"] == STATUS_FAILURE
        assert entries["n2"]["detail"] == "emltriton failed"

    def test_binding_nodes_from_extra_node_ids_are_untouched(self):
        collector = NodeStatusCollector(NAME_MAP, extra_node_ids=["b1"])
        collector.mark_running_all()
        collector.mark_pipeline_success()
        # Pipeline nodes terminal, binding node still running (R2.4).
        assert collector.status_of("n1") == STATUS_SUCCESS
        assert collector.status_of("b1") == STATUS_RUNNING

    def test_freezes_duration_at_eos(self):
        collector = NodeStatusCollector(NAME_MAP)
        collector.mark_running_all()
        collector.mark_pipeline_success()
        frozen = collector.to_map()["n1"]["durationMs"]
        # Later terminal markings never overwrite the EOS duration (R2.2).
        collector.mark_success_all()
        collector.finalize()
        assert collector.to_map()["n1"]["durationMs"] == frozen

    def test_is_contained(self):
        # A broken internals state must not raise out of the marking (R2.6).
        collector = NodeStatusCollector(NAME_MAP)
        collector._statuses = None  # force an internal error
        collector.mark_pipeline_success()  # swallowed, no exception


class TestTerminalMapOnFailure:
    def test_failure_attributes_exactly_the_mapped_node(self):
        collector = NodeStatusCollector(NAME_MAP)
        collector.mark_running_all()
        collector.mark_failure("n2", "emltriton failed to load model")
        collector.finalize()
        result = collector.to_map()
        assert result["n2"]["status"] == STATUS_FAILURE
        assert result["n2"]["detail"] == "emltriton failed to load model"
        # The non-failing node resolves best-effort (success), never left
        # pending/running (R3.6).
        assert result["n1"]["status"] == STATUS_SUCCESS
        for entry in result.values():
            assert entry["status"] in TERMINAL_STATES

    def test_none_failing_node_marks_nothing_as_failure(self):
        collector = NodeStatusCollector(NAME_MAP)
        collector.mark_running_all()
        collector.mark_failure(None, "unattributable error")
        collector.finalize(failure_detail="unattributable error")
        result = collector.to_map()
        assert all(e["status"] != STATUS_FAILURE for e in result.values())

    def test_unattributed_failure_resolves_nodes_to_warning_not_success(self):
        """A failed run whose failing element cannot be mapped (e.g. a
        pre-parse gst_parse_error) must not finalize to an all-green map
        (R3.6/R6.6: coloring consistent with the run outcome)."""
        collector = NodeStatusCollector(NAME_MAP)
        collector.mark_running_all()
        collector.mark_failure(None, 'gst_parse_error: no element "resize_image" (1)')
        collector.finalize(
            failure_detail='gst_parse_error: no element "resize_image" (1)'
        )
        result = collector.to_map()
        for entry in result.values():
            assert entry["status"] == STATUS_WARNING
            assert 'no element "resize_image"' in entry["detail"]

    def test_attributed_failure_keeps_best_effort_success_for_others(self):
        """When the failing node IS identified, the pre-existing best-effort
        resolution of the other nodes to success is unchanged (R3.6)."""
        collector = NodeStatusCollector(NAME_MAP)
        collector.mark_running_all()
        collector.mark_failure("n2", "emltriton failed")
        collector.finalize(failure_detail="emltriton failed")
        result = collector.to_map()
        assert result["n2"]["status"] == STATUS_FAILURE
        assert result["n1"]["status"] == STATUS_SUCCESS

    def test_unattributed_failure_does_not_downgrade_terminal_states(self):
        collector = NodeStatusCollector(NAME_MAP)
        collector.sink("videotestsrc0", "warning", "flaky source")
        collector.finalize(failure_detail="unattributable error")
        result = collector.to_map()
        # The pre-existing warning and its own detail are retained.
        assert result["n1"]["status"] == STATUS_WARNING
        assert result["n1"]["detail"] == "flaky source"

    def test_to_json_round_trips_the_map(self):
        collector = NodeStatusCollector(NAME_MAP)
        collector.mark_failure("n2", "boom")
        collector.finalize()
        parsed = json.loads(collector.to_json())
        assert parsed["n2"] == {"status": STATUS_FAILURE, "detail": "boom"}
        assert parsed["n1"] == {"status": STATUS_SUCCESS}


class TestSetDetail:
    """set_detail records a sent-message/skipped detail without changing
    status (output-node-sent-message feature)."""

    def test_records_detail_for_tracked_node_without_changing_status(self):
        collector = NodeStatusCollector(NAME_MAP)
        before = collector.status_of("n2")
        collector.set_detail("n2", "sent to topic 't' (qos 0, plain): {}")
        assert collector.status_of("n2") == before  # status unchanged
        assert collector.to_map()["n2"]["detail"] == (
            "sent to topic 't' (qos 0, plain): {}"
        )

    def test_untracked_node_is_a_noop(self):
        collector = NodeStatusCollector(NAME_MAP)
        collector.set_detail("does-not-exist", "ignored")
        assert "does-not-exist" not in collector.to_map()

    def test_none_node_and_empty_detail_are_noops(self):
        collector = NodeStatusCollector(NAME_MAP)
        collector.set_detail(None, "ignored")
        collector.set_detail("n1", None)
        collector.set_detail("n1", "")
        assert "detail" not in collector.to_map()["n1"]

    def test_never_overwrites_a_failure_detail(self):
        collector = NodeStatusCollector(NAME_MAP)
        collector.mark_failure("n2", "publish failed: broker unreachable")
        collector.set_detail("n2", "sent to topic 't'")
        entry = collector.to_map()["n2"]
        assert entry["status"] == STATUS_FAILURE
        assert entry["detail"] == "publish failed: broker unreachable"

    def test_is_contained(self):
        # A broken internals state must not raise out of set_detail.
        collector = NodeStatusCollector(NAME_MAP)
        collector._statuses = None  # force an internal error
        collector.set_detail("n1", "detail")  # swallowed, no exception


# --- Executor integration ----------------------------------------------------


class _RecordingManager:
    """Records the status_sink it received; optionally raises to fail the run
    or drives the sink to simulate live per-element bus signals."""

    def __init__(self, tag_values=None, error=None, drive=None):
        self.tag_values = tag_values or {}
        self.error = error
        self.drive = drive or []
        self.status_sink = "unset"

    def run_pipeline(self, pipeline_str, frame_data=None, latency_metrics=None,
                     status_sink=None):
        self.status_sink = status_sink
        for element_name, kind, detail in self.drive:
            status_sink(element_name, kind, detail)
        if self.error is not None:
            raise self.error
        return dict(self.tag_values)


@pytest.fixture
def session_factory():
    return make_session_factory()


@pytest.fixture(autouse=True)
def no_registry_scan():
    with patch.object(gst_plugins, "_scan_registry", return_value=True):
        yield


def _seed(session_factory, artifact_path):
    session = session_factory()
    try:
        session.add(WorkflowRegistration(
            id="wf-1:3", workflow_id="wf-1", version="3", arch=DEVICE_ARCH,
            artifact_path=str(artifact_path), status="registered",
            registered_at=int(time.time()),
        ))
        session.add(WorkflowExecution(
            id="exec-1", registration_id="wf-1:3",
            started_at=int(time.time()), status=EXECUTION_STATUS_PENDING,
        ))
        session.commit()
    finally:
        session.close()
    return "exec-1"


def _get(session_factory, execution_id="exec-1"):
    session = session_factory()
    try:
        return session.get(WorkflowExecution, execution_id)
    finally:
        session.close()


def test_executor_passes_a_sink_and_persists_terminal_status_on_success(
    tmp_path, session_factory
):
    artifact_path = write_artifact_set(tmp_path, compiled=COMPILED_DOC)
    execution_id = _seed(session_factory, artifact_path)
    manager = _RecordingManager(tag_values={"is_anomalous": False})
    capture_root = str(tmp_path / "captures")

    with patch.object(pipeline_executor, "_WORKFLOW_CAPTURE_ROOT", capture_root):
        WorkflowExecutor(
            session_factory=session_factory,
            pipeline_manager_factory=lambda: manager,
        ).execute(execution_id)

    # A real sink (not None) was threaded into run_pipeline (R3 / R8.1).
    assert callable(manager.status_sink)
    row = _get(session_factory)
    assert row.status == EXECUTION_STATUS_COMPLETED
    status = json.loads(row.node_status_json)
    # Exactly the participating nodeIds, all terminal success (R3.1, R3.3).
    assert set(status) == {"n1", "n2"}
    assert status["n1"]["status"] == STATUS_SUCCESS
    assert status["n2"]["status"] == STATUS_SUCCESS


def test_executor_captures_a_live_warning_on_success(tmp_path, session_factory):
    artifact_path = write_artifact_set(tmp_path, compiled=COMPILED_DOC)
    execution_id = _seed(session_factory, artifact_path)
    manager = _RecordingManager(
        tag_values={"is_anomalous": False},
        drive=[("emltriton0", "warning", "recoverable element warning")],
    )
    capture_root = str(tmp_path / "captures")

    with patch.object(pipeline_executor, "_WORKFLOW_CAPTURE_ROOT", capture_root):
        WorkflowExecutor(
            session_factory=session_factory,
            pipeline_manager_factory=lambda: manager,
        ).execute(execution_id)

    status = json.loads(_get(session_factory).node_status_json)
    # The warned node keeps its warning (not downgraded to success) with detail
    # retained (R3.4); the other node is success.
    assert status["n2"]["status"] == STATUS_WARNING
    assert status["n2"]["detail"] == "recoverable element warning"
    assert status["n1"]["status"] == STATUS_SUCCESS


def test_executor_attributes_failure_to_the_failing_node(
    tmp_path, session_factory
):
    artifact_path = write_artifact_set(tmp_path, compiled=COMPILED_DOC)
    execution_id = _seed(session_factory, artifact_path)
    # The error names the emltriton element, so it maps back to node n2.
    manager = _RecordingManager(
        error=RuntimeError(
            "Pipeline failed with: could not link emltriton0 to next element."
        )
    )
    capture_root = str(tmp_path / "captures")

    with patch.object(pipeline_executor, "_WORKFLOW_CAPTURE_ROOT", capture_root):
        WorkflowExecutor(
            session_factory=session_factory,
            pipeline_manager_factory=lambda: manager,
        ).execute(execution_id)

    row = _get(session_factory)
    assert row.status == EXECUTION_STATUS_FAILED
    assert row.failing_node_id == "n2"
    status = json.loads(row.node_status_json)
    # Exactly n2 is failure; the map is fully terminal (R3.2, R3.6).
    assert status["n2"]["status"] == STATUS_FAILURE
    assert "emltriton0" in status["n2"]["detail"]
    assert status["n1"]["status"] == STATUS_SUCCESS
    for entry in status.values():
        assert entry["status"] in TERMINAL_STATES


def test_executor_never_persists_all_success_for_an_unattributable_failure(
    tmp_path, session_factory
):
    """A pre-parse pipeline syntax error (e.g. a custom node whose declared
    factory does not exist in the registry) names no known element, so no
    node can be marked failure — but the terminal map must still be
    consistent with the failed run outcome (R3.6/R6.6), not all-green."""
    artifact_path = write_artifact_set(tmp_path, compiled=COMPILED_DOC)
    execution_id = _seed(session_factory, artifact_path)
    manager = _RecordingManager(
        error=RuntimeError('gst_parse_error: no element "resize_image" (1)')
    )
    capture_root = str(tmp_path / "captures")

    with patch.object(pipeline_executor, "_WORKFLOW_CAPTURE_ROOT", capture_root):
        WorkflowExecutor(
            session_factory=session_factory,
            pipeline_manager_factory=lambda: manager,
        ).execute(execution_id)

    row = _get(session_factory)
    assert row.status == EXECUTION_STATUS_FAILED
    assert row.failing_node_id is None
    status = json.loads(row.node_status_json)
    assert set(status) == {"n1", "n2"}
    for entry in status.values():
        assert entry["status"] in TERMINAL_STATES
        # Not all-green: every unresolved node carries the run error as a
        # warning instead of a spurious success.
        assert entry["status"] == STATUS_WARNING
        assert 'no element "resize_image"' in entry["detail"]


def test_preflight_fails_the_run_naming_the_node_and_the_plugin_elements(
    tmp_path, session_factory
):
    """The pipeline-factory preflight (MissingPipelineElementError): a
    declared factory absent from the GStreamer registry after the plugin
    scan fails the run BEFORE the parse, with the originating node
    identified and the plugin's actual element names in the error —
    instead of an unattributable `no element "..."` gst_parse_error."""
    doc = json.loads(json.dumps(COMPILED_DOC))
    # The inference node's factory is a custom-node declaration mismatch:
    # the built plugin registers "customresizeimage".
    doc["segments"][0]["elements"][1] = {
        "nodeId": "n2", "factory": "resize_image", "args": {},
    }
    artifact_path = write_artifact_set(tmp_path, compiled=doc)
    execution_id = _seed(session_factory, artifact_path)
    manager = _RecordingManager(tag_values={"is_anomalous": False})
    capture_root = str(tmp_path / "captures")

    with patch.object(pipeline_executor, "_WORKFLOW_CAPTURE_ROOT", capture_root), \
            patch.object(
                gst_plugins, "missing_factories",
                side_effect=lambda factories, scan_dirs=None: [
                    f for f in factories if f == "resize_image"
                ],
            ), \
            patch.object(
                gst_plugins, "provided_elements",
                return_value=["customresizeimage"],
            ):
        WorkflowExecutor(
            session_factory=session_factory,
            pipeline_manager_factory=lambda: manager,
        ).execute(execution_id)

    # The pipeline was never parsed/run.
    assert manager.status_sink == "unset"
    row = _get(session_factory)
    assert row.status == EXECUTION_STATUS_FAILED
    # The failure is attributed to the declaring node...
    assert row.failing_node_id == "n2"
    # ...and the error names both the missing factory and what the
    # delivered plugin actually registers.
    assert 'resize_image' in row.error
    assert "customresizeimage" in row.error
    status = json.loads(row.node_status_json)
    assert status["n2"]["status"] == STATUS_FAILURE
    assert status["n1"]["status"] == STATUS_SUCCESS


def test_preflight_with_nothing_missing_is_a_noop(tmp_path, session_factory):
    """With every factory registered (or GStreamer unavailable, as in
    this environment) the preflight changes nothing about a clean run."""
    artifact_path = write_artifact_set(tmp_path, compiled=COMPILED_DOC)
    execution_id = _seed(session_factory, artifact_path)
    manager = _RecordingManager(tag_values={"is_anomalous": False})
    capture_root = str(tmp_path / "captures")

    with patch.object(pipeline_executor, "_WORKFLOW_CAPTURE_ROOT", capture_root):
        WorkflowExecutor(
            session_factory=session_factory,
            pipeline_manager_factory=lambda: manager,
        ).execute(execution_id)

    assert _get(session_factory).status == EXECUTION_STATUS_COMPLETED
