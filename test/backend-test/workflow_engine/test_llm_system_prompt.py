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
"""Unit tests for the llm_inference ``system_prompt`` executor path
(json-trigger-metadata-pipeline task 5.5).

**Validates: Requirements 8.10** (invoker legs; also exercises 8.2, 8.5,
8.9): the Text_Generation_API request body emitted by the executor
invoker with and without a System_Prompt, and with anomaly mode combined
with a System_Prompt — captured-body fakes, no HTTP.

Covers:
- ``_default_llm_invoker`` body additivity: a non-empty ``system_prompt``
  rides the POST body verbatim; ``None``/empty leaves the body
  byte-identical to the pre-feature request.
- ``LlmInferenceProcessor._run_one`` seam: a configured system prompt is
  supplied as the ``system_prompt=`` keyword; absent/empty/whitespace
  values keep the pre-feature invocation (no keyword), so pre-feature
  injected fakes keep working; anomaly mode appends the JSON instruction
  to the rendered user prompt only, never the system prompt.
- Processor + default invoker end to end (fake ``requests`` module): the
  complete request body for the with/without/anomaly-combined cases.

Harness follows ``test_llm_reference_attachment.py`` (fake requests
module via ``patch.dict(sys.modules, ...)``) and
``test_workflow_bedrock_inference.py`` (arity/kwargs-recording invoker).
"""
import sys
import types
from unittest.mock import patch

from workflow_engine_test_utils import DEVICE_ARCH

from workflow_engine.output_bindings import (
    BEDROCK_JSON_INSTRUCTION,
    LLM_GENERATION_TIMEOUT_SEC,
    TEXT_GENERATION_URL,
    LlmInferenceProcessor,
    _default_llm_invoker,
)

SYSTEM_PROMPT = "You are a meticulous quality inspector."
USER_PROMPT = "Summarize the run."
VERDICT_ANSWER = '{"is_anomalous": true, "confidence": 0.9}'


def llm_binding(node_id="llm1", **parameter_overrides):
    parameters = {
        "modelName": "opt-125m",
        "prompt_template": USER_PROMPT,
        "max_tokens": 128,
        "temperature": 0.7,
        "top_p": 1.0,
    }
    parameters.update(parameter_overrides)
    return {
        "nodeId": node_id,
        "binding": "llm_inference",
        "parameters": parameters,
        "upstreamNodeIds": ["cam"],
        "downstreamNodeIds": ["mqtt"],
    }


def make_document(bindings):
    return {
        "schemaVersion": 1,
        "workflowId": "wf-1",
        "workflowVersion": "3",
        "targetArch": DEVICE_ARCH,
        "segments": [],
        "executorBindings": list(bindings),
        "pluginDependencies": [],
    }


# ---------------------------------------------------------------------------
# Captured-body fake for the default invoker's lazy `import requests`
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


def _invoke_default(*args, **kwargs):
    fake = _fake_requests([_Response(200, {"generated_text": "ok"})])
    with patch.dict(sys.modules, {"requests": fake}):
        result = _default_llm_invoker(*args, **kwargs)
    return result, fake.calls


class KwargsRecordingInvoker:
    """Capturing invoker recording the exact (args, kwargs) of every
    call, for asserting the processor->invoker seam.

    Its ``**kwargs`` signature accepts every keyword, so the processor
    also forwards the ``metrics_sink`` capture callable added by
    vllm-workflow-latency-optimization (the ``_accepts_keyword``
    pattern); the seam assertions below account for it explicitly."""

    def __init__(self, text="generated answer"):
        self.text = text
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.text


class PreFeatureInvoker:
    """A fixed 3-parameter fake predating every optional invoker
    parameter (image, reference, system_prompt): it must keep working
    for bindings without those features (Requirement 8.5)."""

    def __init__(self, text="generated answer"):
        self.text = text
        self.calls = []

    def __call__(self, model_name, prompt, parameters):
        self.calls.append((model_name, prompt, dict(parameters)))
        return self.text


