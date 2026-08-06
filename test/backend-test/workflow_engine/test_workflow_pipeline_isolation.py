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
"""Workflow / Pipeline_Configuration isolation regression tests (task 13.2).

Covers Requirements 13.3, 13.4 and 13.7 at the level feasible without a
device:

- A ``SimulatedPipelineConfiguration`` stands in for the existing
  Pipeline_Configuration path: like ``GstPipelineExecutor`` it owns ONE
  pipeline-manager instance for all of its runs and executes them on its
  own thread, recording per-run results and status reports.
- The manager stand-in scripts the Gst boundary deterministically and can
  hold one run open on an event, so workflow activity provably overlaps
  an in-flight Pipeline_Configuration run without sleeps.
- The real WorkflowWatcher / FastAPI router / WorkflowExecutor run
  end-to-end, exactly as in the integration suite.

Assertions:

1. Deploying and removing a Workflow_Component while a
   Pipeline_Configuration runs never restarts anything: the watcher
   registers/invalidates on its own thread, the Pipeline_Configuration's
   manager instance, thread, results and status reports are untouched,
   and no exception ever reaches the main thread (13.3).
2. A Pipeline_Configuration flow produces byte-identical results and
   status reports with and without concurrent workflow activity, and the
   workflow executor uses its own pipeline-manager instance — never the
   Pipeline_Configuration's (13.4).
3. A failing workflow run is confined to its WorkflowExecution record:
   the Pipeline_Configuration completes unaffected and GST_PLUGIN_PATH
   is restored even under failure (13.7).
"""
import os
import shutil
import threading
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from workflow_engine_test_utils import (
    DEVICE_ARCH,
    VALID_MANIFEST,
    make_session_factory,
    make_watcher,
    write_artifact_set,
)

from workflow_engine import api as workflow_engine_api
from workflow_engine import executor as executor_hook
from workflow_engine import gst_plugins, runtime
from workflow_engine.pipeline_executor import (
    EXECUTION_STATUS_COMPLETED,
    EXECUTION_STATUS_FAILED,
    register_workflow_executor,
)

POLL_TIMEOUT_SEC = 10.0
WATCH_POLL_INTERVAL_SEC = 0.2

PIPELINE_ITERATIONS = 3

#: The Pipeline_Configuration launch-string dialect (existing builder).
PIPELINE_LAUNCH_TEMPLATE = (
    "appsrc name=source ! videoconvert ! emltriton model=legacy-model ! "
    "emlcapture capture-id={0} ! fakesink"
)

#: Compiled Workflow_Component document segments.
WORKFLOW_SEGMENTS = [
    {
        "name": "s0",
        "elements": [
            {"nodeId": "cam", "factory": "videotestsrc",
             "args": {"num-buffers": 2}},
            {"nodeId": "infer", "factory": "emltriton",
             "args": {"model": "widget-anomaly-v3"}},
            {"nodeId": None, "factory": "fakesink", "args": {}},
        ],
    }
]

