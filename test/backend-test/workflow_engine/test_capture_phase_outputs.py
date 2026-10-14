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
"""Capture-phase outputs (capture-phase-outputs).

An ``mqtt_publish`` / ``modbus_write`` binding whose ``phase`` parameter
is ``"capture"`` fires right after the run's camera frame is grabbed —
before the pipeline / model / Bedrock steps — so a cell controller can be
released the moment the picture is taken. This module pins:

- the planner helpers (``is_capture_phase_binding`` /
  ``capture_phase_binding_ids``) and that ``bedrock_branches`` never
  makes a capture-phase output a branch member;
- the executor ordering: a capture-phase publish happens BEFORE
  ``run_pipeline`` and renders against the capture-time metadata
  (``trigger``, ``capture_id``, ``timestamp`` ...), while the
  completion-phase outputs still run after the pipeline against the
  run metadata, and the capture-phase id is NOT run a second time;
- containment: a failing capture-phase output marks its node ``failure``
  in the run's node status and the run still completes;
- the legacy plain-callable handler receives a document without the
  capture-phase bindings (never publishes them twice);
- the pre-feature path: a document without ``phase: capture`` makes no
  early call at all.
"""
import json
import time
from unittest.mock import patch

import pytest

from workflow_engine_test_utils import (
    DEVICE_ARCH,
    make_session_factory,
    write_artifact_set,
)

from workflow_engine import branching, gst_plugins
from workflow_engine.models import WorkflowExecution, WorkflowRegistration
from workflow_engine.output_bindings import OutputBindingProcessor
from workflow_engine.pipeline_executor import (
    EXECUTION_STATUS_COMPLETED,
    EXECUTION_STATUS_PENDING,
    WorkflowExecutor,
)

REGISTRATION_ID = "wf-1:3"
CAMERA_ID = "Aravis-Fake-GV01"


# ---------------------------------------------------------------------------
# Document builders
# ---------------------------------------------------------------------------

def mqtt_binding(node_id, phase=None, topic=None, payload_template=None,
                 upstream=("n1",)):
    parameters = {
        "broker_host": "broker.local",
        "broker_port": 1883,
        "topic": topic or "t/{0}".format(node_id),
        "qos": 0,
    }
    if payload_template is not None:
        parameters["payload_template"] = payload_template
    if phase is not None:
        parameters["phase"] = phase
    return {
        "nodeId": node_id,
        "binding": "mqtt_publish",
        "parameters": parameters,
        "upstreamNodeIds": list(upstream),
        "downstreamNodeIds": [],
    }


def modbus_binding(node_id, phase=None, value_template="true",
                   upstream=("n1",)):
    parameters = {
        "host": "192.168.1.30", "port": 502, "unit_id": 1,
        "register_type": "coil", "address": 7, "pulse_ms": 0,
        "value_template": value_template,
    }
    if phase is not None:
        parameters["phase"] = phase
    return {
        "nodeId": node_id,
        "binding": "modbus_write",
        "parameters": parameters,
        "upstreamNodeIds": list(upstream),
        "downstreamNodeIds": [],
    }


def make_aravis_document(*bindings):
    """A compiled document with one Aravis camera source (appsrc chain +
    aravisBinding point) and the given executor bindings."""
    return {
        "schemaVersion": 1,
        "workflowId": "wf-1",
        "workflowVersion": "3",
        "targetArch": DEVICE_ARCH,
        "segments": [
            {
                "name": "s0",
                "elements": [
                    {"nodeId": "n1", "factory": "appsrc",
                     "args": {"name": "appsrc_n1"}},
                    {"nodeId": "n1", "factory": "videoconvert", "args": {}},
                    {"nodeId": None, "factory": "fakesink", "args": {}},
                ],
            }
        ],
        "bindingPoints": [
            {
                "nodeId": "n1",
                "nodeType": "aravis_camera_source",
                "parameters": {"camera_id": CAMERA_ID, "gain": 4,
                               "exposure": 5000000},
                "slots": [],
                "aravisBinding": True,
            }
        ],
        "executorBindings": list(bindings),
        "pluginDependencies": [],
    }


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class EventLog:
    """One shared, ordered record of everything that happened in a run."""

    def __init__(self):
        self.events = []

    def kinds(self):
        return [event[0] for event in self.events]


class FakePipelineManager:
    def __init__(self, log, tag_values=None):
        self.log = log
        self.tag_values = tag_values or {"is_anomalous": True,
                                         "confidence": 0.9}

    def run_pipeline(self, pipeline_str, *args, **kwargs):
        self.log.events.append(("pipeline", pipeline_str))
        return dict(self.tag_values)


class FakeCameraManager:
    def __init__(self, log):
        self.log = log

    def __call__(self, camera_id, config):
        self.log.events.append(("grab", camera_id))
        return {"data": b"\x00" * 8, "width": 4, "height": 2}


def recording_mqtt(log, fail_topics=()):
    def publish(host, port, topic, payload, qos, *args, **kwargs):
        log.events.append(("mqtt", topic, payload))
        if topic in fail_topics:
            raise RuntimeError("broker unreachable for {0}".format(topic))
    return publish


