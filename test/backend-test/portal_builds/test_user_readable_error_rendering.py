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
"""
Property test for the Generation_Gate user-readable error rendering in
``edge-cv-portal/backend/functions/generation_gate.py``.

Spec: .kiro/specs/portal-build-fleet-and-workflow-gates (task 4.5)

**Validates: Requirements 8.8**

For any Structural_Error over any Workflow_Definition — including errors
referencing nodes/connections absent from the definition, elements
without display names, and definitions that are JSON strings, malformed
JSON, or not mappings at all — ``user_readable_errors`` is total: it
never raises, returns exactly one entry per error, every entry carries
the error code and a non-empty plain-language explanation, and every
affected node/connection is resolved to its identifier plus display
name (identifier alone when no display name exists).
"""
import json
import os
import sys

from hypothesis import given, settings
from hypothesis import strategies as st

# Import the pure gate module from the portal Lambda bundle and the real
# workflow_core layer it builds on. The layer path is APPENDED, not
# prepended (mirroring the layer's own tests/conftest.py): python/ also
# carries the layer's vendored Lambda-runtime dependencies, which must
# not shadow the host interpreter's own packages.
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.normpath(os.path.join(_HERE, "..", "..", ".."))
_FUNCTIONS_DIR = os.path.join(_REPO_ROOT, "edge-cv-portal", "backend", "functions")
_WORKFLOW_CORE_DIR = os.path.join(
    _REPO_ROOT, "edge-cv-portal", "backend", "layers", "workflow_core", "python")
if _FUNCTIONS_DIR not in sys.path:
    sys.path.insert(0, _FUNCTIONS_DIR)
if _WORKFLOW_CORE_DIR not in sys.path:
    sys.path.append(_WORKFLOW_CORE_DIR)

import generation_gate  # noqa: E402


# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------

_IDS = st.text(alphabet="abcdef0123456789-_", min_size=1, max_size=8)

#: Display-name candidate values: real names, empty, whitespace-only
#: (whitespace-only and non-string values carry no usable display name).
_NAME_VALUES = st.one_of(
    st.text(min_size=1, max_size=12),
    st.just(""),
    st.just("   "),
    st.integers(),
    st.none(),
)

_STRUCTURAL_CODES = st.sampled_from(sorted(generation_gate.STRUCTURAL_ERROR_CODES))
#: Codes fed to the renderer: real structural codes plus arbitrary
#: unknown codes (the renderer must stay total and fall back to the
#: generic explanation for codes it does not know).
_CODES = st.one_of(_STRUCTURAL_CODES, st.text(min_size=0, max_size=10))


def _element(draw, element_id):
    """One definition node/connection: id plus optional display-name
    carriers (``data.label`` / ``data.displayName`` /
    ``parameters.name`` / ``parameters.label``), any of which may hold
    unusable values (empty, whitespace, non-string)."""
    element = {"id": element_id}
    if draw(st.booleans()):
        element["data"] = draw(st.fixed_dictionaries(
            {},
            optional={"label": _NAME_VALUES, "displayName": _NAME_VALUES},
        ))
    if draw(st.booleans()):
        element["parameters"] = draw(st.fixed_dictionaries(
            {},
            optional={"name": _NAME_VALUES, "label": _NAME_VALUES},
        ))
    return element


