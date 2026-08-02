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
"""Preservation property tests for the opcua-output-node-bugfix spec.

**Property 2: Preservation** - Binding isolation and all-success behavior
unchanged.

These tests are written BEFORE the fix and follow the observation-first
methodology: each invariant below was observed on the UNFIXED tree and is
asserted here as the baseline the fix MUST preserve. They MUST PASS on the
current (unfixed) code; the same file is re-run after the fix (task 3.4) to
prove no regression.

Baselines captured (all observed on the UNFIXED code):

1. All-success -> completed: a run where every output binding succeeds
   finalizes ``EXECUTION_STATUS_COMPLETED`` (with no ``failing_node_id``)
   and logs "Workflow execution %s completed". (Preservation 3.2)

2. Requirement 13.7 isolation: with several output bindings and an
   arbitrary failing subset, ``OutputBindingProcessor.process`` still
   invokes EVERY binding's runner (all bindings attempted) and never lets a
   binding failure propagate. (Preservation 3.1)

3. ``_default_opcua_writer`` with a fake ``opcua`` module injected at the
   client boundary performs connect -> set_value -> disconnect, in order.
   (Preservation 3.3)

4. Existing inference failure-surfacing paths are unchanged: a
   ``bedrock_inference`` binding failure finalizes the run ``failed`` with
   its node id, and an ``llm_inference`` binding failure is recorded (not
   raised) so the run still finalizes ``completed``. (Preservation 3.5)

**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5**
"""
import os
import shutil
import sys
import tempfile
import time
import types
from unittest.mock import patch

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

import workflow_engine_test_utils  # noqa: F401 - sets COMPONENT_WORK_PATH

from workflow_engine_test_utils import (
    DEVICE_ARCH,
    make_session_factory,
    write_artifact_set,
)

from workflow_engine import gst_plugins
from workflow_engine.models import WorkflowExecution, WorkflowRegistration
from workflow_engine.output_bindings import (
    BedrockInferenceError,
    BedrockInferenceProcessor,
    LlmInferenceProcessor,
    OutputBindingError,
    OutputBindingProcessor,
    _default_opcua_writer,
)
from workflow_engine.pipeline_executor import (
    EXECUTION_STATUS_COMPLETED,
    EXECUTION_STATUS_FAILED,
    EXECUTION_STATUS_PENDING,
    WorkflowExecutor,
)

METADATA = {"is_anomalous": True, "confidence": 0.9}

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


@pytest.fixture(autouse=True)
def no_registry_scan():
    """No delivered plugins in these documents; record registry scans
    instead of importing gi."""
    with patch.object(gst_plugins, "_scan_registry", return_value=True):
        yield


# ---------------------------------------------------------------------------
# Harness (mirrors test_opcua_binding_failure_surfaces / bedrock tests)
# ---------------------------------------------------------------------------


class FakePipelineManager:
    """Stub GstPipelineManager returning scripted tag values."""

    def __init__(self, tag_values=None):
        self.tag_values = tag_values or {}

    def run_pipeline(self, pipeline_str, frame_data=None, latency_metrics=None,
                     status_sink=None):
        return dict(self.tag_values)


def compiled_document(executor_bindings, segments=None):
    return {
        "schemaVersion": 1,
        "workflowId": "wf-1",
        "workflowVersion": "3",
        "targetArch": DEVICE_ARCH,
        "segments": segments if segments is not None else BASE_SEGMENTS,
        "executorBindings": list(executor_bindings),
        "pluginDependencies": [],
    }


def seed_run(session_factory, artifact_path, execution_id="exec-1"):
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
                id=execution_id,
                registration_id="wf-1:3",
                started_at=int(time.time()),
                status=EXECUTION_STATUS_PENDING,
            )
        )
        session.commit()
    finally:
        session.close()
    return execution_id


