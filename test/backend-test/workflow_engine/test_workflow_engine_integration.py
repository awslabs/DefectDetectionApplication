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
"""Edge integration tests for the workflow engine (task 12.6).

Exercises the full flows end-to-end with real components wherever the
host allows (Requirements 9.1-9.8):

- artifact drop -> real WorkflowWatcher (startup scan + watch thread)
  -> registration surfaced through the real FastAPI /workflows API;
- trigger through the API -> real executor hook (daemon thread) -> real
  WorkflowExecutor rendering and running the compiled document, with a
  scripted manager standing in only at the Gst boundary;
- post-run output bindings through the REAL default clients, with fakes
  standing at the client boundary only (paho module, opcua module,
  utils.dio_utils GPIO functions);
- failure reporting with the failing node identified, surfaced via
  GET /workflows/executions/{id};
- Custom_Python_Node bridge truly end-to-end: real handler subprocesses
  processing real frames through the framed protocol, with a frame pump
  standing in only for the GStreamer appsink/appsrc wiring.

Tests that need real GStreamer/Triton/device hardware are marked
``@pytest.mark.edge_device`` and skip with a clear reason when the real
``gi`` (or Triton) is unavailable, so the suite is ready to run in the
device container unchanged.
"""
import json
import os
import sys
import threading
import time
import types
import unittest.mock
from unittest.mock import patch

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

from utils.constants import GPIO_RISING

from workflow_engine import api as workflow_engine_api
from workflow_engine import executor as executor_hook
from workflow_engine import gst_plugins, runtime
from workflow_engine.models import WorkflowExecution, WorkflowRegistration
from workflow_engine.output_bindings import OutputBindingProcessor
from workflow_engine.pipeline_executor import (
    EXECUTION_STATUS_COMPLETED,
    EXECUTION_STATUS_FAILED,
    WorkflowExecutor,
    register_workflow_executor,
)
from workflow_engine.python_bridge import CustomPythonNodeError

POLL_TIMEOUT_SEC = 10.0
WATCH_POLL_INTERVAL_SEC = 0.2


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def wait_for(predicate, timeout=POLL_TIMEOUT_SEC, interval=0.02, message="condition"):
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


def trigger(client, registration_id):
    response = client.post(
        "/workflows/registrations/{0}/trigger".format(registration_id)
    )
    assert response.status_code == 200, response.text
    return response.json()["executionId"]


def join_execution_thread(execution_id, timeout=POLL_TIMEOUT_SEC):
    """Wait for the run's daemon thread to finish completely.

    The post-run handler (output bindings) runs on that thread AFTER the
    ``completed`` status is committed, so waiting on the execution
    status alone is not enough to observe binding side effects.
    """
    name = "workflow-execution-{0}".format(execution_id)
    for thread in threading.enumerate():
        if thread.name == name:
            thread.join(timeout)
            assert not thread.is_alive(), (
                "execution thread {0} did not finish".format(name)
            )


class ScriptedPipelineManager:
    """Stands in for GstPipelineManager at the Gst boundary only.

    Records the launch string and the GST_PLUGIN_PATH visible during the
    run, then returns scripted tag values (or raises the scripted
    error), exactly matching the ``run_pipeline`` contract.
    """

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


def deploy_artifact_set(
    workflow_root,
    workflow_id,
    compiled,
    version="1",
    manifest=None,
    plugin_files=(),
    handlers=None,
    raw_manifest=None,
    omit=(),
):
    """Write one Workflow_Component artifact set under the watched root.

    ``plugin_files`` creates dummy delivered plugin .so files under
    ``plugins/<arch>/``; ``handlers`` maps nodeId -> handler.py source
    under ``python/<nodeId>/``.
    """
    artifact_path = write_artifact_set(
        workflow_root,
        workflow_id=workflow_id,
        version=version,
        manifest=manifest,
        compiled=compiled,
        raw_manifest=raw_manifest,
        omit=omit,
    )
    if plugin_files:
        plugin_dir = os.path.join(artifact_path, "plugins", DEVICE_ARCH)
        os.makedirs(plugin_dir, exist_ok=True)
        for name in plugin_files:
            with open(os.path.join(plugin_dir, name), "wb") as f:
                f.write(b"\x7fELF-fake-plugin")
    for node_id, source in (handlers or {}).items():
        handler_dir = os.path.join(artifact_path, "python", node_id)
        os.makedirs(handler_dir, exist_ok=True)
        with open(os.path.join(handler_dir, "handler.py"), "w") as f:
            f.write(source)
    return artifact_path


