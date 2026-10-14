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
"""Run retention units (rtsp-rtmp-stream-cameras Requirements 12.1-12.4,
12.7, 12.8, 12.9): staging selection, classification, promotion, the
executor's artifact root, the startup index, the startup reconciliation
of interrupted runs, directory safety, and the GStreamer debug log
bound."""
import json
import os
import shutil
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from test_workflow_stream_executor import (
    FakePipelineManager,
    FakeStreamManager,
    make_frame,
    make_stream_document,
    seed_run,
)
from workflow_engine_test_utils import make_session_factory, write_artifact_set

from workflow_engine import gst_plugins, pipeline_executor
from workflow_engine.models import WorkflowContinuousState, WorkflowExecution
from workflow_engine.pipeline_executor import WorkflowExecutor
from workflow_engine.run_retention import (
    INTERRUPTED_ERROR,
    RunRetention,
    classify_run,
    output_node_ids,
    outputs_sent,
    resolve_staging_root,
    rotate_debug_log,
)
from workflow_engine.stream_feed import FrameHandoff

UNLIMITED = SimpleNamespace(retention_bytes=10 ** 12, staging_bytes=10 ** 12)
CONTINUOUS = {"source": "continuous", "frameSeq": 7, "frameAcquiredAtMs": 1, "tickAtMs": 2}


@pytest.fixture(autouse=True)
def no_registry_scan():
    with patch.object(gst_plugins, "_scan_registry", return_value=True):
        yield


@pytest.fixture
def session_factory():
    return make_session_factory()


@pytest.fixture
def roots(tmp_path):
    captures, staging = tmp_path / "captures", tmp_path / "shm" / "dda-continuous"
    captures.mkdir()
    return str(captures), str(staging)


def make_retention(session_factory, roots, limits=UNLIMITED, **kwargs):
    captures, staging = roots
    kwargs.setdefault("min_staging_free_bytes", 0)
    return RunRetention(session_factory=session_factory, persistent_root=captures, staging_candidate=staging,
                        limits=lambda: limits, debug_log_path=None, **kwargs)


def add_run(session_factory, directory, execution_id, status="completed", context=CONTINUOUS, size=100,
            node_status=None, tags=None, registration_id="wf-1:3", error=None):
    os.makedirs(directory, exist_ok=True)
    capture_id = "wf-1-" + execution_id
    with open(os.path.join(directory, capture_id + ".jpg"), "wb") as handle:
        handle.write(b"\xff" * size)
    with open(os.path.join(directory, "run.log"), "w") as handle:
        handle.write("run\n")
    with open(os.path.join(directory, capture_id + ".json"), "w") as handle:
        json.dump(tags or {}, handle)
    session = session_factory()
    session.add(WorkflowExecution(
        id=execution_id, registration_id=registration_id, started_at=1, status=status,
        capture_id=capture_id, output_dir=directory, log_path=os.path.join(directory, "run.log"),
        node_status_json=json.dumps(node_status or {}), error=error,
        trigger_context_json=json.dumps(context) if context is not None else None))
    session.commit()
    session.close()


def row(session_factory, execution_id):
    session = session_factory()
    try:
        found = session.get(WorkflowExecution, execution_id)
        if found is not None:
            session.expunge(found)
        return found
    finally:
        session.close()


class TestStagingRoot:
    def test_a_writable_root_with_room_is_used(self, tmp_path):
        root = str(tmp_path / "shm" / "dda-continuous")
        assert resolve_staging_root(root, 0) == root
        assert oct(os.stat(root).st_mode & 0o777) == oct(0o700)

    def test_too_little_room_or_no_root_means_persistent_storage(self, tmp_path):
        root = str(tmp_path / "shm" / "dda-continuous")
        tight = lambda path: SimpleNamespace(free=10 * 1024 * 1024)  # noqa: E731
        assert resolve_staging_root(root, 64 * 1024 * 1024, tight) is None
        blocked = tmp_path / "file"
        blocked.write_text("x")
        assert resolve_staging_root(str(blocked / "dda-continuous"), 0) is None

    def test_only_continuous_runs_are_staged(self, session_factory, roots):
        retention = make_retention(session_factory, roots)
        assert retention.capture_root_for(None, CONTINUOUS) == roots[1]
        for context in ({}, None, {"source": "mqtt"}, {"source": "manual"}):
            assert retention.capture_root_for(None, context) is None

    def test_without_staging_continuous_runs_use_the_capture_root(self, session_factory, roots, tmp_path):
        blocked = tmp_path / "blocked"
        blocked.write_text("x")
        retention = make_retention(session_factory, (roots[0], str(blocked / "dda-continuous")))
        assert retention.capture_root_for(None, CONTINUOUS) is None


