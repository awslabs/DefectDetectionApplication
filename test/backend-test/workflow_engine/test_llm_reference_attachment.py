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
"""Tests for the LlmInferenceProcessor's reference-frame attachment
(vlm-anomaly-reference-parity task 2).

Requirements 4.1-4.4, 7.1: after the existing ``in``-frame handling the
processor reads ``capturePaths["reference"]`` with Bedrock's OPTIONAL
semantics — a readable reference rides the invocation base64-encoded as
the fifth positional argument; ``None``, an absent key (pre-feature
package), or an unreadable file logs the omission and proceeds with
single-image inference (never a node error). The default invoker adds
``reference_image`` to the POST body only when a reference is supplied;
reference-less bodies stay byte-identical to pre-feature bodies.
"""
import base64
import sys
import types
from unittest.mock import patch

import pytest

from workflow_engine_test_utils import DEVICE_ARCH

from workflow_engine.output_bindings import (
    LLM_GENERATION_TIMEOUT_SEC,
    TEXT_GENERATION_URL,
    LlmInferenceProcessor,
    _default_llm_invoker,
)

INPUT_JPEG = b"\xff\xd8\xff\xe0input-frame-bytes"
REFERENCE_JPEG = b"\xff\xd8\xff\xe0reference-frame-bytes"

INPUT_B64 = base64.b64encode(INPUT_JPEG).decode("ascii")
REFERENCE_B64 = base64.b64encode(REFERENCE_JPEG).decode("ascii")


