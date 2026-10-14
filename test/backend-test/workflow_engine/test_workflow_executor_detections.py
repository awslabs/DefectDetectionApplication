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
"""Executor integration for detection-guided Bedrock inspection (task 11).

Unit tests over ``WorkflowExecutor.execute`` with fake pipeline managers
and injected processors (detection-guided-bedrock-inspection Requirements
1.1, 1.6, 4.5, 5.6, 5.7, 7.1, 7.3):

- **Merge ordering** (R1.1): the run's Detection_List is merged into the
  Run_Metadata after the capture artifacts are repaired and BEFORE the
  Bedrock processor runs, so the processor sees ``detections`` /
  ``detection_count``; the executor passes a populated ``run_context``.
- **Cache identity** (design Property 1): the bridge pump's
  DetectionsInjector and the post-pipeline merge share ONE run-state
  cache, so both see identical entries with identical Detection_IDs.
- **Exclusion correctness** (R5.7): branch-scoped output bindings publish
  exactly once — in the Bedrock processor's completion path — and are
  excluded from the post-run handler; non-branch bindings keep today's
  post-run path; the sequential legacy path excludes nothing.
- **Persistence content** (R1.6, R4.5): the persisted
  ``{capture_id}.json`` carries the Detection_List (IDs included) and the
  nested ``bedrock.{nodeId}.*`` verdict keys.
- **Detection-less runs** (R7.1, R7.3): a run with no detections record
  and no new parameters produces byte-identical Run_Metadata and
  persisted metadata to the pre-feature engine.

Harness: temp sqlite sessions, a stubbed GStreamer registry scan, a
capturing fake pipeline manager, and a tmp-dir ``_WORKFLOW_CAPTURE_ROOT``
so runs never touch the real ``/aws_dda`` tree.
"""
import copy
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
from workflow_engine.detections import CACHE_KEY_DETECTIONS
from workflow_engine.models import WorkflowExecution, WorkflowRegistration
from workflow_engine.output_bindings import (
    BedrockInferenceProcessor,
    OutputBindingProcessor,
)
from workflow_engine.pipeline_executor import (
    EXECUTION_STATUS_COMPLETED,
    WorkflowExecutor,
)

WORKFLOW_ID = "wf-1"
EXECUTION_ID = "exec-1"
CAPTURE_ID = "{0}-{1}".format(WORKFLOW_ID, EXECUTION_ID)

JPEG_BYTES_IN = b"\xff\xd8fake-input-jpeg\xff\xd9"

BEDROCK_ANSWER = '{"is_anomalous": true, "confidence": 0.93}'

#: Raw marshal-shaped detections payload (two entries, left-to-right
#: order already: centers x=60 then x=260).
RAW_DETECTIONS = {
    "detections": {
        "0": {
            "class_index": 0,
            "class_label": "blue box",
            "bounding_box": [10.0, 20.0, 110.0, 120.0],
            "confidence": 0.9,
        },
        "1": {
            "class_index": 0,
            "class_label": "blue box",
            "bounding_box": [210.0, 20.0, 310.0, 120.0],
            "confidence": 0.8,
        },
    }
}


def bedrock_binding(node_id="b1", upstream=("cam",)):
    return {
        "nodeId": node_id,
        "binding": "bedrock_inference",
        "parameters": {
            "model": "us.amazon.nova-lite-v1:0",
            "prompt": "Compare the images.",
            "region": "us-east-1",
            "max_tokens": 256,
        },
        "upstreamNodeIds": list(upstream),
        "downstreamNodeIds": [],
        "capturePaths": {"in": "{work_dir}/bedrock_frame_cam.jpg"},
    }


def mqtt_binding(node_id, topic, upstream):
    return {
        "nodeId": node_id,
        "binding": "mqtt_publish",
        "parameters": {
            "broker_host": "b",
            "topic": topic,
            "qos": 1,
            "payload_template": "{inference_json}",
        },
        "upstreamNodeIds": list(upstream),
        "downstreamNodeIds": [],
    }


def make_document(bindings):
    """A capture-sink pipeline (the fake manager writes the multifilesink
    location) plus the given executor bindings."""
    return {
        "schemaVersion": 1,
        "workflowId": WORKFLOW_ID,
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
            }
        ],
        "executorBindings": list(bindings),
        "pluginDependencies": [],
    }