WORKFLOW_LAUNCH = (
    "videotestsrc num-buffers=2 ! emltriton model=widget-anomaly-v3 ! fakesink"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def wait_for(predicate, timeout=POLL_TIMEOUT_SEC, interval=0.02,
             message="condition"):
    """Poll until ``predicate()`` is truthy, returning its value."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(interval)
    pytest.fail("Timed out waiting for {0}".format(message))


def wait_for_execution(client, execution_id, timeout=POLL_TIMEOUT_SEC):
    """Poll GET /workflows/executions/{id} until the run finishes."""

    def finished():
        response = client.get("/workflows/executions/{0}".format(execution_id))
        assert response.status_code == 200
        body = response.json()
        if body["status"] in (EXECUTION_STATUS_COMPLETED, EXECUTION_STATUS_FAILED):
            return body
        return None

    return wait_for(
        finished, timeout=timeout,
        message="execution {0} to finish".format(execution_id),
    )


def wait_for_registration_status(
    client, registration_id, status, include_inactive=False
):
    def matches():
        url = "/workflows/registrations"
        if include_inactive:
            url += "?includeInactive=true"
        response = client.get(url)
        assert response.status_code == 200
        for registration in response.json():
            if (registration["registrationId"] == registration_id
                    and registration["status"] == status):
                return registration
        return None

    return wait_for(
        matches,
        message="registration {0} to become '{1}'".format(
            registration_id, status
        ),
    )


def make_compiled(workflow_id, plugin_dependencies=()):
    return {
        "schemaVersion": 1,
        "workflowId": workflow_id,
        "workflowVersion": "1",
        "targetArch": DEVICE_ARCH,
        "segments": WORKFLOW_SEGMENTS,
        "executorBindings": [],
        "pluginDependencies": list(plugin_dependencies),
    }


def deploy_workflow(workflow_root, workflow_id, plugin_files=()):
    """Write one Workflow_Component artifact set under the watched root."""
    compiled = make_compiled(
        workflow_id,
        plugin_dependencies=["dda-custom"] if plugin_files else (),
    )
    artifact_path = write_artifact_set(
        workflow_root,
        workflow_id=workflow_id,
        version="1",
        manifest=dict(VALID_MANIFEST, workflowId=workflow_id),
        compiled=compiled,
    )
    if plugin_files:
        plugin_dir = os.path.join(artifact_path, "plugins", DEVICE_ARCH)
        os.makedirs(plugin_dir, exist_ok=True)
        for name in plugin_files:
            with open(os.path.join(plugin_dir, name), "wb") as f:
                f.write(b"\x7fELF-fake-plugin")
    return artifact_path


# ---------------------------------------------------------------------------
# Pipeline_Configuration stand-ins (deterministic at the Gst boundary)
# ---------------------------------------------------------------------------


class PipelineConfigurationManager:
    """The Pipeline_Configuration's own pipeline-manager instance.

    Scripted at the Gst boundary: results depend only on the call index,
    so two flows with the same iteration count produce identical results
    regardless of what else runs concurrently. ``hold_open`` blocks one
    designated run on an event, letting a test overlap workflow activity
    with an in-flight Pipeline_Configuration run deterministically.
    """

    def __init__(self):
        self.calls = []
        self.run_threads = []
        self._hold_at_call = None
        self.entered_hold = threading.Event()
        self.release_hold = threading.Event()

    def hold_open(self, call_index):
        """Block run ``call_index`` until ``release_hold`` is set."""
        self._hold_at_call = call_index
        return self.entered_hold, self.release_hold

    def run_pipeline(self, pipeline_str, frame_data=None, latency_metrics=None,
                     status_sink=None):
        index = len(self.calls)
        self.calls.append(pipeline_str)
        self.run_threads.append(threading.current_thread())
        if self._hold_at_call == index:
            self.entered_hold.set()
            assert self.release_hold.wait(POLL_TIMEOUT_SEC), (
                "held Pipeline_Configuration run was never released"
            )
        return {
            "is_anomalous": index % 2 == 0,
            "confidence": round(0.5 + index * 0.01, 2),
        }


class SimulatedPipelineConfiguration:
    """The existing Pipeline_Configuration execution flow, simulated.

    Mirrors ``GstPipelineExecutor``: one manager instance shared by all
    runs, each run producing a capture id, parsed tags, and a status
    report through the existing reporting path (recorded here).
    Runs on its own thread, exactly like production workflow-vs-pipeline
    threading.
    """

    def __init__(self, iterations=PIPELINE_ITERATIONS):
        self.manager = PipelineConfigurationManager()
        self.iterations = iterations
        self.results = []
        self.status_reports = []
        self.errors = []
        self.thread = threading.Thread(
            target=self._run_all, name="pipeline-configuration", daemon=True
        )

    def start(self):
        self.thread.start()

    def join(self, timeout=POLL_TIMEOUT_SEC):
        self.thread.join(timeout)
        assert not self.thread.is_alive(), (
            "Pipeline_Configuration flow did not finish"
        )

    def _run_all(self):
        for index in range(self.iterations):
            capture_id = "cap-{0}".format(index)
            try:
                tags = self.manager.run_pipeline(
                    PIPELINE_LAUNCH_TEMPLATE.format(capture_id)
                )
                self.results.append((capture_id, tags))
                self.status_reports.append(
                    {"captureId": capture_id, "status": "completed",
                     "tags": tags}
                )
            except Exception as e:  # noqa: BLE001 - recorded, never raised
                self.errors.append(e)
                self.status_reports.append(
                    {"captureId": capture_id, "status": "failed",
                     "error": str(e)}
                )


def expected_pipeline_outputs(iterations=PIPELINE_ITERATIONS):
    """The results/status reports every undisturbed flow must produce."""
    results = []
    reports = []
    for index in range(iterations):
        capture_id = "cap-{0}".format(index)
        tags = {
            "is_anomalous": index % 2 == 0,
            "confidence": round(0.5 + index * 0.01, 2),
        }
        results.append((capture_id, tags))
        reports.append(
            {"captureId": capture_id, "status": "completed", "tags": tags}
        )
    return results, reports


class WorkflowPipelineManager:
    """The workflow executor's own manager instance (scripted)."""

    def __init__(self, tag_values=None, error=None):
        self.tag_values = tag_values or {}
        self.error = error
        self.calls = []
        self.env_during_run = None
        self.run_thread = None

    def run_pipeline(self, pipeline_str, frame_data=None, latency_metrics=None,
                     status_sink=None):
        self.calls.append(pipeline_str)
        self.env_during_run = os.environ.get(gst_plugins.GST_PLUGIN_PATH_ENV)
        self.run_thread = threading.current_thread()
        if self.error is not None:
            raise self.error
        return dict(self.tag_values)


# ---------------------------------------------------------------------------
# Fixtures (same real-component wiring as the integration suite)
# ---------------------------------------------------------------------------


@pytest.fixture
def session_factory():
    return make_session_factory()


@pytest.fixture
def workflow_root(tmp_path):
    return str(tmp_path / "aws_dda_workflows")


@pytest.fixture
def watcher(workflow_root, session_factory):
    from unittest.mock import patch

    instance = make_watcher(
        workflow_root, session_factory, poll_interval=WATCH_POLL_INTERVAL_SEC
    )
    with patch.object(runtime, "_watcher", instance):
        yield instance
    instance.stop()


@pytest.fixture
def client(session_factory):
    app = FastAPI()
    app.include_router(workflow_engine_api.router)

    def override_get_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[workflow_engine_api.get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides = {}


@pytest.fixture(autouse=True)
def reset_executor_hook():
    yield
    executor_hook.set_executor(None)


@pytest.fixture(autouse=True)
def no_registry_scan():
    """Never import gi; record registry scans instead."""
    from unittest.mock import patch

    with patch.object(gst_plugins, "_scan_registry", return_value=True) as scan:
        yield scan


# ---------------------------------------------------------------------------
# 13.3  Deploy/remove a Workflow_Component while a Pipeline_Configuration
#       runs: no restarts, execution untouched
# ---------------------------------------------------------------------------


class TestDeployAndRemoveWhilePipelineRuns:
    def test_deploy_and_remove_never_touch_the_running_pipeline(
        self, workflow_root, watcher, client
    ):
        env_before = os.environ.get(gst_plugins.GST_PLUGIN_PATH_ENV)

        pipeline = SimulatedPipelineConfiguration()
        entered, release = pipeline.manager.hold_open(0)
        manager_before = pipeline.manager

        watcher.start()
        assert client.get("/workflows/registrations").json() == []

        # The Pipeline_Configuration is mid-run when the component lands.
        pipeline.start()
        assert entered.wait(POLL_TIMEOUT_SEC)

        # Greengrass delivers the Workflow_Component (install-only).
        deploy_workflow(workflow_root, "wf-iso")
        registration = wait_for_registration_status(
            client, "wf-iso:1", "registered"
        )
        assert registration["arch"] == DEVICE_ARCH

        # ... and later removes it, still mid-run. Stale-workflow-
        # registrations bugfix (expected behavior 2.2/2.4): the vanished
        # directory retires the registration as 'removed' (row preserved)
        # and the default listing filters it, so poll includeInactive.
        shutil.rmtree(os.path.join(workflow_root, "wf-iso"))
        invalidated = wait_for_registration_status(
            client, "wf-iso:1", "removed", include_inactive=True
        )
        assert "removed" in invalidated["invalidReason"]

        # Throughout deploy + remove the Pipeline_Configuration thread
        # stayed alive in its original run: nothing restarted it, nothing
        # injected results.
        assert pipeline.thread.is_alive()
        assert pipeline.results == []
        assert pipeline.errors == []

        release.set()
        pipeline.join()

        # The flow finished exactly as an undisturbed flow would.
        expected_results, expected_reports = expected_pipeline_outputs()
        assert pipeline.results == expected_results
        assert pipeline.status_reports == expected_reports
        assert pipeline.errors == []

        # Its manager instance was never swapped and only ever ran
        # Pipeline_Configuration launch strings, all on the one thread.
        assert pipeline.manager is manager_before
        assert pipeline.manager.calls == [
            PIPELINE_LAUNCH_TEMPLATE.format("cap-{0}".format(i))
            for i in range(PIPELINE_ITERATIONS)
        ]
        assert set(pipeline.manager.run_threads) == {pipeline.thread}

        # No process-level restart: the watcher thread survived the
        # deploy/remove cycle (nothing raised into the main thread), the
        # process watcher instance is unchanged, and the environment is
        # untouched.
        assert watcher._thread.is_alive()
        assert runtime.get_watcher() is watcher
        assert os.environ.get(gst_plugins.GST_PLUGIN_PATH_ENV) == env_before

        # Deploy/remove alone never executed anything.
        detail = client.get("/workflows/registrations/wf-iso:1").json()
        assert detail["executions"] == []


# ---------------------------------------------------------------------------
# 13.4  Unchanged results and status reporting under concurrent workflow
#       activity; the executor uses its own manager instance
# ---------------------------------------------------------------------------


class TestUnchangedResultsUnderConcurrentWorkflow:
    def test_pipeline_results_identical_with_and_without_workflow_activity(
        self, workflow_root, watcher, client, session_factory
    ):
        # Baseline: the flow with no workflow anywhere.
        baseline = SimulatedPipelineConfiguration()
        baseline.start()
        baseline.join()

        # Concurrent: the same flow, held open mid-run while a workflow is
        # registered AND triggered to completion.
        concurrent = SimulatedPipelineConfiguration()
        entered, release = concurrent.manager.hold_open(1)
        concurrent.start()
        assert entered.wait(POLL_TIMEOUT_SEC)

        watcher.start()
        deploy_workflow(workflow_root, "wf-concurrent")
        wait_for_registration_status(client, "wf-concurrent:1", "registered")

        workflow_manager = WorkflowPipelineManager(
            tag_values={"is_anomalous": True, "confidence": 0.93}
        )
        register_workflow_executor(
            session_factory=session_factory,
            pipeline_manager_factory=lambda: workflow_manager,
        )

        response = client.post(
            "/workflows/registrations/wf-concurrent:1/trigger"
        )
        assert response.status_code == 200, response.text
        execution_id = response.json()["executionId"]
        body = wait_for_execution(client, execution_id)
        assert body["status"] == EXECUTION_STATUS_COMPLETED

        # The workflow ran to completion while the Pipeline_Configuration
        # run was still in flight.
        assert concurrent.thread.is_alive()

        release.set()
        concurrent.join()

        # Identical results and status reporting, run for run (13.4).
        assert concurrent.results == baseline.results
        assert concurrent.status_reports == baseline.status_reports
        assert concurrent.manager.calls == baseline.manager.calls
        assert concurrent.errors == [] and baseline.errors == []

        # The executor used its OWN manager instance/registry — never the
        # Pipeline_Configuration's, and no cross-calls in either direction.
        assert workflow_manager is not concurrent.manager
        assert workflow_manager.calls == [WORKFLOW_LAUNCH]
        assert WORKFLOW_LAUNCH not in concurrent.manager.calls
        for call in workflow_manager.calls:
            assert "emlcapture" not in call  # no pipeline strings leaked in

        # And on its own daemon thread — never the Pipeline_Configuration
        # thread, never the API/main thread.
        assert workflow_manager.run_thread is not concurrent.thread
        assert workflow_manager.run_thread is not threading.current_thread()


# ---------------------------------------------------------------------------
# 13.7  Workflow failures contained to the WorkflowExecution record;
#       GST_PLUGIN_PATH restored under failure in the concurrent scenario
# ---------------------------------------------------------------------------


class TestWorkflowFailureContainment:
    def test_failing_workflow_leaves_the_running_pipeline_untouched(
        self, workflow_root, watcher, client, session_factory, no_registry_scan
    ):
        from unittest.mock import patch

        with patch.dict(
            os.environ, {gst_plugins.GST_PLUGIN_PATH_ENV: "/bundled/plugins"}
        ):
            pipeline = SimulatedPipelineConfiguration()
            entered, release = pipeline.manager.hold_open(1)
            pipeline.start()
            assert entered.wait(POLL_TIMEOUT_SEC)

            watcher.start()
            artifact_path = deploy_workflow(
                workflow_root, "wf-fails",
                plugin_files=("libdda_custom.so",),
            )
            wait_for_registration_status(client, "wf-fails:1", "registered")

            # The workflow's pipeline crashes at the Gst boundary, naming
            # the emltriton element (-> node "infer").
            workflow_manager = WorkflowPipelineManager(
                error=RuntimeError(
                    "Pipeline failed with: Could not reach Triton. "
                    "gst_emltriton_start (): "
                    "/GstPipeline:pipeline0/GstEmltriton:emltriton0:"
                )
            )
            register_workflow_executor(
                session_factory=session_factory,
                pipeline_manager_factory=lambda: workflow_manager,
            )

            response = client.post(
                "/workflows/registrations/wf-fails:1/trigger"
            )
            assert response.status_code == 200, response.text
            execution_id = response.json()["executionId"]
            body = wait_for_execution(client, execution_id)

            # The failure is confined to the WorkflowExecution record,
            # with the failing node identified through the existing
            # status reporting path (13.7).
            assert body["status"] == EXECUTION_STATUS_FAILED
            assert body["failingNodeId"] == "infer"
            assert "Could not reach Triton" in body["error"]

            # The registration itself stays runnable; only the run failed.
            detail = client.get("/workflows/registrations/wf-fails:1").json()
            assert detail["status"] == "registered"
            assert [e["executionId"] for e in detail["executions"]] == [
                execution_id
            ]
            assert detail["executions"][0]["status"] == EXECUTION_STATUS_FAILED

            # GST_PLUGIN_PATH restored despite the failure, while the
            # Pipeline_Configuration run is still in flight: the delivered
            # plugin dir was scoped to the failed run only.
            plugin_dir = os.path.join(artifact_path, "plugins", DEVICE_ARCH)
            assert workflow_manager.env_during_run.startswith(plugin_dir)
            no_registry_scan.assert_called_once_with(plugin_dir)
            assert pipeline.thread.is_alive()
            assert (
                os.environ[gst_plugins.GST_PLUGIN_PATH_ENV]
                == "/bundled/plugins"
            )

            release.set()
            pipeline.join()

            # The Pipeline_Configuration finished exactly as an
            # undisturbed flow would: same results, same status reports,
            # no errors, its own manager only ever ran its own strings.
            expected_results, expected_reports = expected_pipeline_outputs()
            assert pipeline.results == expected_results
            assert pipeline.status_reports == expected_reports
            assert pipeline.errors == []
            assert WORKFLOW_LAUNCH not in pipeline.manager.calls
            assert workflow_manager.run_thread is not pipeline.thread
