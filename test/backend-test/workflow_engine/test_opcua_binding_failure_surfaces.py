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
"""Bug condition exploration test for the opcua-output-node-bugfix spec (Part 2).

The pipeline executor finalizes a run as ``completed`` (commit + log)
BEFORE it invokes the post-run output-binding handler, and
``OutputBindingProcessor.process`` swallows each binding failure inside a
contained ``try/except`` (Requirement 13.7 isolation). As a result a run
whose ``opcua_write`` binding fails -- e.g. because the ``opcua`` package
is missing and ``_default_opcua_writer`` re-raises
``RuntimeError("The 'opcua' Python package is not available ...")`` -- is
reported as a SILENT SUCCESS: terminal status ``completed`` with no
``failing_node_id``.

This test drives ``WorkflowExecutor.execute`` with a stub pipeline
manager and an injected ``opcua_writer`` that raises, then asserts the
expected (fixed) behaviour: the run finalizes ``failed`` with
``failing_node_id == "n4"``. It is scoped to the concrete failing case
and varies the incidental binding layout to show the counterexample
holds regardless of surrounding bindings.

On the UNFIXED tree this test FAILS (the run is ``completed`` /
``failing_node_id is None``) -- the counterexample that confirms Part 2
of the bug. After the fix it encodes the expected behaviour and PASSES.

**Validates: Requirements 1.3, 1.4, 2.3, 2.4**
"""
import time

import pytest

import workflow_engine_test_utils  # noqa: F401 - sets COMPONENT_WORK_PATH

from workflow_engine_test_utils import (
    DEVICE_ARCH,
    make_session_factory,
    write_artifact_set,
)

from workflow_engine.models import WorkflowExecution, WorkflowRegistration
from workflow_engine.output_bindings import OutputBindingProcessor
from workflow_engine.pipeline_executor import (
    EXECUTION_STATUS_COMPLETED,
    EXECUTION_STATUS_FAILED,
    EXECUTION_STATUS_PENDING,
    WorkflowExecutor,
)

# The exact message the real _default_opcua_writer re-raises when the
# opcua package is missing on the device.
MISSING_OPCUA_MESSAGE = (
    "The 'opcua' Python package is not available; it is delivered as a "
    "Workflow_Component dependency"
)

BASE_SEGMENTS = [
    {
        "name": "s0",
        "elements": [
            {"nodeId": "n1", "factory": "videotestsrc",
             "args": {"num-buffers": 1}},
            {"nodeId": None, "factory": "fakesink", "args": {}},
        ],
    }
]

OPCUA_BINDING = {
    "nodeId": "n4",
    "binding": "opcua_write",
    "parameters": {
        "endpoint": "opc.tcp://plc.local:4840",
        "node_id": "ns=2;s=DefectFlag",
        "value_template": "{is_anomalous}",
    },
}

MQTT_BINDING = {
    "nodeId": "m1",
    "binding": "mqtt_publish",
    "parameters": {"broker_host": "broker.local", "topic": "dda/results"},
}

DIO_BINDING = {
    "nodeId": "d1",
    "binding": "digital_output",
    "parameters": {
        "pin": 245,
        "signal_type": "pulse",
        "pulse_width_ms": 20,
        "condition": "is_anomalous == true",
    },
}

# Incidental binding layouts around the failing opcua_write (node "n4"):
# opcua alone, opcua after other outputs, opcua before other outputs.
BINDING_LAYOUTS = {
    "opcua_only": [OPCUA_BINDING],
    "opcua_last": [MQTT_BINDING, DIO_BINDING, OPCUA_BINDING],
    "opcua_first": [OPCUA_BINDING, MQTT_BINDING, DIO_BINDING],
}


def compiled_document(executor_bindings):
    return {
        "schemaVersion": 1,
        "workflowId": "wf-1",
        "workflowVersion": "3",
        "targetArch": DEVICE_ARCH,
        "segments": BASE_SEGMENTS,
        "executorBindings": list(executor_bindings),
        "pluginDependencies": [],
    }


class FakePipelineManager:
    """Stub GstPipelineManager returning scripted tag values."""

    def __init__(self, tag_values=None):
        self.tag_values = tag_values or {}

    def run_pipeline(self, pipeline_str, frame_data=None, latency_metrics=None,
                     status_sink=None):
        return dict(self.tag_values)


def raising_opcua_writer(*args, **kwargs):
    """Simulates the missing-package / write failure at the client
    boundary (matches the real _default_opcua_writer re-raise)."""
    raise RuntimeError(MISSING_OPCUA_MESSAGE)


class Recorder:
    """No-op client that records calls and never raises.

    Injected as the mqtt/dio clients so the ONLY failing binding is the
    opcua_write node. Without this the default mqtt publisher imports
    ``paho`` (absent in the test sandbox), making the incidental mqtt
    binding fail spuriously. Works for both signatures:
    ``mqtt_publisher(host, port, topic, payload_text, qos, ...)`` and
    ``dio_actuator(pin, signal_type, pulse_width_ms)``.
    """

    def __init__(self):
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return None


def seed_run(session_factory, artifact_path):
    """A registered wf-1:3 registration + pending execution ready for
    execute()."""
    session = session_factory()
    try:
        session.add(
            WorkflowRegistration(
                id="wf-1:3",
                workflow_id="wf-1",
                version="3",
                arch=DEVICE_ARCH,
                artifact_path=str(artifact_path),
                status="registered",
                registered_at=int(time.time()),
            )
        )
        session.add(
            WorkflowExecution(
                id="exec-1",
                registration_id="wf-1:3",
                started_at=int(time.time()),
                status=EXECUTION_STATUS_PENDING,
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


@pytest.mark.parametrize("layout", sorted(BINDING_LAYOUTS))
def test_failing_opcua_write_binding_fails_the_run(tmp_path, layout):
    """A run whose ``opcua_write`` binding (node ``n4``) raises MUST
    finalize ``failed`` with ``failing_node_id == "n4"`` -- across any
    incidental binding layout.

    UNFIXED: FAILS -- the run is finalized ``completed`` with
    ``failing_node_id is None`` (the silent-success counterexample).
    """
    session_factory = make_session_factory()
    document = compiled_document(BINDING_LAYOUTS[layout])
    artifact_path = write_artifact_set(tmp_path, compiled=document)
    execution_id = seed_run(session_factory, artifact_path)

    processor = OutputBindingProcessor(
        dio_actuator=Recorder(),
        mqtt_publisher=Recorder(),
        opcua_writer=raising_opcua_writer,
    )
    executor = WorkflowExecutor(
        session_factory=session_factory,
        pipeline_manager_factory=lambda: FakePipelineManager(
            tag_values={"is_anomalous": True, "confidence": 0.9}
        ),
        post_run_handler=processor,
    )

    executor.execute(execution_id)

    row = get_execution(session_factory)
    assert row.status == EXECUTION_STATUS_FAILED, (
        "run with a failing opcua_write binding should finalize FAILED, "
        "got {0!r} (silent success: the output-binding failure was not "
        "surfaced into the terminal status)".format(row.status)
    )
    assert row.failing_node_id == "n4", (
        "expected failing_node_id 'n4' (the opcua_write node), got "
        "{0!r}".format(row.failing_node_id)
    )
    assert row.status != EXECUTION_STATUS_COMPLETED