# ---------------------------------------------------------------------------
# _default_llm_invoker: system_prompt body additivity (Requirement 8.2, 8.5)
# ---------------------------------------------------------------------------

class TestDefaultInvokerSystemPromptBody:
    PARAMS = {"max_tokens": 64, "temperature": 0.2}

    def test_system_prompt_rides_the_body_verbatim(self):
        result, calls = _invoke_default(
            "opt-125m", USER_PROMPT, dict(self.PARAMS),
            system_prompt=SYSTEM_PROMPT,
        )
        assert result == "ok"
        assert calls == [{
            "url": TEXT_GENERATION_URL.format(model_name="opt-125m"),
            "json": {
                "prompt": USER_PROMPT,
                "max_tokens": 64,
                "temperature": 0.2,
                "system_prompt": SYSTEM_PROMPT,
            },
            "timeout": LLM_GENERATION_TIMEOUT_SEC,
        }]

    def test_no_system_prompt_body_is_byte_identical_to_prefeature(self):
        _result, calls = _invoke_default(
            "opt-125m", USER_PROMPT, dict(self.PARAMS))
        assert calls[0]["json"] == {
            "prompt": USER_PROMPT,
            "max_tokens": 64,
            "temperature": 0.2,
        }

    def test_empty_system_prompt_matches_the_absent_body(self):
        _result, calls = _invoke_default(
            "opt-125m", USER_PROMPT, dict(self.PARAMS), system_prompt="")
        assert calls[0]["json"] == {
            "prompt": USER_PROMPT,
            "max_tokens": 64,
            "temperature": 0.2,
        }
        assert "system_prompt" not in calls[0]["json"]

    def test_system_prompt_rides_beside_an_image(self):
        _result, calls = _invoke_default(
            "qwen2-vl", USER_PROMPT, dict(self.PARAMS), "aW1hZ2U=",
            system_prompt=SYSTEM_PROMPT,
        )
        assert calls[0]["json"] == {
            "prompt": USER_PROMPT,
            "max_tokens": 64,
            "temperature": 0.2,
            "image": "aW1hZ2U=",
            "system_prompt": SYSTEM_PROMPT,
        }


# ---------------------------------------------------------------------------
# LlmInferenceProcessor seam: keyword only when configured
# (Requirements 8.2, 8.5, 8.9)
# ---------------------------------------------------------------------------

