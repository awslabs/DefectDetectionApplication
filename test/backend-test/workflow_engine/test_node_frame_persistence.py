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
"""Executor artifact persistence for inference-node frames
(vlm-parity-run-results Requirements 2.2, 2.3, 2.4, 4.4).

After a run with bedrock_inference/llm_inference bindings completes
(success OR output-binding failure), the executor copies each binding's
captured frames from the per-run work dir into the run's artifact
directory as ``{capture_id}.node.{sanitized_nodeId}.{port}.jpg`` before
the work dir is removed, marks the run as having viewable image
results, and — new with this feature — ALWAYS records
``output_dir``/``capture_id`` on the execution row so Bedrock/VLM-only
runs (no File_Output terminal) persist their metadata JSON and node
images too. Old packages without llm capturePaths are tolerated
(Requirement 4.4).

Uses the executor harness pattern from test_workflow_bedrock_inference:
a temp sqlite session factory, a stubbed GStreamer registry scan, a
capturing fake pipeline manager that writes the multifilesink locations,
and a tmp-dir ``_WORKFLOW_CAPTURE_ROOT`` so runs never touch the real
``/aws_dda`` tree.
"""
import json
import os
import time
from unittest.mock import patch

import pytest

from workflow_engine_test_utils import (
    DEVICE_ARCH,
    make_session_factory,
    write_artifact_set,
)

from workflow_engine import gst_plugins, pipeline_executor
from workflow_engine.models import WorkflowExecution, WorkflowRegistration
from workflow_engine.output_bindings import (
    BedrockInferenceProcessor,
    LlmInferenceProcessor,
    OutputBindingError,
)
from workflow_engine.pipeline_executor import (
    EXECUTION_STATUS_COMPLETED,
    EXECUTION_STATUS_FAILED,
    WorkflowExecutor,
)

JPEG_BYTES_IN = b"\xff\xd8fake-input-jpeg\xff\xd9"
JPEG_BYTES_REF = b"\xff\xd8fake-reference-jpeg\xff\xd9"

BEDROCK_ANSWER = '{"is_anomalous": true, "confidence": 0.9}'


def bedrock_binding(node_id="bedrock1"):
    return {
        "nodeId": node_id,
        "binding": "bedrock_inference",
        "parameters": {
            "model": "us.amazon.nova-lite-v1:0",
            "prompt": "Compare the images.",
            "region": "us-east-1",
            "max_tokens": 256,
        },
        "upstreamNodeIds": ["cam", "ref"],
        "downstreamNodeIds": [],
        "capturePaths": {
            "in": "{work_dir}/bedrock_frame_cam.jpg",
            "reference": "{work_dir}/bedrock_frame_ref.jpg",
        },
    }


def llm_binding(capture_paths=None):
    binding = {
        "nodeId": "llm1",
        "binding": "llm_inference",
        "parameters": {"modelName": "m", "prompt_template": "Describe."},
        "upstreamNodeIds": ["cam"],
        "downstreamNodeIds": [],
    }
    if capture_paths is not None:
        binding["capturePaths"] = capture_paths
    return binding


def capture_segment(name, node_id, location):
    return {
        "name": name,
        "from": None,
        "linkTo": None,
        "elements": [
            {"nodeId": node_id, "factory": "videotestsrc", "args": {}},
            {"nodeId": None, "factory": "videoconvert", "args": {}},
            {"nodeId": None, "factory": "jpegenc", "args": {}},
            {"nodeId": None, "factory": "multifilesink",
             "args": {"location": location}},
        ],
    }


def make_document(segments, bindings):
    return {
        "schemaVersion": 1,
        "workflowId": "wf-1",
        "workflowVersion": "3",
        "targetArch": DEVICE_ARCH,
        "segments": list(segments),
        "executorBindings": list(bindings),
        "pluginDependencies": [],
    }


def bedrock_document(node_id="bedrock1"):
    return make_document(
        [
            capture_segment("s0", "cam", "{work_dir}/bedrock_frame_cam.jpg"),
            capture_segment("s1", "ref", "{work_dir}/bedrock_frame_ref.jpg"),
        ],
        [bedrock_binding(node_id)],
    )


class RecordingInvoker:
    """Injectable Bedrock invoker returning a canned verdict answer."""

    def __init__(self, answer=BEDROCK_ANSWER):
        self.answer = answer
        self.calls = []

    def __call__(self, model, prompt, images, region, max_tokens):
        self.calls.append({"model": model, "prompt": prompt})
        return self.answer


