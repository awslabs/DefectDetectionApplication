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
"""Bug-condition exploration test — case 6: empty-basename capture
unrepaired, no metadata JSON (workflow-output-bindings-fixes, Defect C,
``isBugCondition_C``).

Property 3: Bug Condition — tritonless capture artifacts named and
described.

**These tests assert the FIXED (post-fix) engine behavior, so they are
EXPECTED TO FAIL on the UNFIXED tree.** Each failure is the counterexample
confirming Defect C: in a tritonless capture pipeline (folder_source
compiles to plain ``filesrc``; llm_inference is an executor binding, not an
element) no element attaches a buffer correlation id — only ``emltriton``
does, via ``_inject_inference_metadata``. ``emlcapture`` therefore publishes
with ``c_id = ""`` and the message broker writes ``"" + ".jpg"`` — a file
literally named ``.jpg`` (empty basename). The ``meta`` routing is likewise
empty (no declared emltriton outputs, run log ``meta=<none>``), so no
metadata JSON is ever written. Live run 85bf7a61's directory:
``/aws_dda/captures/1f0b4c0c-.../14f0b38b-.../`` = ``.jpg`` (679428 bytes)
+ ``run.log`` only.

The broker's tritonless product is simulated by the fake pipeline manager
writing a bare ``.jpg`` into the run's ``output_dir`` during the pipeline
run; on unfixed code there is no repair step to call — the file staying
``.jpg`` and the missing ``{capture_id}.json`` ARE the failures.

Expected counterexamples on the UNFIXED tree:
    run dir contains ``.jpg`` (not ``wf-1-exec-1.jpg``) after a COMPLETED
    run; no ``wf-1-exec-1.json`` metadata file exists.

The SAME tests are re-run in task 3.4 against the fixed engine (post-run
empty-basename repair to ``{capture_id}.{ext}``; run metadata JSON written),
where they must PASS.

Validates: Requirements 1.8, 1.9 (expected behavior 2.8, 2.9)
"""
import os

from workflow_engine_test_utils import write_artifact_set

from executor_harness import (
    CAPTURE_ID,
    CAPTURE_SEGMENTS,
    EXECUTION_ID,
    FakePipelineManager,
    WORKFLOW_ID,
    get_execution,
    make_doc,
    seed_run,
)

from workflow_engine.pipeline_executor import (
    EXECUTION_STATUS_COMPLETED,
    WorkflowExecutor,
)

#: The live workflow's capture side: a tritonless terminal-emlcapture
#: document (isBugCondition_C — no correlation-id-attaching element).
TRITONLESS_CAPTURE_DOC = make_doc(segments=CAPTURE_SEGMENTS, bindings=[])


def _run_with_broker_simulation(tmp_path, session_factory, capture_root):
    """Execute the tritonless capture run; the fake pipeline manager plays
    the message broker writing ``{output_dir}/"" + ".jpg"`` (the empty
    correlation id product) during the pipeline run."""
    artifact_path = write_artifact_set(
        tmp_path, compiled=TRITONLESS_CAPTURE_DOC)
    execution_id = seed_run(session_factory, artifact_path)
    output_dir = os.path.join(capture_root, WORKFLOW_ID, EXECUTION_ID)

    def broker_writes_empty_basename_jpg():
        os.makedirs(output_dir, exist_ok=True)
        with open(os.path.join(output_dir, ".jpg"), "wb") as f:
            f.write(b"\xff\xd8\xff\xe0 fake jpeg bytes")

    WorkflowExecutor(
        session_factory=session_factory,
        pipeline_manager_factory=lambda: FakePipelineManager(
            tag_values={"is_anomalous": False},
            on_run=broker_writes_empty_basename_jpg),
    ).execute(execution_id)

    row = get_execution(session_factory)
    assert row.status == EXECUTION_STATUS_COMPLETED
    assert row.output_dir == output_dir, (
        "the capture run recorded output_dir {0!r}, expected {1!r}"
        .format(row.output_dir, output_dir))
    return output_dir


class TestEmptyBasenameRepair:
    def test_bare_jpg_is_repaired_to_capture_id_jpg(
        self, tmp_path, session_factory, capture_root
    ):
        """After a tritonless capture run the frame must be named
        ``{capture_id}.jpg`` (consistent with what ``run_artifacts``
        resolves for Triton runs) and no empty-basename file may remain.

        EXPECTED FAILURE on the unfixed tree: no repair step exists — the
        directory still holds the broker's literal ``.jpg``.

        Validates: Requirements 1.8 (expected behavior 2.8)
        """
        output_dir = _run_with_broker_simulation(
            tmp_path, session_factory, capture_root)
        entries = sorted(os.listdir(output_dir))

        assert ".jpg" not in entries, (
            "COUNTEREXAMPLE (Defect C): the run directory still holds the "
            "empty-basename '.jpg' after the run (entries: {0!r}) — the "
            "broker's c_id was empty (no emltriton to attach a correlation "
            "id) and nothing repaired the name".format(entries))
        assert CAPTURE_ID + ".jpg" in entries, (
            "COUNTEREXAMPLE (Defect C): no {0!r} in the run directory "
            "(entries: {1!r}); run_artifacts.base_output_image_path "
            "resolves {0!r}, so the run view cannot display the frame"
            .format(CAPTURE_ID + ".jpg", entries))


class TestRunMetadataJson:
    def test_metadata_json_is_written_into_the_run_directory(
        self, tmp_path, session_factory, capture_root
    ):
        """A completed run with a per-run artifact directory must leave a
        ``{capture_id}.json`` metadata file carrying the run's inference
        metadata.

        EXPECTED FAILURE on the unfixed tree: the tritonless ``meta``
        routing is empty (``meta=<none>`` in the live run log) and the
        engine writes no metadata JSON itself — the directory holds only
        the frame and run.log.

        Validates: Requirements 1.9 (expected behavior 2.9)
        """
        output_dir = _run_with_broker_simulation(
            tmp_path, session_factory, capture_root)
        entries = sorted(os.listdir(output_dir))

        assert CAPTURE_ID + ".json" in entries, (
            "COUNTEREXAMPLE (Defect C): no {0!r} metadata JSON in the run "
            "directory (entries: {1!r}) — the run's inference metadata has "
            "no on-disk destination, matching the live evidence "
            "('.jpg' + 'run.log' only, meta=<none>)"
            .format(CAPTURE_ID + ".json", entries))