class TestClassification:
    DOCUMENT = {"executorBindings": [
        {"nodeId": "mq", "binding": "mqtt_publish"}, {"nodeId": "do", "binding": "digital_output"},
        {"nodeId": "llm", "binding": "llm_inference"}, {"nodeId": "cnt", "binding": "detection_counter"}]}

    def test_output_nodes_are_the_sending_bindings(self):
        assert output_node_ids(self.DOCUMENT) == ("mq", "do")
        assert output_node_ids(None) == ()

    def test_a_sent_detail_counts_and_a_skip_does_not(self):
        status = {"mq": {"status": "success", "detail": "sent to topic 'a' (qos 1, retain false): {}"},
                  "do": {"status": "success", "detail": "not sent: condition 'x' evaluated false"},
                  "llm": {"status": "success", "detail": "generated 12 tokens"}}
        assert outputs_sent(status, ("mq", "do")) == 1
        assert outputs_sent({"mq": {"status": "failure", "detail": "broker down"}}, ("mq",)) == 0
        assert outputs_sent({"mq": {"status": "success"}}, ("mq",)) == 0

    @pytest.mark.parametrize("status,node_status,tags,expected", [
        ("failed", {}, {}, (True, 0)),
        ("completed", {"mq": {"status": "success", "detail": "wrote 1 to ns=2;s=x at opc"}}, {}, (True, 1)),
        ("completed", {}, {"event": {"g": {"transition": "activated"}}}, (True, 0)),
        ("completed", {}, {"event": {"g": {"transition": "cleared"}}}, (True, 0)),
        ("completed", {}, {"event": {"g": {"transition": "none"}}}, (False, 0)),
        ("completed", {"mq": {"status": "success", "detail": "not sent: gated out"}}, {}, (False, 0)),
    ])
    def test_notable_runs(self, status, node_status, tags, expected):
        assert classify_run(status, node_status, ("mq",), tags) == expected


class TestPromotion:
    def test_a_notable_staged_run_moves_to_the_capture_root(self, session_factory, roots):
        captures, staging = roots
        retention = make_retention(session_factory, roots)
        staged_dir = os.path.join(staging, "wf-1", "e1")
        add_run(session_factory, staged_dir, "e1", status="failed")

        outcome = retention.on_run_complete("e1", 20, 200)

        assert outcome.notable and outcome.status == "failed"
        moved = os.path.join(captures, "wf-1", "e1")
        assert os.path.isdir(moved) and not os.path.exists(staged_dir)
        stored = row(session_factory, "e1")
        assert stored.output_dir == moved
        assert stored.log_path == os.path.join(moved, "run.log")

    def test_a_plain_run_stays_staged(self, session_factory, roots):
        retention = make_retention(session_factory, roots)
        staged_dir = os.path.join(roots[1], "wf-1", "e1")
        add_run(session_factory, staged_dir, "e1")

        assert not retention.on_run_complete("e1", 20, 200).notable
        assert row(session_factory, "e1").output_dir == staged_dir

    def test_a_failed_move_keeps_the_run_staged(self, session_factory, roots):
        retention = make_retention(session_factory, roots)
        staged_dir = os.path.join(roots[1], "wf-1", "e1")
        add_run(session_factory, staged_dir, "e1", status="failed")

        with patch("workflow_engine.run_retention.shutil.move", side_effect=OSError(28, "No space left")):
            outcome = retention.on_run_complete("e1", 20, 200)

        assert outcome.notable
        assert row(session_factory, "e1").output_dir == staged_dir
        assert os.path.isdir(staged_dir)

    def test_an_exact_cap_keeps_both_runs(self, session_factory, roots):
        """Within the cap means at most the cap."""
        captures = roots[0]
        add_run(session_factory, os.path.join(captures, "wf-1", "e1"), "e1", status="failed", size=100)
        add_run(session_factory, os.path.join(captures, "wf-1", "e2"), "e2", status="failed", size=100)
        size = sum(os.path.getsize(os.path.join(root, name))
                   for root, _dirs, files in os.walk(os.path.join(captures, "wf-1")) for name in files)
        retention = make_retention(session_factory, roots,
                                   limits=SimpleNamespace(retention_bytes=size, staging_bytes=0))

        retention.on_run_complete("e2", 0, 5)

        assert retention.retained("wf-1:3") == ["e1", "e2"]


