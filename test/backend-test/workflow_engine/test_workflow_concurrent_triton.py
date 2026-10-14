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
"""Concurrent Triton usage tests (task 13.3).

Requirement 13.8: while a Workflow_Component and an existing
Pipeline_Configuration concurrently use the same Triton-served model,
the LocalServer serves inference from both without altering the results
returned to the Pipeline_Configuration.

The concurrency semantics are exercised at the level this host allows:

- the workflow flow runs end-to-end through the REAL engine (watcher
  discovery -> trigger API -> executor daemon thread -> per-run plugin
  scoping -> a FRESH pipeline-manager instance per run, exactly as
  production wires it);
- the Pipeline_Configuration flow is simulated at the pipeline-manager
  boundary the way ``GstPipelineExecutor`` holds it: one long-lived,
  SEPARATE manager instance running builder-shaped launch strings;
- both flows' launch strings reference the SAME emltriton model, served
  by one shared fake embedded Triton whose responses are a deterministic
  pure function of (model, request payload) — so any cross-talk between
  the flows (mixed-up requests, shared manager state, leaked sessions)
  changes results detectably;
- a rendezvous barrier inside the fake Triton PROVES both flows were
  in-flight against the same model simultaneously; each flow's results
  are then compared against its own sequential baseline.

The real-Triton end-to-end variant is marked ``@pytest.mark.edge_device``
and skips with a clear reason outside the device container (the
convention ``test_workflow_engine_integration.py`` established).
"""
import hashlib
import os
import re
import threading
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from unittest.mock import patch

