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
"""Property test for total, faithful Trigger_Context loading (Task 6.2).

**Feature: custom-python-source, Property 1: Trigger context loading is
total and faithful**

*For any* value of ``trigger_context_json`` — a serialized JSON object,
``None``, the empty string, a non-JSON string, or serialized non-object
JSON — ``load_trigger_context`` never raises; a JSON object's entries are
reproduced in the returned Trigger_Context, and every other input yields
``{}``. When the context carries a ``payload`` string, ``payload_json``
is added holding the parsed value when the payload parses as JSON and
``None`` otherwise, with all other entries preserved.

**Validates: Requirements 2.1, 2.2, 2.3, 2.4**
"""
import json

from hypothesis import given, settings
from hypothesis import strategies as st

# Sets COMPONENT_WORK_PATH before the pipeline_executor import chain
# reaches the DAO layer (the suite-wide convention).
import workflow_engine_test_utils  # noqa: F401

from workflow_engine.pipeline_executor import load_trigger_context

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

_JSON_SCALARS = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-(10 ** 12), max_value=10 ** 12),
    st.floats(allow_nan=False, allow_infinity=False),
    st.text(max_size=30),
)

_JSON_VALUES = st.one_of(
    _JSON_SCALARS,
    st.lists(_JSON_SCALARS, max_size=4),
    st.dictionaries(st.text(max_size=10), _JSON_SCALARS, max_size=4),
)

#: JSON objects as the Trigger_Runtime would persist them. ``payload_json``
#: is excluded from generated keys: it is the loader's derived key, so the
#: faithful-reproduction assertion quantifies over every *other* entry.
_JSON_OBJECTS = st.dictionaries(
    st.text(max_size=15).filter(lambda k: k != "payload_json"),
    _JSON_VALUES,
    max_size=6,
)


def _is_not_json(text: str) -> bool:
    try:
        json.loads(text)
    except (ValueError, TypeError):
        return True
    return False


_NON_JSON_STRINGS = st.text(max_size=30).filter(_is_not_json)

_NON_OBJECT_JSON = st.one_of(
    _JSON_SCALARS, st.lists(_JSON_VALUES, max_size=4)
).map(json.dumps)

_ALL_RAW_INPUTS = st.one_of(
    st.none(),
    st.just(""),
    _NON_JSON_STRINGS,
    _NON_OBJECT_JSON,
    _JSON_OBJECTS.map(json.dumps),
)


# ---------------------------------------------------------------------------
# Sub-property 1: totality — never raises; objects reproduced, everything
# else yields {} (Requirements 2.1, 2.2)
# ---------------------------------------------------------------------------


# Feature: custom-python-source, Property 1: Trigger context loading is
# total and faithful
@settings(max_examples=100, deadline=None)
@given(raw=_ALL_RAW_INPUTS)
def test_loading_is_total_and_faithful(raw):
    """For any raw ``trigger_context_json`` value, loading never raises;
    a serialized JSON object's entries are reproduced (the derived
    ``payload_json`` being the only permitted addition), and every
    non-object input yields exactly ``{}``.

    **Validates: Requirements 2.1, 2.2**
    """
    context = load_trigger_context(raw)

    assert isinstance(context, dict)

    parsed = None
    if isinstance(raw, str) and raw:
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = None

    if isinstance(parsed, dict):
        # Every persisted entry is reproduced verbatim (Req 2.1).
        for key, value in parsed.items():
            assert context[key] == value
        # The derived payload_json is the only key the loader may add.
        assert set(context.keys()) - set(parsed.keys()) <= {"payload_json"}
    else:
        # NULL / empty / non-JSON / non-object -> {} (Req 2.2).
        assert context == {}


# ---------------------------------------------------------------------------
# Sub-property 2: payload_json derivation for payload strings
# (Requirements 2.3, 2.4)
# ---------------------------------------------------------------------------


# Feature: custom-python-source, Property 1: Trigger context loading is
# total and faithful
@settings(max_examples=100, deadline=None)
@given(
    entries=_JSON_OBJECTS,
    payload_value=_JSON_VALUES,
    parses=st.booleans(),
    non_json_payload=_NON_JSON_STRINGS,
)
def test_payload_json_derivation(entries, payload_value, parses,
                                 non_json_payload):
    """For any context carrying a ``payload`` string, the loaded context
    adds ``payload_json`` holding the parsed value when the payload
    parses as JSON and None otherwise, with all other entries preserved.

    **Validates: Requirements 2.3, 2.4**
    """
    if parses:
        payload = json.dumps(payload_value)
        expected_payload_json = json.loads(payload)
    else:
        payload = non_json_payload
        expected_payload_json = None

    persisted = dict(entries)
    persisted["payload"] = payload

    context = load_trigger_context(json.dumps(persisted))

    assert context["payload"] == payload
    assert context["payload_json"] == expected_payload_json
    # Every other entry is preserved (Req 2.3/2.4 "all other entries").
    for key, value in persisted.items():
        assert context[key] == value
    assert set(context.keys()) == set(persisted.keys()) | {"payload_json"}


# ---------------------------------------------------------------------------
# Sub-property 3: contexts without a payload string pass through unchanged
# (the OPC UA / manual shapes; Requirement 2.3 is conditional on `payload`)
# ---------------------------------------------------------------------------


# Feature: custom-python-source, Property 1: Trigger context loading is
# total and faithful
@settings(max_examples=100, deadline=None)
@given(
    entries=_JSON_OBJECTS.filter(
        lambda d: not isinstance(d.get("payload"), str)
    )
)
def test_contexts_without_payload_string_pass_through_unchanged(entries):
    """For any context without a ``payload`` string (OPC UA and manual
    shapes, or a non-string ``payload``), the loaded context equals the
    persisted object exactly — no ``payload_json`` key is added.

    **Validates: Requirements 2.1, 2.3**
    """
    context = load_trigger_context(json.dumps(entries))

    assert context == entries
    if "payload" not in entries:
        assert "payload_json" not in context