class TestWindows:
    def test_a_registration_with_fewer_runs_than_its_window_keeps_them_all(self, session_factory, roots):
        retention = make_retention(session_factory, roots)
        for index in range(3):
            execution_id = "e{0}".format(index)
            add_run(session_factory, os.path.join(roots[1], "wf-1", execution_id), execution_id,
                    context=dict(CONTINUOUS, tickAtMs=1000 + index))
            retention.on_run_complete(execution_id, 20, 200)

        assert retention.retained("wf-1:3") == ["e0", "e1", "e2"]
        assert retention.notable_ids("wf-1:3") == []

    def test_the_window_then_the_newest_notable_runs(self, session_factory, roots):
        retention = make_retention(session_factory, roots)
        statuses = ["failed", "completed", "failed", "failed", "completed", "completed"]
        for index, status in enumerate(statuses):
            execution_id = "e{0}".format(index)
            add_run(session_factory, os.path.join(roots[1], "wf-1", execution_id), execution_id, status=status,
                    context=dict(CONTINUOUS, tickAtMs=1000 + index))
            retention.on_run_complete(execution_id, 2, 1)

        # The two newest, plus the newest older notable run.
        assert retention.retained("wf-1:3") == ["e3", "e4", "e5"]
        assert retention.notable_ids("wf-1:3") == ["e3"]


class TestStartupIndex:
    def test_existing_runs_are_indexed_and_orphans_removed(self, session_factory, roots):
        captures, staging = roots
        for index in range(4):
            context = dict(CONTINUOUS, tickAtMs=1000 + index)
            add_run(session_factory, os.path.join(staging, "wf-1", "e{0}".format(index)), "e{0}".format(index),
                    context=context)
        orphan = os.path.join(staging, "wf-1", "lost-run")
        os.makedirs(orphan)
        retention = make_retention(session_factory, roots)

        assert retention.retained("wf-1:3") == ["e0", "e1", "e2", "e3"]
        assert not os.path.exists(orphan)

        add_run(session_factory, os.path.join(staging, "wf-1", "e4"), "e4",
                context=dict(CONTINUOUS, tickAtMs=2000))
        retention.on_run_complete("e4", 2, 0)
        assert retention.retained("wf-1:3") == ["e3", "e4"]
        assert row(session_factory, "e0") is None
        assert not os.path.exists(os.path.join(staging, "wf-1", "e0"))

    def test_runs_in_progress_are_left_alone(self, session_factory, roots):
        staging = roots[1]
        add_run(session_factory, os.path.join(staging, "wf-1", "live"), "live", status="running")
        retention = make_retention(session_factory, roots,
                                   limits=SimpleNamespace(retention_bytes=0, staging_bytes=0))

        retention.enforce_caps()

        assert row(session_factory, "live") is not None
        assert os.path.isdir(os.path.join(staging, "wf-1", "live"))


