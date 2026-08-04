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
"""bedrock-response-mode: anomaly-mode instruction append + freeform mode.

Property 1 — Anomaly-mode prompt always carries the canonical
instruction: for any configured prompt and a binding whose
``anomaly_mode`` is absent or true, the invoker receives exactly
``prompt + "\\n\\n" + BEDROCK_JSON_INSTRUCTION`` and the parsed verdict
merges into the metadata as today. Intended contract change
(bedrock-response-mode Requirement 5): anomaly mode ALSO records the
raw answer text as ``bedrock_text`` / ``bedrock.{nodeId}.text`` — the
same keys freeform mode uses — so verdict-plus-notes prompts keep the
notes in the run metadata.

Property 2 — Freeform mode records raw text and never parses: for any
prompt and any answer text (parseable or not), a binding with
``anomaly_mode: false`` sends the prompt unchanged, records the answer
as flat ``bedrock_text`` plus nested ``bedrock.{nodeId}.text``, adds no
``is_anomalous``/``confidence`` keys, and never raises for answer
format. ``{bedrock_text}`` renders through ``render_template``.

Harness follows ``test_workflow_bedrock_inference.py`` (injectable
RecordingInvoker, minimal compiled-document builder, temp work dirs);
Hypothesis profiles come from the suite conftest.
"""
import os
import shutil
import tempfile

import pytest

from hypothesis import given, settings
from hypothesis import strategies as st

from workflow_engine.output_bindings import (
    BEDROCK_JSON_INSTRUCTION,
    BedrockInferenceError,
    BedrockInferenceProcessor,
    render_template,
)

JPEG_BYTES_IN = b"\xff\xd8fake-input-jpeg\xff\xd9"


class RecordingInvoker:
    """Injectable Bedrock invoker recording the call and returning a
    canned model answer."""

    def __init__(self, answer='{"is_anomalous": true, "confidence": 0.9}'):
        self.answer = answer
        self.calls = []

    def __call__(self, model, prompt, images, region, max_tokens):
        self.calls.append({
            "model": model,
            "prompt": prompt,
            "images": list(images),
            "region": region,
            "max_tokens": max_tokens,
        })
        return self.answer


class PerNodeInvoker(RecordingInvoker):
    """Answers from a queue so multiple bindings in one document each
    get a distinct model answer."""

    def __init__(self, answers):
        super().__init__()
        self.answers = list(answers)

    def __call__(self, model, prompt, images, region, max_tokens):
        super().__call__(model, prompt, images, region, max_tokens)
        return self.answers.pop(0)


def make_binding(node_id="bedrock1", prompt="Inspect the image.",
                 anomaly_mode="absent", frame="in.jpg"):
    parameters = {
        "model": "us.amazon.nova-lite-v1:0",
        "prompt": prompt,
        "region": "us-east-1",
        "max_tokens": 256,
    }
    if anomaly_mode != "absent":
        parameters["anomaly_mode"] = anomaly_mode
    return {
        "nodeId": node_id,
        "binding": "bedrock_inference",
        "parameters": parameters,
        "upstreamNodeIds": ["cam"],
        "downstreamNodeIds": ["mqtt"],
        "capturePaths": {"in": "{work_dir}/" + frame, "reference": None},
    }


def make_document(bindings):
    """Minimal compiled-document shape the processor consumes — only
    ``executorBindings`` matters to BedrockInferenceProcessor."""
    return {
        "schemaVersion": 1,
        "workflowId": "wf-1",
        "workflowVersion": "3",
        "segments": [],
        "executorBindings": list(bindings),
        "pluginDependencies": ["python:boto3"],
    }


def write_frame(work_dir, name="in.jpg"):
    with open(os.path.join(work_dir, name), "wb") as f:
        f.write(JPEG_BYTES_IN)


_PROMPTS = st.text(max_size=80)
_ANOMALY_MODES = st.sampled_from(["absent", None, True])


# ---------------------------------------------------------------------------
# Property 1: anomaly-mode prompt always carries the canonical instruction
# ---------------------------------------------------------------------------

@given(prompt=_PROMPTS, anomaly_mode=_ANOMALY_MODES,
       is_anomalous=st.booleans(),
       confidence=st.floats(min_value=0.0, max_value=1.0, allow_nan=False))
