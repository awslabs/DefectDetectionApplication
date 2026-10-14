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
"""Unit tests for the LLM_Binding generation-metrics return path
(vllm-workflow-latency-optimization task 5.2).

Validates the plumbing of Requirements 1.2 and 1.5:

- ``_default_llm_invoker`` ``metrics_sink`` keyword: on a 200 the sink
  receives ``payload.get("generation_metrics")`` (``None`` for a
  metrics-less body), contained — a raising sink never disturbs the
  generated-text return; no sink keeps the pre-feature behavior.
- ``LlmInferenceProcessor._run_one`` seam: ``metrics_sink`` is forwarded
  only when the (possibly injected) invoker accepts the keyword (the
  ``_accepts_keyword`` pattern), so pre-feature fakes with fixed
  signatures keep working unchanged.
- Outcome merge: a captured metrics dict lands additively as
  ``outcome["generation_metrics"]``; a metrics-less run's outcome is
  byte-identical to pre-feature.

The run-log emission property (task 5.4) and the concrete line format
(task 5.5) are covered by their own test files.

Harness follows ``test_llm_system_prompt.py`` (fake ``requests`` module
via ``patch.dict(sys.modules, ...)``, recording invokers).
"""
import sys
import types
from unittest.mock import patch

from workflow_engine_test_utils import DEVICE_ARCH

from workflow_engine.output_bindings import (
    LlmInferenceProcessor,
    _default_llm_invoker,
)

METRICS_PAYLOAD = {
    "queueing_ms": 12,
    "prefill_ms": 842,
    "decode_ms": 16571,
    "prefill_includes_queueing": False,
    "prompt_tokens": 1180,
    "output_tokens": 256,
    "image_tokens": "n/a",
    "truncated": True,
}


def llm_binding(node_id="llm1", **parameter_overrides):
    parameters = {
        "modelName": "opt-125m",
        "prompt_template": "Summarize the run.",
        "max_tokens": 128,
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


def _invoke_default(body, **kwargs):
    fake = _fake_requests([_Response(200, body)])
    with patch.dict(sys.modules, {"requests": fake}):
        result = _default_llm_invoker(
            "opt-125m", "prompt", {"max_tokens": 128}, **kwargs)
    return result


# ---------------------------------------------------------------------------
# _default_llm_invoker: metrics_sink delivery and containment (R1.2, R1.5)
# ---------------------------------------------------------------------------

class TestDefaultInvokerMetricsSink:
    def test_sink_receives_the_generation_metrics_payload(self):
        received = []
        result = _invoke_default(
            {"generated_text": "ok", "generation_metrics": METRICS_PAYLOAD},
            metrics_sink=received.append,
        )
        assert result == "ok"
        assert received == [METRICS_PAYLOAD]

    def test_sink_receives_none_for_a_metrics_less_body(self):
        received = []
        result = _invoke_default(
            {"generated_text": "ok"}, metrics_sink=received.append)
        assert result == "ok"
        assert received == [None]

    def test_a_raising_sink_never_disturbs_the_generated_text(self):
        def sink(_metrics):
            raise RuntimeError("sink exploded")

        result = _invoke_default(
            {"generated_text": "ok", "generation_metrics": METRICS_PAYLOAD},
            metrics_sink=sink,
        )
        assert result == "ok"

    def test_no_sink_keeps_the_pre_feature_return(self):
        result = _invoke_default(
            {"generated_text": "ok", "generation_metrics": METRICS_PAYLOAD})
        assert result == "ok"


# ---------------------------------------------------------------------------
# Processor seam: metrics_sink only for invokers that accept it (R1.5)
# ---------------------------------------------------------------------------

class PreFeatureInvoker:
    """Fixed 3-parameter fake predating every optional invoker
    parameter: it must keep working unchanged (no metrics_sink)."""

    def __init__(self, text="generated answer"):
        self.text = text
        self.calls = []

    def __call__(self, model_name, prompt, parameters):
        self.calls.append((model_name, prompt, dict(parameters)))
        return self.text


class MetricsAwareInvoker:
    """Fake declaring the keyword-only ``metrics_sink``, delivering a
    canned metrics payload through it like the real invoker's 200 path."""

    def __init__(self, text="generated answer", metrics=None):
        self.text = text
        self.metrics = metrics
        self.sinks = []

    def __call__(self, model_name, prompt, parameters, *,
                 metrics_sink=None):
        self.sinks.append(metrics_sink)
        if metrics_sink is not None:
            metrics_sink(self.metrics)
        return self.text


class TestProcessorMetricsSeam:
    def test_pre_feature_invoker_keeps_working_without_the_keyword(self):
        invoker = PreFeatureInvoker()
        processor = LlmInferenceProcessor(invoker=invoker)
        metadata = processor.process(make_document([llm_binding()]), {})
        assert len(invoker.calls) == 1
        assert metadata["llm"]["llm1"] == {
            "generated_text": "generated answer"}

    def test_declaring_invoker_receives_a_callable_sink(self):
        invoker = MetricsAwareInvoker(metrics=METRICS_PAYLOAD)
        processor = LlmInferenceProcessor(invoker=invoker)
        processor.process(make_document([llm_binding()]), {})
        assert len(invoker.sinks) == 1
        assert callable(invoker.sinks[0])

    def test_captured_metrics_merge_additively_into_the_outcome(self):
        invoker = MetricsAwareInvoker(metrics=METRICS_PAYLOAD)
        processor = LlmInferenceProcessor(invoker=invoker)
        metadata = processor.process(make_document([llm_binding()]), {})
        assert metadata["llm"]["llm1"] == {
            "generated_text": "generated answer",
            "generation_metrics": METRICS_PAYLOAD,
        }

    def test_a_metrics_less_run_outcome_is_unchanged(self):
        # The sink is forwarded but delivers None (metrics-less 200):
        # the outcome must be byte-identical to pre-feature (R1.5).
        invoker = MetricsAwareInvoker(metrics=None)
        processor = LlmInferenceProcessor(invoker=invoker)
        metadata = processor.process(make_document([llm_binding()]), {})
        assert metadata["llm"]["llm1"] == {
            "generated_text": "generated answer"}

    def test_non_dict_metrics_are_ignored(self):
        invoker = MetricsAwareInvoker(metrics="not-a-dict")
        processor = LlmInferenceProcessor(invoker=invoker)
        metadata = processor.process(make_document([llm_binding()]), {})
        assert metadata["llm"]["llm1"] == {
            "generated_text": "generated answer"}

    def test_metrics_merge_beside_an_anomaly_verdict(self):
        invoker = MetricsAwareInvoker(
            text='{"is_anomalous": true, "confidence": 0.9}',
            metrics=METRICS_PAYLOAD,
        )
        processor = LlmInferenceProcessor(invoker=invoker)
        metadata = processor.process(
            make_document([llm_binding(anomaly_mode=True)]), {})
        outcome = metadata["llm"]["llm1"]
        assert outcome["is_anomalous"] is True
        assert outcome["confidence"] == 0.9
        assert outcome["generation_metrics"] == METRICS_PAYLOAD
        # The flat parity keys still merge (existing behavior untouched).
        assert metadata["is_anomalous"] is True
