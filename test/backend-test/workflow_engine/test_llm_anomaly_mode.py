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
"""vlm-parity-run-results: llm_inference anomaly-mode parity with Bedrock.

Property 1 — VLM anomaly-mode parity: for any prompt/answer,
anomaly-mode llm bindings send rendered-prompt + "\\n\\n" +
BEDROCK_JSON_INSTRUCTION, merge the parsed verdict FLAT into the run
metadata AND keep ``generated_text`` nested under ``llm.{nodeId}``;
unparseable answers record ``{'error': <reason + excerpt>,
'generated_text': text}`` without raising and merge no verdict keys
(the llm containment contract); absent/false ``anomaly_mode`` is
byte-identical to today's freeform path. Mixed bedrock+llm documents
merge sanely.

Harness follows ``test_workflow_llm_inference.py`` (injectable
RecordingInvoker, minimal compiled-document builder); Hypothesis
profiles come from the suite conftest.
"""
import os
import tempfile

import shutil

from hypothesis import given, settings
from hypothesis import strategies as st

from workflow_engine.output_bindings import (
    BEDROCK_JSON_INSTRUCTION,
    BedrockInferenceProcessor,
    LlmInferenceProcessor,
)


def llm_binding(node_id="llm1", template="Summarize the run.",
                model="opt-125m", anomaly_mode="absent"):
    parameters = {
        "modelName": model,
        "prompt_template": template,
        "max_tokens": 128,
        "temperature": 0.7,
        "top_p": 1.0,
    }
    if anomaly_mode != "absent":
        parameters["anomaly_mode"] = anomaly_mode
    return {
        "nodeId": node_id,
        "binding": "llm_inference",
        "parameters": parameters,
        "upstreamNodeIds": ["cam"],
        "downstreamNodeIds": ["mqtt"],
    }


def make_document(bindings):
    """Minimal compiled-document shape the processors consume — only
    ``executorBindings`` matters here."""
    return {
        "schemaVersion": 1,
        "workflowId": "wf-1",
        "workflowVersion": "3",
        "segments": [],
        "executorBindings": list(bindings),
        "pluginDependencies": [],
    }


class RecordingInvoker:
    """Injectable Text_Generation_API invoker recording each call and
    returning canned text."""

    def __init__(self, text="generated answer"):
        self.text = text
        self.calls = []

    def __call__(self, model_name, prompt, parameters):
        self.calls.append({
            "model_name": model_name,
            "prompt": prompt,
            "parameters": dict(parameters),
        })
        return self.text


# Prompt templates without {placeholders}: render_prompt is strict, and
# prompt rendering is not the property under test here.
_PROMPTS = st.text(
    alphabet=st.characters(blacklist_characters="{}"), max_size=80)
_TRUTHY_ANOMALY_MODES = st.sampled_from([True, "true", 1])
_FALSY_ANOMALY_MODES = st.sampled_from(["absent", None, False, "false", 0])


# ---------------------------------------------------------------------------
# Property 1: VLM anomaly-mode parity
# ---------------------------------------------------------------------------

@given(prompt=_PROMPTS, anomaly_mode=_TRUTHY_ANOMALY_MODES,
       is_anomalous=st.booleans(),
       confidence=st.floats(min_value=0.0, max_value=1.0, allow_nan=False))
@settings(deadline=None)
def test_anomaly_mode_appends_instruction_and_merges_verdict(
        prompt, anomaly_mode, is_anomalous, confidence):
    """**Property 1 (parity path): anomaly-mode llm bindings send
    rendered-prompt + "\\n\\n" + instruction, merge the parsed verdict
    FLAT, and keep generated_text nested.**

    For any rendered prompt and any parseable verdict answer, the
    invoker receives exactly ``rendered + "\\n\\n" +
    BEDROCK_JSON_INSTRUCTION``; the run metadata gains flat
    ``is_anomalous``/``confidence`` (driving downstream gates like
    Bedrock) and the nested ``llm.{nodeId}`` record stays complete
    (``generated_text`` plus the verdict keys).

    **Validates: Requirements 1.2**
    """
    answer = '{{"is_anomalous": {0}, "confidence": {1}}}'.format(
        "true" if is_anomalous else "false", confidence)
    invoker = RecordingInvoker(text=answer)
    processor = LlmInferenceProcessor(invoker=invoker)
    document = make_document(
        [llm_binding(template=prompt, anomaly_mode=anomaly_mode)])

    metadata = processor.process(document, {"existing": "kept"})

    assert len(invoker.calls) == 1
    assert invoker.calls[0]["prompt"] == (
        prompt + "\n\n" + BEDROCK_JSON_INSTRUCTION)
    assert metadata == {
        "existing": "kept",
        "is_anomalous": is_anomalous,
        "confidence": confidence,
        "llm": {"llm1": {
            "generated_text": answer,
            "is_anomalous": is_anomalous,
            "confidence": confidence,
        }},
    }


# Answers guaranteed unparseable by parse_bedrock_answer: no braces, so
# no JSON object can be extracted.
_UNPARSEABLE = st.text(
    alphabet=st.characters(blacklist_characters="{}"), max_size=100)