@settings(deadline=None)
def test_anomaly_mode_appends_canonical_instruction(
        prompt, anomaly_mode, is_anomalous, confidence):
    """**Property 1: Anomaly-mode prompt always carries the canonical
    instruction.**

    For any configured prompt and a binding whose ``anomaly_mode`` is
    absent, None, or true, the invoker receives exactly
    ``prompt + "\\n\\n" + BEDROCK_JSON_INSTRUCTION`` and the parsed
    verdict merges into the metadata as today. Intended contract change
    (bedrock-response-mode Requirement 5): the raw answer text is now
    ALSO recorded as ``bedrock_text`` / ``bedrock.{nodeId}.text``.

    **Validates: Requirements 1.1, 1.2, 1.3, 4.1, 5.1**
    """
    work_dir = tempfile.mkdtemp(prefix="bedrock-response-mode-")
    try:
        write_frame(work_dir)
        binding = make_binding(prompt=prompt, anomaly_mode=anomaly_mode)
        answer = ('{{"is_anomalous": {0}, "confidence": {1}}}'.format(
            "true" if is_anomalous else "false", confidence))
        invoker = RecordingInvoker(answer=answer)
        processor = BedrockInferenceProcessor(invoker=invoker)

        metadata = processor.process(
            make_document([binding]), {"existing": "kept"}, work_dir)

        assert len(invoker.calls) == 1
        assert invoker.calls[0]["prompt"] == (
            prompt + "\n\n" + BEDROCK_JSON_INSTRUCTION)
        assert metadata == {
            "existing": "kept",
            "is_anomalous": is_anomalous,
            "confidence": confidence,
            "bedrock_text": answer,
            "bedrock": {"bedrock1": {"text": answer}},
        }
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


@given(prompt=_PROMPTS)
@settings(deadline=None)
def test_instruction_appended_even_when_prompt_resembles_it(prompt):
    """Appending is unconditional and idempotent per invocation: even a
    prompt that already contains the instruction text gets exactly one
    suffix appended.

    **Validates: Requirements 1.2**
    """
    work_dir = tempfile.mkdtemp(prefix="bedrock-response-mode-")
    try:
        write_frame(work_dir)
        loaded = prompt + BEDROCK_JSON_INSTRUCTION
        binding = make_binding(prompt=loaded)
        invoker = RecordingInvoker()
        processor = BedrockInferenceProcessor(invoker=invoker)

        processor.process(make_document([binding]), {}, work_dir)

        assert invoker.calls[0]["prompt"] == (
            loaded + "\n\n" + BEDROCK_JSON_INSTRUCTION)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Requirement 5: anomaly mode preserves the model's textual notes
# ---------------------------------------------------------------------------

# Prose safe for parse_bedrock_answer's extraction: no braces (the
# parser slices the first '{' to the last '}') and no backticks (the
# fenced-block regex). Any such surrounding text keeps the verdict JSON
# parseable while making the raw answer strictly larger than the JSON.
_PROSE = st.text(
    alphabet=st.characters(blacklist_characters="{}`"), max_size=60)


@given(prompt=_PROMPTS, prose_before=_PROSE, prose_after=_PROSE,
       is_anomalous=st.booleans(),
       confidence=st.floats(min_value=0.0, max_value=1.0, allow_nan=False))
@settings(deadline=None)
def test_anomaly_mode_records_full_raw_answer(
        prompt, prose_before, prose_after, is_anomalous, confidence):
    """**Requirement 5: anomaly mode preserves the model's textual
    notes.**

    For any prompt and any answer that is valid verdict JSON with
    arbitrary surrounding prose, an anomaly-mode binding merges the
    parsed verdict AND records the FULL raw answer (prose included, not
    just the JSON) as ``bedrock_text`` / ``bedrock.{nodeId}.text``;
    ``{bedrock_text}`` renders via render_template.

    **Validates: Requirements 5.1, 5.2**
    """
    work_dir = tempfile.mkdtemp(prefix="bedrock-response-mode-")
    try:
        write_frame(work_dir)
        binding = make_binding(prompt=prompt)
        verdict_json = '{{"is_anomalous": {0}, "confidence": {1}}}'.format(
            "true" if is_anomalous else "false", confidence)
        answer = prose_before + verdict_json + prose_after
        invoker = RecordingInvoker(answer=answer)
        processor = BedrockInferenceProcessor(invoker=invoker)

        metadata = processor.process(make_document([binding]), {}, work_dir)

        # Verdict merged as today (5.1).
        assert metadata["is_anomalous"] is is_anomalous
        assert metadata["confidence"] == confidence
        # The FULL raw answer recorded — prose and all (5.1).
        assert metadata["bedrock_text"] == answer
        assert metadata["bedrock"] == {"bedrock1": {"text": answer}}
        # {bedrock_text} is a working template placeholder (5.2).
        assert render_template("{bedrock_text}", metadata) == answer
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def test_anomaly_mode_unparseable_answer_still_fails(tmp_path):
    """An anomaly-mode answer unparseable as the verdict JSON keeps the
    existing BedrockInferenceError path — no text recording on the
    failure path (the error message carries the answer excerpt).

    **Validates: Requirements 5.3**
    """
    write_frame(str(tmp_path))
    binding = make_binding()
    invoker = RecordingInvoker(answer="I cannot answer in JSON, sorry.")
    processor = BedrockInferenceProcessor(invoker=invoker)

    with pytest.raises(BedrockInferenceError) as excinfo:
        processor.process(make_document([binding]), {}, str(tmp_path))
    assert excinfo.value.node_id == "bedrock1"


# ---------------------------------------------------------------------------
# Property 2: freeform mode records raw text and never parses
# ---------------------------------------------------------------------------

