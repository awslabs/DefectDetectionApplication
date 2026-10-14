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
"""Tests for the llm_inference executor path (task 11.2).

vllm-triton-inference Requirements 7.3-7.7: after a successful pipeline
run — after the Bedrock processor and before the output bindings
evaluate — the LlmInferenceProcessor renders each llm_inference
binding's Prompt_Template from the run metadata, calls the device
Text_Generation_API (injectable invoker: no HTTP in tests), and merges
the outcome under metadata['llm'][nodeId]. Binding failures (unresolved
placeholder, API error/timeout) are recorded, never raised: remaining
bindings and independent nodes continue, and the merged metadata reaches
the post-run output bindings.
"""
import time
from unittest.mock import patch

import pytest

from workflow_engine_test_utils import (
    DEVICE_ARCH,
    make_session_factory,
    write_artifact_set,
)

from workflow_engine import gst_plugins
from workflow_engine.models import WorkflowExecution, WorkflowRegistration
from workflow_engine.output_bindings import (
    LlmInferenceProcessor,
    OutputBindingProcessor,
)
from workflow_engine.pipeline_executor import (
    EXECUTION_STATUS_COMPLETED,
    WorkflowExecutor,
)


def llm_binding(node_id="llm1", template="Summarize: {is_anomalous}",
                model="opt-125m"):
    return {
        "nodeId": node_id,
        "binding": "llm_inference",
        "parameters": {
            "modelName": model,
            "prompt_template": template,
            "max_tokens": 128,
            "temperature": 0.7,
            "top_p": 1.0,
        },
        "upstreamNodeIds": ["cam"],
        "downstreamNodeIds": ["mqtt"],
    }


def make_document(bindings):
    return {
        "schemaVersion": 1,
        "workflowId": "wf-1",
        "workflowVersion": "3",
        "targetArch": DEVICE_ARCH,
        "segments": [
            {
                "name": "s0",
                "from": None,
                "linkTo": None,
                "elements": [
                    {"nodeId": "cam", "factory": "videotestsrc", "args": {}},
                    {"nodeId": None, "factory": "fakesink", "args": {}},
                ],
            }
        ],
        "executorBindings": list(bindings),
        "pluginDependencies": [],
    }


class RecordingInvoker:
    """Injectable Text_Generation_API invoker recording each call and
    returning canned text (or raising per-model errors)."""

    def __init__(self, text="generated answer", errors=None):
        self.text = text
        self.errors = dict(errors or {})
        self.calls = []

    def __call__(self, model_name, prompt, parameters):
        self.calls.append({
            "model_name": model_name,
            "prompt": prompt,
            "parameters": dict(parameters),
        })
        if model_name in self.errors:
            raise self.errors[model_name]
        return self.text


# ---------------------------------------------------------------------------
# LlmInferenceProcessor
# ---------------------------------------------------------------------------

class TestLlmInferenceProcessor:
    def test_merges_generated_text_under_llm_node_id(self):
        invoker = RecordingInvoker(text="all good")
        processor = LlmInferenceProcessor(invoker=invoker)
        document = make_document([llm_binding()])

        metadata = processor.process(
            document, {"is_anomalous": True, "existing": "kept"})

        assert metadata["llm"] == {"llm1": {"generated_text": "all good"}}
        assert metadata["existing"] == "kept"
        # The invoker received the RENDERED prompt and the bound
        # model/parameters (7.3).
        assert invoker.calls == [{
            "model_name": "opt-125m",
            "prompt": "Summarize: True",
            "parameters": {
                "modelName": "opt-125m",
                "prompt_template": "Summarize: {is_anomalous}",
                "max_tokens": 128,
                "temperature": 0.7,
                "top_p": 1.0,
            },
        }]

    def test_unresolved_placeholder_records_error_without_api_call(self):
        invoker = RecordingInvoker()
        processor = LlmInferenceProcessor(invoker=invoker)
        document = make_document(
            [llm_binding(template="Value: {missing.field}")])

        metadata = processor.process(document, {"is_anomalous": True})

        assert metadata["llm"]["llm1"] == {
            "error": "unresolved placeholder missing.field"}
        assert invoker.calls == []  # no API call (7.5)

    def test_api_error_is_recorded_and_other_bindings_continue(self):
        invoker = RecordingInvoker(
            text="ok", errors={"bad-model": RuntimeError("504 timeout")})
        processor = LlmInferenceProcessor(invoker=invoker)
        document = make_document([
            llm_binding(node_id="llm1", model="bad-model"),
            llm_binding(node_id="llm2", model="opt-125m"),
        ])

        metadata = processor.process(document, {"is_anomalous": False})

        # The failure is recorded, not raised (7.6); the second binding
        # still ran and recorded its text (failure containment).
        assert metadata["llm"]["llm1"] == {"error": "504 timeout"}
        assert metadata["llm"]["llm2"] == {"generated_text": "ok"}
        assert len(invoker.calls) == 2

    def test_later_binding_can_reference_earlier_generated_text(self):
        invoker = RecordingInvoker(text="first answer")
        processor = LlmInferenceProcessor(invoker=invoker)
        document = make_document([
            llm_binding(node_id="llm1", template="Q1: {confidence}"),
            llm_binding(node_id="llm2",
                        template="Refine: {llm.llm1.generated_text}"),
        ])

        processor.process(document, {"confidence": 0.9})

        assert invoker.calls[1]["prompt"] == "Refine: first answer"

    def test_documents_without_llm_bindings_are_untouched(self):
        processor = LlmInferenceProcessor(invoker=RecordingInvoker())
        document = make_document(
            [{"nodeId": "f1", "binding": "inference_filter",
              "parameters": {"condition": "is_anomalous == true"}}])
        assert processor.bindings(document) == []
        assert processor.process(document, {"a": 1}) == {"a": 1}