@given(prompt=_PROMPTS, answer=_UNPARSEABLE)
@settings(deadline=None)
def test_anomaly_mode_unparseable_answer_records_error_never_raises(
        prompt, answer):
    """**Property 1 (containment path): unparseable anomaly-mode
    answers record error + generated_text without raising and merge no
    verdict keys.**

    For any answer that cannot parse as the verdict JSON, the node's
    outcome is ``{'error': <reason including an answer excerpt>,
    'generated_text': <text>}``; nothing is raised (unlike Bedrock) and
    no ``is_anomalous``/``confidence`` reach the flat metadata or the
    nested record.

    **Validates: Requirements 1.3**
    """
    invoker = RecordingInvoker(text=answer)
    processor = LlmInferenceProcessor(invoker=invoker)
    document = make_document(
        [llm_binding(template=prompt, anomaly_mode=True)])

    # Never raises — the llm containment contract (1.3).
    metadata = processor.process(document, {"existing": "kept"})

    outcome = metadata["llm"]["llm1"]
    assert outcome["generated_text"] == answer
    assert "error" in outcome
    # The reason carries an answer excerpt (parse_bedrock_answer's
    # message embeds the first 200 chars of the text, repr-formatted).
    assert repr(answer[:200]) in outcome["error"]
    assert "is_anomalous" not in outcome
    assert "confidence" not in outcome
    assert "is_anomalous" not in metadata
    assert "confidence" not in metadata
    assert metadata["existing"] == "kept"


@given(prompt=_PROMPTS, answer=st.text(max_size=200),
       anomaly_mode=_FALSY_ANOMALY_MODES)
@settings(deadline=None)
def test_absent_or_false_anomaly_mode_is_todays_path(
        prompt, answer, anomaly_mode):
    """**Property 1 (preservation path): absent/false anomaly_mode is
    byte-identical to today.**

    For any prompt and any answer text (verdict-shaped or not), a
    binding without a truthy ``anomaly_mode`` sends the rendered prompt
    as-is, records exactly ``{'generated_text': text}`` nested, and
    merges no verdict keys — today's freeform behavior unchanged.

    **Validates: Requirements 1.4**
    """
    invoker = RecordingInvoker(text=answer)
    processor = LlmInferenceProcessor(invoker=invoker)
    document = make_document(
        [llm_binding(template=prompt, anomaly_mode=anomaly_mode)])

    metadata = processor.process(document, {"existing": "kept"})

    assert invoker.calls[0]["prompt"] == prompt
    assert metadata == {
        "existing": "kept",
        "llm": {"llm1": {"generated_text": answer}},
    }


def test_freeform_verdict_shaped_answer_is_not_parsed():
    """A freeform (anomaly_mode absent) answer that WOULD parse as the
    verdict JSON is recorded verbatim with no verdict keys — parsing
    only happens in anomaly mode.

    **Validates: Requirements 1.4**
    """
    answer = '{"is_anomalous": true, "confidence": 0.9}'
    processor = LlmInferenceProcessor(invoker=RecordingInvoker(text=answer))
    document = make_document([llm_binding()])

    metadata = processor.process(document, {})

    assert metadata["llm"]["llm1"] == {"generated_text": answer}
    assert "is_anomalous" not in metadata
    assert "confidence" not in metadata


# ---------------------------------------------------------------------------
# Mixed bedrock + llm documents merge sanely
# ---------------------------------------------------------------------------

JPEG_BYTES_IN = b"\xff\xd8fake-input-jpeg\xff\xd9"


def bedrock_binding(node_id="bedrock1", frame="in.jpg"):
    return {
        "nodeId": node_id,
        "binding": "bedrock_inference",
        "parameters": {
            "model": "us.amazon.nova-lite-v1:0",
            "prompt": "Inspect the image.",
            "region": "us-east-1",
            "max_tokens": 256,
        },
        "upstreamNodeIds": ["cam"],
        "downstreamNodeIds": ["mqtt"],
        "capturePaths": {"in": "{work_dir}/" + frame, "reference": None},
    }


class BedrockRecordingInvoker:
    def __init__(self, answer):
        self.answer = answer
        self.calls = []

    def __call__(self, model, prompt, images, region, max_tokens):
        self.calls.append({"model": model, "prompt": prompt})
        return self.answer


def test_mixed_bedrock_and_llm_document_merges_sanely():
    """A document with both a bedrock_inference and an anomaly-mode
    llm_inference binding merges both nested records; the flat verdict
    follows the documented last-writer-wins convention (the llm
    processor runs after Bedrock).

    **Validates: Requirements 1.2**
    """
    work_dir = tempfile.mkdtemp(prefix="llm-anomaly-mode-")
    try:
        with open(os.path.join(work_dir, "in.jpg"), "wb") as f:
            f.write(JPEG_BYTES_IN)
        document = make_document([
            bedrock_binding(node_id="bedrock1"),
            llm_binding(node_id="llm1", anomaly_mode=True),
        ])
        bedrock_answer = '{"is_anomalous": false, "confidence": 0.4}'
        llm_answer = '{"is_anomalous": true, "confidence": 0.9}'

        bedrock = BedrockInferenceProcessor(
            invoker=BedrockRecordingInvoker(bedrock_answer))
        metadata = bedrock.process(document, {"existing": "kept"}, work_dir)

        llm = LlmInferenceProcessor(invoker=RecordingInvoker(text=llm_answer))
        metadata = llm.process(document, metadata)

        # Bedrock's nested record survives (now carrying the nested
        # verdict keys — detection-guided-bedrock-inspection
        # Requirement 4.1, mirroring llm's); llm's nested record is
        # complete; the flat verdict is the llm node's (last writer).
        assert metadata["bedrock"] == {"bedrock1": {
            "text": bedrock_answer,
            "is_anomalous": False, "confidence": 0.4}}
        assert metadata["bedrock_text"] == bedrock_answer
        assert metadata["llm"] == {"llm1": {
            "generated_text": llm_answer,
            "is_anomalous": True,
            "confidence": 0.9,
        }}
        assert metadata["is_anomalous"] is True
        assert metadata["confidence"] == 0.9
        assert metadata["existing"] == "kept"
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