# ---------------------------------------------------------------------------
# Fixtures: real database, real watcher thread, real API app
# ---------------------------------------------------------------------------


@pytest.fixture
def session_factory():
    return make_session_factory()


@pytest.fixture
def workflow_root(tmp_path):
    return str(tmp_path / "aws_dda_workflows")


@pytest.fixture
def watcher(workflow_root, session_factory):
    """A real WorkflowWatcher wired as THE process watcher, so the API's
    invalid-reason reporting flows through the real runtime lookup."""
    instance = make_watcher(
        workflow_root, session_factory, poll_interval=WATCH_POLL_INTERVAL_SEC
    )
    with patch.object(runtime, "_watcher", instance):
        yield instance
    instance.stop()


@pytest.fixture
def client(session_factory):
    """The real workflow engine router served over an in-memory app."""
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
    """Each test registers its own executor; never leak it."""
    yield
    executor_hook.set_executor(None)


@pytest.fixture
def no_registry_scan():
    """For tests scripting the Gst boundary: record registry scans
    instead of importing gi."""
    with patch.object(gst_plugins, "_scan_registry", return_value=True) as scan:
        yield scan


# ---------------------------------------------------------------------------
# Fixtures: fakes standing at the output client boundaries, so the REAL
# default dio/MQTT/OPC UA client code in output_bindings runs.
# ---------------------------------------------------------------------------


@pytest.fixture
def dio_boundary():
    """Fake GPIO functions at the utils.dio_utils boundary; the real
    _default_dio_actuator signal-type/pulse logic runs above them."""
    calls = []
    with patch(
        "utils.dio_utils.set_output_pin",
        side_effect=lambda pin, signal: calls.append(("set", pin, signal)),
    ), patch(
        "utils.dio_utils.reset_output_pin",
        side_effect=lambda pin, signal: calls.append(("reset", pin, signal)),
    ):
        yield calls


@pytest.fixture
def mqtt_boundary():
    """A fake paho.mqtt.publish module (paho is not installed on this
    host); the real _default_mqtt_publisher runs against it."""
    published = []

    def single(topic, payload=None, qos=0, hostname=None, port=1883,
               client_id="", tls=None):
        published.append(
            {
                "topic": topic,
                "payload": payload,
                "qos": qos,
                "hostname": hostname,
                "port": port,
                "client_id": client_id,
                "tls": tls,
            }
        )

    publish_module = types.ModuleType("paho.mqtt.publish")
    publish_module.single = single
    mqtt_module = types.ModuleType("paho.mqtt")
    mqtt_module.publish = publish_module
    paho_module = types.ModuleType("paho")
    paho_module.mqtt = mqtt_module
    with patch.dict(
        sys.modules,
        {
            "paho": paho_module,
            "paho.mqtt": mqtt_module,
            "paho.mqtt.publish": publish_module,
        },
    ):
        yield published


@pytest.fixture
def opcua_boundary():
    """A fake opcua module (the client is delivered as a component
    dependency); the real _default_opcua_writer connect/write/disconnect
    sequence runs against it."""
    events = []

    class FakeNode:
        def __init__(self, endpoint, node_id):
            self._endpoint = endpoint
            self._node_id = node_id

        def set_value(self, value):
            events.append(("write", self._endpoint, self._node_id, value))

    class FakeClient:
        def __init__(self, endpoint):
            self._endpoint = endpoint

        def connect(self):
            events.append(("connect", self._endpoint))

        def get_node(self, node_id):
            return FakeNode(self._endpoint, node_id)

        def disconnect(self):
            events.append(("disconnect", self._endpoint))

    opcua_module = types.ModuleType("opcua")
    opcua_module.Client = FakeClient
    with patch.dict(sys.modules, {"opcua": opcua_module}):
        yield events