def make_bridged_document():
    """A model_inference -> custom_python pipeline (the bridge pump's
    detections-injection topology, Requirement 1.10)."""
    return {
        "schemaVersion": 1,
        "workflowId": WORKFLOW_ID,
        "workflowVersion": "3",
        "targetArch": DEVICE_ARCH,
        "segments": [
            {
                "name": "s0",
                "from": None,
                "linkTo": None,
                "elements": [
                    {"nodeId": "cam", "factory": "videotestsrc", "args": {}},
                    {"nodeId": "model1", "factory": "emltriton",
                     "args": {"model": "m"}},
                    {"nodeId": "py1", "factory": "emlpython",
                     "args": {"handler-path": "handlers/py1/handler.py"}},
                    {"nodeId": None, "factory": "fakesink", "args": {}},
                ],
            }
        ],
        "executorBindings": [],
        "pluginDependencies": [],
    }


class CapturingPipelineManager:
    """Fake GstPipelineManager that 'captures frames': it writes the
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
                    with open(token[len("location="):], "wb") as f:
                        f.write(JPEG_BYTES_IN)
        return dict(self.tag_values)


class RecordingInvoker:
    """Injectable Bedrock invoker returning a canned anomaly answer."""

    def __init__(self, answer=BEDROCK_ANSWER):
        self.answer = answer
        self.calls = []

    def __call__(self, model, prompt, images, region, max_tokens):
        self.calls.append({"model": model, "images": list(images)})
        return self.answer


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


def write_detections_sidecar(capture_root, payload=None):
    """Land the marshal-written detections sidecar in the run's (future)
    output dir, standing in for the record the pipeline would write."""
    output_dir = os.path.join(capture_root, WORKFLOW_ID, EXECUTION_ID)
    os.makedirs(output_dir, exist_ok=True)
    path = os.path.join(
        output_dir, "{0}.detections.json".format(CAPTURE_ID))
    with open(path, "w") as f:
        json.dump(RAW_DETECTIONS if payload is None else payload, f)
    return output_dir


def seed_run(session_factory, artifact_path):
    session = session_factory()
    try:
        session.add(WorkflowRegistration(
            id="wf-1:3", workflow_id=WORKFLOW_ID, version="3",
            arch=DEVICE_ARCH, artifact_path=str(artifact_path),
            status="registered", registered_at=int(time.time()),
        ))
        session.add(WorkflowExecution(
            id=EXECUTION_ID, registration_id="wf-1:3",
            started_at=int(time.time()), status="pending",
        ))
        session.commit()
    finally:
        session.close()
    return EXECUTION_ID


def get_execution(session_factory):
    session = session_factory()
    try:
        return session.get(WorkflowExecution, EXECUTION_ID)
    finally:
        session.close()


def assert_is_detection_entry(entry, x_min):
    assert isinstance(entry["id"], str) and len(entry["id"]) == 8
    int(entry["id"], 16)  # 8 hex chars (Detection_ID)
    assert entry["label"] == "blue box"
    assert entry["x_min"] == x_min


# ---------------------------------------------------------------------------
# Merge ordering: detections visible to the Bedrock processor (R1.1, R7.1)
# ---------------------------------------------------------------------------

class SpyBedrockProcessor:
    """Records the metadata and run_context the executor hands to
    ``process`` (new-keyword-aware double)."""

    def __init__(self):
        self.calls = []

    def bindings(self, document):
        return [
            binding for binding in (document.get("executorBindings") or [])
            if binding.get("binding") == "bedrock_inference"
        ]

    def process(self, document, tag_values, work_dir, run_context=None):
        self.calls.append({
            "tag_values": copy.deepcopy(tag_values),
            "run_context": run_context,
        })
        return dict(tag_values)


class LegacySpyBedrockProcessor:
    """A pre-feature double whose ``process`` accepts no new keywords —
    the executor must call it exactly as today (R7.3 back-compat)."""

    def __init__(self):
        self.calls = []

    def bindings(self, document):
        return [
            binding for binding in (document.get("executorBindings") or [])
            if binding.get("binding") == "bedrock_inference"
        ]

    def process(self, document, tag_values, work_dir):
        self.calls.append((document, dict(tag_values), work_dir))
        return dict(tag_values)


class TestMergeOrdering:
    def test_detections_merged_before_the_bedrock_processor_runs(
        self, tmp_path, session_factory, capture_root
    ):
        write_detections_sidecar(capture_root)
        document = make_document([bedrock_binding()])
        artifact_path = write_artifact_set(tmp_path / "art", compiled=document)
        execution_id = seed_run(session_factory, artifact_path)
        spy = SpyBedrockProcessor()

        WorkflowExecutor(
            session_factory=session_factory,
            pipeline_manager_factory=lambda: CapturingPipelineManager(),
            post_run_handler=lambda reg, doc, tags: None,
            bedrock_processor=spy,
        ).execute(execution_id)

        assert get_execution(session_factory).status == \
            EXECUTION_STATUS_COMPLETED
        assert len(spy.calls) == 1
        tags = spy.calls[0]["tag_values"]
        # The Detection_List (sorted left_to_right, Detection_ID'd) and
        # the count were merged BEFORE the processor ran (R1.1, R1.9).
        assert tags["detection_count"] == 2
        assert len(tags["detections"]) == 2
        assert_is_detection_entry(tags["detections"][0], x_min=10.0)
        assert_is_detection_entry(tags["detections"][1], x_min=210.0)
        assert tags["detections"][0]["id"] != tags["detections"][1]["id"]

    def test_run_context_carries_the_run_state(
        self, tmp_path, session_factory, capture_root
    ):
        output_dir = write_detections_sidecar(capture_root)
        document = make_document([bedrock_binding()])
        artifact_path = write_artifact_set(tmp_path / "art", compiled=document)
        execution_id = seed_run(session_factory, artifact_path)
        spy = SpyBedrockProcessor()

        WorkflowExecutor(
            session_factory=session_factory,
            pipeline_manager_factory=lambda: CapturingPipelineManager(),
            post_run_handler=lambda reg, doc, tags: None,
            bedrock_processor=spy,
        ).execute(execution_id)

        run_context = spy.calls[0]["run_context"]
        assert run_context is not None
        assert run_context.output_dir == output_dir
        assert run_context.capture_id == CAPTURE_ID
        # The registration's workflow.json graph document (the
        # detection_sort_order source), as write_artifact_set wrote it.
        assert run_context.graph_document == {
            "schemaVersion": 1, "nodes": [], "connections": []}
        assert run_context.node_status is not None
        # The context's metadata is the merged run metadata itself.
        assert run_context.tag_values["detection_count"] == 2

    def test_legacy_processor_double_is_called_exactly_as_today(
        self, tmp_path, session_factory, capture_root
    ):
        document = make_document([bedrock_binding()])
        artifact_path = write_artifact_set(tmp_path / "art", compiled=document)
        execution_id = seed_run(session_factory, artifact_path)
        spy = LegacySpyBedrockProcessor()

        WorkflowExecutor(
            session_factory=session_factory,
            pipeline_manager_factory=lambda: CapturingPipelineManager(),
            post_run_handler=lambda reg, doc, tags: None,
            bedrock_processor=spy,
        ).execute(execution_id)

        assert get_execution(session_factory).status == \
            EXECUTION_STATUS_COMPLETED
        assert len(spy.calls) == 1  # 3-positional call succeeded (R7.3)


# ---------------------------------------------------------------------------
# Cache identity: one run-state cache for the pump and the merge
# (design Property 1)
# ---------------------------------------------------------------------------

class TestBridgeCacheIdentity:
    def test_pump_injector_and_post_pipeline_merge_share_the_cache(
        self, tmp_path, session_factory, capture_root
    ):
        write_detections_sidecar(capture_root)
        document = make_bridged_document()
        artifact_path = write_artifact_set(tmp_path / "art", compiled=document)
        execution_id = seed_run(session_factory, artifact_path)

        pump_metadata = {}
        injectors = []

        def fake_bridged_runner(launch_string, bridges, latency_metrics=None,
                                frame_data=None, detections_injector=None):
            # Stand-in for the pump: ask the injector for the downstream
            # node's frame metadata (this builds + caches the list).
            injectors.append(detections_injector)
            if detections_injector is not None:
                pump_metadata.update(
                    detections_injector.metadata_for("py1"))
            return {}

        received = []
        WorkflowExecutor(
            session_factory=session_factory,
            pipeline_manager_factory=lambda: CapturingPipelineManager(),
            bridged_pipeline_runner=fake_bridged_runner,
            post_run_handler=lambda reg, doc, tags: received.append(tags),
        ).execute(execution_id)

        assert get_execution(session_factory).status == \
            EXECUTION_STATUS_COMPLETED
        # The executor constructed and passed an injector for the
        # downstream custom node (Requirement 1.10 wiring).
        assert len(injectors) == 1 and injectors[0] is not None
        # The pump saw the Detection_List...
        assert pump_metadata["detection_count"] == 2
        # ...and the post-pipeline merge reused the SAME cached list —
        # identical entries, identical Detection_IDs, one build per run
        # (design Property 1).
        assert len(received) == 1
        assert received[0]["detections"] is \
            injectors[0].cache[CACHE_KEY_DETECTIONS]
        assert received[0]["detections"] == pump_metadata["detections"]

    def test_runner_without_the_injector_keyword_keeps_working(
        self, tmp_path, session_factory, capture_root
    ):
        """A pre-feature injected bridged runner (no detections_injector
        keyword) is invoked exactly as today (R7.3 back-compat)."""
        write_detections_sidecar(capture_root)
        document = make_bridged_document()
        artifact_path = write_artifact_set(tmp_path / "art", compiled=document)
        execution_id = seed_run(session_factory, artifact_path)

        calls = []

        def legacy_runner(launch_string, bridges, latency_metrics=None,
                          frame_data=None):
            calls.append(launch_string)
            return {}

        WorkflowExecutor(
            session_factory=session_factory,
            pipeline_manager_factory=lambda: CapturingPipelineManager(),
            bridged_pipeline_runner=legacy_runner,
            post_run_handler=lambda reg, doc, tags: None,
        ).execute(execution_id)

        assert get_execution(session_factory).status == \
            EXECUTION_STATUS_COMPLETED
        assert len(calls) == 1


# ---------------------------------------------------------------------------
# Exclusion correctness: branch bindings publish once (R5.6, R5.7)
# ---------------------------------------------------------------------------

class TestBranchExclusion:
    def _run(self, tmp_path, session_factory, concurrent):
        """One run of bedrock b1 -> mqtt m_branch (branch-scoped) plus
        mqtt m_plain (non-branch), returning the recorded publishes."""
        document = make_document([
            bedrock_binding("b1"),
            mqtt_binding("m_branch", "t/branch", upstream=["b1"]),
            mqtt_binding("m_plain", "t/plain", upstream=["cam"]),
        ])
        artifact_path = write_artifact_set(tmp_path / "art", compiled=document)
        execution_id = seed_run(session_factory, artifact_path)

        published = []
        handler = OutputBindingProcessor(
            mqtt_publisher=lambda host, port, topic, payload, qos, **k:
                published.append(topic))
        processor = BedrockInferenceProcessor(
            invoker=RecordingInvoker(),
            output_processor=handler if concurrent else None,
        )
        WorkflowExecutor(
            session_factory=session_factory,
            pipeline_manager_factory=lambda: CapturingPipelineManager(),
            post_run_handler=handler,
            bedrock_processor=processor,
        ).execute(execution_id)
        return published

    def test_concurrent_path_publishes_each_branch_binding_exactly_once(
        self, tmp_path, session_factory, capture_root
    ):
        published = self._run(tmp_path, session_factory, concurrent=True)

        assert get_execution(session_factory).status == \
            EXECUTION_STATUS_COMPLETED
        # The branch binding published exactly once (in the Bedrock
        # completion path, never again post-run) and the non-branch
        # binding exactly once (post-run) — no double publish (R5.7).
        assert sorted(published) == ["t/branch", "t/plain"]

    def test_sequential_legacy_path_excludes_nothing(
        self, tmp_path, session_factory, capture_root
    ):
        """Without the output seam nothing publishes per-branch, so the
        post-run handler must run EVERY binding — today's behavior,
        byte-identical (R5.7, R7.1)."""
        published = self._run(tmp_path, session_factory, concurrent=False)

        assert get_execution(session_factory).status == \
            EXECUTION_STATUS_COMPLETED
        assert sorted(published) == ["t/branch", "t/plain"]

    def test_node_statuses_are_terminal_after_the_run(
        self, tmp_path, session_factory, capture_root
    ):
        """R5.6: the run reaches its terminal status with every binding
        node's outcome reported truthfully."""
        self._run(tmp_path, session_factory, concurrent=True)

        row = get_execution(session_factory)
        status = json.loads(row.node_status_json)
        assert status["b1"]["status"] == "success"
        assert status["m_branch"]["status"] == "success"
        assert status["m_plain"]["status"] == "success"