def get_execution(session_factory, execution_id="exec-1"):
    session = session_factory()
    try:
        return session.get(WorkflowExecution, execution_id)
    finally:
        session.close()


def document_binding(*bindings):
    """A bare output-binding-only document (no segments needed for the
    processor path)."""
    return {
        "schemaVersion": 1,
        "workflowId": "wf-1",
        "workflowVersion": "3",
        "targetArch": DEVICE_ARCH,
        "segments": [],
        "executorBindings": list(bindings),
        "pluginDependencies": [],
    }


# ---------------------------------------------------------------------------
# Generators: output-binding sets with an arbitrary failing subset
# ---------------------------------------------------------------------------

OUTPUT_KIND = st.sampled_from(
    ["digital_output", "mqtt_publish", "opcua_write"]
)
# (kind, should_fail)
BINDING_SPEC = st.tuples(OUTPUT_KIND, st.booleans())
BINDING_SPEC_LISTS = st.lists(BINDING_SPEC, min_size=1, max_size=6)


def build_bindings(specs):
    """Build executor bindings from ``(kind, should_fail)`` specs.

    Every binding carries a unique, client-visible token so an injected
    per-type client can fail exactly the arbitrary subset marked to fail
    while still recording that it was invoked. Returns
    ``(bindings, failing_tokens)`` where ``failing_tokens`` maps each
    client type to the set of tokens whose call should raise.
    """
    bindings = []
    failing = {"dio": set(), "mqtt": set(), "opcua": set()}
    for index, (kind, should_fail) in enumerate(specs):
        node_id = "b{0}".format(index)
        if kind == "opcua_write":
            token = "ns=2;s={0}".format(node_id)
            bindings.append({
                "nodeId": node_id,
                "binding": "opcua_write",
                "parameters": {
                    "endpoint": "opc.tcp://plc.local:4840",
                    "node_id": token,
                    "value_template": "{is_anomalous}",
                },
            })
            if should_fail:
                failing["opcua"].add(token)
        elif kind == "mqtt_publish":
            token = "topic-{0}".format(node_id)
            bindings.append({
                "nodeId": node_id,
                "binding": "mqtt_publish",
                "parameters": {"broker_host": "broker.local", "topic": token},
            })
            if should_fail:
                failing["mqtt"].add(token)
        else:  # digital_output
            token = 1000 + index
            bindings.append({
                "nodeId": node_id,
                "binding": "digital_output",
                "parameters": {
                    "pin": token,
                    "signal_type": "pulse",
                    "pulse_width_ms": 10,
                    # Fires on the anomalous METADATA used throughout.
                    "condition": "is_anomalous == true",
                },
            })
            if should_fail:
                failing["dio"].add(token)
    return bindings, failing


class TokenRecorder:
    """Injectable output client: records every call and raises for the
    subset of calls whose token (at ``token_index``) is marked failing."""

    def __init__(self, failing_tokens, token_index):
        self.calls = []
        self._failing = failing_tokens
        self._token_index = token_index

    def __call__(self, *args):
        self.calls.append(args)
        if args[self._token_index] in self._failing:
            raise RuntimeError("output boom for {0!r}".format(
                args[self._token_index]))


class Recorder:
    """Injectable client recording calls; never raising."""

    def __init__(self):
        self.calls = []

    def __call__(self, *args):
        self.calls.append(args)


# ---------------------------------------------------------------------------
# 1. All-success -> completed (Preservation 3.2)
# ---------------------------------------------------------------------------


