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
"""Unit tests for the executor's LLM work_dir wiring and node-failure
marking (edge-vlm-image-inference task 2.5).

- Requirement 2.4: the WorkflowExecutor passes the per-run work directory
  as the THIRD positional argument to
  ``self._llm_processor.process(document, tag_values, work_dir)`` —
  asserted with a spy processor recording the raw positional call.
- Requirement 5.2: an llm_inference binding whose captured 'in' frame
  cannot be read produces a Node_Error_Record and the executor marks THAT
  node ``failure`` in the run's node status map (``node_status_json``)
  while independent nodes run to completion and the run itself completes.
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

from workflow_engine import gst_plugins
from workflow_engine.models import WorkflowExecution, WorkflowRegistration
from workflow_engine.output_bindings import LlmInferenceProcessor
from workflow_engine.pipeline_executor import (
    EXECUTION_STATUS_COMPLETED,
    WorkflowExecutor,
)


# ---------------------------------------------------------------------------
# Fixture helpers (established style of test_workflow_llm_inference.py)
# ---------------------------------------------------------------------------

def llm_binding(node_id="llm1", model="opt-125m", capture_paths=None,
                template="Summarize: {is_anomalous}"):
    binding = {
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
        "downstreamNodeIds": [],
    }
    if capture_paths is not None:
        binding["capturePaths"] = capture_paths
    return binding


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


class FakePipelineManager:
    def __init__(self, tag_values=None):
        self.tag_values = tag_values or {}
        self.calls = []

    def run_pipeline(self, pipeline_str, frame_data=None, latency_metrics=None,
                     status_sink=None):
        self.calls.append(pipeline_str)
        return dict(self.tag_values)


class SpyLlmProcessor:
    """Spy LlmInferenceProcessor recording process()'s raw positional args.

    Records whether the third positional argument (work_dir) named an
    existing directory AT CALL TIME — the executor removes the per-run
    work dir in its finally block, so the check cannot happen after
    ``execute`` returns."""

    def __init__(self):
        self.process_calls = []

    def bindings(self, document):
        return [
            binding for binding in (document.get("executorBindings") or [])
            if binding.get("binding") == "llm_inference"
        ]

    def process(self, *args):
        work_dir = args[2] if len(args) > 2 else "<missing>"
        self.process_calls.append({
            "args": args,
            "work_dir_isdir": (
                isinstance(work_dir, str) and os.path.isdir(work_dir)
            ),
        })
        # Return the metadata unchanged (no llm outcomes, no errors).
        return dict(args[1])


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


def run_executor(session_factory, artifact_root, document, llm_processor,
                 tag_values=None):
    artifact_path = write_artifact_set(artifact_root, compiled=document)
    execution_id = seed_run(session_factory, artifact_path)
    executor = WorkflowExecutor(
        session_factory=session_factory,
        pipeline_manager_factory=lambda: FakePipelineManager(
            tag_values=tag_values or {"is_anomalous": True}),
        post_run_handler=lambda reg, doc, tags: None,
        llm_processor=llm_processor,
    )
    executor.execute(execution_id)


# ---------------------------------------------------------------------------
# Requirement 2.4 — executor passes work_dir to the LLM processor
# ---------------------------------------------------------------------------

class TestExecutorPassesWorkDir:
    def test_work_dir_is_third_positional_argument(
        self, tmp_path, session_factory
    ):
        """A document whose llm binding carries {work_dir} capturePaths
        makes the executor create a per-run work dir and pass it as the
        third positional argument to process()."""
        document = make_document([llm_binding(
            capture_paths={"in": "{work_dir}/vlm_frame_cam.jpg"})])
        spy = SpyLlmProcessor()

        run_executor(session_factory, tmp_path, document, spy)

        assert len(spy.process_calls) == 1
        call = spy.process_calls[0]
        # Exactly the 3-positional-argument invocation
        # process(document, tag_values, work_dir) (Requirement 2.4).
        assert len(call["args"]) == 3
        received_document, received_tags, received_work_dir = call["args"]
        assert received_document["workflowId"] == "wf-1"
        assert received_tags["is_anomalous"] is True
        # The third argument is the per-run work directory: a real
        # directory that existed at call time.
        assert isinstance(received_work_dir, str)
        assert call["work_dir_isdir"] is True
        assert get_execution(session_factory).status == \
            EXECUTION_STATUS_COMPLETED

    def test_pre_feature_document_passes_none_work_dir(
        self, tmp_path, session_factory
    ):
        """A document with no {work_dir} references (pre-feature package)
        still uses the 3-argument call shape, with work_dir None."""
        document = make_document([llm_binding()])  # no capturePaths
        spy = SpyLlmProcessor()

        run_executor(session_factory, tmp_path, document, spy)

        assert len(spy.process_calls) == 1
        args = spy.process_calls[0]["args"]
        assert len(args) == 3
        assert args[2] is None
        assert get_execution(session_factory).status == \
            EXECUTION_STATUS_COMPLETED


# ---------------------------------------------------------------------------
# Requirement 5.2 — image-read failure marks THAT node failed in the
# node status map; independent nodes complete
# ---------------------------------------------------------------------------

class RecordingInvoker:
    """Injectable Text_Generation_API invoker recording raw positional
    args (arity distinguishes image-carrying from text-only calls)."""

    def __init__(self, text="generated answer"):
        self.text = text
        self.calls = []

    def __call__(self, *args):
        self.calls.append(args)
        return self.text


class TestImageReadFailureMarksNode:
    def test_unreadable_frame_marks_node_failed_others_complete(
        self, tmp_path, session_factory
    ):
        """llm_bad's captured 'in' frame is never written (unreadable) →
        Node_Error_Record, node marked failure in node_status_json; the
        independent llm_ok binding and the pipeline node complete, and
        the run itself completes (binding independence)."""
        document = make_document([
            llm_binding(
                node_id="llm_bad",
                capture_paths={"in": "{work_dir}/missing_frame.jpg"},
            ),
            llm_binding(node_id="llm_ok"),  # text-only, independent
        ])
        invoker = RecordingInvoker(text="ok answer")

        run_executor(
            session_factory, tmp_path, document,
            LlmInferenceProcessor(invoker=invoker),
        )

        row = get_execution(session_factory)
        # The run completes: per-node containment, not a run failure.
        assert row.status == EXECUTION_STATUS_COMPLETED

        status = json.loads(row.node_status_json)
        # THAT node is failure, with the Node_Error_Record naming the
        # node, the port, and the unreadable path retained as detail.
        assert status["llm_bad"]["status"] == "failure"
        assert "llm_bad" in status["llm_bad"]["detail"]
        assert "'in'" in status["llm_bad"]["detail"]
        assert "missing_frame.jpg" in status["llm_bad"]["detail"]
        # Independent nodes ran to completion: the other llm binding and
        # the pipeline element node are success.
        assert status["llm_ok"]["status"] == "success"
        assert status["cam"]["status"] == "success"

        # The failed binding never reached the invoker; the independent
        # binding was invoked once, text-only (3-arg pre-feature form).
        assert len(invoker.calls) == 1
        assert invoker.calls[0][0] == "opt-125m"
        assert len(invoker.calls[0]) == 3
