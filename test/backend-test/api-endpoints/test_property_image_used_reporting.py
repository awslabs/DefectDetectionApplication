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
"""Property test for image_used reporting on the Text_Generation_API.

**Feature: edge-vlm-image-inference, Property 9: image_used reporting**

**Validates: Requirements 3.6, 4.3**

For any valid generate request carrying an Image_Payload, the
non-streaming generate response contains ``image_used == true`` exactly
when the serving model is multimodal-capable (as reported by the
runtime's ``image_supported``); and responses to requests WITHOUT an
image carry no new keys — exactly ``{"model_name", "generated_text"}``,
byte-identical to pre-feature behavior.

Exercised through FastAPI's TestClient with the router's ``get_runtime``
dependency overridden by a fake runtime whose ``image_supported``
capability is toggled per example. A fake WITHOUT ``image_supported`` at
all is also covered: the endpoint's ``_image_used`` treats a missing
capability surface as "not consumed" (``image_used == false``).
"""
import base64

from fastapi import FastAPI
from fastapi.testclient import TestClient
from hypothesis import given
from hypothesis import strategies as st

import endpoints.text_generation as text_generation
from utils.auth import authorize_request


# ---------------------------------------------------------------------------
# Fakes and app plumbing
# ---------------------------------------------------------------------------


class _ClientAddressInjector:
    """ASGI wrapper setting scope['client'] when the test client leaves it
    unset — AccessLogRoute's request logging dereferences request.client,
    which this starlette version's TestClient does not populate."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and not scope.get("client"):
            scope["client"] = ("testclient", 50000)
        await self.app(scope, receive, send)


class _ReadyStatus:
    state = "READY"
    reason = None


class FakeRuntime:
    """Fake VllmRuntimeManager surface for the generate endpoint: READY
    state, engine_args with a max_model_len, a generate() capturing its
    arguments, and a togglable image_supported() capability."""

    def __init__(self, image_capable):
        self._image_capable = image_capable
        self.generate_calls = []

    def state(self, model_name):
        return _ReadyStatus()

    def engine_args(self, model_name):
        return {"max_model_len": 8192}

    async def generate(self, model_name, prompt, sampling_params, **kwargs):
        self.generate_calls.append(
            {
                "model_name": model_name,
                "prompt": prompt,
                "sampling_params": sampling_params,
                "kwargs": kwargs,
            }
        )
        return "generated"

    def image_supported(self, model_name):
        return self._image_capable


class LegacyFakeRuntime(FakeRuntime):
    """A runtime fake WITHOUT any image_supported capability surface —
    the endpoint must treat missing capability reporting as image not
    consumed (image_used == false)."""

    image_supported = None  # not callable: capability surface absent


def _make_client(runtime):
    app = FastAPI()
    app.include_router(text_generation.router)
    app.dependency_overrides[text_generation.get_runtime] = lambda: runtime
    app.dependency_overrides[authorize_request] = lambda: None
    return TestClient(_ClientAddressInjector(app))


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

MODEL_NAMES = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.",
    min_size=1,
    max_size=32,
).filter(lambda name: name not in (".", ".."))

PROMPTS = st.text(min_size=1, max_size=200)

IMAGE_B64 = st.binary(min_size=1, max_size=4096).map(
    lambda raw: base64.b64encode(raw).decode("ascii")
)

SAMPLING_PARAMS = st.fixed_dictionaries(
    {},
    optional={
        "max_tokens": st.integers(min_value=1, max_value=8192),
        "temperature": st.floats(
            min_value=0.0, max_value=2.0, allow_nan=False, allow_infinity=False
        ),
        "top_p": st.floats(
            min_value=0.01, max_value=1.0, allow_nan=False, allow_infinity=False
        ),
    },
)


# ---------------------------------------------------------------------------
# Property 9: image_used reporting
# ---------------------------------------------------------------------------


@given(
    model_name=MODEL_NAMES,
    prompt=PROMPTS,
    image_b64=IMAGE_B64,
    params=SAMPLING_PARAMS,
    image_capable=st.booleans(),
)
def test_image_used_mirrors_multimodal_capability(
    model_name, prompt, image_b64, params, image_capable
):
    """**Feature: edge-vlm-image-inference, Property 9: image_used reporting**

    **Validates: Requirements 3.6, 4.3**

    For any valid image-carrying generate request, the non-streaming
    response contains ``image_used == true`` exactly when the runtime
    reports the model as multimodal-capable, and the response carries
    exactly the keys {model_name, generated_text, image_used}.
    """
    runtime = FakeRuntime(image_capable=image_capable)
    client = _make_client(runtime)

    body = dict(params)
    body["prompt"] = prompt
    body["image"] = image_b64

    response = client.post(
        "/text-generation/{}/generate".format(model_name), json=body
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert set(payload.keys()) == {"model_name", "generated_text", "image_used"}
    assert payload["image_used"] is image_capable
    assert payload["model_name"] == model_name
    # The runtime was invoked exactly once, with the decoded image bytes.
    assert len(runtime.generate_calls) == 1
    call = runtime.generate_calls[0]
    assert call["kwargs"] == {"image": base64.b64decode(image_b64)}


@given(
    model_name=MODEL_NAMES,
    prompt=PROMPTS,
    params=SAMPLING_PARAMS,
    image_capable=st.booleans(),
)
def test_imageless_response_carries_no_new_keys(
    model_name, prompt, params, image_capable
):
    """**Feature: edge-vlm-image-inference, Property 9: image_used reporting**

    **Validates: Requirements 3.6, 4.3**

    For any valid generate request WITHOUT an image — regardless of the
    model's multimodal capability — the response carries exactly the
    pre-feature keys {model_name, generated_text} (no image_used key),
    and the runtime generate invocation carries no image argument.
    """
    runtime = FakeRuntime(image_capable=image_capable)
    client = _make_client(runtime)

    body = dict(params)
    body["prompt"] = prompt

    response = client.post(
        "/text-generation/{}/generate".format(model_name), json=body
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert set(payload.keys()) == {"model_name", "generated_text"}
    assert payload["model_name"] == model_name
    # The runtime invocation is byte-identical to pre-feature behavior:
    # no image keyword at all.
    assert len(runtime.generate_calls) == 1
    assert runtime.generate_calls[0]["kwargs"] == {}


@given(
    model_name=MODEL_NAMES,
    prompt=PROMPTS,
    image_b64=IMAGE_B64,
)
def test_runtime_without_capability_surface_reports_not_used(
    model_name, prompt, image_b64
):
    """**Feature: edge-vlm-image-inference, Property 9: image_used reporting**

    **Validates: Requirements 3.6, 4.3**

    A runtime fake exposing no callable ``image_supported`` (a runtime
    that cannot report capability) yields ``image_used == false`` for any
    image-carrying request: no consumption can be claimed.
    """
    runtime = LegacyFakeRuntime(image_capable=True)
    client = _make_client(runtime)

    response = client.post(
        "/text-generation/{}/generate".format(model_name),
        json={"prompt": prompt, "image": image_b64},
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert set(payload.keys()) == {"model_name", "generated_text", "image_used"}
    assert payload["image_used"] is False
