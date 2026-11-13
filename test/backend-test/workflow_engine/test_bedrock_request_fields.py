# Copyright 2026 Amazon Web Services, Inc.
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
"""bedrock-additional-model-request-fields: the device half.

``additional_model_request_fields`` reaches the Converse call on both
executor paths, and nothing changes for a node that leaves it empty:

- ``_default_bedrock_invoker`` sends the object as
  ``additionalModelRequestFields`` only when given, and the kwargs equal
  the shared Invocation_Builder's ``converse_kwargs()`` (Property 6 of
  quality-prompt-tuning: executor and Bedrock_Scorer send one request);
- the legacy (``anomaly_mode`` / freeform) path and the inspection path
  hand the invoker the ``additional_model_request_fields`` keyword only
  when the node sets it, so injected invokers that predate the keyword
  are called exactly as before;
- a value that is not a JSON object makes no Bedrock call: the legacy
  path fails the run naming the node, the inspection path records the
  failure per ``on_error``.
"""
import json
import os

import pytest

import workflow_engine_test_utils  # noqa: F401 - sets COMPONENT_WORK_PATH

from test_bedrock_attempt_policy import (  # noqa: F401 - stub_boto3 is a fixture
    DEMO_POLICY,
    GOOD,
    JPEG,
    NON_DEFAULT_OPTIONS,
    FakeClock,
    ScriptedInvoker,
    run,
    stub_boto3,
    write_frame,
)
from workflow_engine.output_bindings import (
    BedrockInferenceError,
    BedrockInferenceProcessor,
    _default_bedrock_invoker,
)
from workflow_engine.vendor.workflow_core.anomaly_invocation import (
    build_bedrock_invocation,
)

THINKING_OFF = '{"thinking": {"type": "disabled"}}'
COMPACT = '{"thinking":{"type":"disabled"}}'
FIELDS = "additional_model_request_fields"


class TestDefaultInvoker:
    ARGS = ("us.anthropic.claude-haiku-5-5", "Inspect.", [("Input image", JPEG)],
            "us-west-2", 512)

    def test_without_the_keyword_the_request_has_no_fields(self, stub_boto3):
        _default_bedrock_invoker(*self.ARGS)
        assert "additionalModelRequestFields" not in stub_boto3["client"].kwargs[0]

    def test_the_keyword_is_sent_as_additional_model_request_fields(self, stub_boto3):
        _default_bedrock_invoker(*self.ARGS, additional_model_request_fields=COMPACT)
        assert stub_boto3["client"].kwargs[0]["additionalModelRequestFields"] == \
            {"thinking": {"type": "disabled"}}

    def test_beside_system_prompt_and_policy_options(self, stub_boto3):
        _default_bedrock_invoker(*self.ARGS, "Be terse.", **NON_DEFAULT_OPTIONS,
                                 additional_model_request_fields=COMPACT)
        sent = stub_boto3["client"].kwargs[0]
        assert sent["system"] == [{"text": "Be terse."}]
        assert sent["additionalModelRequestFields"] == {"thinking": {"type": "disabled"}}
        assert stub_boto3["captured"]["config"].kwargs["read_timeout"] == 6

    @pytest.mark.parametrize("fields", ["", THINKING_OFF])
    def test_transport_sends_what_the_shared_builder_builds(self, stub_boto3, fields):
        parameters = {"model": "us.anthropic.claude-haiku-5-5", "region": "us-west-2",
                      "prompt": "Inspect.", "anomaly_mode": False,
                      "system_prompt": "Be terse.", "max_tokens": 512, FIELDS: fields}
        invocation = build_bedrock_invocation(parameters, JPEG, b"\xff\xd8ref")
        keyword = ({} if invocation.additional_model_request_fields is None
                   else {FIELDS: invocation.additional_model_request_fields})
        _default_bedrock_invoker(
            invocation.model, invocation.prompt, list(invocation.images),
            invocation.region, invocation.max_tokens, invocation.system_prompt,
            **keyword)
        assert stub_boto3["client"].kwargs[0] == invocation.converse_kwargs()


# ---------------------------------------------------------------------------
# Legacy path (anomaly_mode / freeform)
# ---------------------------------------------------------------------------

class PositionalInvoker:
    """An injected invoker that predates every keyword: positional only."""

    def __init__(self, answer):
        self.answer = answer
        self.calls = []

    def __call__(self, model, prompt, images, region, max_tokens, system_prompt=None):
        self.calls.append((model, prompt, images, region, max_tokens, system_prompt))
        return self.answer


