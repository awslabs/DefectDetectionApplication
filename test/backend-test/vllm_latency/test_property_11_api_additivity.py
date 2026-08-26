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
"""Property test for the Text_Generation_API additive metrics field
(task 3.2).

# Feature: vllm-workflow-latency-optimization, Property 11: API additivity

*For any* valid generate request driven through the endpoint with a fake
runtime, the response SHALL contain every pre-feature field (``model_name``,
``generated_text``, and ``image_used`` exactly per its existing conditional
rule) with values identical to the pre-feature handler's, and SHALL differ
at most by the additional ``generation_metrics`` field.

**Validates: Requirements 9.2**

The test drives every hypothesis-generated request body through TWO
FastAPI TestClients over the real router in
``endpoints.text_generation``:

* a *pre-feature* reference runtime WITHOUT ``generate_with_breakdown``
  (exactly the injected-fake surface that existed before the feature);
* a *featured* runtime that may expose ``generate_with_breakdown``
  returning either ``None`` (capture failed) or a generated
  ``GenerationPhaseBreakdown``.

Both runtimes are otherwise identical (same generated text, same
``image_supported`` capability, READY state, same ``max_model_len``), so
any divergence between the two responses is attributable to the feature.
Request bodies cover valid and invalid prompts, sampling parameters
(``max_tokens`` / ``temperature`` / ``top_p``, in and out of range, wrong
types), ``system_prompt`` variants, and ``image`` payloads (valid base64,
invalid base64, wrong type) so both the 200 path (with and without
``image_used``) and the 422 error path are exercised.

Asserted per generated request:

* identical status code;
* non-200 responses (422 findings etc.) byte-identical;
* 200 responses: every pre-feature field present in the featured body with
  the identical name, JSON type, and value;
* the featured body's extra keys are at most ``{"generation_metrics"}``,
  and the field is present exactly when the featured runtime exposes the
  method AND a breakdown was captured — in which case it equals
  ``breakdown.to_payload()``.

Note: hypothesis and function-scoped fixtures do not mix, so the two
TestClients are built once at module level over mutable runtime holders
that each example swaps in place.
"""
import base64

from fastapi import FastAPI
from fastapi.testclient import TestClient
from hypothesis import given, settings
from hypothesis import strategies as st

from endpoints import text_generation
from utils.auth import authorize_request
from vllm_runtime.generation_metrics import GenerationPhaseBreakdown
from vllm_runtime.manager import ModelState

_MODEL = "qwen2-vl"
_URL = "/text-generation/{0}/generate".format(_MODEL)
_MAX_MODEL_LEN = 4096

_OMIT = object()  # sentinel: leave the field out of the request body


# ---------------------------------------------------------------------------
# Fake runtimes (adapted from
# test/backend-test/text_generation/test_generation_metrics_field.py)
# ---------------------------------------------------------------------------

class _ReadyStatus:
    state = ModelState.READY
    reason = None


class _PreFeatureRuntime:
    """Fake runtime WITHOUT generate_with_breakdown (pre-feature surface)."""

    def __init__(self, text, image_capability):
        self._text = text
        # None = capability not reported (no image_supported attribute
        # behavior via a non-callable), True/False = reported capability.
        self._image_capability = image_capability

    def state(self, model_name):
        return _ReadyStatus()

    def engine_args(self, model_name):
        return {"max_model_len": _MAX_MODEL_LEN}

    async def generate(self, model_name, prompt, sampling_params, **kwargs):
        return self._text

    def __getattr__(self, name):
        # image_supported is exposed only when the capability is reported,
        # so the "fake without the method" variant is exercised too.
        if name == "image_supported" and self._image_capability is not None:
            return lambda model_name: self._image_capability
        raise AttributeError(name)


class _BreakdownRuntime(_PreFeatureRuntime):
    """Fake runtime exposing generate_with_breakdown."""

    def __init__(self, text, image_capability, breakdown):
        super().__init__(text, image_capability)
        self._breakdown = breakdown

    async def generate_with_breakdown(
        self, model_name, prompt, sampling_params, **kwargs
    ):
        return self._text, self._breakdown


# ---------------------------------------------------------------------------
# Module-level clients over swappable runtime holders (hypothesis and
# function-scoped fixtures do not mix)
# ---------------------------------------------------------------------------

def _make_client(holder):
    app = FastAPI()
    app.include_router(text_generation.router)
    app.dependency_overrides[text_generation.get_runtime] = (
        lambda: holder["runtime"])
    app.dependency_overrides[authorize_request] = lambda: None
    return TestClient(app)


_PRE_HOLDER = {"runtime": None}
_FEAT_HOLDER = {"runtime": None}
_PRE_CLIENT = _make_client(_PRE_HOLDER)
_FEAT_CLIENT = _make_client(_FEAT_HOLDER)


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

_PROMPTS = st.one_of(
    st.text(min_size=1, max_size=30),         # valid
    st.just(""),                              # invalid: empty
    st.just(_OMIT),                           # invalid: missing
    st.integers(min_value=0, max_value=9),    # invalid: wrong type
)

