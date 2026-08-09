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
"""Property test for the Text_Generation_API image byte round trip (task 4.4).

**Feature: edge-vlm-image-inference, Property 7: Image bytes round-trip to
the runtime**

*For any* valid generate request carrying a base64 Image_Payload, the bytes
the Text_Generation_API forwards to the runtime generate invocation SHALL
equal the base64-decoding of the request's ``image`` field, alongside the
same prompt and sampling parameters the request would produce without the
image.

**Validates: Requirements 3.2**

The endpoint is exercised through a minimal FastAPI app carrying the real
router, with the ``get_runtime`` dependency overridden by a fake runtime
that records every ``generate(...)`` invocation (positional args plus the
conditional ``image=`` keyword). Each request is issued twice — once with
the ``image`` field and once without — and the two captured invocations are
compared.

Runs with the hypothesis profiles registered in this directory's conftest
(``textgen-fast`` = 25 examples locally, ``HYPOTHESIS_PROFILE=ci`` = 100).
"""
import base64

from fastapi import FastAPI
from fastapi.testclient import TestClient
from hypothesis import given, settings
from hypothesis import strategies as st

from endpoints import text_generation
from utils.auth import authorize_request
from vllm_runtime.manager import ModelState


class _ReadyStatus:
    """A manager model status whose state is READY (the shape the router
    reads through ``getattr(status, "state", ...)``)."""

    state = ModelState.READY
    reason = None


class _CapturingRuntime:
    """Fake runtime exposing the surface the router needs (``state``,
    ``engine_args``, ``generate``) and recording every generate
    invocation's args, including the conditional ``image=`` kwarg."""

    def __init__(self, max_model_len=4096):
        self._max_model_len = max_model_len
        self.calls = []

    def state(self, model_name):
        return _ReadyStatus()

    def engine_args(self, model_name):
        return {"max_model_len": self._max_model_len}

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
    # The access-log router attaches the auth decision matrix to every
    # route; the property under test is the generate invocation, so auth
    # is overridden to open access.
    app.dependency_overrides[authorize_request] = lambda: None
    return TestClient(app)


_MAX_MODEL_LEN = 4096

_model_names = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz0123456789-_",
    min_size=1,
    max_size=24,
)

_prompts = st.text(min_size=1, max_size=200)

_image_payloads = st.binary(min_size=1, max_size=512)


@st.composite
def _sampling_bodies(draw):
    """A random subset of the generation parameters, each drawn from its
    valid range (omitted parameters exercise the defaulting path)."""
    body = {}
    if draw(st.booleans()):
        body["max_tokens"] = draw(
            st.integers(min_value=1, max_value=_MAX_MODEL_LEN))
    if draw(st.booleans()):
        body["temperature"] = draw(st.floats(
            min_value=0.0, max_value=2.0,
            allow_nan=False, allow_infinity=False))
    if draw(st.booleans()):
        body["top_p"] = draw(st.floats(
            min_value=0.0, max_value=1.0, exclude_min=True,
            allow_nan=False, allow_infinity=False))
    return body


@given(
    model_name=_model_names,
    prompt=_prompts,
    sampling_body=_sampling_bodies(),
    image_bytes=_image_payloads,
)
@settings(deadline=None)
def test_image_bytes_round_trip_to_runtime(
    model_name, prompt, sampling_body, image_bytes
):
    """**Feature: edge-vlm-image-inference, Property 7: Image bytes
    round-trip to the runtime**

    **Validates: Requirements 3.2**
    """
    runtime = _CapturingRuntime(max_model_len=_MAX_MODEL_LEN)
    client = _make_client(runtime)

    base_body = dict(sampling_body)
    base_body["prompt"] = prompt
    image_body = dict(base_body)
    image_body["image"] = base64.b64encode(image_bytes).decode("ascii")

    url = "/text-generation/{0}/generate".format(model_name)

    image_response = client.post(url, json=image_body)
    assert image_response.status_code == 200, image_response.text

    plain_response = client.post(url, json=base_body)
    assert plain_response.status_code == 200, plain_response.text

    assert len(runtime.calls) == 2
    image_call, plain_call = runtime.calls

    # The forwarded bytes equal the base64-decoding of the request's
    # image field (Requirement 3.2).
    assert image_call["kwargs"] == {
        "image": base64.b64decode(image_body["image"])
    }
    assert image_call["kwargs"]["image"] == image_bytes

    # The imageless request produces a runtime invocation with no image
    # keyword at all.
    assert plain_call["kwargs"] == {}

    # Alongside the image, the prompt and sampling parameters are the
    # same ones the request would produce without the image.
    assert image_call["model_name"] == plain_call["model_name"] == model_name
    assert image_call["prompt"] == plain_call["prompt"] == prompt
    assert image_call["sampling_params"] == plain_call["sampling_params"]

    # And those sampling parameters are the request's values overlaid on
    # the documented defaults for exactly the omitted ones.
    expected_sampling = dict(text_generation.GENERATION_DEFAULTS)
    expected_sampling.update(sampling_body)
    assert image_call["sampling_params"] == expected_sampling