class TestStartupReconciliation:
    """A continuous run a previous backend process left ``pending`` or
    ``running`` is finished at startup (Requirement 12.9)."""

    def state_counters(self, session_factory, registration_id):
        session = session_factory()
        try:
            found = session.get(WorkflowContinuousState, registration_id)
            return json.loads(found.counters_json) if found and found.counters_json else None
        finally:
            session.close()

    def seed_state(self, session_factory, registration_id, counters):
        session = session_factory()
        session.add(WorkflowContinuousState(registration_id=registration_id, paused=False,
                                            counters_json=json.dumps(counters), updated_at=1))
        session.commit()
        session.close()

    def test_pending_and_running_continuous_runs_become_failed(self, session_factory, roots):
        staging = roots[1]
        for execution_id, status in (("p1", "pending"), ("r1", "running")):
            add_run(session_factory, os.path.join(staging, "wf-1", execution_id), execution_id, status=status)
        retention = make_retention(session_factory, roots, wall=lambda: 1700.9)

        assert retention.reconcile_interrupted() == 2

        for execution_id in ("p1", "r1"):
            found = row(session_factory, execution_id)
            assert found.status == "failed"
            assert found.error == INTERRUPTED_ERROR
            assert found.finished_at == 1700

    def test_an_existing_finish_time_is_kept(self, session_factory, roots):
        add_run(session_factory, os.path.join(roots[1], "wf-1", "r1"), "r1", status="running")
        session = session_factory()
        session.get(WorkflowExecution, "r1").finished_at = 42
        session.commit()
        session.close()

        make_retention(session_factory, roots, wall=lambda: 1700.0).reconcile_interrupted()

        assert row(session_factory, "r1").finished_at == 42

    @pytest.mark.parametrize("execution_id,status,context", [
        ("triggered", "running", {"source": "mqtt", "tickAtMs": 1}),
        ("manual", "pending", None),
        ("done", "completed", CONTINUOUS),
        ("gone", "failed", CONTINUOUS),
    ])
    def test_other_runs_are_untouched(self, session_factory, roots, execution_id, status, context):
        add_run(session_factory, os.path.join(roots[1], "wf-1", execution_id), execution_id, status=status,
                context=context)

        assert make_retention(session_factory, roots).reconcile_interrupted() == 0

        found = row(session_factory, execution_id)
        assert (found.status, found.error, found.finished_at) == (status, None, None)

    def test_the_failed_counter_is_bumped_per_registration(self, session_factory, roots):
        staging = roots[1]
        self.seed_state(session_factory, "wf-1:3", {"started": 9, "failed": 4})
        self.seed_state(session_factory, "wf-2:1", {"started": 1})
        for execution_id, registration_id in (("a", "wf-1:3"), ("b", "wf-1:3"), ("c", "wf-2:1")):
            add_run(session_factory, os.path.join(staging, "wf-1", execution_id), execution_id,
                    status="running", registration_id=registration_id)
        # A registration with no state row must not create one.
        add_run(session_factory, os.path.join(staging, "wf-1", "d"), "d", status="pending",
                registration_id="wf-3:1")

        assert make_retention(session_factory, roots).reconcile_interrupted() == 4

        assert self.state_counters(session_factory, "wf-1:3") == {"started": 9, "failed": 6}
        assert self.state_counters(session_factory, "wf-2:1") == {"started": 1, "failed": 1}
        assert self.state_counters(session_factory, "wf-3:1") is None

    def test_a_missing_or_broken_snapshot_still_counts(self, session_factory, roots):
        self.seed_state(session_factory, "wf-1:3", {"failed": "many"})
        session = session_factory()
        session.get(WorkflowContinuousState, "wf-1:3").counters_json = "not json"
        session.commit()
        session.close()
        add_run(session_factory, os.path.join(roots[1], "wf-1", "r1"), "r1", status="running")

        assert make_retention(session_factory, roots).reconcile_interrupted() == 1

        assert self.state_counters(session_factory, "wf-1:3") == {"failed": 1}

    def test_the_surplus_staged_runs_are_then_evicted(self, session_factory, roots):
        """The MIC-730 held 23-27 staged runs per workflow instead of 20:
        the interrupted rows were never finished, so the recent window
        never evicted them."""
        staging = roots[1]
        for index in range(3):
            execution_id = "stuck{0}".format(index)
            add_run(session_factory, os.path.join(staging, "wf-1", execution_id), execution_id,
                    status="running", context=dict(CONTINUOUS, tickAtMs=1000 + index))
        for index in range(20):
            execution_id = "e{0}".format(index)
            add_run(session_factory, os.path.join(staging, "wf-1", execution_id), execution_id,
                    context=dict(CONTINUOUS, tickAtMs=2000 + index))
        retention = make_retention(session_factory, roots)

        assert retention.reconcile_interrupted() == 3
        assert len(retention.retained("wf-1:3")) == 23

        add_run(session_factory, os.path.join(staging, "wf-1", "fresh"), "fresh",
                context=dict(CONTINUOUS, tickAtMs=3000))
        retention.on_run_complete("fresh", 20, 0)

        assert len(retention.retained("wf-1:3")) == 20
        for index in range(3):
            assert not os.path.exists(os.path.join(staging, "wf-1", "stuck{0}".format(index)))
            assert row(session_factory, "stuck{0}".format(index)) is None

    def test_an_already_loaded_index_picks_the_rows_up(self, session_factory, roots):
        staging = roots[1]
        add_run(session_factory, os.path.join(staging, "wf-1", "e0"), "e0",
                context=dict(CONTINUOUS, tickAtMs=1000))
        add_run(session_factory, os.path.join(staging, "wf-1", "r1"), "r1", status="running",
                context=dict(CONTINUOUS, tickAtMs=1001))
        retention = make_retention(session_factory, roots)
        assert retention.retained("wf-1:3") == ["e0"]  # loads the index first

        assert retention.reconcile_interrupted() == 1

        assert retention.retained("wf-1:3") == ["e0", "r1"]
        assert retention.notable_ids("wf-1:3") == []

    def test_an_interrupted_run_is_not_notable_but_other_failures_are(self, session_factory, roots):
        staging = roots[1]
        add_run(session_factory, os.path.join(staging, "wf-1", "boom"), "boom", status="failed",
                error="pipeline element sink failed", context=dict(CONTINUOUS, tickAtMs=1000))
        add_run(session_factory, os.path.join(staging, "wf-1", "r1"), "r1", status="running",
                context=dict(CONTINUOUS, tickAtMs=1001))
        retention = make_retention(session_factory, roots)
        retention.reconcile_interrupted()

        assert retention.retained("wf-1:3") == ["boom", "r1"]
        assert retention.notable_ids("wf-1:3") == ["boom"]
        # Not promoted out of staging either, so a restart cannot push the
        # real Notable_Runs out.
        assert os.path.isdir(os.path.join(staging, "wf-1", "r1"))

        assert classify_run("failed", {}, (), {}, error=INTERRUPTED_ERROR) == (False, 0)
        assert classify_run("failed", {}, (), {}) == (True, 0)

    def test_a_database_failure_is_contained(self, session_factory, roots):
        retention = make_retention(session_factory, roots)
        with patch.object(retention, "_session", side_effect=RuntimeError("no database")):
            assert retention.reconcile_interrupted() == 0

    def test_a_failure_while_reconciling_is_contained(self, session_factory, roots):
        add_run(session_factory, os.path.join(roots[1], "wf-1", "r1"), "r1", status="running")
        retention = make_retention(session_factory, roots)
        with patch.object(retention, "_count_interrupted_locked", side_effect=RuntimeError("boom")):
            assert retention.reconcile_interrupted() == 0

        assert row(session_factory, "r1").status == "running"