@settings(max_examples=25)
@given(kinds=st.lists(OUTPUT_KIND, min_size=1, max_size=6))
def test_all_success_output_bindings_finalize_completed(kinds):
    """A run where every output binding succeeds finalizes COMPLETED with
    no failing node id -- for any set of succeeding output bindings.

    Baseline (unfixed): PASSES. The fix must keep this behavior.
    """
    specs = [(kind, False) for kind in kinds]
    bindings, _failing = build_bindings(specs)

    # A fresh, self-cleaning artifact root per generated input (hypothesis
    # does not reset function-scoped fixtures between inputs).
    root = tempfile.mkdtemp(prefix="preservation_all_success_")
    try:
        session_factory = make_session_factory()
        document = compiled_document(bindings)
        artifact_path = write_artifact_set(root, compiled=document)
        execution_id = seed_run(session_factory, artifact_path)

        processor = OutputBindingProcessor(
            dio_actuator=Recorder(),
            mqtt_publisher=Recorder(),
            opcua_writer=Recorder(),
        )
        executor = WorkflowExecutor(
            session_factory=session_factory,
            pipeline_manager_factory=lambda: FakePipelineManager(
                tag_values=dict(METADATA)
            ),
            post_run_handler=processor,
        )

        executor.execute(execution_id)

        row = get_execution(session_factory)
        assert row.status == EXECUTION_STATUS_COMPLETED, (
            "all-success run should finalize COMPLETED, got {0!r}".format(
                row.status)
        )
        assert row.failing_node_id is None
    finally:
        shutil.rmtree(root, ignore_errors=True)


class TestAllSuccessLogging:
    """Class-based so the project's ``caplog`` fixture (which binds to
    ``self.caplog``) is available."""

    def test_all_success_run_logs_completed_message(self, tmp_path, caplog):
        # The shared conftest overrides caplog to stash the real fixture on
        # request.cls, so reach it through self.caplog (the injected
        # argument arrives as None).
        """The all-success finalization logs "Workflow execution %s
        completed" (the exact message the fix must not drop on success)."""
        bindings, _failing = build_bindings([
            ("digital_output", False),
            ("mqtt_publish", False),
            ("opcua_write", False),
        ])
        session_factory = make_session_factory()
        document = compiled_document(bindings)
        artifact_path = write_artifact_set(tmp_path, compiled=document)
        execution_id = seed_run(session_factory, artifact_path)

        executor = WorkflowExecutor(
            session_factory=session_factory,
            pipeline_manager_factory=lambda: FakePipelineManager(
                tag_values=dict(METADATA)
            ),
            post_run_handler=OutputBindingProcessor(
                dio_actuator=Recorder(),
                mqtt_publisher=Recorder(),
                opcua_writer=Recorder(),
            ),
        )

        with self.caplog.at_level(
            "INFO", logger="workflow_engine.pipeline_executor"
        ):
            executor.execute(execution_id)

        assert get_execution(session_factory).status == \
            EXECUTION_STATUS_COMPLETED
        assert any(
            "completed" in record.getMessage()
            and execution_id in record.getMessage()
            for record in self.caplog.records
        ), "expected a 'Workflow execution <id> completed' log line"


# ---------------------------------------------------------------------------
# 2. Requirement 13.7 isolation: every binding attempted (Preservation 3.1)
# ---------------------------------------------------------------------------


@settings(max_examples=25)
@given(specs=BINDING_SPEC_LISTS)
def test_every_output_binding_runner_is_invoked_despite_failures(specs):
    """With an arbitrary failing subset, ``process`` still invokes EVERY
    output binding's runner -- Requirement 13.7 isolation is the PRESERVED
    invariant: no binding failure short-circuits the remaining bindings.

    This holds under both the pre-fix contract (process swallowed every
    failure) and the fixed contract (process attempts every binding, then
    raises ``OutputBindingError`` to surface the failure). The isolation
    invariant asserted here — every runner invoked — is identical either
    way; only whether the collected failure is re-raised afterward differs,
    which this test deliberately tolerates.
    """
    bindings, failing = build_bindings(specs)

    dio = TokenRecorder(failing["dio"], token_index=0)      # (pin, sig, width)
    mqtt = TokenRecorder(failing["mqtt"], token_index=2)    # (host, port, topic, ...)
    opcua = TokenRecorder(failing["opcua"], token_index=1)  # (endpoint, node_id, value)

    processor = OutputBindingProcessor(
        dio_actuator=dio,
        mqtt_publisher=mqtt,
        opcua_writer=opcua,
    )

    # Every binding is attempted before any failure is surfaced. Under the
    # fixed contract a failing subset raises OutputBindingError AFTER the
    # loop; the runner-invocation count below is recorded regardless, so the
    # 13.7 isolation invariant is what is actually asserted.
    try:
        processor.process(None, document_binding(*bindings), METADATA)
    except OutputBindingError:
        pass

    attempted = len(dio.calls) + len(mqtt.calls) + len(opcua.calls)
    assert attempted == len(bindings), (
        "every output binding must be attempted (13.7): expected {0} runner "
        "invocations, recorded {1}".format(len(bindings), attempted)
    )


