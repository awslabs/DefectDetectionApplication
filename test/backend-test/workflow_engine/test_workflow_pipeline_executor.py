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
"""Tests for the WorkflowExecutor (Requirements 9.2, 9.3, 9.7, 13.1, 13.4, 13.7).

Uses a mocked pipeline manager so the executor's state transitions,
failure mapping, and GST_PLUGIN_PATH scoping are exercised without
GStreamer or a real /aws_dda tree.
"""
import os
import time
from unittest.mock import patch

import pytest

from workflow_engine_test_utils import (
    DEVICE_ARCH,
    make_session_factory,
    write_artifact_set,
)

from workflow_engine import executor as executor_hook
from workflow_engine import gst_plugins
from workflow_engine.models import WorkflowExecution, WorkflowRegistration
from workflow_engine.pipeline_executor import (
    EXECUTION_STATUS_COMPLETED,
    EXECUTION_STATUS_FAILED,
    EXECUTION_STATUS_PENDING,
    EXECUTION_STATUS_RUNNING,
    WorkflowExecutor,
    register_workflow_executor,
)

COMPILED_DOC = {
    "schemaVersion": 1,
    "workflowId": "wf-1",
    "workflowVersion": "3",
    "targetArch": DEVICE_ARCH,
    "segments": [
        {
            "name": "s0",
            "elements": [
                {"nodeId": "n1", "factory": "videotestsrc", "args": {"num-buffers": 1}},
                {
                    "nodeId": "n2",
                    "factory": "emltriton",
                    "args": {"model": "widget-anomaly-v3"},
                },
                {"nodeId": None, "factory": "fakesink", "args": {}},
            ],
        }
    ],
    "executorBindings": [
        {"nodeId": "n3", "binding": "mqtt_publish", "parameters": {}}
    ],
    "pluginDependencies": [],
}

EXPECTED_LAUNCH = (
    "videotestsrc num-buffers=1 ! emltriton model=widget-anomaly-v3 ! fakesink"
)


class FakePipelineManager:
    """Mocked GstPipelineManager capturing the run and its environment."""

    def __init__(self, tag_values=None, error=None):
        self.tag_values = tag_values or {}
        self.error = error
        self.calls = []
        self.env_during_run = None

    def run_pipeline(self, pipeline_str, frame_data=None, latency_metrics=None):
        self.calls.append(pipeline_str)
        self.env_during_run = os.environ.get(gst_plugins.GST_PLUGIN_PATH_ENV)
        if self.error is not None:
            raise self.error
        return dict(self.tag_values)


@pytest.fixture
def session_factory():
    return make_session_factory()


@pytest.fixture(autouse=True)
def no_registry_scan():
    """Never import gi in these tests; record scan calls instead."""
    with patch.object(gst_plugins, "_scan_registry", return_value=True) as scan:
        yield scan


def seed_run(session_factory, artifact_path, status="registered",
             registration_id="wf-1:3", execution_status=EXECUTION_STATUS_PENDING):
    """A registration + execution pair ready for execute()."""
    session = session_factory()
    try:
        session.add(
            WorkflowRegistration(
                id=registration_id,
                workflow_id="wf-1",
                version="3",
                arch=DEVICE_ARCH,
                artifact_path=str(artifact_path),
                status=status,
                registered_at=int(time.time()),
            )
        )
        session.add(
            WorkflowExecution(
                id="exec-1",
                registration_id=registration_id,
                started_at=int(time.time()),
                status=execution_status,
            )
        )
        session.commit()
    finally:
        session.close()
    return "exec-1"


def get_execution(session_factory, execution_id="exec-1"):
    session = session_factory()
    try:
        return session.get(WorkflowExecution, execution_id)
    finally:
        session.close()


def make_executor(session_factory, manager, post_run_handler=None):
    return WorkflowExecutor(
        session_factory=session_factory,
        pipeline_manager_factory=lambda: manager,
        post_run_handler=post_run_handler,
    )