@given(prompt=_PROMPTS, answer=st.text(max_size=200))
@settings(deadline=None)
def test_freeform_passthrough_and_raw_text_recording(prompt, answer):
    """**Property 2: Freeform mode records raw text and never parses.**

    For any prompt and any answer text (parseable or not — including
    text unparseable as the verdict JSON), a binding with
    ``anomaly_mode: false`` sends the prompt unchanged, records the
    answer as flat ``bedrock_text`` plus nested
    ``bedrock.{nodeId}.text``, adds no verdict keys, and never raises
    for answer format. ``{bedrock_text}`` renders via render_template.

    **Validates: Requirements 2.1, 2.2, 2.3, 2.4**
    """
    work_dir = tempfile.mkdtemp(prefix="bedrock-response-mode-")
    try:
        write_frame(work_dir)
        binding = make_binding(prompt=prompt, anomaly_mode=False)
        invoker = RecordingInvoker(answer=answer)
        processor = BedrockInferenceProcessor(invoker=invoker)

        # Never raises for answer format (2.3).
        metadata = processor.process(
            make_document([binding]), {"existing": "kept"}, work_dir)

        # Prompt sent unchanged (2.1).
        assert invoker.calls[0]["prompt"] == prompt
        # Raw text recorded flat and nested; no verdict keys (2.2).
        assert metadata["bedrock_text"] == answer
        assert metadata["bedrock"] == {"bedrock1": {"text": answer}}
        assert "is_anomalous" not in metadata
        assert "confidence" not in metadata
        assert metadata["existing"] == "kept"
        # {bedrock_text} is a working template placeholder (2.4).
        assert render_template("{bedrock_text}", metadata) == answer
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def test_freeform_tolerates_verdict_shaped_answer(tmp_path):
    """A freeform answer that WOULD parse as the verdict JSON is still
    recorded verbatim — no parsing happens at all.

    **Validates: Requirements 2.2, 2.3**
    """
    write_frame(str(tmp_path))
    answer = '{"is_anomalous": true, "confidence": 0.9}'
    binding = make_binding(anomaly_mode=False)
    invoker = RecordingInvoker(answer=answer)
    processor = BedrockInferenceProcessor(invoker=invoker)

    metadata = processor.process(make_document([binding]), {}, str(tmp_path))

    assert metadata["bedrock_text"] == answer
    assert "is_anomalous" not in metadata
    assert "confidence" not in metadata


def test_two_freeform_nodes_keep_both_nested_entries(tmp_path):
    """process() merges the ``bedrock`` sub-dict across nodes: two
    freeform bindings in one document each keep their per-node
    ``bedrock.{nodeId}.text``; the flat ``bedrock_text`` carries the
    later node's answer (documented last-writer-wins convention).

    **Validates: Requirements 2.2**
    """
    write_frame(str(tmp_path), "a.jpg")
    write_frame(str(tmp_path), "b.jpg")
    bindings = [
        make_binding(node_id="node-a", anomaly_mode=False, frame="a.jpg"),
        make_binding(node_id="node-b", anomaly_mode=False, frame="b.jpg"),
    ]
    invoker = PerNodeInvoker(["answer A", "answer B"])
    processor = BedrockInferenceProcessor(invoker=invoker)

    metadata = processor.process(make_document(bindings), {}, str(tmp_path))

    assert metadata["bedrock"] == {
        "node-a": {"text": "answer A"},
        "node-b": {"text": "answer B"},
    }
    assert metadata["bedrock_text"] == "answer B"


def test_mixed_modes_merge_verdict_and_text(tmp_path):
    """An anomaly-mode node and a freeform node coexist: the verdict
    keys and the raw-text keys both land in the merged metadata.
    Intended contract change (bedrock-response-mode Requirement 5): the
    anomaly node now ALSO contributes text keys — its raw answer lands
    under ``bedrock.{nodeId}.text`` alongside the freeform node's, and
    the flat ``bedrock_text`` carries the later node's answer
    (last-writer-wins convention).

    **Validates: Requirements 1.3, 2.2, 5.1**
    """
    write_frame(str(tmp_path), "a.jpg")
    write_frame(str(tmp_path), "b.jpg")
    bindings = [
        make_binding(node_id="verdict-node", frame="a.jpg"),
        make_binding(node_id="text-node", anomaly_mode=False, frame="b.jpg"),
    ]
    verdict_answer = '{"is_anomalous": true, "confidence": 0.8}'
    invoker = PerNodeInvoker([
        verdict_answer,
        "The part looks scratched near the top edge.",
    ])
    processor = BedrockInferenceProcessor(invoker=invoker)

    metadata = processor.process(make_document(bindings), {}, str(tmp_path))

    assert metadata["is_anomalous"] is True
    assert metadata["confidence"] == 0.8
    assert metadata["bedrock_text"] == \
        "The part looks scratched near the top edge."
    assert metadata["bedrock"] == {
        "verdict-node": {"text": verdict_answer},
        "text-node": {"text": "The part looks scratched near the top edge."}}