def recording_modbus(log):
    def write(host, port, unit_id, function_code, address, value, *args,
              **kwargs):
        log.events.append(("modbus", address, value))
    return write


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def session_factory():
    return make_session_factory()


@pytest.fixture(autouse=True)
def no_registry_scan():
    with patch.object(gst_plugins, "_scan_registry", return_value=True):
        yield


def seed_run(session_factory, artifact_path, trigger_context=None):
    session = session_factory()
    try:
        session.add(
            WorkflowRegistration(
                id=REGISTRATION_ID, workflow_id="wf-1", version="3",
                arch=DEVICE_ARCH, artifact_path=str(artifact_path),
                status="registered", registered_at=int(time.time()),
            )
        )
        execution = WorkflowExecution(
            id="exec-1", registration_id=REGISTRATION_ID,
            started_at=int(time.time()), status=EXECUTION_STATUS_PENDING,
        )
        if trigger_context is not None:
            execution.trigger_context_json = json.dumps(trigger_context)
        session.add(execution)
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


def run_document(tmp_path, session_factory, document, log, handler,
                 trigger_context=None):
    artifact_path = write_artifact_set(tmp_path, compiled=document)
    execution_id = seed_run(session_factory, artifact_path, trigger_context)
    executor = WorkflowExecutor(
        session_factory=session_factory,
        pipeline_manager_factory=lambda: FakePipelineManager(log),
        frame_grabber=FakeCameraManager(log),
        post_run_handler=handler,
    )
    executor.execute(execution_id)
    return get_execution(session_factory, execution_id)


# ---------------------------------------------------------------------------
# Planner helpers
# ---------------------------------------------------------------------------

class TestPlannerHelpers:
    def test_only_capture_phase_mqtt_and_modbus_bindings_qualify(self):
        assert branching.is_capture_phase_binding(
            mqtt_binding("m", phase="capture"))
        assert branching.is_capture_phase_binding(
            modbus_binding("w", phase="capture"))
        # Default / explicit completion / no parameters / other kinds.
        assert not branching.is_capture_phase_binding(mqtt_binding("m"))
        assert not branching.is_capture_phase_binding(
            mqtt_binding("m", phase="completion"))
        assert not branching.is_capture_phase_binding(
            {"nodeId": "x", "binding": "mqtt_publish", "parameters": None})
        assert not branching.is_capture_phase_binding(
            {"nodeId": "o", "binding": "opcua_write",
             "parameters": {"phase": "capture"}})
        assert not branching.is_capture_phase_binding(None)
        assert not branching.is_capture_phase_binding("mqtt_publish")

    def test_capture_phase_binding_ids_keep_emission_order(self):
        document = {"executorBindings": [
            mqtt_binding("late"),
            modbus_binding("w", phase="capture"),
            mqtt_binding("early", phase="capture"),
        ]}
        assert branching.capture_phase_binding_ids(document) == ["w", "early"]
        assert branching.capture_phase_binding_ids({}) == []
        assert branching.capture_phase_binding_ids(None) == []

    def test_capture_phase_output_never_joins_a_bedrock_branch(self):
        document = {"executorBindings": [
            {"nodeId": "b1", "binding": "bedrock_inference",
             "parameters": {}, "upstreamNodeIds": ["n1"],
             "downstreamNodeIds": ["m_done", "m_cap"]},
            mqtt_binding("m_done", upstream=("b1",)),
            mqtt_binding("m_cap", phase="capture", upstream=("b1",)),
        ]}
        plans = branching.bedrock_branches(document)
        assert plans["b1"].binding_ids == ["m_done"]


# ---------------------------------------------------------------------------
# Executor behaviour
# ---------------------------------------------------------------------------