class TestSuccessfulRun:
    def test_renders_and_runs_the_compiled_document(self, tmp_path, session_factory):
        artifact_path = write_artifact_set(tmp_path, compiled=COMPILED_DOC)
        execution_id = seed_run(session_factory, artifact_path)
        manager = FakePipelineManager(tag_values={"is_anomalous": True})

        make_executor(session_factory, manager).execute(execution_id)

        assert manager.calls == [EXPECTED_LAUNCH]
        row = get_execution(session_factory)
        assert row.status == EXECUTION_STATUS_COMPLETED
        assert row.finished_at is not None
        assert row.failing_node_id is None
        assert row.error is None

    def test_post_run_hook_receives_bindings_and_tags(self, tmp_path, session_factory):
        artifact_path = write_artifact_set(tmp_path, compiled=COMPILED_DOC)
        execution_id = seed_run(session_factory, artifact_path)
        manager = FakePipelineManager(
            tag_values={"is_anomalous": False, "confidence": 0.42}
        )
        received = []

        def handler(registration, document, tag_values):
            received.append((registration.id, document, tag_values))

        make_executor(session_factory, manager, post_run_handler=handler).execute(
            execution_id
        )

        assert len(received) == 1
        registration_id, document, tag_values = received[0]
        assert registration_id == "wf-1:3"
        assert document["executorBindings"] == COMPILED_DOC["executorBindings"]
        assert tag_values == {"is_anomalous": False, "confidence": 0.42}

    def test_post_run_hook_failure_does_not_fail_the_run(
        self, tmp_path, session_factory
    ):
        artifact_path = write_artifact_set(tmp_path, compiled=COMPILED_DOC)
        execution_id = seed_run(session_factory, artifact_path)

        def broken_handler(registration, document, tag_values):
            raise RuntimeError("output binding exploded")

        make_executor(
            session_factory, FakePipelineManager(), post_run_handler=broken_handler
        ).execute(execution_id)

        assert get_execution(session_factory).status == EXECUTION_STATUS_COMPLETED


class TestFailureMapping:
    def test_pipeline_error_maps_failing_element_to_node(
        self, tmp_path, session_factory
    ):
        artifact_path = write_artifact_set(tmp_path, compiled=COMPILED_DOC)
        execution_id = seed_run(session_factory, artifact_path)
        error = RuntimeError(
            "Pipeline failed with: Could not reach Triton. "
            "gst_emltriton_start (): "
            "/GstPipeline:pipeline0/GstEmltriton:emltriton0:"
        )
        manager = FakePipelineManager(error=error)

        make_executor(session_factory, manager).execute(execution_id)

        row = get_execution(session_factory)
        assert row.status == EXECUTION_STATUS_FAILED
        assert row.failing_node_id == "n2"
        assert "Could not reach Triton" in row.error
        assert row.finished_at is not None

    def test_unidentifiable_error_records_failure_without_node(
        self, tmp_path, session_factory
    ):
        artifact_path = write_artifact_set(tmp_path, compiled=COMPILED_DOC)
        execution_id = seed_run(session_factory, artifact_path)
        manager = FakePipelineManager(
            error=RuntimeError("Pipeline timed out after 120s without completing")
        )

        make_executor(session_factory, manager).execute(execution_id)

        row = get_execution(session_factory)
        assert row.status == EXECUTION_STATUS_FAILED
        assert row.failing_node_id is None
        assert "timed out" in row.error


class TestPreflightGuards:
    def test_unknown_execution_is_a_noop(self, session_factory):
        manager = FakePipelineManager()
        make_executor(session_factory, manager).execute("missing-exec")
        assert manager.calls == []

    def test_non_pending_execution_is_not_rerun(self, tmp_path, session_factory):
        artifact_path = write_artifact_set(tmp_path, compiled=COMPILED_DOC)
        execution_id = seed_run(
            session_factory, artifact_path,
            execution_status=EXECUTION_STATUS_RUNNING,
        )
        manager = FakePipelineManager()

        make_executor(session_factory, manager).execute(execution_id)

        assert manager.calls == []
        assert get_execution(session_factory).status == EXECUTION_STATUS_RUNNING

    def test_invalid_registration_fails_without_running(
        self, tmp_path, session_factory
    ):
        artifact_path = write_artifact_set(tmp_path, compiled=COMPILED_DOC)
        execution_id = seed_run(session_factory, artifact_path, status="invalid")
        manager = FakePipelineManager()

        make_executor(session_factory, manager).execute(execution_id)

        assert manager.calls == []
        row = get_execution(session_factory)
        assert row.status == EXECUTION_STATUS_FAILED
        assert "cannot be run" in row.error

    def test_missing_compiled_document_fails_without_running(
        self, tmp_path, session_factory
    ):
        artifact_path = write_artifact_set(
            tmp_path, omit=("compiled_pipeline.json",)
        )
        execution_id = seed_run(session_factory, artifact_path)
        manager = FakePipelineManager()

        make_executor(session_factory, manager).execute(execution_id)

        assert manager.calls == []
        row = get_execution(session_factory)
        assert row.status == EXECUTION_STATUS_FAILED
        assert "compiled_pipeline.json" in row.error

    def test_empty_pipeline_fails_without_running(self, tmp_path, session_factory):
        empty_doc = dict(COMPILED_DOC, segments=[])
        artifact_path = write_artifact_set(tmp_path, compiled=empty_doc)
        execution_id = seed_run(session_factory, artifact_path)
        manager = FakePipelineManager()

        make_executor(session_factory, manager).execute(execution_id)

        assert manager.calls == []
        row = get_execution(session_factory)
        assert row.status == EXECUTION_STATUS_FAILED
        assert "empty" in row.error


