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
"""Property-based tests for the vLLM load-failure reason extraction.

Feature: vllm-sizing-and-packaging-errors, Property 6: Load-failure reason
extraction.

For any HTTP error body, ``extract_load_failure_reason`` returns the ``error``
field's text when the body is a JSON object with a non-empty ``error``, and
the raw body text (stripped) otherwise; the resulting prominent ERROR line
emitted by ``log_load_failure`` always contains the model name, the HTTP
status, and that reason.

**Validates: Requirements 4.1, 4.3**

Log capture: ``log_load_failure`` logs through the root logger. The repo
conftest rebinds pytest's function-scoped ``caplog`` onto ``request.cls``,
which Hypothesis's function-scoped-fixture health check rejects inside
``@given`` tests (one fixture instance would span every generated example).
A per-example root-logger handler avoids both issues.
"""
import json
import logging
from contextlib import contextmanager

from hypothesis import given, settings
from hypothesis import strategies as st

import dda_triton.vllm_model_prep as mp

# ---------------------------------------------------------------------------
# Log capture (per-example, no fixtures)
# ---------------------------------------------------------------------------


class _RecordCollector(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append(record)


@contextmanager
def _captured_root_records():
    root = logging.getLogger()
    handler = _RecordCollector()
    old_level = root.level
    root.setLevel(logging.DEBUG)
    root.addHandler(handler)
    try:
        yield handler.records
    finally:
        root.removeHandler(handler)
        root.setLevel(old_level)


# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------

# Non-empty (truthy) values for the Triton ``error`` field. Triton itself
# emits strings; non-string truthy JSON values are included because the
# extractor promises ``str(error)`` for any non-empty ``error``.
_error_values = st.one_of(
    st.text(min_size=1),
    st.integers(min_value=1, max_value=10**9),
    st.lists(st.text(max_size=8), min_size=1, max_size=3),
)

# Extra JSON-object keys alongside ``error`` (never named ``error``).
_extra_keys = st.dictionaries(
    st.text(min_size=1, max_size=8).filter(lambda k: k != "error"),
    st.one_of(st.text(max_size=8), st.integers(), st.booleans(), st.none()),
    max_size=3,
)


@st.composite
def _json_error_bodies(draw):
    """A JSON-object body carrying a non-empty ``error`` field; returns
    ``(body_text, expected_reason)``."""
    error = draw(_error_values)
    obj = draw(_extra_keys)
    obj["error"] = error
    # Round-trip through JSON so the expectation is computed on the value the
    # extractor actually sees after json.loads (e.g. tuples never occur).
    body = json.dumps(obj)
    return body, str(json.loads(body)["error"])


def _is_json_object_with_truthy_error(text):
    try:
        parsed = json.loads(text)
    except ValueError:
        return False
    return isinstance(parsed, dict) and bool(parsed.get("error"))


# Bodies where the extractor must fall back to the raw (stripped) text:
# non-JSON plain text, JSON non-objects, and JSON objects whose ``error`` is
# absent or empty/falsy.
_fallback_bodies = st.one_of(
    # Arbitrary plain text (filtered so it never parses to a truthy-error
    # object — e.g. Hypothesis will not accidentally emit '{"error": "x"}').
    st.text(max_size=64).filter(
        lambda t: not _is_json_object_with_truthy_error(t)
    ),
    # JSON non-objects.
    st.one_of(
        st.lists(st.text(max_size=8), max_size=3),
        st.integers(),
        st.floats(allow_nan=False, allow_infinity=False),
        st.text(max_size=16),
        st.booleans(),
        st.none(),
    ).map(json.dumps),
    # JSON objects without an ``error`` key.
    st.dictionaries(
        st.text(min_size=1, max_size=8).filter(lambda k: k != "error"),
        st.one_of(st.text(max_size=8), st.integers()),
        max_size=3,
    ).map(json.dumps),
    # JSON objects with an empty/falsy ``error``.
    st.sampled_from(["", 0, None, [], {}, False]).map(
        lambda v: json.dumps({"error": v})
    ),
)

_any_bodies = st.one_of(
    _json_error_bodies().map(lambda pair: pair[0]), _fallback_bodies
)

_model_names = st.text(min_size=1, max_size=32)
_error_statuses = st.integers(min_value=201, max_value=599).filter(
    lambda c: c != 200
)


# ---------------------------------------------------------------------------
# Property: reason extraction
# ---------------------------------------------------------------------------


# Feature: vllm-sizing-and-packaging-errors, Property 6: Load-failure reason extraction
# **Validates: Requirements 4.1, 4.3**
@settings(max_examples=100, deadline=None)
@given(body_and_expected=_json_error_bodies())
def test_json_object_with_nonempty_error_yields_error_text(body_and_expected):
    """For any JSON-object body with a non-empty ``error`` field, the
    extracted reason is exactly ``str(error)``."""
    body, expected = body_and_expected
    assert mp.extract_load_failure_reason(body) == expected


# Feature: vllm-sizing-and-packaging-errors, Property 6: Load-failure reason extraction
# **Validates: Requirements 4.1, 4.3**
@settings(max_examples=100, deadline=None)
@given(body=_fallback_bodies)
def test_unparseable_or_errorless_body_yields_stripped_raw_text(body):
    """For any body that is not a JSON object with a non-empty ``error``
    (non-JSON text, JSON non-objects, missing or empty ``error``), the
    extracted reason is the raw body text, stripped."""
    assert mp.extract_load_failure_reason(body) == body.strip()


# ---------------------------------------------------------------------------
# Property: the prominent ERROR line
# ---------------------------------------------------------------------------


# Feature: vllm-sizing-and-packaging-errors, Property 6: Load-failure reason extraction
# **Validates: Requirements 4.1, 4.3**
@settings(max_examples=100, deadline=None)
@given(
    model_name=_model_names,
    status_code=_error_statuses,
    body=_any_bodies,
)
def test_error_line_always_carries_model_status_and_reason(
    model_name, status_code, body
):
    """For any model name, non-200 status, and body, ``log_load_failure``
    emits exactly one ERROR record whose line contains the model name, the
    HTTP status, and the extracted reason."""
    with _captured_root_records() as records:
        mp.log_load_failure(model_name, status_code, body)
    errors = [r for r in records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    line = errors[0].getMessage()
    assert "model '{}'".format(model_name) in line
    assert "HTTP {}".format(status_code) in line
    assert mp.extract_load_failure_reason(body) in line