class CapturingPipelineManager:
    """Mocked GstPipelineManager that 'captures frames': it writes the
    multifilesink locations found in the launch string, exactly like the
    real capture sink chains would."""

    def __init__(self, tag_values=None, write_files=True):
        self.tag_values = tag_values or {}
        self.write_files = write_files
        self.calls = []

    def run_pipeline(self, pipeline_str, frame_data=None,
                     latency_metrics=None, status_sink=None):
        self.calls.append(pipeline_str)
        if self.write_files:
            for token in pipeline_str.split():
                if token.startswith("location=") and token.endswith(".jpg"):
                    path = token[len("location="):]
                    payload = (JPEG_BYTES_REF if "_ref" in path
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


@pytest.fixture
def capture_root(tmp_path):
    """Per-test artifact root so runs never touch the real /aws_dda."""
    root = os.path.join(str(tmp_path), "captures")
    with patch.object(pipeline_executor, "_WORKFLOW_CAPTURE_ROOT", root):
        yield root


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


def run_executor(session_factory, document, tmp_path, manager=None,
                 post_run_handler=None, llm_invoker=None):
    artifact_path = write_artifact_set(tmp_path, compiled=document)
    execution_id = seed_run(session_factory, artifact_path)
    manager = manager or CapturingPipelineManager()
    executor = WorkflowExecutor(
        session_factory=session_factory,
        pipeline_manager_factory=lambda: manager,
        post_run_handler=post_run_handler or (lambda reg, doc, tags: None),
        bedrock_processor=BedrockInferenceProcessor(
            invoker=RecordingInvoker()),
        llm_processor=LlmInferenceProcessor(
            invoker=llm_invoker or (lambda model, prompt, params: "text")),
    )
    executor.execute(execution_id)
    return manager


def expected_output_dir(capture_root):
    return os.path.join(capture_root, "wf-1", "exec-1")


def node_files(output_dir):
    try:
        return sorted(n for n in os.listdir(output_dir) if ".node." in n)
    except OSError:
        return []


def work_dir_of(manager):
    return manager.calls[0].split("location=")[1].split("/bedrock_frame_")[0]


CAPTURE_ID = "wf-1-exec-1"


class TestNodeFramePersistence:
    def test_success_path_persists_frames_with_exact_naming(
        self, tmp_path, session_factory, capture_root
    ):
        """A Bedrock-only run (no File_Output terminal) records
        output_dir/capture_id, persists the metadata JSON AND the node
        frames, and is marked as having viewable image results
        (Requirements 2.2, 2.3, 2.4)."""
        manager = run_executor(
            session_factory, bedrock_document(), tmp_path)

        row = get_execution(session_factory)
        output_dir = expected_output_dir(capture_root)
        assert row.status == EXECUTION_STATUS_COMPLETED
        assert row.output_dir == output_dir
        assert row.capture_id == CAPTURE_ID
        assert row.has_image_results is True
        # Exact node-frame naming: {capture_id}.node.{nodeId}.{port}.jpg
        assert node_files(output_dir) == [
            CAPTURE_ID + ".node.bedrock1.in.jpg",
            CAPTURE_ID + ".node.bedrock1.reference.jpg",
        ]
        with open(os.path.join(
                output_dir, CAPTURE_ID + ".node.bedrock1.in.jpg"),
                "rb") as f:
            assert f.read() == JPEG_BYTES_IN
        with open(os.path.join(
                output_dir, CAPTURE_ID + ".node.bedrock1.reference.jpg"),
                "rb") as f:
            assert f.read() == JPEG_BYTES_REF
        # The metadata JSON also landed (Requirement 2.3: bedrock-only
        # runs previously persisted nothing).
        metadata_path = os.path.join(output_dir, CAPTURE_ID + ".json")
        with open(metadata_path, "r", encoding="utf-8") as f:
            metadata = json.load(f)
        assert metadata["is_anomalous"] is True
        # The per-run working directory is still removed afterwards.
        assert not os.path.exists(work_dir_of(manager))

    def test_output_binding_failure_path_persists_frames(
        self, tmp_path, session_factory, capture_root
    ):
        """Frames + metadata persist on the output-binding-failure path
        too (Requirement 2.2: success OR output-binding failure)."""
        def failing_handler(reg, doc, tags):
            raise OutputBindingError(["mqtt1"], "broker unreachable")

        run_executor(session_factory, bedrock_document(), tmp_path,
                     post_run_handler=failing_handler)

        row = get_execution(session_factory)
        output_dir = expected_output_dir(capture_root)
        assert row.status == EXECUTION_STATUS_FAILED
        assert row.failing_node_id == "mqtt1"
        assert row.output_dir == output_dir
        assert row.capture_id == CAPTURE_ID
        assert row.has_image_results is True
        assert node_files(output_dir) == [
            CAPTURE_ID + ".node.bedrock1.in.jpg",
            CAPTURE_ID + ".node.bedrock1.reference.jpg",
        ]
        assert os.path.isfile(
            os.path.join(output_dir, CAPTURE_ID + ".json"))

    def test_node_id_is_sanitized_in_the_filename(
        self, tmp_path, session_factory, capture_root
    ):
        """Filename-unsafe node id characters are replaced with '_',
        the same discipline the compiler applies to capture filenames."""
        run_executor(
            session_factory, bedrock_document(node_id="bedrock/eu:1"),
            tmp_path)

        output_dir = expected_output_dir(capture_root)
        assert node_files(output_dir) == [
            CAPTURE_ID + ".node.bedrock_eu_1.in.jpg",
            CAPTURE_ID + ".node.bedrock_eu_1.reference.jpg",
        ]

    def test_llm_binding_with_capture_paths_persists_its_frame(
        self, tmp_path, session_factory, capture_root
    ):
        """llm_inference bindings carrying capturePaths persist their
        'in' frame exactly like bedrock bindings (Requirement 2.2)."""
        document = make_document(
            [capture_segment("s0", "cam", "{work_dir}/llm_frame_cam.jpg")],
            [llm_binding({"in": "{work_dir}/llm_frame_cam.jpg"})],
        )
        run_executor(session_factory, document, tmp_path)

        row = get_execution(session_factory)
        output_dir = expected_output_dir(capture_root)
        assert row.status == EXECUTION_STATUS_COMPLETED
        assert row.has_image_results is True
        assert node_files(output_dir) == [
            CAPTURE_ID + ".node.llm1.in.jpg",
        ]

    def test_llm_binding_without_capture_paths_is_tolerated(
        self, tmp_path, session_factory, capture_root
    ):
        """Old packages have no llm capturePaths (Requirement 4.4): the
        run completes, output_dir/capture_id are still recorded and the
        metadata JSON persists, but no node images and no image-results
        flag."""
        document = make_document(
            [{
                "name": "s0",
                "from": None,
                "linkTo": None,
                "elements": [
                    {"nodeId": "cam", "factory": "videotestsrc", "args": {}},
                    {"nodeId": None, "factory": "fakesink", "args": {}},
                ],
            }],
            [llm_binding()],
        )
        run_executor(session_factory, document, tmp_path)

        row = get_execution(session_factory)
        output_dir = expected_output_dir(capture_root)
        assert row.status == EXECUTION_STATUS_COMPLETED
        assert row.output_dir == output_dir
        assert row.capture_id == CAPTURE_ID
        assert not row.has_image_results
        assert node_files(output_dir) == []
        # Metadata JSON (the llm generated text) still persists.
        with open(os.path.join(output_dir, CAPTURE_ID + ".json"),
                  "r", encoding="utf-8") as f:
            metadata = json.load(f)
        assert metadata["llm"]["llm1"] == {"generated_text": "text"}

    def test_missing_capture_files_are_skipped_silently(
        self, tmp_path, session_factory, capture_root
    ):
        """capturePaths whose files were never written (e.g. a branch
        produced no frame) are skipped without error and without the
        image-results flag."""
        # The bedrock processor would fail on the missing 'in' frame, so
        # exercise the skip through an llm binding (recorded, not
        # raised) whose capture file the pipeline never writes.
        document = make_document(
            [capture_segment("s0", "cam", "{work_dir}/llm_frame_cam.jpg")],
            [llm_binding({"in": "{work_dir}/llm_frame_cam.jpg"})],
        )
        run_executor(session_factory, document, tmp_path,
                     manager=CapturingPipelineManager(write_files=False))

        row = get_execution(session_factory)
        assert row.status == EXECUTION_STATUS_COMPLETED
        assert not row.has_image_results
        assert node_files(expected_output_dir(capture_root)) == []