_MAX_TOKENS = st.one_of(
    st.just(_OMIT),
    st.integers(min_value=1, max_value=_MAX_MODEL_LEN),        # valid
    st.sampled_from([0, -1, _MAX_MODEL_LEN + 1, 1.5, "many",   # invalid
                     True]),
)

_TEMPERATURES = st.one_of(
    st.just(_OMIT),
    st.floats(min_value=0.0, max_value=2.0,
              allow_nan=False, allow_infinity=False),          # valid
    st.sampled_from([-0.1, 2.5, "hot", True]),                 # invalid
)

_TOP_PS = st.one_of(
    st.just(_OMIT),
    st.floats(min_value=0.01, max_value=1.0,
              allow_nan=False, allow_infinity=False),          # valid
    st.sampled_from([0.0, -0.5, 1.5, "p", False]),             # invalid
)

_SYSTEM_PROMPTS = st.one_of(
    st.just(_OMIT),
    st.text(max_size=20),                                      # valid (or
                                                               # empty=absent)
    st.sampled_from([7, ["x"], True]),                         # invalid
)

_VALID_IMAGE = st.binary(min_size=1, max_size=32).map(
    lambda data: base64.b64encode(data).decode("ascii"))
_IMAGES = st.one_of(
    st.just(_OMIT),
    _VALID_IMAGE,                                              # valid
    st.sampled_from(["%%%not-base64%%%", "", 42, True]),       # invalid
)

_OPT_MS = st.one_of(st.none(), st.integers(min_value=0, max_value=10 ** 6))
_OPT_COUNT = st.one_of(st.none(), st.integers(min_value=0, max_value=10 ** 5))

_BREAKDOWNS = st.builds(
    GenerationPhaseBreakdown,
    queueing_ms=_OPT_MS,
    prefill_ms=_OPT_MS,
    decode_ms=_OPT_MS,
    prompt_tokens=_OPT_COUNT,
    output_tokens=_OPT_COUNT,
    image_tokens=_OPT_COUNT,
    image_tokens_applicable=st.booleans(),
    truncated=st.one_of(st.none(), st.booleans()),
    prefill_includes_queueing=st.booleans(),
)


@st.composite
def _cases(draw):
    body = {}
    for field, strategy in (
        ("prompt", _PROMPTS),
        ("max_tokens", _MAX_TOKENS),
        ("temperature", _TEMPERATURES),
        ("top_p", _TOP_PS),
        ("system_prompt", _SYSTEM_PROMPTS),
        ("image", _IMAGES),
    ):
        value = draw(strategy)
        if value is not _OMIT:
            body[field] = value

    return {
        "body": body,
        "text": draw(st.text(max_size=30)),
        # None = runtime reports no capability (no image_supported method)
        "image_capability": draw(st.sampled_from([None, True, False])),
        "has_breakdown_method": draw(st.booleans()),
        "breakdown": draw(st.one_of(st.none(), _BREAKDOWNS)),
    }


# ---------------------------------------------------------------------------
# Property
# ---------------------------------------------------------------------------

# Feature: vllm-workflow-latency-optimization, Property 11: API additivity
@settings(max_examples=150, deadline=None)
@given(case=_cases())
def test_property_11_api_additivity(case):
    """**Feature: vllm-workflow-latency-optimization, Property 11: API
    additivity**

    **Validates: Requirements 9.2**
    """
    _PRE_HOLDER["runtime"] = _PreFeatureRuntime(
        case["text"], case["image_capability"])
    if case["has_breakdown_method"]:
        featured = _BreakdownRuntime(
            case["text"], case["image_capability"], case["breakdown"])
    else:
        featured = _PreFeatureRuntime(
            case["text"], case["image_capability"])
    _FEAT_HOLDER["runtime"] = featured

    pre = _PRE_CLIENT.post(_URL, json=case["body"])
    feat = _FEAT_CLIENT.post(_URL, json=case["body"])

    # Identical status on every path (200, 422, ...).
    assert feat.status_code == pre.status_code, (
        pre.status_code, feat.status_code, pre.text, feat.text)

    pre_body = pre.json()
    feat_body = feat.json()

    if pre.status_code != 200:
        # Error paths (422 findings etc.) must be identical.
        assert feat_body == pre_body
        return

    # Every pre-feature field present with identical name, type, and value
    # (model_name, generated_text, and image_used per its existing
    # conditional rule — the pre-feature reference embodies that rule).
    for key, value in pre_body.items():
        assert key in feat_body, key
        assert type(feat_body[key]) is type(value), key
        assert feat_body[key] == value, key

    # The featured response differs at most by generation_metrics.
    extra_keys = set(feat_body) - set(pre_body)
    assert extra_keys <= {"generation_metrics"}, extra_keys

    metrics_expected = (case["has_breakdown_method"]
                        and case["breakdown"] is not None)
    if metrics_expected:
        assert feat_body["generation_metrics"] == \
            case["breakdown"].to_payload()
    else:
        # No breakdown captured (or pre-feature surface): byte-identical
        # pre-feature body.
        assert feat_body == pre_body