class TestPluginPathScoping:
    def test_plugin_dir_prepended_during_run_and_restored_after(
        self, tmp_path, session_factory, no_registry_scan
    ):
        artifact_path = write_artifact_set(tmp_path, compiled=COMPILED_DOC)
        plugin_dir = os.path.join(artifact_path, "plugins", DEVICE_ARCH)
        os.makedirs(plugin_dir)
        execution_id = seed_run(session_factory, artifact_path)
        manager = FakePipelineManager()

        with patch.dict(
            os.environ, {gst_plugins.GST_PLUGIN_PATH_ENV: "/bundled/plugins"}
        ):
            make_executor(session_factory, manager).execute(execution_id)
            after_run = os.environ[gst_plugins.GST_PLUGIN_PATH_ENV]

        # Prepended for the run only (13.1: per-run, not process-wide)
        assert manager.env_during_run == f"{plugin_dir}:/bundled/plugins"
        assert after_run == "/bundled/plugins"
        no_registry_scan.assert_called_once_with(plugin_dir)

    def test_env_restored_even_when_pipeline_fails(
        self, tmp_path, session_factory
    ):
        artifact_path = write_artifact_set(tmp_path, compiled=COMPILED_DOC)
        os.makedirs(os.path.join(artifact_path, "plugins", DEVICE_ARCH))
        execution_id = seed_run(session_factory, artifact_path)
        manager = FakePipelineManager(error=RuntimeError("boom"))

        with patch.dict(
            os.environ, {gst_plugins.GST_PLUGIN_PATH_ENV: "/bundled/plugins"}
        ):
            make_executor(session_factory, manager).execute(execution_id)
            assert os.environ[gst_plugins.GST_PLUGIN_PATH_ENV] == "/bundled/plugins"

    def test_missing_plugin_dir_is_a_noop(
        self, tmp_path, session_factory, no_registry_scan
    ):
        # Most workflows deliver no extra plugins; the run proceeds without
        # touching the environment or the registry.
        artifact_path = write_artifact_set(tmp_path, compiled=COMPILED_DOC)
        execution_id = seed_run(session_factory, artifact_path)
        manager = FakePipelineManager()

        with patch.dict(os.environ):
            os.environ.pop(gst_plugins.GST_PLUGIN_PATH_ENV, None)
            make_executor(session_factory, manager).execute(execution_id)
            assert manager.env_during_run is None
            assert gst_plugins.GST_PLUGIN_PATH_ENV not in os.environ

        assert get_execution(session_factory).status == EXECUTION_STATUS_COMPLETED
        no_registry_scan.assert_not_called()

    def test_unset_env_var_is_removed_after_run(self, tmp_path, session_factory):
        artifact_path = write_artifact_set(tmp_path, compiled=COMPILED_DOC)
        plugin_dir = os.path.join(artifact_path, "plugins", DEVICE_ARCH)
        os.makedirs(plugin_dir)
        execution_id = seed_run(session_factory, artifact_path)
        manager = FakePipelineManager()

        with patch.dict(os.environ):
            os.environ.pop(gst_plugins.GST_PLUGIN_PATH_ENV, None)
            make_executor(session_factory, manager).execute(execution_id)
            assert manager.env_during_run == plugin_dir
            assert gst_plugins.GST_PLUGIN_PATH_ENV not in os.environ


class TestRegistration:
    def test_register_workflow_executor_sets_the_hook(self, session_factory):
        executor_hook.set_executor(None)
        try:
            instance = register_workflow_executor(
                session_factory=session_factory,
                pipeline_manager_factory=FakePipelineManager,
            )
            assert executor_hook.get_executor() == instance.execute
        finally:
            executor_hook.set_executor(None)

    def test_dispatch_runs_execution_on_separate_thread(
        self, tmp_path, session_factory
    ):
        # End to end through the hook: trigger-style dispatch executes on a
        # daemon thread, never the caller's (13.4).
        import threading

        artifact_path = write_artifact_set(tmp_path, compiled=COMPILED_DOC)
        execution_id = seed_run(session_factory, artifact_path)
        run_thread = []
        done = threading.Event()

        class ThreadRecordingManager(FakePipelineManager):
            def run_pipeline(self, *args, **kwargs):
                run_thread.append(threading.current_thread())
                result = super().run_pipeline(*args, **kwargs)
                return result

        manager = ThreadRecordingManager()
        instance = register_workflow_executor(
            session_factory=session_factory,
            pipeline_manager_factory=lambda: manager,
            post_run_handler=lambda *a: done.set(),
        )
        try:
            assert executor_hook.dispatch(execution_id) is True
            assert done.wait(timeout=5)
        finally:
            executor_hook.set_executor(None)

        assert run_thread and run_thread[0] is not threading.current_thread()
        # Poll briefly: the row commit happens just before the handler fires
        deadline = time.time() + 5
        while time.time() < deadline:
            if get_execution(session_factory).status == EXECUTION_STATUS_COMPLETED:
                break
            time.sleep(0.05)
        assert get_execution(session_factory).status == EXECUTION_STATUS_COMPLETED
