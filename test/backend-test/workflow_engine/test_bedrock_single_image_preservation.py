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
"""Preservation property tests (Task 2) for bedrock-single-image-inference.

Property 2: Preservation — Two-image and no-primary behavior unchanged:
for any ``bedrock_inference`` binding where both frames are captured and
readable, the processor makes an invoker call byte-identical to today's
(model, prompt, images list with both labeled pairs in order, region,
max_tokens) and merges the parsed answer identically; for any binding
where the ``in`` (primary) frame is unavailable, ``process()`` raises
``BedrockInferenceError`` carrying the node id; invoker failures surface
``BedrockInferenceError`` with the node id; documents without
``bedrock_inference`` bindings pass tag_values through unchanged.

**Validates: Requirements 3.1, 3.2, 3.3, 3.4**

Observation-first: each property below encodes behavior OBSERVED on the
current (UNFIXED) ``BedrockInferenceProcessor._run_one`` — these tests
MUST PASS today and are re-run after the fix (task 3.3) to prove that
making the reference frame optional changed nothing outside the bug
condition:

* both frames present → the injected invoker is called exactly once with
  ``(model, prompt, [("Input image", in_bytes), ("Reference image",
  ref_bytes)], region, max_tokens)`` and the parsed answer is merged over
  the run's tag values (Requirement 3.1);
* the ``in`` frame unavailable in any shape (``capturePaths.in`` None,
  key absent, or the file missing on disk) → ``BedrockInferenceError``
  naming the node, the invoker never called (Requirement 2.4 / 3.2
  surfacing contract);
* a raising invoker → ``BedrockInferenceError`` carrying the node id and
  the underlying message (Requirement 3.2);
* documents with no ``bedrock_inference`` bindings → tag_values pass
  through unchanged (Requirement 3.3).

Follows the ``test_workflow_bedrock_inference.py`` harness pattern
(RecordingInvoker, minimal compiled-document builder, per-example temp
work dirs). Hypothesis profiles come from the suite conftests (25
examples locally, ``HYPOTHESIS_PROFILE=ci`` = 100).
"""
import json
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
)


# ---------------------------------------------------------------------------
# Harness (mirrors test_workflow_bedrock_inference.py)
# ---------------------------------------------------------------------------

class RecordingInvoker:
    """Injectable Bedrock invoker recording the call and returning a
    canned model answer (or raising ``error``)."""

    def __init__(self, answer='{"is_anomalous": true, "confidence": 0.9}',
                 error=None):
        self.answer = answer
        self.error = error
        self.calls = []

    def __call__(self, model, prompt, images, region, max_tokens):
        self.calls.append({
            "model": model,
            "prompt": prompt,
            "images": list(images),
            "region": region,
            "max_tokens": max_tokens,
        })
        if self.error is not None:
            raise self.error
        return self.answer