# ---------------------------------------------------------------------------
# Compiled documents
# ---------------------------------------------------------------------------


def make_compiled(workflow_id, segments, executor_bindings=(),
                  plugin_dependencies=()):
    return {
        "schemaVersion": 1,
        "workflowId": workflow_id,
        "workflowVersion": "1",
        "targetArch": DEVICE_ARCH,
        "segments": segments,
        "executorBindings": list(executor_bindings),
        "pluginDependencies": list(plugin_dependencies),
    }


#: Camera -> delivered-plugin filter -> Triton inference -> sink.
PLUGIN_AND_TRITON_SEGMENTS = [
    {
        "name": "s0",
        "elements": [
            {"nodeId": "cam", "factory": "videotestsrc",
             "args": {"num-buffers": 5}},
            {"nodeId": "edge", "factory": "emlcustomedge",
             "args": {"threshold": 12}},
            {"nodeId": "infer", "factory": "emltriton",
             "args": {"model": "widget-anomaly-v3"}},
            {"nodeId": None, "factory": "fakesink", "args": {}},
        ],
    }
]

OUTPUT_BINDINGS = [
    {
        "nodeId": "f1",
        "binding": "inference_filter",
        "parameters": {"condition": "is_anomalous == true && confidence >= 0.8"},
    },
    {
        "nodeId": "dio1",
        "binding": "digital_output",
        "upstreamNodeIds": ["f1"],
        "parameters": {
            "pin": 245,
            "signal_type": "pulse",
            "pulse_width_ms": 20,
            "condition": "is_anomalous == true",
        },
    },
    {
        "nodeId": "mqtt1",
        "binding": "mqtt_publish",
        "upstreamNodeIds": ["f1"],
        "parameters": {
            "broker_host": "127.0.0.1",
            "broker_port": 1883,
            "topic": "dda/defects",
            "payload_template": "{inference_json}",
            "qos": 1,
        },
    },
    {
        "nodeId": "opc1",
        "binding": "opcua_write",
        "parameters": {
            "endpoint": "opc.tcp://127.0.0.1:4840/dda/",
            "node_id": "ns=2;s=DefectFlag",
            "value_template": "{is_anomalous}",
        },
    },
]


# ---------------------------------------------------------------------------
# 9.1  Artifact drop -> watcher discovery -> registration via the API
# ---------------------------------------------------------------------------


class TestDiscoveryAndRegistration:
    def test_valid_and_invalid_drops_are_registered_and_listed(
        self, workflow_root, watcher, client
    ):
        # Valid set, plus one invalid for this device (wrong architecture).
        deploy_artifact_set(
            workflow_root, "wf-good",
            make_compiled("wf-good", PLUGIN_AND_TRITON_SEGMENTS),
        )
        wrong_arch = dict(VALID_MANIFEST, workflowId="wf-wrong-arch",
                          targetArch="armv7l")
        deploy_artifact_set(
            workflow_root, "wf-wrong-arch",
            make_compiled("wf-wrong-arch", PLUGIN_AND_TRITON_SEGMENTS),
            manifest=wrong_arch,
        )
        deploy_artifact_set(
            workflow_root, "wf-broken",
            make_compiled("wf-broken", PLUGIN_AND_TRITON_SEGMENTS),
            raw_manifest="{not json",
        )

        watcher.start()  # startup scan + watch thread, as at LocalServer boot

        response = client.get("/workflows/registrations")
        assert response.status_code == 200
        registrations = {r["registrationId"]: r for r in response.json()}
        assert set(registrations) == {
            "wf-good:1", "wf-wrong-arch:1", "wf-broken:1",
        }

        good = registrations["wf-good:1"]
        assert good["status"] == "registered"
        assert good["arch"] == DEVICE_ARCH
        assert "invalidReason" not in good

        wrong = registrations["wf-wrong-arch:1"]
        assert wrong["status"] == "invalid"
        assert "does not match this device's architecture" in wrong["invalidReason"]

        broken = registrations["wf-broken:1"]
        assert broken["status"] == "invalid"
        assert "Malformed manifest.json" in broken["invalidReason"]

    def test_artifact_dropped_after_startup_is_discovered_by_the_watch_thread(
        self, workflow_root, watcher, client
    ):
        watcher.start()
        assert client.get("/workflows/registrations").json() == []

        # Greengrass delivers a component while LocalServer is running.
        deploy_artifact_set(
            workflow_root, "wf-late",
            make_compiled("wf-late", PLUGIN_AND_TRITON_SEGMENTS),
        )

        def registered():
            body = client.get("/workflows/registrations").json()
            return body if body else None

        body = wait_for(
            registered, message="the watch thread to register the late drop"
        )
        assert body[0]["registrationId"] == "wf-late:1"
        assert body[0]["status"] == "registered"

    def test_invalid_registration_can_never_be_triggered(
        self, workflow_root, watcher, client
    ):
        wrong_arch = dict(VALID_MANIFEST, workflowId="wf-bad",
                          targetArch="armv7l")
        deploy_artifact_set(
            workflow_root, "wf-bad",
            make_compiled("wf-bad", PLUGIN_AND_TRITON_SEGMENTS),
            manifest=wrong_arch,
        )
        watcher.start()

        response = client.post("/workflows/registrations/wf-bad:1/trigger")

        assert response.status_code == 409
        assert "cannot be run" in response.json()["detail"]
        assert "architecture" in response.json()["detail"]