# ---------------------------------------------------------------------------
# 3. _default_opcua_writer connect -> set_value -> disconnect (Preservation 3.3)
# ---------------------------------------------------------------------------


def test_default_opcua_writer_connects_writes_and_disconnects():
    """With a fake ``opcua`` module injected at the client boundary, the
    real ``_default_opcua_writer`` connects, sets the node value, and
    disconnects, in that order (the integration-test writer path)."""
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

    endpoint = "opc.tcp://127.0.0.1:4840/dda/"
    with patch.dict(sys.modules, {"opcua": opcua_module}):
        _default_opcua_writer(endpoint, "ns=2;s=DefectFlag", True)

    assert events == [
        ("connect", endpoint),
        ("write", endpoint, "ns=2;s=DefectFlag", True),
        ("disconnect", endpoint),
    ]


# ---------------------------------------------------------------------------
# 4. Existing inference failure-surfacing paths unchanged (Preservation 3.5)
# ---------------------------------------------------------------------------

JPEG_BYTES_IN = b"\xff\xd8fake-input-jpeg\xff\xd9"
JPEG_BYTES_REF = b"\xff\xd8fake-reference-jpeg\xff\xd9"

BEDROCK_BINDING = {
    "nodeId": "bedrock1",
    "binding": "bedrock_inference",
    "parameters": {
        "model": "us.amazon.nova-lite-v1:0",
        "prompt": "Compare the images.",
        "region": "us-east-1",
        "max_tokens": 256,
    },
    "upstreamNodeIds": ["cam", "ref"],
    "downstreamNodeIds": ["mqtt"],
    "capturePaths": {
        "in": "{work_dir}/bedrock_frame_cam.jpg",
        "reference": "{work_dir}/bedrock_frame_ref.jpg",
    },
}

BEDROCK_SEGMENTS = [
    {
        "name": "s0",
        "elements": [
            {"nodeId": "cam", "factory": "videotestsrc", "args": {}},
            {"nodeId": None, "factory": "jpegenc", "args": {}},
            {"nodeId": None, "factory": "multifilesink",
             "args": {"location": "{work_dir}/bedrock_frame_cam.jpg"}},
        ],
    },
    {
        "name": "s1",
        "elements": [
            {"nodeId": "ref", "factory": "videotestsrc", "args": {}},
            {"nodeId": None, "factory": "jpegenc", "args": {}},
            {"nodeId": None, "factory": "multifilesink",
             "args": {"location": "{work_dir}/bedrock_frame_ref.jpg"}},
        ],
    },
]


class CapturingPipelineManager:
    """Writes the multifilesink capture locations found in the launch
    string, like the real capture sink chains would."""

    def __init__(self, tag_values=None):
        self.tag_values = tag_values or {}

    def run_pipeline(self, pipeline_str, frame_data=None, latency_metrics=None,
                     status_sink=None):
        for token in pipeline_str.split():
            if token.startswith("location=") and token.endswith(".jpg"):
                path = token[len("location="):]
                payload = (JPEG_BYTES_REF if "bedrock_frame_ref" in path
                           else JPEG_BYTES_IN)
                with open(path, "wb") as f:
                    f.write(payload)
        return dict(self.tag_values)


