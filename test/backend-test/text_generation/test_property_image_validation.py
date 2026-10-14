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
"""Property test for image validation at the Text_Generation_API boundary
(task 4.3).

**Feature: edge-vlm-image-inference, Property 6: Image validation exactness
at the API boundary**

*For any* generate request body, ``normalize_generation_request`` SHALL
return findings naming the ``image`` field if and only if the body contains
an ``image`` value that is not a string, is not valid base64, decodes to
zero bytes, or decodes to more than the configured maximum; and when the
``image`` field is absent the normalized result SHALL be identical to the
pre-feature normalization of the same body (no ``image_bytes`` key,
everything else identical). When findings exist the runtime is never
invoked.

**Validates: Requirements 3.1, 3.3, 3.4, 3.5, 6.2**

``normalize_generation_request`` is pure, so the exactness property is
tested directly; the never-invoked clause is checked at the endpoint level
with a recording fake runtime. The size cap is exercised cheaply around a
small ``TEXT_GEN_MAX_IMAGE_BYTES`` override instead of 16 MiB payloads.

Runs with the hypothesis profiles registered in this directory's conftest
(``textgen-fast`` = 25 examples locally, ``HYPOTHESIS_PROFILE=ci`` = 100).
"""
import asyncio
import base64
import json
import os

from hypothesis import given, settings
from hypothesis import strategies as st

from endpoints.text_generation import (
    GENERATION_DEFAULTS,
    generate_text,
    normalize_generation_request,
)

#: Small env-overridden image size cap so oversized payloads are cheap to
#: generate (the production default is 16 MiB; get_max_image_bytes reads
#: TEXT_GEN_MAX_IMAGE_BYTES per call, task 4.1 pattern).
_CAP = 64

#: The loaded model's max_model_len used for max_tokens validation.
_MODEL_MAX_LEN = 2048

#: Marker for "no image key in the body".
_ABSENT = object()


# ---------------------------------------------------------------------------
# Strategies (constructive: every drawn variant carries its expected
# classification, so the test never re-runs the implementation's own
# validation logic as its oracle)
# ---------------------------------------------------------------------------


@st.composite
def _image_variants(draw):
    """One image-field variant as ``(value, expect_finding, decoded)``:

    - absent (no ``image`` key)                      -> valid
    - valid base64 of 1.._CAP decoded bytes          -> valid
    - valid base64 of exactly _CAP decoded bytes     -> valid (at the cap)
    - valid base64 of _CAP+1.._CAP+64 decoded bytes  -> finding (too large)
    - empty string (decodes to zero bytes)           -> finding
    - non-string value                               -> finding
    - string that is not valid base64                -> finding
    """
    kind = draw(st.sampled_from([
        "absent", "valid", "valid_at_cap", "oversized",
        "empty", "non_string", "invalid_base64",
    ]))
    if kind == "absent":
        return _ABSENT, False, None
    if kind in ("valid", "valid_at_cap", "oversized"):
        if kind == "valid":
            size = draw(st.integers(min_value=1, max_value=_CAP))
        elif kind == "valid_at_cap":
            size = _CAP
        else:
            size = draw(st.integers(min_value=_CAP + 1, max_value=_CAP + 64))
        payload = draw(st.binary(min_size=size, max_size=size))
        encoded = base64.b64encode(payload).decode("ascii")
        if kind == "oversized":
            return encoded, True, None
        return encoded, False, payload
    if kind == "empty":
        # "" is valid base64 but decodes to zero bytes (Requirement 3.4).
        return "", True, None
    if kind == "non_string":
        value = draw(st.sampled_from(
            [None, 0, 1, -7, 3.5, True, False, [], ["a"], {}, {"k": "v"},
             b"raw-bytes"]
        ))
        return value, True, None
    # invalid_base64: a base64 prefix with a character outside the
    # alphabet appended is never decodable with validate=True.
    prefix = base64.b64encode(
        draw(st.binary(min_size=0, max_size=12))).decode("ascii")
    return prefix + "!", True, None


@st.composite
def _other_fields(draw):
    """A request body (without ``image``) plus the set of its invalid
    fields, drawn constructively so validity is known at generation time."""
    body = {}
    invalid = set()

    if draw(st.integers(min_value=0, max_value=9)) < 9:
        body["prompt"] = draw(st.text(min_size=1, max_size=24))
    else:
        # Missing or empty prompt is a finding (pre-feature behavior).
        if draw(st.booleans()):
            body["prompt"] = ""
        invalid.add("prompt")

    choice = draw(st.sampled_from(["absent", "valid", "invalid"]))
    if choice == "valid":
        body["max_tokens"] = draw(
            st.integers(min_value=1, max_value=_MODEL_MAX_LEN))
    elif choice == "invalid":
        body["max_tokens"] = draw(
            st.sampled_from([0, -1, _MODEL_MAX_LEN + 1, "many", 1.5]))
        invalid.add("max_tokens")

    choice = draw(st.sampled_from(["absent", "valid", "invalid"]))
    if choice == "valid":
        body["temperature"] = draw(st.floats(
            min_value=0.0, max_value=2.0,
            allow_nan=False, allow_infinity=False))
    elif choice == "invalid":
        body["temperature"] = draw(st.sampled_from([-0.5, 2.5, "hot"]))
        invalid.add("temperature")

    choice = draw(st.sampled_from(["absent", "valid", "invalid"]))
    if choice == "valid":
        body["top_p"] = draw(st.floats(
            min_value=0.0, max_value=1.0, exclude_min=True,
            allow_nan=False, allow_infinity=False))
    elif choice == "invalid":
        body["top_p"] = draw(st.sampled_from([0.0, -1.0, 1.5, "p"]))
        invalid.add("top_p")

    return body, invalid


