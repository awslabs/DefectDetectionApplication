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
"""Property tests for LlmInferenceProcessor behavior invariance under
image attachment (task 2.4).

**Feature: edge-vlm-image-inference, Property 10: Processor behavior
invariance under image attachment**

*For any* ``llm_inference`` binding with a rendered prompt, the prompt
text, anomaly-mode instruction appending, verdict parsing, and error
containment produced by the processor SHALL be identical whether or not
an image is attached — the image only adds the ``image`` argument to
the invocation.

**Validates: Requirements 5.1, 6.4**

Method: the same binding is processed twice with an injected recording
invoker — once with no ``capturePaths`` (text-only) and once with
``capturePaths`` naming a readable captured frame under a real tmp work
dir — across randomly drawn prompt templates (with a resolved
placeholder), anomaly modes, and invoker outcomes (freeform text, valid
verdict JSON, raised API error). The two runs must produce identical
merged metadata and identical invocations except for the image
argument. A trailing control binding proves processing always continues
after a contained failure (Requirement 5.1).

Runs with the hypothesis profiles registered in this directory's
conftest (``engine-fast`` = 25 examples locally,
``HYPOTHESIS_PROFILE=ci`` = 100); no hardcoded ``max_examples``.
"""
import base64
import copy
import json
import os
import tempfile

from hypothesis import given, settings
from hypothesis import strategies as st

from workflow_engine.output_bindings import (
    BEDROCK_JSON_INSTRUCTION,
    LlmInferenceProcessor,
)

MODEL_UNDER_TEST = "model-under-test"
CONTROL_MODEL = "control-model"
CONTROL_TEXT = "control ok"
FRAME_FILE = "bedrock_frame_cam.jpg"

# Prompt/answer fragments: printable, brace-free (braces would either be
# placeholder syntax in templates or accidental JSON in answers).
_FRAGMENT = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz0123456789 .,:!?-",
    min_size=0,
    max_size=40,
)

# A valid anomaly-verdict answer: the canonical JSON object the shared
# parser accepts.
_VERDICT_ANSWERS = st.fixed_dictionaries({
    "is_anomalous": st.booleans(),
    "confidence": st.floats(min_value=0.0, max_value=1.0,
                            allow_nan=False, allow_infinity=False),
}).map(json.dumps)


@st.composite
def _scenarios(draw):
    prefix = draw(_FRAGMENT)
    suffix = draw(_FRAGMENT)
    status = draw(_FRAGMENT)
    anomaly_mode = draw(st.booleans())
    # Invoker outcome for the binding under test:
    #   text    → freeform answer (verdict-parse failure in anomaly mode)
    #   verdict → canonical JSON verdict answer
    #   raise   → API error (contained per-node failure)
    outcome_kind = draw(st.sampled_from(["text", "verdict", "raise"]))
    if outcome_kind == "verdict":
        response = draw(_VERDICT_ANSWERS)
    else:
        response = draw(_FRAGMENT)
    frame_bytes = draw(st.binary(min_size=1, max_size=64))
    return {
        "template": prefix + "{status}" + suffix,
        "rendered": prefix + status + suffix,
        "status": status,
        "anomaly_mode": anomaly_mode,
        "outcome_kind": outcome_kind,
        "response": response,
        "frame_bytes": frame_bytes,
    }


class RecordingInvoker:
    """Injectable Text_Generation_API invoker matching the extended
    arity: ``(model_name, prompt, parameters, image_b64=None)``. Records
    every call and returns canned text or raises per-model errors."""

    def __init__(self, results):
        # results: model_name -> ("text", answer) | ("error", message)
        self.results = dict(results)
        self.calls = []

    def __call__(self, model_name, prompt, parameters, image_b64=None):
        self.calls.append({
            "model_name": model_name,
            "prompt": prompt,
            "parameters": dict(parameters),
            "image_b64": image_b64,
        })
        kind, value = self.results[model_name]
        if kind == "error":
            raise RuntimeError(value)
        return value


def _binding(node_id, model, template, anomaly_mode, capture_paths=None):
    binding = {
        "nodeId": node_id,
        "binding": "llm_inference",
        "parameters": {
            "modelName": model,
            "prompt_template": template,
            "max_tokens": 128,
            "temperature": 0.7,
            "top_p": 1.0,
            "anomaly_mode": anomaly_mode,
        },
        "upstreamNodeIds": ["cam"],
        "downstreamNodeIds": ["mqtt"],
    }
    if capture_paths is not None:
        binding["capturePaths"] = dict(capture_paths)
    return binding


def _document(bindings):
    return {
        "schemaVersion": 1,
        "workflowId": "wf-1",
        "workflowVersion": "3",
        "executorBindings": copy.deepcopy(list(bindings)),
        "pluginDependencies": [],
    }


def _run(scenario, with_image, work_dir):
    """Process a two-binding document (binding under test + trailing
    control binding) and return (merged metadata, invoker calls)."""
    outcome_kind = scenario["outcome_kind"]
    results = {
        MODEL_UNDER_TEST: (
            ("error", scenario["response"]) if outcome_kind == "raise"
            else ("text", scenario["response"])
        ),
        CONTROL_MODEL: ("text", CONTROL_TEXT),
    }
    invoker = RecordingInvoker(results)
    processor = LlmInferenceProcessor(invoker=invoker)
    capture_paths = (
        {"in": "{work_dir}/" + FRAME_FILE} if with_image else None
    )
    document = _document([
        _binding("llm1", MODEL_UNDER_TEST, scenario["template"],
                 scenario["anomaly_mode"], capture_paths),
        _binding("llm2", CONTROL_MODEL, "control prompt", False),
    ])
    metadata = processor.process(
        document, {"status": scenario["status"]}, work_dir)
    return metadata, invoker.calls