def make_binding(node_id, parameters, capture_paths):
    return {
        "nodeId": node_id,
        "binding": "bedrock_inference",
        "parameters": dict(parameters),
        "upstreamNodeIds": ["cam", "ref"],
        "downstreamNodeIds": ["mqtt"],
        "capturePaths": capture_paths,
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


# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------

_NODE_IDS = st.sampled_from(["bedrock1", "bedrock_inference_1", "n-42"])
# Non-empty so the observed passthrough (str(x or <default>)) is identity.
_MODELS = st.sampled_from([
    "us.amazon.nova-lite-v1:0",
    "us.amazon.nova-pro-v1:0",
    "anthropic.claude-3-haiku-20240307-v1:0",
])
_PROMPTS = st.text(min_size=1, max_size=60)
_REGIONS = st.sampled_from(["us-east-1", "us-west-2", "eu-central-1"])
# >= 1 so the observed falsy-default (int(x or 256)) never kicks in.
_MAX_TOKENS = st.integers(min_value=1, max_value=4096)
_FRAME_BYTES = st.binary(min_size=1, max_size=128)
_TAG_KEYS = st.text(alphabet="abcdefghij_", min_size=1, max_size=8)
_TAG_VALUES = st.dictionaries(
    _TAG_KEYS,
    st.one_of(st.booleans(), st.integers(), st.text(max_size=10)),
    max_size=3,
).filter(lambda d: not ({"is_anomalous", "confidence"} & set(d)))


@st.composite
def _bedrock_parameters(draw):
    return {
        "model": draw(_MODELS),
        "prompt": draw(_PROMPTS),
        "region": draw(_REGIONS),
        "max_tokens": draw(_MAX_TOKENS),
    }


def _write(work_dir, name, payload):
    path = os.path.join(work_dir, name)
    with open(path, "wb") as f:
        f.write(payload)
    return path


# ---------------------------------------------------------------------------
# Property 2a: both-frames invoker call shape and metadata merge
# ---------------------------------------------------------------------------

@given(node_id=_NODE_IDS, parameters=_bedrock_parameters(),
       in_bytes=_FRAME_BYTES, ref_bytes=_FRAME_BYTES,
       tag_values=_TAG_VALUES, is_anomalous=st.booleans(),
       confidence=st.floats(min_value=0.0, max_value=1.0,
                            allow_nan=False))
@settings(deadline=None)
def test_both_frames_call_shape_and_merge_unchanged(
        node_id, parameters, in_bytes, ref_bytes, tag_values,
        is_anomalous, confidence):
    """**Property 2: Preservation — both-frames invoker call.**

    For any model/prompt/region/max_tokens parameters and frame bytes,
    ``process()`` calls the invoker exactly once with ``(model, prompt,
    [("Input image", in_bytes), ("Reference image", ref_bytes)], region,
    max_tokens)`` and merges the parsed answer over tag_values.

    Intended contract change (bedrock-response-mode): in anomaly mode
    (default, ``anomaly_mode`` absent) the executor now appends the
    canonical JSON instruction, so the expected invoker prompt is
    ``prompt + "\\n\\n" + BEDROCK_JSON_INSTRUCTION`` rather than the
    configured prompt verbatim. Per bedrock-response-mode Requirement 5,
    anomaly mode now ALSO records the raw answer text as
    ``bedrock_text`` / ``bedrock.{nodeId}.text`` alongside the parsed
    verdict. Everything else is unchanged.

    **Validates: Requirements 3.1**
    """
    work_dir = tempfile.mkdtemp(prefix="bedrock-preservation-")
    try:
        _write(work_dir, "in.jpg", in_bytes)
        _write(work_dir, "ref.jpg", ref_bytes)
        binding = make_binding(node_id, parameters, {
            "in": "{work_dir}/in.jpg",
            "reference": "{work_dir}/ref.jpg",
        })
        answer = json.dumps(
            {"is_anomalous": is_anomalous, "confidence": confidence})
        invoker = RecordingInvoker(answer=answer)
        processor = BedrockInferenceProcessor(invoker=invoker)

        metadata = processor.process(
            make_document([binding]), dict(tag_values), work_dir)

        assert invoker.calls == [{
            "model": parameters["model"],
            "prompt": parameters["prompt"] + "\n\n" + BEDROCK_JSON_INSTRUCTION,
            "images": [("Input image", in_bytes),
                       ("Reference image", ref_bytes)],
            "region": parameters["region"],
            "max_tokens": parameters["max_tokens"],
        }], ("PRESERVATION REGRESSION (Property 2): the both-frames "
             "invoker call changed: {0!r}".format(invoker.calls))
        expected = dict(tag_values)
        expected.update({
            "is_anomalous": is_anomalous, "confidence": confidence,
            "bedrock_text": answer,
            "bedrock": {node_id: {"text": answer}},
        })
        assert metadata == expected, (
            "PRESERVATION REGRESSION (Property 2): merged metadata "
            "changed: {0!r} != {1!r}".format(metadata, expected))
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Property 2b: unavailable 'in' (primary) frame raises with the node id
# ---------------------------------------------------------------------------

_IN_UNAVAILABLE_SHAPES = st.sampled_from(
    ["in_path_none", "in_key_absent", "in_file_missing"])


@given(node_id=_NODE_IDS, parameters=_bedrock_parameters(),
       ref_bytes=_FRAME_BYTES, shape=_IN_UNAVAILABLE_SHAPES)
@settings(deadline=None)
def test_unavailable_primary_frame_raises_with_the_node(
        node_id, parameters, ref_bytes, shape):
    """**Property 2: Preservation — no-primary failure surfacing.**

    For any binding whose ``in`` frame is unavailable (path None, key
    absent, or file missing on disk), ``process()`` raises
    ``BedrockInferenceError`` carrying the node id and the invoker is
    never called — inference with no primary image is never attempted.

    **Validates: Requirements 2.4, 3.2**
    """
    work_dir = tempfile.mkdtemp(prefix="bedrock-preservation-")
    try:
        _write(work_dir, "ref.jpg", ref_bytes)
        capture_paths = {"reference": "{work_dir}/ref.jpg"}
        if shape == "in_path_none":
            capture_paths["in"] = None
        elif shape == "in_file_missing":
            capture_paths["in"] = "{work_dir}/in.jpg"  # never written
        binding = make_binding(node_id, parameters, capture_paths)
        invoker = RecordingInvoker()
        processor = BedrockInferenceProcessor(invoker=invoker)

        with pytest.raises(BedrockInferenceError) as excinfo:
            processor.process(make_document([binding]), {}, work_dir)

        assert excinfo.value.node_id == node_id, (
            "PRESERVATION REGRESSION (Property 2): a no-primary failure "
            "is no longer attributed to the node: {0!r}".format(
                excinfo.value.node_id))
        assert "'in'" in str(excinfo.value), (
            "PRESERVATION REGRESSION (Property 2): the no-primary error "
            "no longer names the 'in' port: {0!r}".format(
                str(excinfo.value)))
        assert invoker.calls == [], (
            "PRESERVATION REGRESSION (Property 2): the invoker was "
            "called despite an unavailable primary frame")
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Property 2c: a raising invoker surfaces BedrockInferenceError + node id
# ---------------------------------------------------------------------------

_INVOKER_ERRORS = st.sampled_from([
    "credentials missing",
    "endpoint unreachable",
    "throttled: too many requests",
])


@given(node_id=_NODE_IDS, parameters=_bedrock_parameters(),
       in_bytes=_FRAME_BYTES, ref_bytes=_FRAME_BYTES,
       message=_INVOKER_ERRORS)
@settings(deadline=None)
def test_invoker_failure_surfaces_with_the_node(
        node_id, parameters, in_bytes, ref_bytes, message):
    """**Property 2: Preservation — invoker-failure surfacing.**

    For any binding with both frames available whose invoker raises,
    ``process()`` raises ``BedrockInferenceError`` carrying the node id
    and the underlying message (the existing failure-surfacing contract).

    **Validates: Requirements 3.2**
    """
    work_dir = tempfile.mkdtemp(prefix="bedrock-preservation-")
    try:
        _write(work_dir, "in.jpg", in_bytes)
        _write(work_dir, "ref.jpg", ref_bytes)
        binding = make_binding(node_id, parameters, {
            "in": "{work_dir}/in.jpg",
            "reference": "{work_dir}/ref.jpg",
        })
        invoker = RecordingInvoker(error=RuntimeError(message))
        processor = BedrockInferenceProcessor(invoker=invoker)

        with pytest.raises(BedrockInferenceError) as excinfo:
            processor.process(make_document([binding]), {}, work_dir)

        assert excinfo.value.node_id == node_id, (
            "PRESERVATION REGRESSION (Property 2): an invoker failure is "
            "no longer attributed to the node: {0!r}".format(
                excinfo.value.node_id))
        assert message in str(excinfo.value), (
            "PRESERVATION REGRESSION (Property 2): the invoker failure "
            "no longer carries the underlying message: {0!r}".format(
                str(excinfo.value)))
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Property 2d: documents without bedrock bindings pass tag_values through
# ---------------------------------------------------------------------------

_OTHER_BINDINGS = st.lists(
    st.sampled_from([
        {"nodeId": "f1", "binding": "inference_filter",
         "parameters": {"condition": "is_anomalous == true"}},
        {"nodeId": "m1", "binding": "mqtt",
         "parameters": {"topic": "results"}},
        {"nodeId": "d1", "binding": "dio", "parameters": {"pin": 7}},
    ]),
    max_size=3,
)


@given(bindings=_OTHER_BINDINGS, tag_values=_TAG_VALUES)
@settings(deadline=None)
def test_documents_without_bedrock_bindings_pass_through(
        bindings, tag_values):
    """**Property 2: Preservation — no-bindings passthrough.**

    For any document with no ``bedrock_inference`` bindings (empty or
    only other binding kinds), ``process()`` returns tag_values unchanged
    and never calls the invoker.

    **Validates: Requirements 3.3**
    """
    invoker = RecordingInvoker()
    processor = BedrockInferenceProcessor(invoker=invoker)
    document = make_document(bindings)

    assert processor.bindings(document) == []
    result = processor.process(document, dict(tag_values), None)
    assert result == tag_values, (
        "PRESERVATION REGRESSION (Property 2): tag_values no longer pass "
        "through a bedrock-free document: {0!r} != {1!r}".format(
            result, tag_values))
    assert invoker.calls == []