# ---------------------------------------------------------------------------
# 9.2 / 9.3  Trigger -> executor renders and runs the launch string with
# delivered plugins scoped to the run and emltriton carrying the model
# ---------------------------------------------------------------------------


class TestTriggeredExecution:
    def test_trigger_runs_rendered_pipeline_with_plugins_and_triton_model(
        self, workflow_root, watcher, client, session_factory, no_registry_scan
    ):
        artifact_path = deploy_artifact_set(
            workflow_root, "wf-run",
            make_compiled(
                "wf-run", PLUGIN_AND_TRITON_SEGMENTS,
                plugin_dependencies=["dda-emlcustomedge"],
            ),
            plugin_files=("libdda_emlcustomedge.so",),
        )
        watcher.start()
        manager = ScriptedPipelineManager(
            tag_values={"is_anomalous": True, "confidence": 0.93}
        )
        register_workflow_executor(
            session_factory=session_factory,
            pipeline_manager_factory=lambda: manager,
        )

        execution_id = trigger(client, "wf-run:1")
        body = wait_for_execution(client, execution_id)

        assert body["status"] == EXECUTION_STATUS_COMPLETED
        assert body["failingNodeId"] is None
        assert body["error"] is None

        # The launch string is the rendered compiled document: the
        # delivered-plugin element and emltriton with the node's model.
        assert manager.calls == [
            "videotestsrc num-buffers=5 ! emlcustomedge threshold=12 ! "
            "emltriton model=widget-anomaly-v3 ! fakesink"
        ]

        # Delivered plugins were scoped to the run (9.2): the component
        # plugin directory led GST_PLUGIN_PATH during the run, the
        # registry scan targeted it, and nothing leaked afterwards.
        plugin_dir = os.path.join(artifact_path, "plugins", DEVICE_ARCH)
        assert manager.env_during_run.startswith(plugin_dir)
        no_registry_scan.assert_called_once_with(plugin_dir)
        assert os.environ.get(gst_plugins.GST_PLUGIN_PATH_ENV) != plugin_dir

        # And never on the API thread (13.4).
        assert manager.run_thread is not threading.current_thread()

    def test_execution_status_visible_through_registration_detail(
        self, workflow_root, watcher, client, session_factory, no_registry_scan
    ):
        deploy_artifact_set(
            workflow_root, "wf-status",
            make_compiled("wf-status", PLUGIN_AND_TRITON_SEGMENTS),
        )
        watcher.start()
        register_workflow_executor(
            session_factory=session_factory,
            pipeline_manager_factory=ScriptedPipelineManager,
        )

        execution_id = trigger(client, "wf-status:1")
        wait_for_execution(client, execution_id)

        detail = client.get("/workflows/registrations/wf-status:1").json()
        assert [e["executionId"] for e in detail["executions"]] == [execution_id]
        assert detail["executions"][0]["status"] == EXECUTION_STATUS_COMPLETED


