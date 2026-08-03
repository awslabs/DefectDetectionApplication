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
"""Preservation property tests (Task 2) for workflow-output-bindings-fixes.

Property 5: Preservation — LLM containment and binding independence: a
recorded llm_inference error still lets the remaining bindings run, leaves
the run's terminal status exactly as today, preserves the
``metadata['llm'][nodeId]`` result shape, and an unresolved-placeholder
prompt still fails WITHOUT calling the API.

**Validates: Requirements 3.3, 3.4**

Observation-first, OBSERVED on the current (unfixed) tree:

* ``LlmInferenceProcessor.process`` NEVER raises: each binding's outcome
  merges under ``metadata['llm'][nodeId]`` as ``{'generated_text': text}``
  on success, ``{'error': str(e)}`` on an invoker failure, and
  ``{'error': 'unresolved placeholder NAME'}`` (WITHOUT calling the
  invoker) on an unrenderable prompt; every remaining binding is still
  processed in order, and the input tag values pass through unchanged;
* at the executor level a recorded llm error never fails the run: the
  post-run output bindings still execute and the run completes
  ``COMPLETED``.

The Defect B fix transfers recorded llm errors into ``node_status_json``
and persists outputs — it must NOT change the containment semantics, the
merge shape, or the run's terminal status decision. These tests MUST PASS
today and keep passing after the fix.

Runs with the hypothesis profiles registered in ``test/backend-test/
conftest.py`` (``fast`` = 25 examples locally, ``HYPOTHESIS_PROFILE=ci``
= 100).
"""
from hypothesis import given, settings
from hypothesis import strategies as st

from workflow_engine_test_utils import write_artifact_set

from executor_harness import (
    PLAIN_SEGMENTS,
    FakePipelineManager,
    get_execution,
    make_doc,
    seed_run,
)

from workflow_engine.output_bindings import (
    LlmInferenceProcessor,
    OutputBindingProcessor,
)
from workflow_engine.pipeline_executor import (
    EXECUTION_STATUS_COMPLETED,
    WorkflowExecutor,
)


# ---------------------------------------------------------------------------
# 1. Processor-level containment identity (Hypothesis)
# ---------------------------------------------------------------------------

#: Per-binding outcome plans: an invoker success, an invoker failure, or an
#: unresolved-placeholder prompt (which must never reach the invoker).
_TEXTS = st.one_of(
    st.sampled_from(["ok", "the part is defective", ""]),
    st.text(max_size=40),
)
_ERRORS = st.one_of(
    st.sampled_from([
        "Text_Generation_API returned 409 for model 'opt125m-smoke': "
        "{'model_name': 'opt125m-smoke', 'state': 'loading'}",
        "connection refused",
    ]),
    st.text(min_size=1, max_size=60),
)
_PLANS = st.one_of(
    st.tuples(st.just("ok"), _TEXTS),
    st.tuples(st.just("api_error"), _ERRORS),
    st.tuples(st.just("placeholder"), st.just(None)),
)


@st.composite
def _binding_plans(draw):
    plans = draw(st.lists(_PLANS, min_size=1, max_size=4))
    return [
        ("llm_inference_{0}".format(index + 1), plan)
        for index, plan in enumerate(plans)
    ]


def _document(binding_plans):
    bindings = []
    for node_id, (kind, _) in binding_plans:
        template = (
            "{missing_placeholder}" if kind == "placeholder"
            else "Describe the inspection result"
        )
        bindings.append({
            "nodeId": node_id,
            "binding": "llm_inference",
            "parameters": {"modelName": node_id,
                           "prompt_template": template},
        })
    return {"executorBindings": bindings}


@given(binding_plans=_binding_plans(),
       is_anomalous=st.booleans())