class TestStartupOrder:
    def test_reconciliation_precedes_the_executor_and_the_continuous_manager(self):
        """``runtime.start_workflow_engine`` reconciles right after the
        retention is created: before the executor is registered and before
        the ContinuousRunnerManager's first ``on_registrations_changed``."""
        from workflow_engine import continuous_runner as continuous_module
        from workflow_engine import pipeline_executor as executor_module
        from workflow_engine import run_retention as retention_module
        from workflow_engine import runtime, stream_leases, trigger_runtime

        order = []

        class FakeRetention:
            def __init__(self, *args, **kwargs):
                order.append("retention")

            def reconcile_interrupted(self):
                order.append("reconcile")
                return 0

            def capture_root_for(self, registration, context):
                return None

            def add_housekeeping_task(self, task):
                pass

        class FakeWatcher:
            def __init__(self, *args, **kwargs):
                self.registrations_listeners = []
                self.lease_refusal_lookup = None

            def start(self):
                order.append("watcher")

            def binding_resolution(self, *args, **kwargs):
                return None

            def _resync_for_bindings(self, *args, **kwargs):
                return None

        class FakeContinuousManager:
            def __init__(self, *args, **kwargs):
                pass

            def on_registrations_changed(self, *args, **kwargs):
                order.append("continuous")

            def persist_counters(self):
                pass

        class FakeListener:
            def __init__(self, *args, **kwargs):
                pass

            def on_registrations_changed(self, *args, **kwargs):
                pass

            def refusal_reason(self, *args, **kwargs):
                return None

        def fake_register(**kwargs):
            order.append("executor")
            return object()

        with patch.object(runtime, "_watcher", None), \
                patch.object(runtime, "_executor_instance", None), \
                patch.object(runtime, "_trigger_manager", None), \
                patch.object(runtime, "_stream_lease_keeper", None), \
                patch.object(runtime, "_continuous_manager", None), \
                patch.object(runtime, "WorkflowWatcher", FakeWatcher), \
                patch.object(runtime, "_camera_binding_dependencies", return_value=(None, None)), \
                patch.object(runtime, "_wire_camera_binding_hooks"), \
                patch.object(runtime, "_configure_sample_export", return_value=None), \
                patch.object(retention_module, "RunRetention", FakeRetention), \
                patch.object(executor_module, "register_workflow_executor", fake_register), \
                patch.object(trigger_runtime, "TriggerSubscriptionManager", FakeListener), \
                patch.object(stream_leases, "StreamLeaseKeeper", FakeListener), \
                patch.object(continuous_module, "ContinuousRunnerManager", FakeContinuousManager):
            assert runtime.start_workflow_engine() is not None

        assert order == ["watcher", "retention", "reconcile", "executor", "continuous"]