from workflow_engine_test_utils import (
    DEVICE_ARCH,
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
RENDEZVOUS_TIMEOUT_SEC = 10.0

#: The one Triton-served model both flows use (Requirement 13.8).
SHARED_MODEL = "widget-anomaly-v3"

#: What GstPipelineBuilder produces for an existing Pipeline_Configuration:
#: source -> decode -> the emltriton element bound to the shared model.
PIPELINE_CONFIGURATION_LAUNCH = (
    "multifilesrc location=/aws_dda/captures/line-3/frame.jpg ! jpegdec ! "
    "videoconvert ! emltriton model={0} ! fakesink".format(SHARED_MODEL)
)

WORKFLOW_SEGMENTS = [
    {
        "name": "s0",
        "elements": [
            {"nodeId": "cam", "factory": "videotestsrc",
             "args": {"num-buffers": 5}},
            {"nodeId": "infer", "factory": "emltriton",
             "args": {"model": SHARED_MODEL}},
            {"nodeId": None, "factory": "fakesink", "args": {}},
        ],
    }
]


# ---------------------------------------------------------------------------
# Deterministic fake embedded Triton, shared by both flows
# ---------------------------------------------------------------------------


def deterministic_inference(model, payload_digest):
    """A pure function of (model, request payload): the same request
    always yields the same result — any cross-talk between the two
    flows therefore produces results that differ from baseline."""
    digest = hashlib.sha256(
        "{0}:{1}".format(model, payload_digest).encode("utf-8")
    ).hexdigest()
    confidence = int(digest[:8], 16) / float(0xFFFFFFFF)
    return {
        "model": model,
        "is_anomalous": confidence >= 0.5,
        "confidence": round(confidence, 6),
    }


class FakeEmbeddedTriton:
    """One thread-safe model server standing in for the embedded Triton
    that serves BOTH the workflow path and the Pipeline_Configuration
    path on a device.

    When a rendezvous barrier is armed, the first inference of each flow
    waits on it — proof the flows were in-flight against the same model
    at the same time. A timed-out barrier raises and fails the run.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self.requests = []
        self._rendezvous = None
        self._met = set()

    def arm_rendezvous(self, parties):
        self._rendezvous = threading.Barrier(parties)
        self._met = set()

    def disarm_rendezvous(self):
        self._rendezvous = None

    def infer(self, flow, model, payload_digest):
        rendezvous = self._rendezvous
        if rendezvous is not None:
            with self._lock:
                first_for_flow = flow not in self._met
                if first_for_flow:
                    self._met.add(flow)
            if first_for_flow:
                rendezvous.wait(timeout=RENDEZVOUS_TIMEOUT_SEC)
        with self._lock:
            self.requests.append(
                {
                    "flow": flow,
                    "model": model,
                    "thread": threading.current_thread().name,
                }
            )
        return deterministic_inference(model, payload_digest)


class TritonBackedPipelineManager:
    """Stands in for GstPipelineManager at the Gst boundary only: parses
    the emltriton model out of the launch string and requests inference
    from the shared fake Triton, matching the ``run_pipeline`` contract
    (returns the parsed tag values)."""

    def __init__(self, triton, flow):
        self._triton = triton
        self._flow = flow
        self.calls = []
        self.env_during_runs = []
        self.run_threads = []

    def run_pipeline(self, pipeline_str, frame_data=None, latency_metrics=None,
                     status_sink=None):
        self.calls.append(pipeline_str)
        self.env_during_runs.append(
            os.environ.get(gst_plugins.GST_PLUGIN_PATH_ENV)
        )
        self.run_threads.append(threading.current_thread())
        match = re.search(r"emltriton model=([\w.\-]+)", pipeline_str)
        assert match, "launch string carries no emltriton model"
        payload_digest = hashlib.sha256(
            "{0}|{1}".format(pipeline_str, frame_data or "").encode("utf-8")
        ).hexdigest()
        return self._triton.infer(self._flow, match.group(1), payload_digest)


class SimulatedPipelineConfiguration:
    """The existing Pipeline_Configuration path at the manager boundary,
    the way ``GstPipelineExecutor`` holds it: one long-lived manager
    instance of its own, never shared with workflow runs."""

    def __init__(self, triton):
        self.manager = TritonBackedPipelineManager(
            triton, flow="pipeline_configuration"
        )

    def run_inference(self, frame_data):
        return self.manager.run_pipeline(
            PIPELINE_CONFIGURATION_LAUNCH, frame_data=frame_data
        )


# ---------------------------------------------------------------------------
# Helpers and fixtures (the real engine, as in the integration suite)
# ---------------------------------------------------------------------------


def wait_for_execution(client, execution_id, timeout=POLL_TIMEOUT_SEC):
    deadline = time.time() + timeout
    while time.time() < deadline:
        response = client.get("/workflows/executions/{0}".format(execution_id))
        assert response.status_code == 200
        body = response.json()
        if body["status"] in (EXECUTION_STATUS_COMPLETED, EXECUTION_STATUS_FAILED):
            return body
        time.sleep(0.02)
    pytest.fail("Timed out waiting for execution {0}".format(execution_id))


def trigger(client, registration_id):
    response = client.post(
        "/workflows/registrations/{0}/trigger".format(registration_id)
    )
    assert response.status_code == 200, response.text
    return response.json()["executionId"]


def join_execution_thread(execution_id, timeout=POLL_TIMEOUT_SEC):
    name = "workflow-execution-{0}".format(execution_id)
    for thread in threading.enumerate():
        if thread.name == name:
            thread.join(timeout)
            assert not thread.is_alive(), (
                "execution thread {0} did not finish".format(name)
            )


@pytest.fixture
def session_factory():
    return make_session_factory()


@pytest.fixture
def workflow_root(tmp_path):
    return str(tmp_path / "aws_dda_workflows")


@pytest.fixture
def watcher(workflow_root, session_factory):
    instance = make_watcher(workflow_root, session_factory, poll_interval=0.2)
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


@pytest.fixture
def no_registry_scan():
    """Record registry scans instead of importing gi on this host."""
    with patch.object(gst_plugins, "_scan_registry", return_value=True) as scan:
        yield scan


def deploy_workflow(workflow_root, workflow_id):
    """One Workflow_Component artifact set carrying the shared-model
    pipeline plus a delivered plugin, so per-run plugin scoping is
    genuinely exercised while the Pipeline_Configuration runs."""
    compiled = {
        "schemaVersion": 1,
        "workflowId": workflow_id,
        "workflowVersion": "1",
        "targetArch": DEVICE_ARCH,
        "segments": WORKFLOW_SEGMENTS,
        "executorBindings": [],
        "pluginDependencies": [],
    }
    artifact_path = write_artifact_set(
        workflow_root, workflow_id=workflow_id, version="1", compiled=compiled
    )
    plugin_dir = os.path.join(artifact_path, "plugins", DEVICE_ARCH)
    os.makedirs(plugin_dir, exist_ok=True)
    with open(os.path.join(plugin_dir, "libdda_extra.so"), "wb") as f:
        f.write(b"\x7fELF-fake-plugin")
    return artifact_path


# ---------------------------------------------------------------------------
# 13.8 — concurrent workflow and Pipeline_Configuration inference against
# the same Triton-served model, compared against sequential baselines
# ---------------------------------------------------------------------------


class TestConcurrentTritonInference:
    def test_concurrent_flows_reproduce_their_sequential_baselines(
        self, workflow_root, watcher, client, session_factory, no_registry_scan
    ):
        artifact_path = deploy_workflow(workflow_root, "wf-shared-model")
        watcher.start()

        triton = FakeEmbeddedTriton()
        pipeline_configuration = SimulatedPipelineConfiguration(triton)

        # The executor builds a FRESH manager per run from this factory,
        # exactly as _default_pipeline_manager_factory does in production.
        workflow_managers = []

        def workflow_manager_factory():
            manager = TritonBackedPipelineManager(triton, flow="workflow")
            workflow_managers.append(manager)
            return manager

        workflow_results = []
        register_workflow_executor(
            session_factory=session_factory,
            pipeline_manager_factory=workflow_manager_factory,
            post_run_handler=(
                lambda registration, document, tags: workflow_results.append(tags)
            ),
        )

        frames = ["frame-{0:02d}".format(i) for i in range(8)]
        env_before = os.environ.get(gst_plugins.GST_PLUGIN_PATH_ENV)

        # --- Baselines: each flow alone, sequentially -----------------
        baseline_execution = trigger(client, "wf-shared-model:1")
        assert (
            wait_for_execution(client, baseline_execution)["status"]
            == EXECUTION_STATUS_COMPLETED
        )
        join_execution_thread(baseline_execution)
        assert len(workflow_results) == 1
        workflow_baseline = workflow_results[0]

        pipeline_baseline = [
            pipeline_configuration.run_inference(frame) for frame in frames
        ]

        # --- Concurrent phase -----------------------------------------
        # The barrier holds each flow's first inference until BOTH flows
        # are inside Triton — overlap is guaranteed, not incidental.
        triton.arm_rendezvous(parties=2)

        pipeline_concurrent = []
        pipeline_errors = []

        def run_pipeline_configuration():
            try:
                for frame in frames:
                    pipeline_concurrent.append(
                        pipeline_configuration.run_inference(frame)
                    )
            except Exception as e:  # noqa: BLE001 - surfaced by the assert below
                pipeline_errors.append(e)

        pipeline_thread = threading.Thread(
            target=run_pipeline_configuration,
            name="pipeline-configuration-inference",
        )

        concurrent_execution = trigger(client, "wf-shared-model:1")
        pipeline_thread.start()
        body = wait_for_execution(client, concurrent_execution)
        pipeline_thread.join(POLL_TIMEOUT_SEC)
        assert not pipeline_thread.is_alive()
        join_execution_thread(concurrent_execution)
        triton.disarm_rendezvous()

        # Both flows completed against the shared model.
        assert pipeline_errors == []
        assert body["status"] == EXECUTION_STATUS_COMPLETED
        assert body["failingNodeId"] is None

        # 13.8 — the Pipeline_Configuration's inference results are
        # unaltered by the concurrent workflow: frame for frame identical
        # to its own sequential baseline.
        assert pipeline_concurrent == pipeline_baseline

        # The workflow's own results are equally undisturbed.
        assert len(workflow_results) == 2
        assert workflow_results[1] == workflow_baseline

        # Both flows really hit the SAME Triton-served model, and the
        # rendezvous proves they were in-flight simultaneously.
        flows_seen = {request["flow"] for request in triton.requests}
        assert flows_seen == {"workflow", "pipeline_configuration"}
        assert all(
            request["model"] == SHARED_MODEL for request in triton.requests
        )

        # No cross-talk through shared managers or threads: each flow's
        # launch strings went only to its own manager instance, workflow
        # runs each got a FRESH instance, and the Pipeline_Configuration
        # manager was never one of them.
        assert pipeline_configuration.manager.calls == (
            [PIPELINE_CONFIGURATION_LAUNCH] * 2 * len(frames)
        )
        assert len(workflow_managers) == 2
        assert all(
            manager is not pipeline_configuration.manager
            for manager in workflow_managers
        )
        for manager in workflow_managers:
            assert manager.calls == [
                "videotestsrc num-buffers=5 ! "
                "emltriton model={0} ! fakesink".format(SHARED_MODEL)
            ]
            # Delivered plugins were scoped to the workflow's own run.
            plugin_dir = os.path.join(artifact_path, "plugins", DEVICE_ARCH)
            assert manager.env_during_runs[0].startswith(plugin_dir)

        # Workflow runs stayed off the Pipeline_Configuration's thread.
        pipeline_threads = set(pipeline_configuration.manager.run_threads)
        for manager in workflow_managers:
            assert not pipeline_threads.intersection(manager.run_threads)

        # And no lasting environment mutation leaked out of either phase.
        assert os.environ.get(gst_plugins.GST_PLUGIN_PATH_ENV) == env_before


# ---------------------------------------------------------------------------
# Real Triton end-to-end — device container only
# ---------------------------------------------------------------------------


@pytest.mark.edge_device
class TestConcurrentTritonOnEdgeDevice:
    def test_concurrent_real_triton_inference_matches_baseline(
        self, workflow_root, watcher, client, session_factory
    ):
        """The same scenario against the REAL embedded Triton: a
        Pipeline_Configuration-shaped launch string through its own
        fresh GstPipelineManager, concurrent with a triggered workflow
        run using the same model, with the Pipeline_Configuration's tag
        values compared against a sequential baseline. Runs only in the
        device container with a deployed Triton model."""
        pytest.importorskip(
            "gi",
            reason="real GStreamer (gi) is only available in the device "
            "container",
        )
        if "INFERENCE_COMPONENT_DECOMPRESED_PATH" not in os.environ:
            pytest.skip(
                "requires the device container environment "
                "(INFERENCE_COMPONENT_DECOMPRESED_PATH locates the bundled "
                "DDA GStreamer plugins, including emltriton)"
            )
        model_name = os.environ.get("DDA_TEST_TRITON_MODEL")
        if not model_name:
            pytest.skip(
                "requires DDA_TEST_TRITON_MODEL naming a model deployed to "
                "the device's embedded Triton server"
            )

        from gstreamer.gst_pipeline import GstPipelineManager

        launch = (
            "videotestsrc num-buffers=5 ! videoconvert ! "
            "emltriton model={0} ! fakesink".format(model_name)
        )

        # Sequential baseline through the Pipeline_Configuration's own
        # manager instance (as GstPipelineExecutor holds one).
        pipeline_manager = GstPipelineManager()
        baseline = pipeline_manager.run_pipeline(launch)

        compiled = {
            "schemaVersion": 1,
            "workflowId": "wf-real-triton",
            "workflowVersion": "1",
            "targetArch": DEVICE_ARCH,
            "segments": [
                {
                    "name": "s0",
                    "elements": [
                        {"nodeId": "cam", "factory": "videotestsrc",
                         "args": {"num-buffers": 5}},
                        {"nodeId": None, "factory": "videoconvert", "args": {}},
                        {"nodeId": "infer", "factory": "emltriton",
                         "args": {"model": model_name}},
                        {"nodeId": None, "factory": "fakesink", "args": {}},
                    ],
                }
            ],
            "executorBindings": [],
            "pluginDependencies": [],
        }
        write_artifact_set(
            workflow_root, workflow_id="wf-real-triton", version="1",
            compiled=compiled,
        )
        watcher.start()
        register_workflow_executor(session_factory=session_factory)

        results = []
        errors = []

        def run_pipeline_configuration():
            try:
                results.append(pipeline_manager.run_pipeline(launch))
            except Exception as e:  # noqa: BLE001 - surfaced below
                errors.append(e)

        pipeline_thread = threading.Thread(target=run_pipeline_configuration)
        execution_id = trigger(client, "wf-real-triton:1")
        pipeline_thread.start()
        body = wait_for_execution(client, execution_id, timeout=60.0)
        pipeline_thread.join(60.0)

        assert not pipeline_thread.is_alive()
        assert errors == []
        assert body["status"] == EXECUTION_STATUS_COMPLETED
        # 13.8 — the Pipeline_Configuration's inference results are the
        # same with and without the concurrent workflow run.
        assert results == [baseline]