def llm_binding(capture_paths=None, node_id="llm1", **params):
    parameters = {
        "modelName": "qwen2-vl-2b",
        "prompt_template": "Compare the input to the reference.",
        "max_tokens": 128,
    }
    parameters.update(params)
    binding = {
        "nodeId": node_id,
        "binding": "llm_inference",
        "parameters": parameters,
        "upstreamNodeIds": ["cam"],
        "downstreamNodeIds": ["mqtt"],
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


class ArityRecordingInvoker:
    """Injectable Text_Generation_API invoker recording every call's raw
    positional args — the arity distinguishes the two-image 5-argument
    invocation from the single-image 4-argument and text-only 3-argument
    forms."""

    def __init__(self, text="generated answer"):
        self.text = text
        self.calls = []

    def __call__(self, *args):
        self.calls.append(args)
        return self.text


@pytest.fixture
def work_dir(tmp_path):
    (tmp_path / "vlm_frame_in.jpg").write_bytes(INPUT_JPEG)
    (tmp_path / "vlm_frame_ref.jpg").write_bytes(REFERENCE_JPEG)
    return str(tmp_path)


def run_one(capture_paths, work_dir):
    invoker = ArityRecordingInvoker()
    processor = LlmInferenceProcessor(invoker=invoker)
    document = make_document([llm_binding(capture_paths=capture_paths)])
    metadata = processor.process(document, {"seed": 1}, work_dir)
    return metadata["llm"]["llm1"], invoker.calls


# ---------------------------------------------------------------------------
# Processor: the four reference shapes (Requirements 4.1, 4.2, 7.1)
# ---------------------------------------------------------------------------

class TestReferenceShapes:
    def test_readable_reference_rides_as_fifth_argument(self, work_dir):
        # Requirement 4.1: readable reference → base64-encoded and passed
        # to the invoker alongside the input frame (extended arity).
        outcome, calls = run_one(
            {"in": "{work_dir}/vlm_frame_in.jpg",
             "reference": "{work_dir}/vlm_frame_ref.jpg"},
            work_dir,
        )
        assert outcome == {"generated_text": "generated answer"}
        assert len(calls) == 1
        model_name, prompt, parameters, image_b64, reference_b64 = calls[0]
        assert image_b64 == INPUT_B64
        assert reference_b64 == REFERENCE_B64

    def test_reference_none_is_single_image_inference(self, work_dir):
        # Requirement 4.2: unfed port (compiler emits None) → single-image
        # inference with the shipped 4-argument arity, never a node error.
        outcome, calls = run_one(
            {"in": "{work_dir}/vlm_frame_in.jpg", "reference": None},
            work_dir,
        )
        assert outcome == {"generated_text": "generated answer"}
        assert calls == [(
            "qwen2-vl-2b", "Compare the input to the reference.",
            {"modelName": "qwen2-vl-2b",
             "prompt_template": "Compare the input to the reference.",
             "max_tokens": 128},
            INPUT_B64,
        )]

    def test_reference_key_absent_is_single_image_inference(self, work_dir):
        # Requirement 7.1: pre-feature package (no reference key) →
        # invocation byte-identical to today's single-image form.
        outcome, calls = run_one(
            {"in": "{work_dir}/vlm_frame_in.jpg"}, work_dir)
        assert outcome == {"generated_text": "generated answer"}
        assert len(calls) == 1
        assert len(calls[0]) == 4
        assert calls[0][3] == INPUT_B64

    def test_unreadable_reference_is_never_a_node_error(
        self, work_dir, caplog
    ):
        # Requirement 4.2: a fed-but-unreadable reference logs a warning
        # and proceeds single-image — unlike the 'in' frame, NEVER a
        # node error.
        with caplog.at_level("WARNING"):
            outcome, calls = run_one(
                {"in": "{work_dir}/vlm_frame_in.jpg",
                 "reference": "{work_dir}/never_written.jpg"},
                work_dir,
            )
        assert outcome == {"generated_text": "generated answer"}
        assert len(calls) == 1
        assert len(calls[0]) == 4  # single-image arity
        assert calls[0][3] == INPUT_B64
        assert any(
            "reference" in record.message for record in caplog.records
            if record.levelname == "WARNING"
        )

    def test_no_frames_keeps_prefeature_three_argument_arity(self):
        # Requirement 7.1: no capturePaths at all → the pre-feature
        # text-only 3-argument invocation, so pre-feature injected
        # invokers keep working unchanged.
        outcome, calls = run_one(None, None)
        assert outcome == {"generated_text": "generated answer"}
        assert len(calls) == 1
        assert len(calls[0]) == 3

    def test_unreadable_in_frame_still_errors_before_reference(
        self, work_dir
    ):
        # The 'in' contract is UNTOUCHED (Requirement 4.4): a fed but
        # unreadable input frame stays a contained node error and the
        # invoker is never called, readable reference or not.
        outcome, calls = run_one(
            {"in": "{work_dir}/missing_in.jpg",
             "reference": "{work_dir}/vlm_frame_ref.jpg"},
            work_dir,
        )
        assert set(outcome.keys()) == {"error"}
        assert "in" in outcome["error"]
        assert calls == []

    def test_reference_invariance_of_anomaly_mode_handling(self, work_dir):
        # Requirement 4.4: the reference only adds an argument — prompt
        # rendering, anomaly-mode instruction appending, and verdict
        # parsing are identical to the no-reference invocation.
        invoker = ArityRecordingInvoker(
            text='{"is_anomalous": true, "confidence": 0.9}')
        processor = LlmInferenceProcessor(invoker=invoker)
        document = make_document([
            llm_binding(
                capture_paths={"in": "{work_dir}/vlm_frame_in.jpg",
                               "reference": "{work_dir}/vlm_frame_ref.jpg"},
                anomaly_mode=True,
            ),
        ])
        metadata = processor.process(document, {"seed": 1}, work_dir)
        outcome = metadata["llm"]["llm1"]
        assert outcome["is_anomalous"] is True
        assert outcome["confidence"] == 0.9
        assert outcome["generated_text"] == (
            '{"is_anomalous": true, "confidence": 0.9}')
        # Flat merge (anomaly-mode parity) still happens with a
        # reference attached.
        assert metadata["is_anomalous"] is True
        # The rendered prompt carried the appended JSON instruction
        # exactly once, same as the no-reference path.
        prompt = invoker.calls[0][1]
        assert prompt.startswith("Compare the input to the reference.")
        assert prompt.count("JSON") >= 1


# ---------------------------------------------------------------------------
# Default invoker: reference_image body additivity (Requirement 4.3)
# ---------------------------------------------------------------------------

class _Response:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return self._payload


def _fake_requests(responses):
    queue = list(responses)
    posts = []
    module = types.ModuleType("requests")

    def post(url, json=None, timeout=None):
        posts.append({"url": url, "json": json, "timeout": timeout})
        assert queue, "unexpected extra POST"
        return queue.pop(0)

    module.post = post
    module.calls = posts
    return module


def _invoke_default(*args):
    fake = _fake_requests([_Response(200, {"generated_text": "ok"})])
    with patch.dict(sys.modules, {"requests": fake}):
        result = _default_llm_invoker(*args)
    return result, fake.calls


class TestDefaultInvokerReferenceField:
    PARAMS = {"max_tokens": 64, "temperature": 0.2}

    def test_reference_supplied_rides_the_body(self):
        result, calls = _invoke_default(
            "qwen2-vl-2b", "compare", dict(self.PARAMS),
            INPUT_B64, REFERENCE_B64,
        )
        assert result == "ok"
        assert calls == [{
            "url": TEXT_GENERATION_URL.format(model_name="qwen2-vl-2b"),
            "json": {
                "prompt": "compare",
                "max_tokens": 64,
                "temperature": 0.2,
                "image": INPUT_B64,
                "reference_image": REFERENCE_B64,
            },
            "timeout": LLM_GENERATION_TIMEOUT_SEC,
        }]

    def test_no_reference_body_is_byte_identical_to_prefeature(self):
        # Requirement 4.3: invocations without a reference produce a
        # request body byte-identical to today's (no reference_image key).
        _result, calls = _invoke_default(
            "qwen2-vl-2b", "compare", dict(self.PARAMS), INPUT_B64)
        assert calls[0]["json"] == {
            "prompt": "compare",
            "max_tokens": 64,
            "temperature": 0.2,
            "image": INPUT_B64,
        }

    def test_text_only_body_unchanged(self):
        _result, calls = _invoke_default(
            "opt-125m", "summarize", dict(self.PARAMS))
        assert calls[0]["json"] == {
            "prompt": "summarize",
            "max_tokens": 64,
            "temperature": 0.2,
        }
