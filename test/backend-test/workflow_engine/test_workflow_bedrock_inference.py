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
"""Tests for the bedrock_inference executor path.

The compiled pipeline terminates the node's two VideoFrames input
branches in frame-capture sinks ({work_dir}-rooted multifilesink
locations); after a successful run the BedrockInferenceProcessor reads
the captured frames, calls the Bedrock runtime (injectable invoker: no
boto3/network in tests), parses the model's JSON answer (tolerating
fenced code blocks), and merges {is_anomalous, confidence} into the
run's tag values BEFORE the gating/output bindings evaluate. Failures
mark the run failed with the node identified and touch nothing else
(Requirements 9.7, 13.7).
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

from workflow_engine import gst_plugins
from workflow_engine.models import WorkflowExecution, WorkflowRegistration
from workflow_engine.output_bindings import (
    BEDROCK_JSON_INSTRUCTION,
    BedrockInferenceError,
    BedrockInferenceProcessor,
    OutputBindingProcessor,
    parse_bedrock_answer,
)
from workflow_engine.pipeline_executor import (
    EXECUTION_STATUS_COMPLETED,
    EXECUTION_STATUS_FAILED,
    WorkflowExecutor,
)

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


def make_document(bindings=None):
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
                    {"nodeId": None, "factory": "videoconvert", "args": {}},
                    {"nodeId": None, "factory": "jpegenc", "args": {}},
                    {"nodeId": None, "factory": "multifilesink",
                     "args": {"location": "{work_dir}/bedrock_frame_cam.jpg"}},
                ],
            },
            {
                "name": "s1",
                "from": None,
                "linkTo": None,
                "elements": [
                    {"nodeId": "ref", "factory": "videotestsrc", "args": {}},
                    {"nodeId": None, "factory": "videoconvert", "args": {}},
                    {"nodeId": None, "factory": "jpegenc", "args": {}},
                    {"nodeId": None, "factory": "multifilesink",
                     "args": {"location": "{work_dir}/bedrock_frame_ref.jpg"}},
                ],
            },
        ],
        "executorBindings": list(
            bindings if bindings is not None else [BEDROCK_BINDING]
        ),
        "pluginDependencies": ["python:boto3"],
    }


def write_frames(work_dir):
    with open(os.path.join(work_dir, "bedrock_frame_cam.jpg"), "wb") as f:
        f.write(JPEG_BYTES_IN)
    with open(os.path.join(work_dir, "bedrock_frame_ref.jpg"), "wb") as f:
        f.write(JPEG_BYTES_REF)


class RecordingInvoker:
    """Injectable Bedrock invoker recording the call and returning a
    canned model answer."""

    def __init__(self, answer='{"is_anomalous": true, "confidence": 0.9}',
                 error=None):
        self.answer = answer
        self.error = error
        self.calls = []

    def __call__(self, model, prompt, images, region, max_tokens):
        self.calls.append({
            "model": model,
            "prompt": prompt,
            "images": list(images),
            "region": region,
            "max_tokens": max_tokens,
        })
        if self.error is not None:
            raise self.error
        return self.answer


# ---------------------------------------------------------------------------
# Response parsing (tolerates fenced code blocks and surrounding prose)
# ---------------------------------------------------------------------------

class TestParseBedrockAnswer:
    def test_plain_json(self):
        assert parse_bedrock_answer(
            '{"is_anomalous": true, "confidence": 0.87}'
        ) == {"is_anomalous": True, "confidence": 0.87}

    def test_fenced_json_block(self):
        text = 'Here is my analysis:\n```json\n{"is_anomalous": false, ' \
               '"confidence": 0.42}\n```\nThanks!'
        assert parse_bedrock_answer(text) == {
            "is_anomalous": False, "confidence": 0.42}

    def test_unlabeled_fence(self):
        text = '```\n{"is_anomalous": true, "confidence": 1}\n```'
        assert parse_bedrock_answer(text) == {
            "is_anomalous": True, "confidence": 1.0}

    def test_json_embedded_in_prose(self):
        text = ('The input differs from the reference. '
                '{"is_anomalous": true, "confidence": 0.75} is my verdict.')
        assert parse_bedrock_answer(text) == {
            "is_anomalous": True, "confidence": 0.75}

    def test_string_typed_fields_are_coerced(self):
        assert parse_bedrock_answer(
            '{"is_anomalous": "true", "confidence": "0.5"}'
        ) == {"is_anomalous": True, "confidence": 0.5}

    def test_missing_confidence_defaults_to_zero(self):
        assert parse_bedrock_answer('{"is_anomalous": false}') == {
            "is_anomalous": False, "confidence": 0.0}

    @pytest.mark.parametrize("text", [
        "", "no json here", '{"other": 1}', "```\nnot json\n```",
    ])
    def test_unparseable_answers_raise(self, text):
        with pytest.raises(ValueError):
            parse_bedrock_answer(text)


# ---------------------------------------------------------------------------
# BedrockInferenceProcessor
# ---------------------------------------------------------------------------

class TestBedrockInferenceProcessor:
    def test_merges_parsed_fields_into_tag_values(self, tmp_path):
        """Intended contract change (bedrock-response-mode): in anomaly
        mode (default) the executor appends the canonical JSON
        instruction to the configured prompt, so the invoker receives
        ``prompt + "\\n\\n" + BEDROCK_JSON_INSTRUCTION`` rather than the
        configured prompt verbatim. Per bedrock-response-mode
        Requirement 5, anomaly mode now ALSO records the raw answer
        text as ``bedrock_text`` / ``bedrock.{nodeId}.text`` alongside
        the parsed verdict."""
        write_frames(str(tmp_path))
        answer = '{"is_anomalous": true, "confidence": 0.9}'
        invoker = RecordingInvoker(answer=answer)
        processor = BedrockInferenceProcessor(invoker=invoker)

        metadata = processor.process(
            make_document(), {"existing": "kept"}, str(tmp_path))

        assert metadata == {
            "existing": "kept", "is_anomalous": True, "confidence": 0.9,
            "bedrock_text": answer,
            "bedrock": {"bedrock1": {"text": answer}}}
        assert len(invoker.calls) == 1
        call = invoker.calls[0]
        assert call["model"] == "us.amazon.nova-lite-v1:0"
        assert call["prompt"] == (
            "Compare the images." + "\n\n" + BEDROCK_JSON_INSTRUCTION)
        assert call["region"] == "us-east-1"
        assert call["max_tokens"] == 256
        # Both captured frames attached, input first, reference second.
        assert call["images"] == [
            ("Input image", JPEG_BYTES_IN),
            ("Reference image", JPEG_BYTES_REF),
        ]

    def test_missing_captured_frame_fails_with_the_node(self, tmp_path):
        # Only the reference frame exists; the primary 'in' file is
        # missing — the required primary frame still fails the node.
        with open(os.path.join(str(tmp_path), "bedrock_frame_ref.jpg"),
                  "wb") as f:
            f.write(JPEG_BYTES_REF)
        processor = BedrockInferenceProcessor(invoker=RecordingInvoker())

        with pytest.raises(BedrockInferenceError) as excinfo:
            processor.process(make_document(), {}, str(tmp_path))
        assert excinfo.value.node_id == "bedrock1"
        assert "'in'" in str(excinfo.value)

    def test_unfed_port_fails_with_the_node(self, tmp_path):
        # An unfed 'in' (primary) port still fails the node; the
        # reference port is optional and covered separately below.
        binding = dict(BEDROCK_BINDING)
        binding["capturePaths"] = {
            "in": None, "reference": "{work_dir}/bedrock_frame_ref.jpg"}
        write_frames(str(tmp_path))
        processor = BedrockInferenceProcessor(invoker=RecordingInvoker())

        with pytest.raises(BedrockInferenceError) as excinfo:
            processor.process(
                make_document([binding]), {}, str(tmp_path))
        assert excinfo.value.node_id == "bedrock1"
        assert "not fed" in str(excinfo.value)
        assert "'in'" in str(excinfo.value)

    def test_missing_reference_frame_falls_back_to_single_image(
            self, tmp_path):
        # The reference file is missing on disk: inference proceeds with
        # the primary image alone instead of failing the run.
        # (bedrock-response-mode Requirement 5: anomaly mode also
        # records the raw answer text.)
        with open(os.path.join(str(tmp_path), "bedrock_frame_cam.jpg"),
                  "wb") as f:
            f.write(JPEG_BYTES_IN)
        invoker = RecordingInvoker()
        processor = BedrockInferenceProcessor(invoker=invoker)

        metadata = processor.process(make_document(), {}, str(tmp_path))

        assert metadata == {
            "is_anomalous": True, "confidence": 0.9,
            "bedrock_text": invoker.answer,
            "bedrock": {"bedrock1": {"text": invoker.answer}}}
        assert len(invoker.calls) == 1
        assert invoker.calls[0]["images"] == [("Input image", JPEG_BYTES_IN)]

    def test_unfed_reference_port_falls_back_to_single_image(self, tmp_path):
        # The compiler's unfed-reference shape (capturePaths.reference is
        # None): inference proceeds with the primary image alone.
        # (bedrock-response-mode Requirement 5: anomaly mode also
        # records the raw answer text.)
        binding = dict(BEDROCK_BINDING)
        binding["capturePaths"] = {
            "in": "{work_dir}/bedrock_frame_cam.jpg", "reference": None}
        write_frames(str(tmp_path))
        invoker = RecordingInvoker()
        processor = BedrockInferenceProcessor(invoker=invoker)

        metadata = processor.process(
            make_document([binding]), {}, str(tmp_path))

        assert metadata == {
            "is_anomalous": True, "confidence": 0.9,
            "bedrock_text": invoker.answer,
            "bedrock": {"bedrock1": {"text": invoker.answer}}}
        assert len(invoker.calls) == 1
        assert invoker.calls[0]["images"] == [("Input image", JPEG_BYTES_IN)]

    def test_invoker_failure_carries_the_node_id(self, tmp_path):
        write_frames(str(tmp_path))
        invoker = RecordingInvoker(error=RuntimeError("credentials missing"))
        processor = BedrockInferenceProcessor(invoker=invoker)

        with pytest.raises(BedrockInferenceError) as excinfo:
            processor.process(make_document(), {}, str(tmp_path))
        assert excinfo.value.node_id == "bedrock1"
        assert "credentials missing" in str(excinfo.value)

    def test_unparseable_answer_carries_the_node_id(self, tmp_path):
        write_frames(str(tmp_path))
        invoker = RecordingInvoker(answer="I cannot answer in JSON, sorry.")
        processor = BedrockInferenceProcessor(invoker=invoker)

        with pytest.raises(BedrockInferenceError) as excinfo:
            processor.process(make_document(), {}, str(tmp_path))
        assert excinfo.value.node_id == "bedrock1"

    def test_documents_without_bedrock_bindings_are_untouched(self):
        processor = BedrockInferenceProcessor(invoker=RecordingInvoker())
        document = make_document(
            [{"nodeId": "f1", "binding": "inference_filter",
              "parameters": {"condition": "is_anomalous == true"}}])
        assert processor.bindings(document) == []
        assert processor.process(document, {"a": 1}, None) == {"a": 1}


# ---------------------------------------------------------------------------
# Output bindings skip bedrock_inference (it ran earlier)
# ---------------------------------------------------------------------------

class TestOutputBindingsSkipBedrock:
    def test_bedrock_binding_triggers_no_output_client(self):
        calls = []
        processor = OutputBindingProcessor(
            dio_actuator=lambda *a: calls.append(("dio", a)),
            mqtt_publisher=lambda *a, **k: calls.append(("mqtt", a)),
            opcua_writer=lambda *a: calls.append(("opcua", a)),
        )
        processor.process(
            None,
            {"executorBindings": [BEDROCK_BINDING]},
            {"is_anomalous": True, "confidence": 0.9},
        )
        assert calls == []


# ---------------------------------------------------------------------------
# WorkflowExecutor integration ({work_dir} resolution + run finalization)
# ---------------------------------------------------------------------------

class CapturingPipelineManager:
    """Mocked GstPipelineManager that 'captures frames': it writes the
    multifilesink locations found in the launch string, exactly like the
    real capture sink chains would."""

    def __init__(self, tag_values=None, write_files=True):
        self.tag_values = tag_values or {}
        self.write_files = write_files
        self.calls = []

    def run_pipeline(self, pipeline_str, frame_data=None, latency_metrics=None,
                     status_sink=None):
        self.calls.append(pipeline_str)
        if self.write_files:
            for token in pipeline_str.split():
                if token.startswith("location=") and token.endswith(".jpg"):
                    path = token[len("location="):]
                    payload = (JPEG_BYTES_REF if "bedrock_frame_ref" in path
                               else JPEG_BYTES_IN)
                    with open(path, "wb") as f:
                        f.write(payload)
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
    def test_work_dir_resolved_and_fields_merged_into_post_run_tags(
        self, tmp_path, session_factory
    ):
        artifact_path = write_artifact_set(tmp_path, compiled=make_document())
        execution_id = seed_run(session_factory, artifact_path)
        manager = CapturingPipelineManager(tag_values={"confidence": 0.1})
        answer = '```json\n{"is_anomalous": true, "confidence": 0.93}\n```'
        invoker = RecordingInvoker(answer=answer)
        received = []

        executor = WorkflowExecutor(
            session_factory=session_factory,
            pipeline_manager_factory=lambda: manager,
            post_run_handler=lambda reg, doc, tags: received.append(tags),
            bedrock_processor=BedrockInferenceProcessor(invoker=invoker),
        )
        executor.execute(execution_id)

        # The launch string carries real paths, not the placeholder.
        assert len(manager.calls) == 1
        assert "{work_dir}" not in manager.calls[0]
        assert "bedrock_frame_cam.jpg" in manager.calls[0]
        # Bedrock ran with the captured frames and its parsed answer
        # reached the output bindings merged over the pipeline tags.
        # (bedrock-response-mode Requirement 5: the raw answer text is
        # recorded alongside the verdict.)
        assert invoker.calls and invoker.calls[0]["images"] == [
            ("Input image", JPEG_BYTES_IN),
            ("Reference image", JPEG_BYTES_REF),
        ]
        # `trigger: {}` is the seeded trigger-less-run delta
        # (custom-python-source Requirements 2.5, 11.1).
        assert received == [{
            "is_anomalous": True, "confidence": 0.93,
            "bedrock_text": answer,
            "bedrock": {"bedrock1": {"text": answer}},
            "trigger": {}}]
        row = get_execution(session_factory)
        assert row.status == EXECUTION_STATUS_COMPLETED
        # The per-run working directory is removed afterwards.
        work_dir = manager.calls[0].split("location=")[1].split(
            "/bedrock_frame_")[0]
        assert not os.path.exists(work_dir)

    def test_bedrock_failure_marks_the_run_failed_with_the_node(
        self, tmp_path, session_factory
    ):
        artifact_path = write_artifact_set(tmp_path, compiled=make_document())
        execution_id = seed_run(session_factory, artifact_path)
        manager = CapturingPipelineManager()
        invoker = RecordingInvoker(error=RuntimeError("endpoint unreachable"))
        received = []

        executor = WorkflowExecutor(
            session_factory=session_factory,
            pipeline_manager_factory=lambda: manager,
            post_run_handler=lambda reg, doc, tags: received.append(tags),
            bedrock_processor=BedrockInferenceProcessor(invoker=invoker),
        )
        executor.execute(execution_id)

        row = get_execution(session_factory)
        assert row.status == EXECUTION_STATUS_FAILED
        assert row.failing_node_id == "bedrock1"
        assert "endpoint unreachable" in row.error
        # Output bindings never ran on a failed inference (13.7).
        assert received == []

    def test_documents_without_work_dir_references_run_unchanged(
        self, tmp_path, session_factory
    ):
        document = make_document(bindings=[])
        for segment in document["segments"]:
            segment["elements"] = [
                e for e in segment["elements"]
                if "{work_dir}" not in str(e.get("args", {}))
            ] + [{"nodeId": None, "factory": "fakesink", "args": {}}]
        artifact_path = write_artifact_set(tmp_path, compiled=document)
        execution_id = seed_run(session_factory, artifact_path)
        manager = CapturingPipelineManager(write_files=False)

        executor = WorkflowExecutor(
            session_factory=session_factory,
            pipeline_manager_factory=lambda: manager,
        )
        executor.execute(execution_id)

        assert get_execution(session_factory).status == \
            EXECUTION_STATUS_COMPLETED
        assert "location=" not in manager.calls[0]

    # -- output-node-sent-message: node_status_json carries sent/skip details

    def test_run_records_mqtt_sent_detail_in_node_status(
        self, tmp_path, session_factory
    ):
        import json as _json

        mqtt_binding = {
            "nodeId": "mqtt1", "binding": "mqtt_publish",
            "parameters": {"broker_host": "b", "topic": "factory/line1",
                           "qos": 1, "payload_template": "{inference_json}"},
            "upstreamNodeIds": [], "downstreamNodeIds": [],
        }
        document = output_document([mqtt_binding])
        artifact_path = write_artifact_set(tmp_path, compiled=document)
        execution_id = seed_run(session_factory, artifact_path)
        manager = CapturingPipelineManager(
            tag_values={"is_anomalous": True, "confidence": 0.9},
            write_files=False,
        )
        published = []
        processor = OutputBindingProcessor(
            mqtt_publisher=lambda *a, **k: published.append((a, k)))

        WorkflowExecutor(
            session_factory=session_factory,
            pipeline_manager_factory=lambda: manager,
            post_run_handler=processor,
        ).execute(execution_id)

        row = get_execution(session_factory)
        assert row.status == EXECUTION_STATUS_COMPLETED
        assert published  # the stubbed publisher fired
        status = _json.loads(row.node_status_json)
        assert status["mqtt1"]["status"] == "success"
        assert "factory/line1" in status["mqtt1"]["detail"]
        assert "(qos 1, plain)" in status["mqtt1"]["detail"]

    def test_run_records_gated_skip_detail_in_node_status(
        self, tmp_path, session_factory
    ):
        import json as _json

        filter_binding = {
            "nodeId": "f1", "binding": "inference_filter",
            "parameters": {"condition": "confidence >= 0.8"},
            "upstreamNodeIds": [], "downstreamNodeIds": ["mqtt1"],
        }
        mqtt_binding = {
            "nodeId": "mqtt1", "binding": "mqtt_publish",
            "parameters": {"broker_host": "b", "topic": "t"},
            "upstreamNodeIds": ["f1"], "downstreamNodeIds": [],
        }
        document = output_document([filter_binding, mqtt_binding])
        artifact_path = write_artifact_set(tmp_path, compiled=document)
        execution_id = seed_run(session_factory, artifact_path)
        # confidence 0.1 -> filter "confidence >= 0.8" fails -> mqtt gated.
        manager = CapturingPipelineManager(
            tag_values={"is_anomalous": True, "confidence": 0.1},
            write_files=False,
        )
        published = []
        processor = OutputBindingProcessor(
            mqtt_publisher=lambda *a, **k: published.append((a, k)))

        WorkflowExecutor(
            session_factory=session_factory,
            pipeline_manager_factory=lambda: manager,
            post_run_handler=processor,
        ).execute(execution_id)

        row = get_execution(session_factory)
        assert row.status == EXECUTION_STATUS_COMPLETED
        assert published == []  # gated out, never published
        status = _json.loads(row.node_status_json)
        assert status["mqtt1"]["detail"] == (
            "not sent: gated out by an upstream inference filter or conditional"
        )

    def test_failing_output_node_keeps_failure_detail_no_sent_detail(
        self, tmp_path, session_factory
    ):
        import json as _json

        mqtt_binding = {
            "nodeId": "mqtt1", "binding": "mqtt_publish",
            "parameters": {"broker_host": "b", "topic": "factory/line1"},
            "upstreamNodeIds": [], "downstreamNodeIds": [],
        }
        document = output_document([mqtt_binding])
        artifact_path = write_artifact_set(tmp_path, compiled=document)
        execution_id = seed_run(session_factory, artifact_path)
        manager = CapturingPipelineManager(
            tag_values={"is_anomalous": True, "confidence": 0.9},
            write_files=False,
        )

        def boom(*a, **k):
            raise RuntimeError("broker unreachable")

        processor = OutputBindingProcessor(mqtt_publisher=boom)

        WorkflowExecutor(
            session_factory=session_factory,
            pipeline_manager_factory=lambda: manager,
            post_run_handler=processor,
        ).execute(execution_id)

        row = get_execution(session_factory)
        assert row.status == EXECUTION_STATUS_FAILED
        assert row.failing_node_id == "mqtt1"
        status = _json.loads(row.node_status_json)
        assert status["mqtt1"]["status"] == "failure"
        # The failure detail is retained; NO sent-message detail replaced it.
        assert "broker unreachable" in status["mqtt1"]["detail"]
        assert "sent to topic" not in status["mqtt1"]["detail"]


def output_document(bindings):
    """A minimal compiled document with a trivial pipeline plus the given
    executor bindings (output-node-sent-message integration harness)."""
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
            },
        ],
        "executorBindings": list(bindings),
        "pluginDependencies": [],
    }
