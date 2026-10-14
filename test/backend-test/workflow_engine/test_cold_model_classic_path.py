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
"""Cold-model gate wiring on the CLASSIC run path
(`POST /workflows/{id}/run`).

Bugfix: `.kiro/specs/cold-model-first-run-failure/`, tasks 1.3 and 3.2.

**Why these are static (source-level) assertions.** `endpoints/workflow.py`
cannot be imported on the host: importing it starts multiprocessing workers
and pulls in GStreamer, Triton and the image-source machinery, none of which
exists off-device (observed here as `ModuleNotFoundError: No module named
'encodings'` from a spawned interpreter). A test that tried to drive the
handler would assert nothing about the gate.

So this suite pins the three things about the classic path that ARE
statically checkable and that a future edit could silently break, and the
GATE'S BEHAVIOUR is covered where it is testable — exhaustively — in
`test/backend-test/dda_triton/test_model_readiness.py`, which drives the same
`ensure_model_ready` this path calls. The classic path's end-to-end runtime
behaviour is verified on hardware (spec task 6(b)), which is where the
original 2026-08-14 report was made and the only place the folder-source
`failed/` behaviour can honestly be observed.

The ordering assertion is the load-bearing one: Requirement 2.2 (a cold
run must not consume its input) is satisfied by the gate preceding the
pipeline, not by a special case in the failure handler (design.md Decision 7).

_Requirements: 2.1, 2.2, 2.3, Property 1_
"""
import pathlib
import re


ENDPOINT = (
    pathlib.Path(__file__).resolve().parents[3]
    / "src" / "backend" / "endpoints" / "workflow.py"
)


def source():
    return ENDPOINT.read_text()


def run_handler_source():
    """The body of `run_inference_for_stream`, from its def to the next
    top-level def."""
    text = source()
    start = text.index("async def run_inference_for_stream(")
    rest = text[start + 1:]
    match = re.search(r"\n(?:async )?def ", rest)
    return rest[: match.start()] if match else rest


# ------------------------------------------------------------ the wiring

def test_the_run_path_calls_the_readiness_gate():
    """Nothing on this path read model state before this bugfix.
    _Requirements: 2.1_"""
    assert "validate_model_readiness(" in run_handler_source(), (
        "run_inference_for_stream does not call the readiness gate; a run "
        "against a still-loading model will fail inside emltriton with a "
        "message naming neither the model nor its state"
    )


def test_the_gate_runs_before_the_pipeline_is_built():
    """design.md Decision 7 — and the reason Requirement 2.2 needs no
    special case in the failure handler.

    `execute_workflow_pipeline`'s catch-all moves a folder-source image to
    `failed/` for ANY pipeline exception. Because the gate precedes the
    pipeline, a cold model never reaches it and the input survives. Reverse
    these two statements and that harm returns, silently.
    """
    body = run_handler_source()
    gate_at = body.find("validate_model_readiness(")
    pipeline_at = body.find("configure_image_source_and_run_pipeline(")

    assert gate_at != -1, "the readiness gate is not called on this path"
    assert pipeline_at != -1, (
        "configure_image_source_and_run_pipeline was renamed; revisit this "
        "test, the ordering claim it protects still applies"
    )
    assert gate_at < pipeline_at, (
        "the readiness gate must run BEFORE the pipeline is built, otherwise "
        "a transient not-ready model still destroys the run's input image"
    )


def test_the_gate_reports_unavailability_not_a_pipeline_error():
    """Requirement 2.3: the reply names the model and its state. The gate
    raises 503 — the status this module already uses for "cannot run" — and
    carries the helper's message, never the generic pipeline text."""
    text = source()
    start = text.index("def validate_model_readiness(")
    rest = text[start + 1:]
    match = re.search(r"\n(?:async )?def ", rest)
    gate = rest[: match.start()] if match else rest

    assert "ensure_model_ready(" in gate, (
        "the gate does not call the shared readiness helper; design.md "
        "Decision 6 requires ONE helper for both run paths so they cannot "
        "drift"
    )
    assert "status_code=503" in gate
    assert "outcome.message" in gate, (
        "the 503 must carry the helper's message, which names the model and "
        "its state"
    )


def test_the_gate_is_contained():
    """The gate must never be the sole reason a previously working run stops
    working: a failure of the gate's own plumbing proceeds to the pipeline,
    which then behaves exactly as it did before the gate existed."""
    text = source()
    start = text.index("def validate_model_readiness(")
    rest = text[start + 1:]
    match = re.search(r"\n(?:async )?def ", rest)
    gate = rest[: match.start()] if match else rest

    # Two contained blocks: the deferred import and the call itself.
    assert gate.count("except Exception") >= 2, (
        "the gate's import and its call must both be contained"
    )
    # A workflow with no model configured never consults Triton.
    assert "featureConfigurations" in gate
    assert "return" in gate