class TestExecutorOrdering:
    def test_capture_phase_publish_happens_before_the_pipeline(
        self, tmp_path, session_factory
    ):
        log = EventLog()
        handler = OutputBindingProcessor(
            mqtt_publisher=recording_mqtt(log),
            modbus_writer=recording_modbus(log),
        )
        trigger = {"topic": "quality/invoke", "payload": '{"refs": []}',
                   "qos": 0, "timestamp": 1700000000.25}
        document = make_aravis_document(
            # Literal JSON braces are doubled in a payload_template, exactly
            # as the designer's completion-phase templates do today.
            mqtt_binding("released", phase="capture", topic="cell/released",
                         payload_template='{{"captured":"{capture_id}",'
                                          '"at":"{trigger.timestamp}"}}'),
            modbus_binding("coil", phase="capture", value_template="true"),
            mqtt_binding("verdict", topic="cell/verdict",
                         payload_template="{inference_json}"),
        )

        execution = run_document(
            tmp_path, session_factory, document, log, handler, trigger)

        assert execution.status == EXECUTION_STATUS_COMPLETED
        kinds = log.kinds()
        # grab -> capture-phase outputs -> pipeline -> completion outputs
        assert kinds.index("grab") < kinds.index("mqtt")
        first_mqtt = kinds.index("mqtt")
        assert first_mqtt < kinds.index("pipeline")
        assert kinds.index("modbus") < kinds.index("pipeline")
        mqtt_events = [e for e in log.events if e[0] == "mqtt"]
        assert [e[1] for e in mqtt_events] == ["cell/released", "cell/verdict"]
        released = json.loads(mqtt_events[0][2])
        assert released == {"captured": "wf-1-exec-1", "at": "1700000000.25"}
        # The completion-phase output saw the run metadata, not the
        # capture map; the capture-phase output ran exactly once.
        verdict = json.loads(mqtt_events[1][2])
        assert verdict["is_anomalous"] is True
        # load_trigger_context adds the parsed payload alongside the raw one.
        assert verdict["trigger"] == dict(trigger, payload_json={"refs": []})
        assert [e for e in log.events if e[0] == "modbus"] == [
            ("modbus", 7, True)]

    def test_capture_metadata_carries_run_identifiers(
        self, tmp_path, session_factory
    ):
        log = EventLog()
        handler = OutputBindingProcessor(mqtt_publisher=recording_mqtt(log))
        document = make_aravis_document(
            mqtt_binding("released", phase="capture", topic="cell/released"),
        )
        before = time.time()
        execution = run_document(
            tmp_path, session_factory, document, log, handler)

        assert execution.status == EXECUTION_STATUS_COMPLETED
        payload = json.loads(
            [e for e in log.events if e[0] == "mqtt"][0][2])
        assert payload["capture_id"] == "wf-1-exec-1"
        assert payload["execution_id"] == "exec-1"
        assert payload["workflow_id"] == "wf-1"
        # process_subset normalises numeric strings (the shared _coerce), so
        # the registration's "3" renders as 3 — same as every tag value.
        assert str(payload["workflow_version"]) == "3"
        assert payload["phase"] == "capture"
        assert payload["trigger"] == {}
        assert before <= payload["timestamp"] <= time.time()
        assert "is_anomalous" not in payload

    def test_capture_phase_nodes_land_in_node_status_as_success(
        self, tmp_path, session_factory
    ):
        log = EventLog()
        handler = OutputBindingProcessor(mqtt_publisher=recording_mqtt(log))
        document = make_aravis_document(
            mqtt_binding("released", phase="capture", topic="cell/released"),
        )
        execution = run_document(
            tmp_path, session_factory, document, log, handler)

        status = json.loads(execution.node_status_json)
        assert status["released"]["status"] == "success"
        assert status["released"]["detail"].startswith(
            "sent to topic 'cell/released'")
        assert "durationMs" in status["released"]


class TestFailureContainment:
    def test_failing_capture_output_marks_its_node_and_the_run_continues(
        self, tmp_path, session_factory
    ):
        log = EventLog()
        handler = OutputBindingProcessor(
            mqtt_publisher=recording_mqtt(log, fail_topics={"cell/released"}))
        document = make_aravis_document(
            mqtt_binding("released", phase="capture", topic="cell/released"),
            mqtt_binding("verdict", topic="cell/verdict"),
        )
        execution = run_document(
            tmp_path, session_factory, document, log, handler)

        # The inspection still ran and its completion output still went out.
        assert execution.status == EXECUTION_STATUS_COMPLETED
        assert "pipeline" in log.kinds()
        assert [e[1] for e in log.events if e[0] == "mqtt"] == [
            "cell/released", "cell/verdict"]
        status = json.loads(execution.node_status_json)
        assert status["released"]["status"] == "failure"
        assert "broker unreachable" in status["released"]["detail"]
        assert status["verdict"]["status"] == "success"


class TestHandlerCompatibility:
    def test_legacy_callable_handler_never_sees_capture_phase_bindings(
        self, tmp_path, session_factory
    ):
        log = EventLog()
        seen = []

        def legacy_handler(registration, document, tag_values):
            seen.append([b["nodeId"] for b in document["executorBindings"]])

        document = make_aravis_document(
            mqtt_binding("released", phase="capture", topic="cell/released"),
            mqtt_binding("verdict", topic="cell/verdict"),
        )
        execution = run_document(
            tmp_path, session_factory, document, log, legacy_handler)

        assert execution.status == EXECUTION_STATUS_COMPLETED
        # No process_subset: nothing ran early (the warning path) and the
        # legacy handler gets the FULL document exactly as before.
        assert "mqtt" not in log.kinds()
        assert seen == [["released", "verdict"]]

    def test_document_without_capture_phase_makes_no_early_call(
        self, tmp_path, session_factory
    ):
        log = EventLog()
        handler = OutputBindingProcessor(mqtt_publisher=recording_mqtt(log))
        document = make_aravis_document(
            mqtt_binding("verdict", topic="cell/verdict"),
            mqtt_binding("explicit", phase="completion", topic="cell/x"),
        )
        execution = run_document(
            tmp_path, session_factory, document, log, handler)

        assert execution.status == EXECUTION_STATUS_COMPLETED
        kinds = log.kinds()
        assert kinds.index("pipeline") < kinds.index("mqtt")
        assert [e[1] for e in log.events if e[0] == "mqtt"] == [
            "cell/verdict", "cell/x"]