# ---------------------------------------------------------------------------
# Output bindings skip llm_inference (it ran earlier) and treat the
# sim stub as an unknown-binding no-op on device
# ---------------------------------------------------------------------------

class TestOutputBindingsSkipLlm:
    @pytest.mark.parametrize("kind", ["llm_inference", "sim_llm_inference"])
    def test_llm_bindings_trigger_no_output_client(self, kind):
        calls = []
        processor = OutputBindingProcessor(
            dio_actuator=lambda *a: calls.append(("dio", a)),
            mqtt_publisher=lambda *a, **k: calls.append(("mqtt", a)),
            opcua_writer=lambda *a: calls.append(("opcua", a)),
        )
        binding = dict(llm_binding(), binding=kind)
        processor.process(
            None,
            {"executorBindings": [binding]},
            {"is_anomalous": True},
        )
        assert calls == []


# ---------------------------------------------------------------------------
# WorkflowExecutor integration (lifecycle + metadata flow)
# ---------------------------------------------------------------------------

class FakePipelineManager:
    def __init__(self, tag_values=None):
        self.tag_values = tag_values or {}
        self.calls = []

    def run_pipeline(self, pipeline_str, frame_data=None, latency_metrics=None,
                     status_sink=None):
        self.calls.append(pipeline_str)
        return dict(self.tag_values)


@pytest.fixture
def session_factory():
    return make_session_factory()


@pytest.fixture(autouse=True)
def no_registry_scan():
    with patch.object(gst_plugins, "_scan_registry", return_value=True):
        yield


def seed_run(session_factory, artifact_path):
    session = session_factory()
    try:
        session.add(WorkflowRegistration(
            id="wf-1:3", workflow_id="wf-1", version="3", arch=DEVICE_ARCH,
            artifact_path=str(artifact_path), status="registered",
            registered_at=int(time.time()),
        ))
        session.add(WorkflowExecution(
            id="exec-1", registration_id="wf-1:3",
            started_at=int(time.time()), status="pending",
        ))
        session.commit()
    finally:
        session.close()
    return "exec-1"


def get_execution(session_factory):
    session = session_factory()
    try:
        return session.get(WorkflowExecution, "exec-1")
    finally:
        session.close()


class TestExecutorIntegration:
    def test_generated_text_reaches_post_run_tags(
        self, tmp_path, session_factory
    ):
        artifact_path = write_artifact_set(
            tmp_path, compiled=make_document([llm_binding()]))
        execution_id = seed_run(session_factory, artifact_path)
        manager = FakePipelineManager(tag_values={"is_anomalous": True})
        invoker = RecordingInvoker(text="anomaly summary")
        received = []

        executor = WorkflowExecutor(
            session_factory=session_factory,
            pipeline_manager_factory=lambda: manager,
            post_run_handler=lambda reg, doc, tags: received.append(tags),
            llm_processor=LlmInferenceProcessor(invoker=invoker),
        )
        executor.execute(execution_id)

        # The merged metadata (7.4) reached the output bindings (7.7).
        # `trigger: {}` is the seeded trigger-less-run delta
        # (custom-python-source Requirements 2.5, 11.1).
        assert received == [{
            "is_anomalous": True,
            "llm": {"llm1": {"generated_text": "anomaly summary"}},
            "trigger": {},
        }]
        assert get_execution(session_factory).status == \
            EXECUTION_STATUS_COMPLETED

    def test_binding_failure_does_not_fail_the_run(
        self, tmp_path, session_factory
    ):
        artifact_path = write_artifact_set(
            tmp_path, compiled=make_document([llm_binding()]))
        execution_id = seed_run(session_factory, artifact_path)
        manager = FakePipelineManager(tag_values={"is_anomalous": True})
        invoker = RecordingInvoker(
            errors={"opt-125m": RuntimeError("connection refused")})
        received = []

        executor = WorkflowExecutor(
            session_factory=session_factory,
            pipeline_manager_factory=lambda: manager,
            post_run_handler=lambda reg, doc, tags: received.append(tags),
            llm_processor=LlmInferenceProcessor(invoker=invoker),
        )
        executor.execute(execution_id)

        # Recorded, not raised (7.6): the run completes and the error
        # indication reaches downstream consumers (7.7).
        # `trigger: {}` is the seeded trigger-less-run delta
        # (custom-python-source Requirements 2.5, 11.1).
        assert received == [{
            "is_anomalous": True,
            "llm": {"llm1": {"error": "connection refused"}},
            "trigger": {},
        }]
        assert get_execution(session_factory).status == \
            EXECUTION_STATUS_COMPLETED
