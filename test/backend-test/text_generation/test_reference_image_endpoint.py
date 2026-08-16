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
"""Example-based tests for the Text_Generation_API ``reference_image``
field (vlm-anomaly-reference-parity task 3).

Covers:
- valid two-image pass-through to the runtime for both the generate and
  generate-stream endpoints (Requirement 5.2)
- each invalid ``reference_image`` shape producing a 422 finding naming
  the field, with the runtime never invoked (Requirement 5.1)
- ``reference_image`` without ``image`` rejected (Requirement 5.4)
- absent-field identity: requests without ``reference_image`` normalize
  and invoke the runtime exactly as pre-feature (Requirements 5.3, 7.5)
"""
import base64
import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from endpoints import text_generation
from endpoints.text_generation import (
    GENERATION_DEFAULTS,
    normalize_generation_request,
)
from utils.auth import authorize_request
from vllm_runtime.manager import ModelState

_MODEL_MAX_LEN = 4096
_MODEL = "qwen2-vl"
_URL = "/text-generation/{0}/generate".format(_MODEL)
_STREAM_URL = "/text-generation/{0}/generate-stream".format(_MODEL)

_IMAGE_BYTES = b"\xff\xd8input-jpeg-bytes"
_REFERENCE_BYTES = b"\xff\xd8reference-jpeg-bytes"
_IMAGE_B64 = base64.b64encode(_IMAGE_BYTES).decode("ascii")
_REFERENCE_B64 = base64.b64encode(_REFERENCE_BYTES).decode("ascii")


class _ReadyStatus:
    state = ModelState.READY
    reason = None


class _CapturingRuntime:
    """Fake runtime recording every generate / generate_stream call."""

    def __init__(self):
        self.calls = []
        self.stream_calls = []

    def state(self, model_name):
        return _ReadyStatus()

    def engine_args(self, model_name):
        return {"max_model_len": _MODEL_MAX_LEN}

    async def generate(self, model_name, prompt, sampling_params, **kwargs):
        self.calls.append({
            "model_name": model_name,
            "prompt": prompt,
            "sampling_params": dict(sampling_params),
            "kwargs": dict(kwargs),
        })
        return "generated answer"

    async def generate_stream(
        self, model_name, prompt, sampling_params, **kwargs
    ):
        self.stream_calls.append({
            "model_name": model_name,
            "prompt": prompt,
            "sampling_params": dict(sampling_params),
            "kwargs": dict(kwargs),
        })
        for token in ("a", "b"):
            yield token


def _make_client(runtime):
    app = FastAPI()
    app.include_router(text_generation.router)
    app.dependency_overrides[text_generation.get_runtime] = lambda: runtime
    app.dependency_overrides[authorize_request] = lambda: None
    return TestClient(app)


def _findings_for(response, field):
    assert response.status_code == 422, response.text
    findings = response.json()["findings"]
    return [f for f in findings if f["field"] == field]


# ---------------------------------------------------------------------------
# Valid two-image pass-through (Requirement 5.2)
# ---------------------------------------------------------------------------


def test_generate_two_image_pass_through():
    """A valid two-image request forwards both decoded byte payloads to
    the runtime as keywords, beside the unchanged prompt and sampling."""
    runtime = _CapturingRuntime()
    client = _make_client(runtime)

    response = client.post(_URL, json={
        "prompt": "compare",
        "image": _IMAGE_B64,
        "reference_image": _REFERENCE_B64,
    })

    assert response.status_code == 200, response.text
    assert len(runtime.calls) == 1
    call = runtime.calls[0]
    assert call["prompt"] == "compare"
    assert call["sampling_params"] == dict(GENERATION_DEFAULTS)
    assert call["kwargs"] == {
        "image": _IMAGE_BYTES,
        "reference_image": _REFERENCE_BYTES,
    }


def test_generate_stream_two_image_pass_through():
    """The streaming endpoint forwards reference_image identically."""
    runtime = _CapturingRuntime()
    client = _make_client(runtime)

    response = client.post(_STREAM_URL, json={
        "prompt": "compare",
        "image": _IMAGE_B64,
        "reference_image": _REFERENCE_B64,
    })

    assert response.status_code == 200, response.text
    assert "data:" in response.text
    assert len(runtime.stream_calls) == 1
    assert runtime.stream_calls[0]["kwargs"] == {
        "image": _IMAGE_BYTES,
        "reference_image": _REFERENCE_BYTES,
    }


