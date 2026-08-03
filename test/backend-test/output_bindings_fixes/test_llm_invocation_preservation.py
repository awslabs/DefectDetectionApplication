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
"""Preservation property tests (Task 2) for workflow-output-bindings-fixes.

Property 5: Preservation — the llm 200-path invocation contract unchanged:
for any prompt/parameters answered 200 on the FIRST attempt,
``_default_llm_invoker`` makes exactly one POST with the original URL,
body, and timeout, and returns the same generated text.

**Validates: Requirements 3.3**

Observation-first, OBSERVED on the current (unfixed) tree by driving
``_default_llm_invoker`` with a fake ``requests`` module:

* exactly ONE ``requests.post`` per invocation;
* URL ``http://localhost:5000/text-generation/{model_name}/generate``;
* JSON body ``{'prompt': prompt}`` plus ONLY the generation parameters
  ``max_tokens``/``temperature``/``top_p`` that are present AND non-None
  in the binding parameters (any other parameter — ``modelName``,
  ``prompt_template``, strays — is never forwarded);
* ``timeout=LLM_GENERATION_TIMEOUT_SEC`` (130);
* the return value is ``str(response.json().get('generated_text', ''))``
  — a missing key yields ``''``.

The Defect B fix adds a 409-loading retry loop to this invoker; the
200-first-attempt path must stay byte-identical (a single POST, same
URL/body/timeout, same result). These tests MUST PASS today and keep
passing after the fix.

Runs with the hypothesis profiles registered in ``test/backend-test/
conftest.py`` (``fast`` = 25 examples locally, ``HYPOTHESIS_PROFILE=ci``
= 100).
"""
import sys
import types
from unittest.mock import patch

from hypothesis import given, settings
from hypothesis import strategies as st

from workflow_engine.output_bindings import (
    LLM_GENERATION_TIMEOUT_SEC,
    TEXT_GENERATION_URL,
    _default_llm_invoker,
)


# ---------------------------------------------------------------------------
# Fake HTTP boundary (the invoker imports ``requests`` lazily)
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
        assert queue, "unexpected extra POST — the response queue is empty"
        return queue.pop(0)

    module.post = post
    module.calls = posts
    return module


def _invoke(model_name, prompt, parameters, payload):
    fake = _fake_requests([_Response(200, payload)])
    with patch.dict(sys.modules, {"requests": fake}):
        result = _default_llm_invoker(model_name, prompt, dict(parameters))
    return result, fake.calls


# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------

_MODEL_NAMES = st.one_of(
    st.sampled_from(["opt125m-smoke", "llama-3-8b", "m"]),
    st.text(
        alphabet="abcdefghijklmnopqrstuvwxyz0123456789-_.",
        min_size=1, max_size=30,
    ),
)
_PROMPTS = st.one_of(
    st.sampled_from(["Describe the inspection result", ""]),
    st.text(max_size=120),
)
_GENERATED = st.one_of(
    st.sampled_from(["ok", "", "the part is defective"]),
    st.text(max_size=120),
)

#: Per generation parameter: absent, explicitly None (omitted from the
#: body — observed), or a value.
_MAX_TOKENS = st.one_of(
    st.just("ABSENT"), st.none(), st.integers(min_value=1, max_value=4096))
_TEMPERATURE = st.one_of(
    st.just("ABSENT"), st.none(),
    st.floats(min_value=0.0, max_value=2.0,
              allow_nan=False, allow_infinity=False))
_TOP_P = st.one_of(
    st.just("ABSENT"), st.none(),
    st.floats(min_value=0.0, max_value=1.0,
              allow_nan=False, allow_infinity=False))


@st.composite
def _parameters(draw):
    """Binding parameters as the compiled document carries them: the three
    generation parameters plus the compiled fields and strays the invoker
    must never forward."""
    params = {}
    for key, strategy in (
        ("max_tokens", _MAX_TOKENS),
        ("temperature", _TEMPERATURE),
        ("top_p", _TOP_P),
    ):
        value = draw(strategy)
        if value != "ABSENT":
            params[key] = value
    if draw(st.booleans()):
        params["modelName"] = "opt125m-smoke"
    if draw(st.booleans()):
        params["prompt_template"] = "Describe {x}"
    if draw(st.booleans()):
        params["stray_parameter"] = draw(st.integers())
    return params


def _expected_body(prompt, parameters):
    """The OBSERVED body construction: prompt plus only the present,
    non-None generation parameters."""
    body = {"prompt": prompt}
    for key in ("max_tokens", "temperature", "top_p"):
        if parameters.get(key) is not None:
            body[key] = parameters[key]
    return body


# ---------------------------------------------------------------------------
# Property 5: 200-first-attempt single-POST identity
# ---------------------------------------------------------------------------

class Test200PathInvocationPreserved:
    """**Property 5: Preservation — llm invocation contract.** A 200 first
    attempt costs exactly one POST with the original URL, body, and
    timeout, and returns the response's generated text.

    **Validates: Requirements 3.3**
    """

    @given(model_name=_MODEL_NAMES, prompt=_PROMPTS,
           parameters=_parameters(), generated=_GENERATED)
    @settings(deadline=None)
    def test_single_post_with_identical_url_body_timeout(
            self, model_name, prompt, parameters, generated):
        result, calls = _invoke(
            model_name, prompt, parameters, {"generated_text": generated})

        assert len(calls) == 1, (
            "PRESERVATION REGRESSION (Property 5): a 200 first attempt "
            "cost {0} POSTs instead of exactly one".format(len(calls)))
        call = calls[0]
        assert call["url"] == TEXT_GENERATION_URL.format(
            model_name=model_name), (
            "PRESERVATION REGRESSION (Property 5): URL changed: "
            "{0!r}".format(call["url"]))
        assert call["json"] == _expected_body(prompt, parameters), (
            "PRESERVATION REGRESSION (Property 5): body changed: "
            "{0!r} != {1!r}".format(
                call["json"], _expected_body(prompt, parameters)))
        assert call["timeout"] == LLM_GENERATION_TIMEOUT_SEC, (
            "PRESERVATION REGRESSION (Property 5): timeout changed: "
            "{0!r}".format(call["timeout"]))
        assert result == str(generated), (
            "PRESERVATION REGRESSION (Property 5): result changed: "
            "{0!r} != {1!r}".format(result, str(generated)))

    def test_missing_generated_text_yields_empty_string(self):
        """OBSERVED: a 200 body without ``generated_text`` returns ''."""
        result, calls = _invoke("opt125m-smoke", "p", {}, {"other": 1})
        assert len(calls) == 1
        assert result == ""

    def test_observed_example(self):
        """The exact observed baseline call (documentation anchor)."""
        result, calls = _invoke(
            "opt125m-smoke",
            "Describe the inspection result",
            {"modelName": "opt125m-smoke",
             "prompt_template": "Describe the inspection result",
             "max_tokens": 64},
            {"generated_text": "looks fine"},
        )
        assert result == "looks fine"
        assert calls == [{
            "url": "http://localhost:5000/text-generation/opt125m-smoke"
                   "/generate",
            "json": {"prompt": "Describe the inspection result",
                     "max_tokens": 64},
            "timeout": 130,
        }]