# ---------------------------------------------------------------------------
# Persistence content (R1.6, R4.5)
# ---------------------------------------------------------------------------

class TestPersistence:
    def test_persisted_metadata_carries_detections_and_nested_verdicts(
        self, tmp_path, session_factory, capture_root
    ):
        output_dir = write_detections_sidecar(capture_root)
        document = make_document([bedrock_binding("b1")])
        artifact_path = write_artifact_set(tmp_path / "art", compiled=document)
        execution_id = seed_run(session_factory, artifact_path)

        WorkflowExecutor(
            session_factory=session_factory,
            pipeline_manager_factory=lambda: CapturingPipelineManager(),
            post_run_handler=lambda reg, doc, tags: None,
            bedrock_processor=BedrockInferenceProcessor(
                invoker=RecordingInvoker()),
        ).execute(execution_id)

        assert get_execution(session_factory).status == \
            EXECUTION_STATUS_COMPLETED
        path = os.path.join(output_dir, "{0}.json".format(CAPTURE_ID))
        with open(path) as f:
            persisted = json.load(f)
        # Detection_List with Detection_IDs (R1.6).
        assert persisted["detection_count"] == 2
        assert_is_detection_entry(persisted["detections"][0], x_min=10.0)
        assert_is_detection_entry(persisted["detections"][1], x_min=210.0)
        # Nested per-node verdict keys (R4.5) beside the flat keys.
        assert persisted["bedrock"]["b1"]["is_anomalous"] is True
        assert persisted["bedrock"]["b1"]["confidence"] == 0.93
        assert persisted["bedrock"]["b1"]["text"] == BEDROCK_ANSWER
        assert persisted["is_anomalous"] is True
        assert persisted["confidence"] == 0.93