# ---------------------------------------------------------------------------
# 9.4 / 9.5 / 9.6  Output bindings end-to-end: real OutputBindingProcessor
# as the executor's post-run handler, real default clients, fakes standing
# only at the GPIO/paho/opcua boundaries
# ---------------------------------------------------------------------------


class TestOutputBindingsEndToEnd:
    def _deploy_and_register(
        self, workflow_root, watcher, session_factory,
        executor_bindings, tag_values, workflow_id,
    ):
        deploy_artifact_set(
            workflow_root, workflow_id,
            make_compiled(
                workflow_id, PLUGIN_AND_TRITON_SEGMENTS,
                executor_bindings=executor_bindings,
            ),
        )
        watcher.start()
        register_workflow_executor(
            session_factory=session_factory,
            pipeline_manager_factory=lambda: ScriptedPipelineManager(
                tag_values=tag_values
            ),
            post_run_handler=OutputBindingProcessor(),
        )

    def test_anomalous_run_drives_dio_mqtt_and_opcua(
        self, workflow_root, watcher, client, session_factory,
        no_registry_scan, dio_boundary, mqtt_boundary, opcua_boundary,
    ):
        self._deploy_and_register(
            workflow_root, watcher, session_factory,
            executor_bindings=OUTPUT_BINDINGS,
            tag_values={"is_anomalous": True, "confidence": 0.93},
            workflow_id="wf-outputs",
        )

        execution_id = trigger(client, "wf-outputs:1")
        body = wait_for_execution(client, execution_id)
        join_execution_thread(execution_id)

        assert body["status"] == EXECUTION_STATUS_COMPLETED

        # digital_output (9.4): the real _default_dio_actuator drove the
        # configured pin through utils.dio_utils as a pulse — set high,
        # hold the pulse width, reset.
        assert dio_boundary == [
            ("set", 245, GPIO_RISING),
            ("reset", 245, GPIO_RISING),
        ]

        # mqtt_publish (9.5): the real _default_mqtt_publisher published
        # the rendered {inference_json} payload to the configured
        # broker/topic/qos through the paho client.
        assert len(mqtt_boundary) == 1
        message = mqtt_boundary[0]
        assert message["topic"] == "dda/defects"
        assert message["qos"] == 1
        assert message["hostname"] == "127.0.0.1"
        assert message["port"] == 1883
        assert json.loads(message["payload"]) == {
            "is_anomalous": True, "confidence": 0.93,
        }

        # opcua_write (9.6): the real _default_opcua_writer connected,
        # wrote the rendered value (native bool, single placeholder),
        # and disconnected.
        endpoint = "opc.tcp://127.0.0.1:4840/dda/"
        assert opcua_boundary == [
            ("connect", endpoint),
            ("write", endpoint, "ns=2;s=DefectFlag", True),
            ("disconnect", endpoint),
        ]

    def test_normal_run_gated_by_filter_and_condition_actuates_nothing(
        self, workflow_root, watcher, client, session_factory,
        no_registry_scan, dio_boundary, mqtt_boundary, opcua_boundary,
    ):
        # Same document, but every output hangs downstream of the
        # inference filter (opc1 included) — a normal frame passes
        # nothing: the filter gates dio/mqtt/opcua and dio's own
        # condition is false too.
        gated = [dict(binding) for binding in OUTPUT_BINDINGS]
        gated[3]["upstreamNodeIds"] = ["f1"]
        self._deploy_and_register(
            workflow_root, watcher, session_factory,
            executor_bindings=gated,
            tag_values={"is_anomalous": False, "confidence": 0.2},
            workflow_id="wf-quiet",
        )

        execution_id = trigger(client, "wf-quiet:1")
        body = wait_for_execution(client, execution_id)
        join_execution_thread(execution_id)

        # The run itself completed; the bindings simply did not fire.
        assert body["status"] == EXECUTION_STATUS_COMPLETED
        assert dio_boundary == []
        assert mqtt_boundary == []
        assert opcua_boundary == []


