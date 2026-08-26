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
"""Example test for direct-caller default behavior
(vllm-workflow-latency-optimization task 3.3).

A Generation_Call that originates from a caller other than the
LLM_Binding (here: the FastAPI TestClient posting directly to the
Text_Generation_API) and omits ``max_tokens`` gets the pre-feature
behavior unchanged: the API's own ``GENERATION_DEFAULTS`` (max_tokens
256) is applied exactly as before this feature — no default is injected
by the feature, no new field appears on the request path, and request
validation is unchanged.

**Validates: Requirements 3.7**
"""
from fastapi import FastAPI
from fastapi.testclient import TestClient

from endpoints import text_generation
from endpoints.text_generation import GENERATION_DEFAULTS
from utils.auth import authorize_request
from vllm_runtime.manager import ModelState

_MODEL = "qwen3-vl-8b"
_URL = "/text-generation/{0}/generate".format(_MODEL)


class _ReadyStatus:
    state = ModelState.READY
    reason = None


class _RecordingRuntime:
    """Fake runtime (pre-feature surface, no generate_with_breakdown)
    that records exactly what the endpoint hands to generate."""

    def __init__(self):
        self.calls = []

    def state(self, model_name):
        return _ReadyStatus()

    def engine_args(self, model_name):
        return {"max_model_len": 4096}

    async def generate(self, model_name, prompt, sampling_params, **kwargs):
        self.calls.append({
            "model_name": model_name,
            "prompt": prompt,
            "sampling_params": dict(sampling_params),
            "kwargs": dict(kwargs),
        })
        return "generated answer"


def _make_client(runtime):
    app = FastAPI()
    app.include_router(text_generation.router)
    app.dependency_overrides[text_generation.get_runtime] = lambda: runtime
    app.dependency_overrides[authorize_request] = lambda: None
    return TestClient(app)


def test_direct_caller_omitting_max_tokens_gets_pre_feature_defaults():
    """A direct (non-LLM_Binding) caller omitting max_tokens is served
    with the pre-feature GENERATION_DEFAULTS max_tokens of 256; the
    sampling params carry exactly the pre-feature keys with no new
    fields on the request path (Requirement 3.7)."""
    runtime = _RecordingRuntime()
    client = _make_client(runtime)

    response = client.post(_URL, json={"prompt": "describe the image"})

    assert response.status_code == 200, response.text
    assert len(runtime.calls) == 1
    call = runtime.calls[0]

    # The runtime receives the pre-feature GENERATION_DEFAULTS 256
    # bound — this is the API's own longstanding default, not a value
    # injected by this feature.
    assert GENERATION_DEFAULTS["max_tokens"] == 256
    assert call["sampling_params"]["max_tokens"] == 256

    # No new fields on the request path: exactly the pre-feature
    # sampling parameter set, with the pre-feature default values for
    # every omitted parameter, and no extra generate kwargs.
    assert set(call["sampling_params"]) == {
        "max_tokens", "temperature", "top_p"}
    assert call["sampling_params"] == dict(GENERATION_DEFAULTS)
    assert call["kwargs"] == {}
    assert call["model_name"] == _MODEL
    assert call["prompt"] == "describe the image"

    # Pre-feature response body, byte-identical field set.
    assert response.json() == {
        "model_name": _MODEL,
        "generated_text": "generated answer",
    }


def test_direct_caller_explicit_max_tokens_is_forwarded_unchanged():
    """A direct caller supplying a valid max_tokens still gets its own
    value — the default applies only to omission, as before."""
    runtime = _RecordingRuntime()
    client = _make_client(runtime)

    response = client.post(
        _URL, json={"prompt": "describe the image", "max_tokens": 33})

    assert response.status_code == 200, response.text
    assert runtime.calls[0]["sampling_params"]["max_tokens"] == 33


def test_direct_caller_request_validation_unchanged():
    """The pre-feature validation path is untouched: an invalid
    max_tokens from a direct caller still 422s with a finding naming the
    field, and the runtime is never invoked (no LLM_Binding-style
    substitution happens on the API)."""
    runtime = _RecordingRuntime()
    client = _make_client(runtime)

    response = client.post(
        _URL, json={"prompt": "describe the image", "max_tokens": 0})

    assert response.status_code == 422, response.text
    findings = response.json()["findings"]
    assert any(f["field"] == "max_tokens" for f in findings)
    assert runtime.calls == []