def _expected_effective(model_name, body):
    """The pre-feature normalized output of a valid imageless request:
    supplied values overlaid on GENERATION_DEFAULTS for exactly the
    omitted parameters, plus model_name and prompt — and nothing else."""
    expected = dict(GENERATION_DEFAULTS)
    for key in GENERATION_DEFAULTS:
        if key in body:
            expected[key] = body[key]
    expected["model_name"] = model_name
    expected["prompt"] = body["prompt"]
    return expected


def _with_cap(fn):
    """Run ``fn`` with TEXT_GEN_MAX_IMAGE_BYTES pinned to _CAP."""
    previous = os.environ.get("TEXT_GEN_MAX_IMAGE_BYTES")
    os.environ["TEXT_GEN_MAX_IMAGE_BYTES"] = str(_CAP)
    try:
        return fn()
    finally:
        if previous is None:
            del os.environ["TEXT_GEN_MAX_IMAGE_BYTES"]
        else:
            os.environ["TEXT_GEN_MAX_IMAGE_BYTES"] = previous


# ---------------------------------------------------------------------------
# Property: normalization exactness (pure core)
# ---------------------------------------------------------------------------


@given(fields=_other_fields(), image=_image_variants())
@settings(deadline=None)
def test_image_validation_exactness(fields, image):
    """**Feature: edge-vlm-image-inference, Property 6: Image validation
    exactness at the API boundary**

    Findings name the ``image`` field iff the body's ``image`` value is
    invalid (non-string / bad base64 / zero bytes / over the cap); an
    absent ``image`` normalizes identically to the pre-feature output of
    the same body; a valid ``image`` yields ``effective["image_bytes"]``
    equal to its base64 decoding with everything else identical.

    **Validates: Requirements 3.1, 3.3, 3.4, 3.5, 6.2**
    """
    body, invalid_fields = fields
    image_value, image_invalid, decoded = image
    if image_value is not _ABSENT:
        body = dict(body)
        body["image"] = image_value
    model_name = "qwen2-vl"

    result = _with_cap(lambda: normalize_generation_request(
        model_name, body, _MODEL_MAX_LEN))

    if invalid_fields or image_invalid:
        # Invalid request: a non-empty finding list, with a finding
        # naming ``image`` exactly when the image value is invalid
        # (Requirements 3.4, 3.5) — never when it is valid or absent.
        assert isinstance(result, list) and result
        image_findings = [f for f in result if f["field"] == "image"]
        assert bool(image_findings) == image_invalid
        for finding in image_findings:
            assert isinstance(finding["reason"], str) and finding["reason"]
        return

    # Valid request: normalized effective dict.
    assert isinstance(result, dict)
    expected = _expected_effective(model_name, body)
    if image_value is _ABSENT:
        # Absent image: identical to the pre-feature normalization —
        # in particular no ``image_bytes`` key (Requirements 3.3, 6.2).
        assert result == expected
        assert "image_bytes" not in result
    else:
        # Valid image: decoded once at the boundary, everything else
        # identical to the imageless normalization (Requirement 3.1).
        expected["image_bytes"] = decoded
        assert result == expected
        assert result["image_bytes"] == base64.b64decode(
            body["image"], validate=True)


# ---------------------------------------------------------------------------
# Property: on findings the runtime is never invoked (endpoint level)
# ---------------------------------------------------------------------------


class _RecordingRuntime:
    """Fake runtime recording every generate invocation and state check."""

    def __init__(self):
        self.generate_calls = []
        self.state_calls = 0

    def engine_args(self, model_name):
        return {"max_model_len": _MODEL_MAX_LEN}

    def state(self, model_name):
        self.state_calls += 1
        return "READY"

    async def generate(self, model_name, prompt, sampling_params, **kwargs):
        self.generate_calls.append(
            (model_name, prompt, sampling_params, kwargs))
        return "generated"


@given(fields=_other_fields(), image=_image_variants())
@settings(deadline=None)
def test_findings_never_invoke_runtime(fields, image):
    """**Feature: edge-vlm-image-inference, Property 6: Image validation
    exactness at the API boundary**

    Whenever normalization produces findings — for an invalid image or
    any other invalid field — the generate endpoint returns the 422
    findings response (naming ``image`` exactly when the image is
    invalid) and the runtime's generate interface is invoked zero times.

    **Validates: Requirements 3.1, 3.3, 3.4, 3.5, 6.2**
    """
    body, invalid_fields = fields
    image_value, image_invalid, _decoded = image
    if image_value is not _ABSENT:
        body = dict(body)
        body["image"] = image_value

    runtime = _RecordingRuntime()
    response = _with_cap(lambda: asyncio.run(
        generate_text("qwen2-vl", body, runtime)))

    if not (invalid_fields or image_invalid):
        # Valid request: the runtime is invoked exactly once (sanity
        # check that the fake wiring exercises the real path).
        assert len(runtime.generate_calls) == 1
        return

    # Findings: 422 carrying the finding list, runtime never invoked.
    assert response.status_code == 422
    assert runtime.generate_calls == []
    content = json.loads(response.body)
    findings = content["findings"]
    assert isinstance(findings, list) and findings
    image_findings = [f for f in findings if f["field"] == "image"]
    assert bool(image_findings) == image_invalid