# ---------------------------------------------------------------------------
# 9.7  Failure reporting with the failing node identified, surfaced via
# GET /workflows/executions/{id}
# ---------------------------------------------------------------------------


class TestFailureReporting:
    def _run_failing(
        self, workflow_root, watcher, client, session_factory,
        error_text, workflow_id,
    ):
        deploy_artifact_set(
            workflow_root, workflow_id,
            make_compiled(workflow_id, PLUGIN_AND_TRITON_SEGMENTS),
        )
        watcher.start()
        register_workflow_executor(
            session_factory=session_factory,
            pipeline_manager_factory=lambda: ScriptedPipelineManager(
                error=RuntimeError(error_text)
            ),
        )
        execution_id = trigger(client, "{0}:1".format(workflow_id))
        return wait_for_execution(client, execution_id)

    def test_gst_debug_path_error_maps_to_the_workflow_node(
        self, workflow_root, watcher, client, session_factory, no_registry_scan
    ):
        # GstPipelineManager folds the bus error + debug text into the
        # exception; the debug text names the failing element by its
        # pipeline object path. emltriton0 is the third element of the
        # document -> node "infer".
        error_text = (
            "Pipeline failed with: Internal data stream error. "
            "gstbasesrc.c(3072): gst_base_src_loop (): "
            "/GstPipeline:pipeline0/GstEmltriton:emltriton0:\n"
            "streaming stopped, reason error (-5)"
        )

        body = self._run_failing(
            workflow_root, watcher, client, session_factory,
            error_text, "wf-fail-path",
        )

        assert body["status"] == EXECUTION_STATUS_FAILED
        assert body["failingNodeId"] == "infer"
        assert "Internal data stream error" in body["error"]

    def test_element_name_token_in_the_error_maps_to_the_workflow_node(
        self, workflow_root, watcher, client, session_factory, no_registry_scan
    ):
        # Errors without the object path (e.g. parse/link failures) are
        # mapped by scanning for a known element name: emlcustomedge0 is
        # the delivered-plugin element -> node "edge".
        body = self._run_failing(
            workflow_root, watcher, client, session_factory,
            "Pipeline failed with: could not link emlcustomedge0 to "
            "emltriton0",
            "wf-fail-token",
        )

        assert body["status"] == EXECUTION_STATUS_FAILED
        assert body["failingNodeId"] == "edge"
        assert "could not link" in body["error"]


# ---------------------------------------------------------------------------
# 9.8  Custom Python bridge end-to-end: REAL handler subprocesses, real
# framed protocol; a frame pump stands in only for the appsink/appsrc wiring
# ---------------------------------------------------------------------------


PYTHON_NODE_SEGMENTS = [
    {
        "name": "s0",
        "elements": [
            {"nodeId": "cam", "factory": "videotestsrc",
             "args": {"num-buffers": 2}},
            {"nodeId": "py1", "factory": "emlpython",
             "args": {"handler-path": "python/py1/handler.py"}},
            {"nodeId": None, "factory": "fakesink", "args": {}},
        ],
    }
]

UPPERCASE_HANDLER = """\
def handle(frame_bytes, metadata):
    return frame_bytes.upper(), {"transformed": True}
"""

RAISING_HANDLER = """\
def handle(frame_bytes, metadata):
    raise ValueError("bad frame math")
"""


