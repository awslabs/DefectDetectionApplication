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
"""Bug-condition exploration test — case 5: llm output not persisted
(workflow-output-bindings-fixes, Defect B, ``isBugCondition_B``).

Property 2: Bug Condition — persisted llm output.

**This test asserts the FIXED (post-fix) engine behavior, so it is
EXPECTED TO FAIL on the UNFIXED tree.** The failure is the counterexample
confirming Defect B's third gap: a successful llm_inference's generated
text is merged only into the in-memory tag values (and the run log's
"completed; tags:" line) — nothing writes it into the run's artifact
directory, so the user has nowhere to find where the output went. On live
run 85bf7a61 the run directory held only ``.jpg`` and ``run.log``.

Expected counterexample on the UNFIXED tree:
    ``{output_dir}/{capture_id}.json`` does not exist after a COMPLETED run
    whose llm_inference binding produced generated text.

The SAME test is re-run in task 3.4 against the fixed engine (run metadata
JSON written to ``{output_dir}/{capture_id}.json`` after post-run
processing, carrying the ``llm`` section), where it must PASS.

Validates: Requirements 1.7 (expected behavior 2.7)
"""
import json
import os

from workflow_engine_test_utils import write_artifact_set

from executor_harness import (
    CAPTURE_ID,
    CAPTURE_SEGMENTS,
    FakePipelineManager,
    LLM_BINDING,
    get_execution,
    make_doc,
    seed_run,
)

from workflow_engine.output_bindings import LlmInferenceProcessor
from workflow_engine.pipeline_executor import (
    EXECUTION_STATUS_COMPLETED,
    WorkflowExecutor,
)

#: The live workflow shape: tritonless capture pipeline + llm binding
#: (folder_source -> llm_inference -> capture).
LLM_CAPTURE_DOC = make_doc(segments=CAPTURE_SEGMENTS, bindings=[LLM_BINDING])

GENERATED_TEXT = "The part shows no visible defects."


def test_generated_text_is_persisted_into_the_run_directory(
    tmp_path, session_factory, capture_root
):
    """A successful llm_inference on a run with a per-run artifact
    directory must persist its generated text into
    ``{output_dir}/{capture_id}.json`` (the ``llm`` section).

    EXPECTED FAILURE on the unfixed tree: no such file exists — the text
    lives only in in-memory tag values that die with the executor thread.

    Validates: Requirements 1.7 (expected behavior 2.7)
    """
    artifact_path = write_artifact_set(tmp_path, compiled=LLM_CAPTURE_DOC)
    execution_id = seed_run(session_factory, artifact_path)

    WorkflowExecutor(
        session_factory=session_factory,
        pipeline_manager_factory=lambda: FakePipelineManager(
            tag_values={"is_anomalous": False}),
        llm_processor=LlmInferenceProcessor(
            invoker=lambda model_name, prompt, parameters: GENERATED_TEXT),
    ).execute(execution_id)

    row = get_execution(session_factory)
    assert row.status == EXECUTION_STATUS_COMPLETED
    assert row.output_dir, "the capture run recorded no output_dir"

    metadata_path = os.path.join(row.output_dir, CAPTURE_ID + ".json")
    assert os.path.isfile(metadata_path), (
        "COUNTEREXAMPLE (Defect B): no run metadata JSON at {0!r}; the run "
        "directory holds {1!r} — the llm generated text was persisted "
        "nowhere (in-memory tag values only)".format(
            metadata_path, sorted(os.listdir(row.output_dir))))

    with open(metadata_path, encoding="utf-8") as f:
        metadata = json.load(f)
    llm_section = metadata.get("llm") or {}
    assert llm_section.get("llm_inference_1", {}).get("generated_text") \
        == GENERATED_TEXT, (
            "COUNTEREXAMPLE (Defect B): the run metadata JSON carries no "
            "llm generated_text for llm_inference_1 (llm section: {0!r})"
            .format(llm_section))