# ---------------------------------------------------------------------------
# Detection-less runs: byte-identical modulo the additive keys (R7.1, R7.3)
# ---------------------------------------------------------------------------

class TestDetectionLessRuns:
    def test_run_without_a_detections_record_is_byte_identical(
        self, tmp_path, session_factory, capture_root
    ):
        """No record, no sidecar, no new parameters: the Run_Metadata the
        output bindings see and the persisted JSON are EXACTLY the
        pre-feature content — the additive keys are absent, not empty
        (R1.8 vs R1.5 distinction, R7.1)."""
        document = make_document([bedrock_binding("b1")])
        artifact_path = write_artifact_set(tmp_path / "art", compiled=document)
        execution_id = seed_run(session_factory, artifact_path)

        received = []
        WorkflowExecutor(
            session_factory=session_factory,
            pipeline_manager_factory=lambda: CapturingPipelineManager(
                tag_values={"confidence": 0.1}),
            post_run_handler=lambda reg, doc, tags: received.append(tags),
            bedrock_processor=BedrockInferenceProcessor(
                invoker=RecordingInvoker()),
        ).execute(execution_id)

        assert get_execution(session_factory).status == \
            EXECUTION_STATUS_COMPLETED
        # Exact pre-feature metadata (plus the nested bedrock namespace
        # from tasks 7/10, additive by design): no detections keys.
        assert received == [{
            "is_anomalous": True,
            "confidence": 0.93,
            "bedrock_text": BEDROCK_ANSWER,
            "bedrock": {"b1": {
                "text": BEDROCK_ANSWER,
                "is_anomalous": True,
                "confidence": 0.93,
            }},
            "trigger": {},
        }]
        output_dir = os.path.join(capture_root, WORKFLOW_ID, EXECUTION_ID)
        with open(os.path.join(
                output_dir, "{0}.json".format(CAPTURE_ID))) as f:
            assert json.load(f) == received[0]

    def test_zero_detections_merge_an_empty_list_not_no_key(
        self, tmp_path, session_factory, capture_root
    ):
        """An empty detections map distinguishes 'ran with no detections'
        from 'no detection model' (R1.5)."""
        write_detections_sidecar(capture_root, payload={"detections": {}})
        document = make_document([])
        artifact_path = write_artifact_set(tmp_path / "art", compiled=document)
        execution_id = seed_run(session_factory, artifact_path)

        received = []
        WorkflowExecutor(
            session_factory=session_factory,
            pipeline_manager_factory=lambda: CapturingPipelineManager(
                tag_values={"is_anomalous": False, "confidence": 0.5}),
            post_run_handler=lambda reg, doc, tags: received.append(tags),
        ).execute(execution_id)

        assert get_execution(session_factory).status == \
            EXECUTION_STATUS_COMPLETED
        assert received[0]["detections"] == []
        assert received[0]["detection_count"] == 0