# ---------------------------------------------------------------------------
# Invalid reference_image shapes (Requirement 5.1)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("reference_value", [
    123,                     # not a string
    ["bXVsdGk="],            # not a string
    "not-valid-base64!!!",   # invalid base64
    "",                      # decodes to zero bytes
])
def test_invalid_reference_shapes_rejected(reference_value):
    """Each invalid reference_image shape yields a 422 finding naming
    reference_image and never invokes the runtime."""
    runtime = _CapturingRuntime()
    client = _make_client(runtime)

    response = client.post(_URL, json={
        "prompt": "compare",
        "image": _IMAGE_B64,
        "reference_image": reference_value,
    })

    findings = _findings_for(response, "reference_image")
    assert findings, response.text
    assert all(f["reason"] for f in findings)
    assert runtime.calls == []


def test_oversized_reference_rejected():
    """A reference_image decoding beyond the configured maximum is a
    finding naming reference_image."""
    previous = os.environ.get("TEXT_GEN_MAX_IMAGE_BYTES")
    os.environ["TEXT_GEN_MAX_IMAGE_BYTES"] = "8"
    try:
        runtime = _CapturingRuntime()
        client = _make_client(runtime)
        small = base64.b64encode(b"12345678").decode("ascii")
        oversized = base64.b64encode(b"123456789").decode("ascii")
        response = client.post(_URL, json={
            "prompt": "compare",
            "image": small,
            "reference_image": oversized,
        })
        findings = _findings_for(response, "reference_image")
        assert findings
        assert "exceeding" in findings[0]["reason"]
        assert runtime.calls == []
    finally:
        if previous is None:
            del os.environ["TEXT_GEN_MAX_IMAGE_BYTES"]
        else:
            os.environ["TEXT_GEN_MAX_IMAGE_BYTES"] = previous


def test_reference_without_image_rejected():
    """A reference_image without a primary image is rejected with a
    finding explaining the dependency (Requirement 5.4)."""
    runtime = _CapturingRuntime()
    client = _make_client(runtime)

    response = client.post(_URL, json={
        "prompt": "compare",
        "reference_image": _REFERENCE_B64,
    })

    findings = _findings_for(response, "reference_image")
    assert findings
    assert "image" in findings[0]["reason"]
    assert runtime.calls == []


def test_reference_beside_invalid_image_rejected():
    """A valid reference_image beside an invalid image yields findings
    for both fields (the reference has no valid primary image)."""
    runtime = _CapturingRuntime()
    client = _make_client(runtime)

    response = client.post(_URL, json={
        "prompt": "compare",
        "image": "not-base64!!!",
        "reference_image": _REFERENCE_B64,
    })

    assert response.status_code == 422, response.text
    findings = response.json()["findings"]
    assert [f for f in findings if f["field"] == "image"]
    assert [f for f in findings if f["field"] == "reference_image"]
    assert runtime.calls == []


# ---------------------------------------------------------------------------
# Absent-field identity (Requirements 5.3, 7.5)
# ---------------------------------------------------------------------------


def test_absent_reference_normalization_identity():
    """Without a reference_image field, normalization output carries no
    reference_image_bytes key and is identical to pre-feature output."""
    body = {"prompt": "hello", "max_tokens": 5, "image": _IMAGE_B64}
    result = normalize_generation_request(_MODEL, body, _MODEL_MAX_LEN)

    assert isinstance(result, dict)
    assert "reference_image_bytes" not in result
    expected = dict(GENERATION_DEFAULTS)
    expected["max_tokens"] = 5
    expected["model_name"] = _MODEL
    expected["prompt"] = "hello"
    expected["image_bytes"] = _IMAGE_BYTES
    assert result == expected


def test_absent_reference_runtime_invocation_identity():
    """Without a reference_image field the runtime invocation carries no
    reference_image keyword — single-image and imageless requests are
    byte-identical to pre-feature behavior."""
    runtime = _CapturingRuntime()
    client = _make_client(runtime)

    single = client.post(_URL, json={"prompt": "hi", "image": _IMAGE_B64})
    assert single.status_code == 200, single.text
    plain = client.post(_URL, json={"prompt": "hi"})
    assert plain.status_code == 200, plain.text

    assert len(runtime.calls) == 2
    assert runtime.calls[0]["kwargs"] == {"image": _IMAGE_BYTES}
    assert runtime.calls[1]["kwargs"] == {}


def test_image_findings_unchanged_for_imageless_bodies():
    """Bodies without any image field keep pre-feature validation: a
    valid text-only request normalizes with no image keys at all."""
    result = normalize_generation_request(
        _MODEL, {"prompt": "text only"}, _MODEL_MAX_LEN)
    assert isinstance(result, dict)
    assert "image_bytes" not in result
    assert "reference_image_bytes" not in result