@st.composite
def _errors_and_definition(draw):
    """Structural_Errors plus a Workflow_Definition, sharing an id pool
    so errors sometimes reference elements that exist in the definition
    and sometimes reference missing ones."""
    id_pool = draw(st.lists(_IDS, min_size=1, max_size=6, unique=True))
    pool_or_fresh = st.one_of(st.sampled_from(id_pool), _IDS)

    nodes = [
        _element(draw, node_id)
        for node_id in id_pool
        if draw(st.booleans())
    ]
    connections = [
        _element(draw, connection_id)
        for connection_id in id_pool
        if draw(st.booleans())
    ]

    definition_doc = {"nodes": nodes, "connections": connections}
    # The definition reaches the renderer as a mapping, a JSON string,
    # malformed JSON, or something that is not a definition at all —
    # rendering must be total over every shape.
    definition = draw(st.one_of(
        st.just(definition_doc),
        st.just(json.dumps(definition_doc)),
        st.just("{not json"),
        st.none(),
        st.integers(),
        st.just({"nodes": "not-a-list", "connections": None}),
    ))

    errors = draw(st.lists(
        st.fixed_dictionaries(
            {
                "severity": st.just("error"),
                "code": _CODES,
                "message": st.text(max_size=30),
            },
            optional={
                "nodeId": st.one_of(pool_or_fresh, st.none()),
                "connectionId": st.one_of(pool_or_fresh, st.none()),
            },
        ),
        max_size=8,
    ))
    return errors, definition, definition_doc


# ---------------------------------------------------------------------------
# Test oracle: display-name resolution transcribed from the design
# (data.label / data.displayName, then parameters.name / parameters.label,
# non-empty strings only), applied to the pre-serialization document.
# ---------------------------------------------------------------------------

def _expected_display_name(element):
    if not isinstance(element, dict):
        return None
    data = element.get("data")
    if isinstance(data, dict):
        for key in ("label", "displayName"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value
    parameters = element.get("parameters")
    if isinstance(parameters, dict):
        for key in ("name", "label"):
            value = parameters.get(key)
            if isinstance(value, str) and value.strip():
                return value
    return None


def _elements_by_id(definition, definition_doc, key):
    """The elements the renderer can possibly resolve: the document's
    elements when the definition arrived as that mapping or its JSON
    serialization, nothing otherwise (malformed/non-mapping input)."""
    if definition is definition_doc or (
            isinstance(definition, str) and definition != "{not json"):
        return {e["id"]: e for e in definition_doc[key]}
    return {}


# Feature: portal-build-fleet-and-workflow-gates, Property 22: User-readable error rendering is total
@settings(max_examples=200)
@given(data=_errors_and_definition())
def test_user_readable_error_rendering_is_total(data):
    """For any Structural_Errors and any definition shape,
    ``user_readable_errors`` never raises, returns one entry per error
    with the error's code and a non-empty explanation, and resolves each
    affected node/connection to its id plus display name — id alone when
    the element is missing or carries no display name."""
    errors, definition, definition_doc = data

    entries = generation_gate.user_readable_errors(errors, definition)

    # Totality: exactly one entry per Structural_Error, in order.
    assert isinstance(entries, list)
    assert len(entries) == len(errors)

    nodes_by_id = _elements_by_id(definition, definition_doc, "nodes")
    connections_by_id = _elements_by_id(definition, definition_doc, "connections")

    for error, entry in zip(errors, entries):
        # The entry carries the error's code and message.
        assert entry["code"] == error["code"]
        assert entry["message"] == error["message"]

        # Non-empty plain-language explanation, always.
        explanation = entry["explanation"]
        assert isinstance(explanation, str)
        assert explanation.strip()

        # Affected list: one node item iff the error names a node, one
        # connection item iff it names a connection; empty for
        # graph-level errors.
        affected = entry["affected"]
        assert isinstance(affected, list)
        expected_kinds = []
        if error.get("nodeId"):
            expected_kinds.append(("node", error["nodeId"], nodes_by_id))
        if error.get("connectionId"):
            expected_kinds.append(
                ("connection", error["connectionId"], connections_by_id))
        assert len(affected) == len(expected_kinds)

        for item, (kind, element_id, lookup) in zip(affected, expected_kinds):
            # Every affected item identifies its element by id and kind.
            assert item["kind"] == kind
            assert item["id"] == element_id

            # Display name when the definition's element carries one;
            # id alone (no displayName key) otherwise — including when
            # the element is absent from the definition entirely.
            expected_name = _expected_display_name(lookup.get(element_id))
            if expected_name is None:
                assert "displayName" not in item
            else:
                assert item["displayName"] == expected_name
