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
"""Focused example tests for the additive ``generation_metrics`` field on
the non-streaming generate endpoint (vllm-workflow-latency-optimization
task 3.1).

Covers:
- a runtime exposing ``generate_with_breakdown`` serves the 200 response
  with the additive ``generation_metrics`` field carrying
  ``breakdown.to_payload()`` beside the untouched existing fields
  (Requirements 1.1, 9.2)
- a ``None`` breakdown (capture failed) yields the pre-feature body with
  no ``generation_metrics`` key
- a runtime without the method (pre-feature injected fake) keeps working
  through ``generate`` and produces the pre-feature body byte-identically

The full API-additivity property lives in task 3.2 (Property 11).
"""
from fastapi import FastAPI
from fastapi.testclient import TestClient

from endpoints import text_generation
from utils.auth import authorize_request
from vllm_runtime.generation_metrics import GenerationPhaseBreakdown
from vllm_runtime.manager import ModelState

_MODEL = "qwen2-vl"
_URL = "/text-generation/{0}/generate".format(_MODEL)

_BREAKDOWN = GenerationPhaseBreakdown(
    queueing_ms=3,
    prefill_ms=120,
    decode_ms=800,
    prompt_tokens=42,
    output_tokens=17,
    image_tokens=None,
    image_tokens_applicable=False,
    truncated=False,
    prefill_includes_queueing=False,
)


class _ReadyStatus:
    state = ModelState.READY
    reason = None


class _PreFeatureRuntime:
    """Fake runtime without generate_with_breakdown (pre-feature surface)."""

    def state(self, model_name):
        return _ReadyStatus()

    def engine_args(self, model_name):
        return {"max_model_len": 4096}

    async def generate(self, model_name, prompt, sampling_params, **kwargs):
        return "generated answer"


class _BreakdownRuntime(_PreFeatureRuntime):
    """Fake runtime exposing generate_with_breakdown."""

    def __init__(self, breakdown):
        self._breakdown = breakdown
        self.generate_calls = 0
        self.breakdown_calls = 0

    async def generate(self, model_name, prompt, sampling_params, **kwargs):
        self.generate_calls += 1
        return "generated answer"

    async def generate_with_breakdown(
        self, model_name, prompt, sampling_params, **kwargs
    ):
        self.breakdown_calls += 1
        return "generated answer", self._breakdown


def _make_client(runtime):
    app = FastAPI()
    app.include_router(text_generation.router)
    app.dependency_overrides[text_generation.get_runtime] = lambda: runtime
    app.dependency_overrides[authorize_request] = lambda: None
    return TestClient(app)


def test_breakdown_runtime_adds_generation_metrics():
    """A captured breakdown adds exactly the generation_metrics field,
    carrying to_payload(), beside the untouched existing fields."""
    runtime = _BreakdownRuntime(_BREAKDOWN)
    client = _make_client(runtime)

    response = client.post(_URL, json={"prompt": "hello"})

    assert response.status_code == 200, response.text
    body = response.json()
    assert runtime.breakdown_calls == 1
    assert runtime.generate_calls == 0
    assert body["model_name"] == _MODEL
    assert body["generated_text"] == "generated answer"
    assert body["generation_metrics"] == _BREAKDOWN.to_payload()
    assert set(body) == {"model_name", "generated_text",
                         "generation_metrics"}


def test_none_breakdown_yields_pre_feature_body():
    """When generate_with_breakdown returns (text, None), the response is
    the pre-feature body with no generation_metrics key."""
    runtime = _BreakdownRuntime(None)
    client = _make_client(runtime)

    response = client.post(_URL, json={"prompt": "hello"})

    assert response.status_code == 200, response.text
    assert response.json() == {
        "model_name": _MODEL,
        "generated_text": "generated answer",
    }


def test_pre_feature_runtime_without_method_keeps_working():
    """A fake without generate_with_breakdown is served through generate
    exactly as today, with the pre-feature body."""
    runtime = _PreFeatureRuntime()
    client = _make_client(runtime)

    response = client.post(_URL, json={"prompt": "hello"})

    assert response.status_code == 200, response.text
    assert response.json() == {
        "model_name": _MODEL,
        "generated_text": "generated answer",
    }