class TestCustomPythonBridgeEndToEnd:
    def _register_with_frame_pump(self, session_factory, frame_pump):
        """A real WorkflowExecutor whose bridged runner is the pump —
        the pump receives the REAL CustomPythonBridge subprocesses the
        executor built from the component artifacts."""
        instance = WorkflowExecutor(
            session_factory=session_factory,
            bridged_pipeline_runner=frame_pump,
        )
        executor_hook.set_executor(instance.execute)

    def test_handler_subprocess_transforms_frames_end_to_end(
        self, workflow_root, watcher, client, session_factory
    ):
        deploy_artifact_set(
            workflow_root, "wf-python",
            make_compiled("wf-python", PYTHON_NODE_SEGMENTS),
            handlers={"py1": UPPERCASE_HANDLER},
        )
        watcher.start()

        pumped = {"launch": None, "frames": []}

        def frame_pump(launch_string, bridges, latency_metrics=None):
            pumped["launch"] = launch_string
            # Pump real bytes through the node's REAL subprocess via the
            # framed protocol, exactly as the appsink callback would.
            for bridge in bridges:
                for frame in (b"frame-one", b"frame-two"):
                    out_frame, out_meta = bridge.process_frame(
                        frame, metadata={}, width=9, height=1,
                        frame_format="GRAY8",
                    )
                    pumped["frames"].append(
                        (bridge.node_id, out_frame, out_meta)
                    )
            return {"is_anomalous": False, "confidence": 0.1}

        self._register_with_frame_pump(session_factory, frame_pump)

        execution_id = trigger(client, "wf-python:1")
        body = wait_for_execution(client, execution_id)

        assert body["status"] == EXECUTION_STATUS_COMPLETED
        assert body["failingNodeId"] is None

        # The rendered string carries the executor-managed bridge pair in
        # place of emlpython, keeping the Gst.parse_launch dialect.
        assert "emlpython" not in pumped["launch"]
        assert "appsink name=py_in_py1" in pumped["launch"]
        assert "appsrc name=py_out_py1" in pumped["launch"]

        # The REAL handler subprocess transformed the real frame bytes.
        assert pumped["frames"] == [
            ("py1", b"FRAME-ONE", {"transformed": True}),
            ("py1", b"FRAME-TWO", {"transformed": True}),
        ]

    def test_failing_handler_fails_the_run_with_the_custom_node_identified(
        self, workflow_root, watcher, client, session_factory
    ):
        deploy_artifact_set(
            workflow_root, "wf-python-bad",
            make_compiled("wf-python-bad", PYTHON_NODE_SEGMENTS),
            handlers={"py1": RAISING_HANDLER},
        )
        watcher.start()

        def frame_pump(launch_string, bridges, latency_metrics=None):
            for bridge in bridges:
                bridge.process_frame(b"frame-one")
            return {}

        self._register_with_frame_pump(session_factory, frame_pump)

        execution_id = trigger(client, "wf-python-bad:1")
        body = wait_for_execution(client, execution_id)

        # The handler exception crossed the framed protocol as a
        # CustomPythonNodeError carrying the node id (9.8) and is
        # surfaced through the status endpoint (9.7).
        assert body["status"] == EXECUTION_STATUS_FAILED
        assert body["failingNodeId"] == "py1"
        assert "bad frame math" in body["error"]
        assert "py1" in body["error"]


# ---------------------------------------------------------------------------
# Real GStreamer path — device container only
# ---------------------------------------------------------------------------


@pytest.mark.edge_device
class TestOnEdgeDevice:
    def test_triggered_run_through_the_real_gst_pipeline_manager(
        self, workflow_root, watcher, client, session_factory
    ):
        """The same trigger flow through the REAL GstPipelineManager
        (default factory): videotestsrc -> fakesink must reach EOS and
        complete. Runs only where gi/GStreamer are installed (the device
        container); Triton/DDA-plugin coverage additionally needs the
        deployed component and models."""
        pytest.importorskip(
            "gi",
            reason="real GStreamer (gi) is only available in the device "
            "container",
        )
        if "INFERENCE_COMPONENT_DECOMPRESED_PATH" not in os.environ:
            pytest.skip(
                "requires the device container environment "
                "(INFERENCE_COMPONENT_DECOMPRESED_PATH locates the bundled "
                "DDA GStreamer plugins GstPipelineManager loads per run)"
            )
        deploy_artifact_set(
            workflow_root, "wf-real-gst",
            make_compiled("wf-real-gst", [
                {
                    "name": "s0",
                    "elements": [
                        {"nodeId": "cam", "factory": "videotestsrc",
                         "args": {"num-buffers": 1}},
                        {"nodeId": None, "factory": "fakesink", "args": {}},
                    ],
                }
            ]),
        )
        watcher.start()
        register_workflow_executor(session_factory=session_factory)

        execution_id = trigger(client, "wf-real-gst:1")
        body = wait_for_execution(client, execution_id, timeout=30.0)

        assert body["status"] == EXECUTION_STATUS_COMPLETED
        assert body["failingNodeId"] is None