@settings(deadline=None)
def test_llm_processor_containment_identity(binding_plans, is_anomalous):
    """**Property 5: Preservation — LLM containment.** For any mix of
    succeeding/failing/unrenderable llm bindings, ``process`` never
    raises, runs EVERY binding (failures included) in order, records the
    observed per-node shapes, and passes the input tag values through.

    **Validates: Requirements 3.3, 3.4**
    """
    plans = dict(binding_plans)
    invoked = []

    def invoker(model_name, prompt, parameters):
        invoked.append(model_name)
        kind, value = plans[model_name]
        assert kind != "placeholder", (
            "PRESERVATION REGRESSION (Property 5): the invoker was called "
            "for an unrenderable prompt (node {0})".format(model_name))
        if kind == "api_error":
            raise RuntimeError(value)
        return value

    tag_values = {"is_anomalous": is_anomalous, "confidence": 0.5}
    processor = LlmInferenceProcessor(invoker=invoker)
    metadata = processor.process(_document(binding_plans), dict(tag_values))

    # Input tag values pass through unchanged.
    for key, value in tag_values.items():
        assert metadata[key] == value

    # Every binding — including every one AFTER a failure — was processed
    # and recorded with today's shape.
    assert set(metadata["llm"]) == {node_id for node_id, _ in binding_plans}
    for node_id, (kind, value) in binding_plans:
        entry = metadata["llm"][node_id]
        if kind == "ok":
            assert entry == {"generated_text": value}, (
                "PRESERVATION REGRESSION (Property 5): success shape "
                "changed for {0}: {1!r}".format(node_id, entry))
        elif kind == "api_error":
            assert entry == {"error": value}, (
                "PRESERVATION REGRESSION (Property 5): error shape changed "
                "for {0}: {1!r}".format(node_id, entry))
        else:
            assert entry == {
                "error": "unresolved placeholder missing_placeholder"}, (
                "PRESERVATION REGRESSION (Property 5): placeholder-failure "
                "shape changed for {0}: {1!r}".format(node_id, entry))

    # The invoker was called exactly once per renderable binding, in
    # binding order (containment: a failure never stops the loop).
    expected_invocations = [
        node_id for node_id, (kind, _) in binding_plans
        if kind != "placeholder"
    ]
    assert invoked == expected_invocations, (
        "PRESERVATION REGRESSION (Property 5): invoker call sequence "
        "changed: {0!r} != {1!r}".format(invoked, expected_invocations))


# ---------------------------------------------------------------------------
# 2. Executor-level containment: run status + downstream bindings (example)
# ---------------------------------------------------------------------------

LLM_ERROR = ("Text_Generation_API returned 409 for model 'opt125m-smoke': "
             "{'model_name': 'opt125m-smoke', 'state': 'loading'}")

_DOC = make_doc(
    segments=PLAIN_SEGMENTS,
    bindings=[
        {"nodeId": "llm_inference_1", "binding": "llm_inference",
         "parameters": {"modelName": "opt125m-smoke",
                        "prompt_template": "Describe the inspection"}},
        {"nodeId": "mqtt_publish_1", "binding": "mqtt_publish",
         "parameters": {"topic": "factory/line1/inspection",
                        "greengrass": True}},
    ],
)


def test_recorded_llm_error_leaves_run_completed_and_bindings_running(
        tmp_path, session_factory, capture_root):
    """**Property 5: Preservation — LLM containment at the executor.** A
    recorded llm error never fails the run: the downstream mqtt_publish
    binding still executes, the run completes COMPLETED, and the handler
    receives the merged ``metadata['llm']`` with today's error shape.

    **Validates: Requirements 3.3, 3.4**
    """
    artifact_path = write_artifact_set(tmp_path, compiled=_DOC)
    execution_id = seed_run(session_factory, artifact_path)

    def invoker(model_name, prompt, parameters):
        raise RuntimeError(LLM_ERROR)

    published = []
    received_metadata = []

    class _RecordingHandler(OutputBindingProcessor):
        def process(self, registration, document, tag_values):
            received_metadata.append(dict(tag_values))
            return OutputBindingProcessor.process(
                self, registration, document, tag_values)

    handler = _RecordingHandler(
        greengrass_publisher=lambda topic, payload, qos:
            published.append((topic, payload, qos)),
    )
    WorkflowExecutor(
        session_factory=session_factory,
        pipeline_manager_factory=lambda: FakePipelineManager(
            tag_values={"is_anomalous": True}),
        llm_processor=LlmInferenceProcessor(invoker=invoker),
        post_run_handler=handler,
    ).execute(execution_id)

    row = get_execution(session_factory, execution_id)

    # Containment: the recorded llm error never fails the run.
    assert row.status == EXECUTION_STATUS_COMPLETED, (
        "PRESERVATION REGRESSION (Property 5): a recorded llm error "
        "changed the run's terminal status to {0!r} (error: {1!r})".format(
            row.status, row.error))

    # The remaining binding still ran (containment: a recorded llm error
    # never gates or aborts the other bindings).
    assert len(published) == 1, (
        "the mqtt_publish binding did not run after the llm error: "
        "{0!r}".format(published))
    assert published[0][0] == "factory/line1/inspection"
    assert published[0][2] == 0
    assert received_metadata and received_metadata[0]["llm"] == {
        "llm_inference_1": {"error": LLM_ERROR}}, (
        "PRESERVATION REGRESSION (Property 5): the llm error shape "
        "reaching downstream bindings changed: {0!r}".format(
            received_metadata))