class RaisingBedrockInvoker:
    def __call__(self, model, prompt, images, region, max_tokens):
        raise RuntimeError("endpoint unreachable")


def test_bedrock_inference_failure_finalizes_failed_with_node_id(tmp_path):
    """A ``bedrock_inference`` binding failure still finalizes the run
    FAILED with its node id -- the existing surfacing path the fix must
    leave intact."""
    document = compiled_document([BEDROCK_BINDING], segments=BEDROCK_SEGMENTS)
    document["pluginDependencies"] = ["python:boto3"]
    session_factory = make_session_factory()
    artifact_path = write_artifact_set(tmp_path, compiled=document)
    execution_id = seed_run(session_factory, artifact_path)

    received = []
    executor = WorkflowExecutor(
        session_factory=session_factory,
        pipeline_manager_factory=lambda: CapturingPipelineManager(
            tag_values={"confidence": 0.1}
        ),
        post_run_handler=lambda reg, doc, tags: received.append(tags),
        bedrock_processor=BedrockInferenceProcessor(
            invoker=RaisingBedrockInvoker()
        ),
    )

    executor.execute(execution_id)

    row = get_execution(session_factory)
    assert row.status == EXECUTION_STATUS_FAILED
    assert row.failing_node_id == "bedrock1"
    assert "endpoint unreachable" in (row.error or "")
    # Output bindings never ran on a failed inference (13.7).
    assert received == []


def test_bedrock_processor_failure_carries_node_id():
    """The BedrockInferenceError surfacing mechanism carries the failing
    node id (mirrors the LLM/Bedrock failure-surfacing contract)."""
    processor = BedrockInferenceProcessor(invoker=RaisingBedrockInvoker())
    with pytest.raises(BedrockInferenceError) as excinfo:
        # Missing captured frames -> fails with the node identified even
        # before invoking the model.
        processor.process(
            document_binding(BEDROCK_BINDING), {}, "/tmp/does-not-exist")
    assert excinfo.value.node_id == "bedrock1"


def llm_binding(node_id="llm1"):
    return {
        "nodeId": node_id,
        "binding": "llm_inference",
        "parameters": {
            "modelName": "opt-125m",
            "prompt_template": "Summarize: {is_anomalous}",
            "max_tokens": 128,
            "temperature": 0.7,
            "top_p": 1.0,
        },
        "upstreamNodeIds": ["n1"],
        "downstreamNodeIds": [],
    }


class RaisingLlmInvoker:
    def __call__(self, model_name, prompt, parameters):
        raise RuntimeError("connection refused")


def test_llm_inference_failure_is_recorded_and_run_completes(tmp_path):
    """An ``llm_inference`` binding failure is recorded (merged under
    metadata), not raised -- the run still finalizes COMPLETED. This is
    the observed baseline for the existing LLM surfacing path the fix
    must not alter."""
    document = compiled_document([llm_binding()])
    session_factory = make_session_factory()
    artifact_path = write_artifact_set(tmp_path, compiled=document)
    execution_id = seed_run(session_factory, artifact_path)

    received = []
    executor = WorkflowExecutor(
        session_factory=session_factory,
        pipeline_manager_factory=lambda: FakePipelineManager(
            tag_values=dict(METADATA)
        ),
        post_run_handler=lambda reg, doc, tags: received.append(tags),
        llm_processor=LlmInferenceProcessor(invoker=RaisingLlmInvoker()),
    )

    executor.execute(execution_id)

    row = get_execution(session_factory)
    assert row.status == EXECUTION_STATUS_COMPLETED
    assert row.failing_node_id is None
    # The failure was recorded under metadata['llm'][nodeId], not raised.
    assert received and received[0].get("llm", {}).get("llm1", {}).get("error")