class TestDirectorySafety:
    def test_only_run_directories_under_a_retention_root_are_removed(self, session_factory, roots, tmp_path):
        retention = make_retention(session_factory, roots)
        outside = tmp_path / "elsewhere" / "e1"
        outside.mkdir(parents=True)
        misnamed = os.path.join(roots[0], "wf-1", "not-the-run")
        os.makedirs(misnamed)

        retention._remove_dir(str(outside), "e1")
        retention._remove_dir(misnamed, "e1")

        assert outside.is_dir() and os.path.isdir(misnamed)


class TestExecutorArtifactRoot:
    def run(self, session_factory, tmp_path, roots, context, handoff):
        document = make_stream_document(processing_mode="continuous")
        artifact_path = write_artifact_set(tmp_path / "workflows", compiled=document)
        execution_id = seed_run(session_factory, artifact_path, trigger_context=context)
        retention = make_retention(session_factory, roots)
        executor = WorkflowExecutor(session_factory=session_factory,
                                    pipeline_manager_factory=lambda: FakePipelineManager(),
                                    stream_ingest_manager=FakeStreamManager(frame=make_frame(seq=7)),
                                    capture_root_for=retention.capture_root_for, frame_handoff=handoff)
        with patch.object(pipeline_executor, "_WORKFLOW_CAPTURE_ROOT", roots[0]):
            executor.execute(execution_id)
        return row(session_factory, execution_id)

    def test_a_continuous_run_writes_to_staging(self, session_factory, tmp_path, roots):
        handoff = FrameHandoff()
        handoff.put("exec-1", make_frame(seq=7))
        stored = self.run(session_factory, tmp_path, roots, CONTINUOUS, handoff)

        assert stored.status == "completed"
        assert stored.output_dir == os.path.join(roots[1], "wf-1", "exec-1")
        assert stored.log_path == os.path.join(roots[1], "wf-1", "exec-1", "run.log")
        assert os.path.isfile(stored.log_path)
        assert len(handoff) == 0

    def test_any_other_run_keeps_the_capture_root(self, session_factory, tmp_path, roots):
        stored = self.run(session_factory, tmp_path, roots, {"source": "mqtt"}, FrameHandoff())

        assert stored.output_dir == os.path.join(roots[0], "wf-1", "exec-1")
        assert not os.path.exists(roots[1]) or os.listdir(roots[1]) == []

    def test_a_failing_root_choice_keeps_the_capture_root(self, session_factory, tmp_path, roots):
        document = make_stream_document()
        artifact_path = write_artifact_set(tmp_path / "workflows", compiled=document)
        execution_id = seed_run(session_factory, artifact_path)

        def broken(registration, context):
            raise RuntimeError("no staging")

        executor = WorkflowExecutor(session_factory=session_factory,
                                    pipeline_manager_factory=lambda: FakePipelineManager(),
                                    stream_ingest_manager=FakeStreamManager(frame=make_frame()),
                                    capture_root_for=broken)
        with patch.object(pipeline_executor, "_WORKFLOW_CAPTURE_ROOT", roots[0]):
            executor.execute(execution_id)

        assert row(session_factory, execution_id).output_dir == os.path.join(roots[0], "wf-1", "exec-1")

    def test_the_handed_frame_is_analyzed_without_reading_the_camera(self, session_factory, tmp_path, roots):
        handoff = FrameHandoff()
        handed = make_frame(seq=7, width=8, height=6)
        handoff.put("exec-1", handed)
        document = make_stream_document(processing_mode="continuous")
        artifact_path = write_artifact_set(tmp_path / "workflows", compiled=document)
        execution_id = seed_run(session_factory, artifact_path, trigger_context=CONTINUOUS)
        camera = FakeStreamManager(frame=make_frame(seq=9))
        pipeline = FakePipelineManager()
        observed = []
        executor = WorkflowExecutor(session_factory=session_factory, pipeline_manager_factory=lambda: pipeline,
                                    stream_ingest_manager=camera, frame_handoff=handoff,
                                    post_run_handler=lambda registration, doc, tags: observed.append(tags))
        with patch.object(pipeline_executor, "_WORKFLOW_CAPTURE_ROOT", roots[0]):
            executor.execute(execution_id)

        assert camera.calls == []
        assert pipeline.calls[0][1][0]["data"] is handed.data
        assert observed[0]["stream"]["cam"]["seq"] == 7

    def test_a_mismatched_handoff_reads_the_camera(self, session_factory, tmp_path, roots):
        handoff = FrameHandoff()
        handoff.put("exec-1", make_frame(seq=3))
        document = make_stream_document(processing_mode="continuous")
        artifact_path = write_artifact_set(tmp_path / "workflows", compiled=document)
        execution_id = seed_run(session_factory, artifact_path, trigger_context=CONTINUOUS)
        camera = FakeStreamManager(frame=make_frame(seq=8))
        executor = WorkflowExecutor(session_factory=session_factory,
                                    pipeline_manager_factory=lambda: FakePipelineManager(),
                                    stream_ingest_manager=camera, frame_handoff=handoff)
        with patch.object(pipeline_executor, "_WORKFLOW_CAPTURE_ROOT", roots[0]):
            executor.execute(execution_id)

        assert [call[0] for call in camera.calls if call[0] == "latest"] == ["latest"]
        assert [call[2] for call in camera.calls if call[0] == "latest"] == [6]
        assert len(handoff) == 0