@given(scenario=_scenarios())
@settings(deadline=None)
def test_processor_behavior_invariant_under_image_attachment(scenario):
    """The image only adds the image argument: rendered prompt text,
    anomaly-mode instruction appending, verdict parsing, and error
    containment are identical with and without a captured frame
    (Requirements 5.1, 6.4)."""
    with tempfile.TemporaryDirectory() as work_dir:
        with open(os.path.join(work_dir, FRAME_FILE), "wb") as f:
            f.write(scenario["frame_bytes"])

        meta_text_only, calls_text_only = _run(scenario, False, work_dir)
        meta_with_image, calls_with_image = _run(scenario, True, work_dir)

        # Merged run metadata — node outcomes (generated_text / verdict
        # keys / error records) and flat is_anomalous/confidence merges —
        # is identical whether or not the frame was attached.
        assert meta_with_image == meta_text_only

        # Both runs made the same invocations for both bindings: same
        # model, same rendered prompt, same parameters. Only the image
        # argument differs.
        assert len(calls_text_only) == len(calls_with_image) == 2
        for call_a, call_b in zip(calls_text_only, calls_with_image):
            assert call_a["model_name"] == call_b["model_name"]
            assert call_a["prompt"] == call_b["prompt"]
            assert call_a["parameters"] == call_b["parameters"]

        # Prompt rendering and anomaly-mode instruction appending are
        # exactly the pre-feature behavior in both runs (6.4).
        expected_prompt = scenario["rendered"]
        if scenario["anomaly_mode"]:
            expected_prompt += "\n\n" + BEDROCK_JSON_INSTRUCTION
        assert calls_text_only[0]["prompt"] == expected_prompt
        assert calls_with_image[0]["prompt"] == expected_prompt

        # The image argument is the only delta: absent everywhere in the
        # text-only run; the captured frame (base64) on the binding under
        # test in the with-image run, None on the imageless control.
        assert [c["image_b64"] for c in calls_text_only] == [None, None]
        expected_b64 = base64.b64encode(
            scenario["frame_bytes"]).decode("ascii")
        assert [c["image_b64"] for c in calls_with_image] == [
            expected_b64, None]

        # Error containment parity (5.1, 6.4): a raised API error or a
        # verdict-parse failure is recorded, never raised, identically in
        # both runs — and the remaining binding is still processed.
        outcome = meta_with_image["llm"]["llm1"]
        if scenario["outcome_kind"] == "raise":
            assert outcome == {"error": scenario["response"]}
        elif scenario["outcome_kind"] == "verdict":
            if scenario["anomaly_mode"]:
                assert outcome["generated_text"] == scenario["response"]
                assert isinstance(outcome["is_anomalous"], bool)
                assert isinstance(outcome["confidence"], float)
            else:
                assert outcome == {"generated_text": scenario["response"]}
        else:  # freeform text
            if scenario["anomaly_mode"]:
                # Verdict parsing fails identically: recorded with the
                # raw text kept, never raised.
                assert outcome["generated_text"] == scenario["response"]
                assert "error" in outcome
            else:
                assert outcome == {"generated_text": scenario["response"]}
        assert meta_with_image["llm"]["llm2"] == {
            "generated_text": CONTROL_TEXT}


@given(
    message=_FRAGMENT,
    frame_bytes=st.binary(min_size=1, max_size=64),
    template_body=_FRAGMENT,
)
@settings(deadline=None)
def test_raising_invoker_with_image_records_error_and_continues(
    message, frame_bytes, template_body
):
    """A raising invoker with an image present records ``{'error': ...}``
    under ``metadata['llm'][nodeId]`` and processing continues with the
    remaining bindings (Requirement 5.1)."""
    with tempfile.TemporaryDirectory() as work_dir:
        with open(os.path.join(work_dir, FRAME_FILE), "wb") as f:
            f.write(frame_bytes)

        invoker = RecordingInvoker({
            MODEL_UNDER_TEST: ("error", message),
            CONTROL_MODEL: ("text", CONTROL_TEXT),
        })
        processor = LlmInferenceProcessor(invoker=invoker)
        document = _document([
            _binding("llm1", MODEL_UNDER_TEST, template_body, False,
                     {"in": "{work_dir}/" + FRAME_FILE}),
            _binding("llm2", CONTROL_MODEL, "control prompt", False),
        ])

        metadata = processor.process(document, {}, work_dir)

        # The failure of the image-carrying invocation is recorded under
        # the node id, never raised.
        assert metadata["llm"]["llm1"] == {"error": message}
        # The invocation did carry the captured frame.
        assert invoker.calls[0]["model_name"] == MODEL_UNDER_TEST
        assert invoker.calls[0]["image_b64"] == base64.b64encode(
            frame_bytes).decode("ascii")
        # Processing continued with the remaining binding.
        assert metadata["llm"]["llm2"] == {"generated_text": CONTROL_TEXT}
        assert len(invoker.calls) == 2