class RecordingInvoker:
    def __init__(self, answer):
        self.answer = answer
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.answer


def run_legacy(tmp_path, invoker, **parameters):
    write_frame(tmp_path)
    params = {"prompt": "Inspect.", "model": "us.anthropic.claude-haiku-5-5",
              "anomaly_mode": False}
    params.update(parameters)
    binding = {
        "nodeId": "check", "binding": "bedrock_inference", "parameters": params,
        "upstreamNodeIds": ["cam"], "downstreamNodeIds": [],
        "capturePaths": {"in": "{work_dir}/bedrock_frame_camera.jpg",
                         "reference": None},
    }
    processor = BedrockInferenceProcessor(invoker=invoker)
    return processor.process({"schemaVersion": 1, "executorBindings": [binding]},
                             {}, str(tmp_path))


class TestLegacyPath:
    @pytest.mark.parametrize("unset", [{}, {FIELDS: ""}, {FIELDS: "{}"}])
    def test_unset_fields_call_an_old_invoker_exactly_as_before(self, tmp_path, unset):
        invoker = PositionalInvoker('{"parts": []}')
        metadata = run_legacy(tmp_path, invoker, **unset)
        assert len(invoker.calls) == 1
        assert metadata["bedrock"]["check"]["text"] == '{"parts": []}'

    def test_set_fields_reach_the_invoker_as_a_keyword(self, tmp_path):
        invoker = RecordingInvoker('{"parts": []}')
        run_legacy(tmp_path, invoker, **{FIELDS: THINKING_OFF})
        (args, kwargs), = invoker.calls
        assert kwargs == {FIELDS: COMPACT}
        assert len(args) == 5  # no system prompt configured

    def test_set_fields_beside_a_system_prompt(self, tmp_path):
        invoker = RecordingInvoker('{"parts": []}')
        run_legacy(tmp_path, invoker, system_prompt="Be terse.", **{FIELDS: THINKING_OFF})
        (args, kwargs), = invoker.calls
        assert args[5] == "Be terse."
        assert kwargs == {FIELDS: COMPACT}

    def test_anomaly_mode_sends_them_too(self, tmp_path):
        invoker = RecordingInvoker('{"is_anomalous": false, "confidence": 0.9}')
        metadata = run_legacy(tmp_path, invoker, anomaly_mode=True, **{FIELDS: THINKING_OFF})
        assert invoker.calls[0][1] == {FIELDS: COMPACT}
        assert metadata["is_anomalous"] is False

    def test_a_non_object_value_fails_the_run_without_a_call(self, tmp_path):
        invoker = RecordingInvoker("unused")
        with pytest.raises(BedrockInferenceError) as caught:
            run_legacy(tmp_path, invoker, **{FIELDS: "[1, 2]"})
        assert caught.value.node_id == "check"
        assert FIELDS in str(caught.value)
        assert invoker.calls == []


# ---------------------------------------------------------------------------
# Inspection path (answer_format: inspection)
# ---------------------------------------------------------------------------

class TestInspectionPath:
    def test_unset_fields_keep_todays_call(self, tmp_path):
        clock = FakeClock()
        invoker = ScriptedInvoker(clock, (0.1, GOOD))
        run(tmp_path, clock, invoker)
        assert invoker.calls[0][1] == {}

    def test_set_fields_with_the_default_policy(self, tmp_path):
        clock = FakeClock()
        invoker = ScriptedInvoker(clock, (0.1, GOOD))
        entry = run(tmp_path, clock, invoker, **{FIELDS: THINKING_OFF})
        assert invoker.calls[0][1] == {FIELDS: COMPACT}
        assert entry["outcome"] == "pass"

    def test_set_fields_beside_the_attempt_policy_on_every_attempt(self, tmp_path):
        clock = FakeClock()
        invoker = ScriptedInvoker(clock, (1.0, RuntimeError("boom")), (1.0, GOOD))
        run(tmp_path, clock, invoker, **DEMO_POLICY, **{FIELDS: THINKING_OFF})
        assert [options for _args, options in invoker.calls] == \
            [dict(NON_DEFAULT_OPTIONS, **{FIELDS: COMPACT})] * 2

    def test_a_non_object_value_is_a_recorded_failure_without_a_call(self, tmp_path):
        clock = FakeClock()
        invoker = ScriptedInvoker(clock)
        entry = run(tmp_path, clock, invoker, **{FIELDS: '"thinking"'})
        assert invoker.calls == []
        assert entry["outcome"] == "error"
        assert FIELDS in entry["error"]
        assert entry["attempts"] == 0