class TestProcessorSystemPromptSeam:
    def run(self, **parameter_overrides):
        invoker = KwargsRecordingInvoker()
        processor = LlmInferenceProcessor(invoker=invoker)
        processor.process(
            make_document([llm_binding(**parameter_overrides)]), {})
        assert len(invoker.calls) == 1
        return invoker.calls[0]

    @staticmethod
    def seam_kwargs(kwargs):
        """The kwargs minus the additive ``metrics_sink`` capture
        callable the processor forwards to ``**kwargs`` invokers
        (vllm-workflow-latency-optimization): assert it is present and
        callable, then return the remaining (system-prompt seam)
        keywords for the exact-equality assertions."""
        kwargs = dict(kwargs)
        assert callable(kwargs.pop("metrics_sink"))
        return kwargs

    def test_configured_system_prompt_is_supplied_as_keyword(self):
        args, kwargs = self.run(system_prompt=SYSTEM_PROMPT)
        assert args[0] == "opt-125m"
        assert args[1] == USER_PROMPT
        assert len(args) == 3  # text-only positional form unchanged
        assert self.seam_kwargs(kwargs) == {"system_prompt": SYSTEM_PROMPT}

    def test_absent_parameter_keeps_pre_feature_invocation(self):
        args, kwargs = self.run()
        assert len(args) == 3
        assert self.seam_kwargs(kwargs) == {}

    def test_empty_value_keeps_pre_feature_invocation(self):
        args, kwargs = self.run(system_prompt="")
        assert len(args) == 3
        assert self.seam_kwargs(kwargs) == {}

    def test_whitespace_only_value_keeps_pre_feature_invocation(self):
        args, kwargs = self.run(system_prompt=" \t\n ")
        assert len(args) == 3
        assert self.seam_kwargs(kwargs) == {}

    def test_absent_parameter_works_with_a_pre_feature_fake(self):
        # PreFeatureInvoker's fixed 3-parameter signature predates
        # system_prompt: it must keep working unchanged.
        invoker = PreFeatureInvoker()
        processor = LlmInferenceProcessor(invoker=invoker)
        metadata = processor.process(make_document([llm_binding()]), {})
        assert len(invoker.calls) == 1
        assert metadata["llm"]["llm1"] == {
            "generated_text": "generated answer"}

    def test_anomaly_mode_appends_to_the_user_prompt_only(self):
        invoker = KwargsRecordingInvoker(text=VERDICT_ANSWER)
        processor = LlmInferenceProcessor(invoker=invoker)
        processor.process(
            make_document([llm_binding(
                anomaly_mode=True, system_prompt=SYSTEM_PROMPT)]),
            {},
        )
        args, kwargs = invoker.calls[0]
        # The JSON instruction lands on the rendered user prompt...
        assert args[1] == USER_PROMPT + "\n\n" + BEDROCK_JSON_INSTRUCTION
        # ...and the system prompt reaches the invoker verbatim (8.9).
        assert self.seam_kwargs(kwargs) == {"system_prompt": SYSTEM_PROMPT}


# ---------------------------------------------------------------------------
# Processor + default invoker end to end: the complete request body
# (Requirement 8.10)
# ---------------------------------------------------------------------------

class TestRequestBodyEndToEnd:
    def run(self, answer="ok", **parameter_overrides):
        fake = _fake_requests([_Response(200, {"generated_text": answer})])
        processor = LlmInferenceProcessor()  # the real default invoker
        with patch.dict(sys.modules, {"requests": fake}):
            metadata = processor.process(
                make_document([llm_binding(**parameter_overrides)]), {})
        assert len(fake.calls) == 1
        return metadata, fake.calls[0]

    def expected_body(self, prompt, system=None):
        body = {
            "prompt": prompt,
            "max_tokens": 128,
            "temperature": 0.7,
            "top_p": 1.0,
        }
        if system is not None:
            body["system_prompt"] = system
        return body

    def test_with_system_prompt(self):
        metadata, call = self.run(system_prompt=SYSTEM_PROMPT)
        assert call["url"] == TEXT_GENERATION_URL.format(
            model_name="opt-125m")
        assert call["json"] == self.expected_body(
            USER_PROMPT, system=SYSTEM_PROMPT)
        assert metadata["llm"]["llm1"] == {"generated_text": "ok"}

    def test_without_system_prompt(self):
        _metadata, call = self.run()
        assert call["json"] == self.expected_body(USER_PROMPT)
        assert "system_prompt" not in call["json"]

    def test_empty_system_prompt_matches_the_absent_body(self):
        _metadata, absent = self.run()
        _metadata, empty = self.run(system_prompt="")
        assert empty["json"] == absent["json"]
        assert "system_prompt" not in empty["json"]

    def test_anomaly_mode_with_system_prompt(self):
        metadata, call = self.run(
            answer=VERDICT_ANSWER, anomaly_mode=True,
            system_prompt=SYSTEM_PROMPT)
        # The instruction is appended to the user prompt in the body;
        # the system_prompt field carries the configured value verbatim.
        assert call["json"] == self.expected_body(
            USER_PROMPT + "\n\n" + BEDROCK_JSON_INSTRUCTION,
            system=SYSTEM_PROMPT)
        assert metadata["is_anomalous"] is True
        assert metadata["confidence"] == 0.9