class TestDebugLog:
    def test_a_small_log_is_left_alone(self, tmp_path):
        log = tmp_path / "gst-debug.log"
        log.write_bytes(b"x" * 100)
        assert not rotate_debug_log(str(log), max_bytes=1000, keep_bytes=10)
        assert log.stat().st_size == 100

    def test_a_large_log_keeps_its_tail_and_is_truncated_in_place(self, tmp_path):
        log = tmp_path / "gst-debug.log"
        log.write_bytes(b"a" * 5000 + b"TAIL")
        with open(str(log), "ab") as writer:
            assert rotate_debug_log(str(log), max_bytes=1000, keep_bytes=10)
            # The writer's handle stays valid: new lines land in the file.
            writer.write(b"new line\n")
        assert (tmp_path / "gst-debug.log.1").read_bytes() == b"aaaaaaTAIL"
        assert log.read_bytes() == b"new line\n"

    def test_a_sparse_log_is_measured_by_its_disk_use(self, tmp_path):
        """A writer that kept its offset after a truncation leaves a hole;
        only the bytes actually on disk count."""
        log = tmp_path / "gst-debug.log"
        with open(str(log), "wb") as writer:
            writer.seek(10 * 1024 * 1024)
            writer.write(b"after the hole\n")
        assert log.stat().st_size > 10 * 1024 * 1024
        assert not rotate_debug_log(str(log), max_bytes=1024 * 1024, keep_bytes=10)

    def test_a_missing_or_unset_log_is_fine(self, tmp_path):
        assert not rotate_debug_log(None)
        assert not rotate_debug_log(str(tmp_path / "absent.log"))


class TestHousekeeping:
    def test_one_pass_runs_caps_tasks_and_the_log_bound(self, session_factory, roots, tmp_path):
        log = tmp_path / "gst-debug.log"
        log.write_bytes(b"z" * 4096)
        ran = []
        retention = RunRetention(session_factory=session_factory, persistent_root=roots[0],
                                 staging_candidate=roots[1], limits=lambda: UNLIMITED,
                                 debug_log_path=str(log), min_staging_free_bytes=0)
        retention.add_housekeeping_task(lambda: ran.append(1))
        retention.add_housekeeping_task(lambda: 1 / 0)
        retention.add_housekeeping_task(lambda: ran.append(2))

        with patch("workflow_engine.run_retention.DEBUG_LOG_MAX_BYTES", 1024):
            retention.housekeeping_once()

        assert ran == [1, 2]

    def test_the_thread_starts_once_and_stops(self, session_factory, roots):
        retention = make_retention(session_factory, roots, interval_s=0.01)
        retention.start()
        first = retention._thread
        retention.start()
        assert retention._thread is first and first.is_alive()
        retention.stop()
        first.join(2)
        assert not first.is_alive()
