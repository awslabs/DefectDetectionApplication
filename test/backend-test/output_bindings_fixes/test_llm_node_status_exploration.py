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
"""Bug-condition exploration test — case 4: executor-binding node invisible
in node_status_json (workflow-output-bindings-fixes, Defect B,
``isBugCondition_B``).

Property 2: Bug Condition — truthful terminal status for executor-binding
nodes.

**These tests assert the FIXED (post-fix) engine behavior, so they are
EXPECTED TO FAIL on the UNFIXED tree.** The failure is the counterexample
confirming Defect B's second gap: the ``NodeStatusCollector`` is built only
from ``rendering.element_name_map(document)`` (pipeline elements), and
``llm_inference`` compiles to an executor binding with no pipeline element —
so llm_inference_1 never appears in the persisted ``node_status_json``,
whether its binding failed (the live 409) or succeeded. The run-view graph
resolves absent nodes to "pending" (``graphGeometry.ts`` nodeVisual), so the
node sits "pending" forever on a COMPLETED run.

Expected counterexample on the UNFIXED tree (live run 85bf7a61):
    node_status_json == {"n1": {"status": "success"}} — llm_inference_1
    absent despite its recorded 409 error; run status COMPLETED.

The SAME tests are re-run in task 3.4 against the fixed engine (collector
seeded with executorBindings node ids; llm outcomes marked from
``metadata['llm']``), where they must PASS.

Harness per ``test_workflow_pipeline_executor.py``: fake pipeline manager,
temp sqlite sessions, injected llm invoker — no GStreamer, no HTTP.

Validates: Requirements 1.5, 1.6 (expected behavior 2.5, 2.6)
"""
from workflow_engine_test_utils import write_artifact_set

from executor_harness import (
    FakePipelineManager,
    LLM_BINDING,
    PLAIN_SEGMENTS,
    get_execution,
    make_doc,
    node_status_map,
    seed_run,
)

from workflow_engine.output_bindings import LlmInferenceProcessor
from workflow_engine.pipeline_executor import (
    EXECUTION_STATUS_COMPLETED,
    WorkflowExecutor,
)

LLM_DOC = make_doc(segments=PLAIN_SEGMENTS, bindings=[LLM_BINDING])

#: The exact live-device failure the node recorded (execution 85bf7a61).
LIVE_409_ERROR = (
    "Text_Generation_API returned 409 for model 'opt125m-smoke': "
    "{'model_name': 'opt125m-smoke', 'state': 'loading'}"
)


def _run(tmp_path, session_factory, invoker):
    artifact_path = write_artifact_set(tmp_path, compiled=LLM_DOC)
    execution_id = seed_run(session_factory, artifact_path)
    WorkflowExecutor(
        session_factory=session_factory,
        pipeline_manager_factory=lambda: FakePipelineManager(
            tag_values={"is_anomalous": True}),
        llm_processor=LlmInferenceProcessor(invoker=invoker),
    ).execute(execution_id)
    return get_execution(session_factory)


class TestFailedLlmBindingStatus:
    def test_failed_llm_node_is_terminal_failure_with_detail(
        self, tmp_path, session_factory, capture_root
    ):
        """A failed llm_inference binding must persist as that node's
        terminal ``failure`` status with the error detail, so the run view
        shows the failure instead of "pending".

        EXPECTED FAILURE on the unfixed tree: the recorded error lives only
        in in-memory ``metadata['llm']`` and the run log; llm_inference_1
        is absent from node_status_json while the run completes COMPLETED.

        Validates: Requirements 1.4, 1.5, 1.6 (expected behavior 2.4, 2.5)
        """
        def invoker(model_name, prompt, parameters):
            raise RuntimeError(LIVE_409_ERROR)

        row = _run(tmp_path, session_factory, invoker)

        # Binding independence (unchanged behavior, requirement 3.4): the
        # contained llm failure never fails the run.
        assert row.status == EXECUTION_STATUS_COMPLETED
        statuses = node_status_map(row)
        assert "llm_inference_1" in statuses, (
            "COUNTEREXAMPLE (Defect B): llm_inference_1 is absent from the "
            "persisted node_status_json ({0!r}) although its binding "
            "recorded {1!r}; the run view resolves the absent node to "
            "'pending' forever".format(statuses, LIVE_409_ERROR))
        entry = statuses["llm_inference_1"]
        assert entry["status"] == "failure", (
            "COUNTEREXAMPLE (Defect B): llm_inference_1 status is {0!r}, "
            "not 'failure'".format(entry["status"]))
        assert "409" in entry.get("detail", ""), (
            "COUNTEREXAMPLE (Defect B): llm_inference_1 carries no error "
            "detail (entry: {0!r}); expected the recorded 409 state "
            "information".format(entry))


class TestSuccessfulLlmBindingStatus:
    def test_successful_llm_node_is_terminal_success(
        self, tmp_path, session_factory, capture_root
    ):
        """A successful llm_inference binding must persist as that node's
        terminal ``success`` status — executor-binding nodes must never
        remain absent ("pending") after the run ends.

        EXPECTED FAILURE on the unfixed tree: even a successful
        llm_inference_1 is absent from node_status_json (only
        element-mapped nodes are tracked).

        Validates: Requirements 1.6 (expected behavior 2.6)
        """
        row = _run(
            tmp_path, session_factory,
            lambda model_name, prompt, parameters: "No visible defects.")

        assert row.status == EXECUTION_STATUS_COMPLETED
        statuses = node_status_map(row)
        assert "llm_inference_1" in statuses, (
            "COUNTEREXAMPLE (Defect B): llm_inference_1 is absent from the "
            "persisted node_status_json ({0!r}) although its binding "
            "succeeded on a COMPLETED run — rendered 'pending' forever"
            .format(statuses))
        assert statuses["llm_inference_1"]["status"] == "success", (
            "COUNTEREXAMPLE (Defect B): llm_inference_1 status is {0!r}, "
            "not 'success'".format(statuses["llm_inference_1"]["status"]))
