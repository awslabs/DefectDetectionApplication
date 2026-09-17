"""Property test for Tunable_Node eligibility (spec task 1.4).

**Feature: quality-prompt-tuning, Property 1: Tunable classification is
one function used everywhere** — **Validates: Requirements 1.5**

*For any* node type and any ``anomaly_mode`` value (absent, null, true,
false, truthy/falsy strings), ``workflow_core.anomaly_invocation.
is_tunable_node``, the Portal frontend's ``isTunableNode`` and the
executor's anomaly-mode decision agree, returning true exactly for
``bedrock_inference`` with ``anomaly_mode`` absent/null/true and for
``llm_inference`` with ``anomaly_mode`` true.

Scope of the half asserted here
-------------------------------

This is the **shared-module half**: it pins
``is_tunable_node`` / ``is_anomaly_mode`` against

1. an **independent restatement** of Requirement 1.5 and of the
   executor's decision — transcribed in this file from
   ``src/backend/workflow_engine/output_bindings.py``
   (``_coerce`` plus ``BedrockInferenceProcessor._run_one``'s
   ``True if coerced is None else bool(coerced)`` and
   ``LlmInferenceProcessor._run_one``'s ``bool(_coerce(...))``), never
   imported from the module under test, and
2. the node catalog's ``anomaly_mode`` defaults (``True`` for
   ``bedrock_inference``, ``False`` for ``llm_inference``), which is
   what makes "absent" well defined.

The **Portal frontend half** is task 8.5
(``eligibility.property.test.ts``). Both halves assert the *same*
cases: this file publishes the enumerated case table as the fixture
``tests/fixtures/anomaly_tuning_eligibility_cases.json``, which the
frontend test consumes. Regenerate the fixture after an intentional
table change with::

    DDA_WRITE_ELIGIBILITY_FIXTURE=1 \\
      python -m pytest tests/test_property_anomaly_invocation_eligibility.py

The *executor's* agreement on real bindings (not just the rule) is
covered by task 1.1's preservation baseline and task 1.5's refactor,
which route the executor through this very function.

Harness: pure values only — no AWS, no moto, no boto3, no device.
"""
from __future__ import annotations

import json
import os
import sys

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

# The workflow_core layer is on sys.path via tests/conftest.py; repeated
# here so the file also runs standalone.
_HERE = os.path.dirname(os.path.abspath(__file__))
_WORKFLOW_CORE_LAYER = os.path.abspath(
    os.path.join(_HERE, "..", "layers", "workflow_core", "python"))
if _WORKFLOW_CORE_LAYER not in sys.path:
    sys.path.append(_WORKFLOW_CORE_LAYER)

from workflow_core import anomaly_invocation as ai  # noqa: E402
from workflow_core.catalog import NODE_CATALOG  # noqa: E402

FIXTURE_PATH = os.path.join(
    _HERE, "fixtures", "anomaly_tuning_eligibility_cases.json")

#: Sentinel for "the ``anomaly_mode`` parameter is not present at all".
#: Distinct from ``None`` in the *definition* (a stored ``null``), which
#: the rule happens to treat the same way — a distinction the fixture
#: keeps explicit so the frontend half exercises both.
ABSENT = object()


# ---------------------------------------------------------------------------
# Reference restatement (Requirement 1.5 + the executor's decision).
# ---------------------------------------------------------------------------

#: The two Inspection_Node types (glossary).
BEDROCK = "bedrock_inference"
LLM = "llm_inference"


def ref_coerce(value):
    """``output_bindings._coerce``, restated.

    ``'true'``/``'false'`` (case-insensitive, surrounding whitespace
    ignored) become booleans; a numeric string becomes ``int``/``float``;
    anything else is returned unchanged.
    """
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered == "true":
            return True
        if lowered == "false":
            return False
        try:
            return float(value) if "." in value else int(value)
        except ValueError:
            return value
    return value


def ref_is_tunable(node_type, anomaly_mode):
    """Requirement 1.5, restated over the executor's decision.

    ``anomaly_mode`` is the raw parameter value, or :data:`ABSENT` when
    the parameter is not present (which the executor reads as ``None``
    through ``parameters.get("anomaly_mode")``).
    """
    raw = None if anomaly_mode is ABSENT else anomaly_mode
    coerced = ref_coerce(raw)
    if node_type == BEDROCK:
        # Executor: `True if anomaly_mode is None else bool(anomaly_mode)`
        # — absent/null defaults to Anomaly_Mode.
        return True if coerced is None else bool(coerced)
    if node_type == LLM:
        # Executor: `bool(_coerce(...))` — truthy only.
        return bool(coerced)
    # Every other node type is not an Inspection_Node, so never tunable.
    return False


# ---------------------------------------------------------------------------
# The enumerated case table — published as the shared fixture.
# ---------------------------------------------------------------------------

#: Node types the table crosses: both Inspection_Node types, a spread of
#: non-inspection catalog types, and near-miss/degenerate type strings.
TABLE_NODE_TYPES = (
    (BEDROCK, "the Bedrock Inspection_Node"),
    (LLM, "the VLM/LLM Inspection_Node"),
    ("model_inference", "catalog node type, not an Inspection_Node"),
    ("inference_filter", "catalog node type, not an Inspection_Node"),
    ("conditional", "catalog node type, not an Inspection_Node"),
    ("custom_python", "catalog node type, not an Inspection_Node"),
    ("capture", "catalog node type, not an Inspection_Node"),
    ("mqtt_publish", "catalog node type, not an Inspection_Node"),
    ("Bedrock_Inference", "wrong case: node types are matched exactly"),
    ("bedrock_inference ", "trailing space: node types are not trimmed"),
    ("", "empty node type"),
    ("not_a_node_type", "unknown node type"),
)

#: ``anomaly_mode`` values the table crosses: ``(case id, value,
#: description)``. Every value is JSON-representable so both halves read
#: the same cases; values whose coercion differs between Python and
#: JavaScript (underscored/hex/exponent numeric strings, unicode digits,
#: arrays, objects) are deliberately kept out of the shared table and
#: exercised in the Hypothesis property below only.
TABLE_ANOMALY_MODES = (
    ("absent", ABSENT, "parameter not present"),
    ("null", None, "stored null"),
    ("true", True, "boolean true"),
    ("false", False, "boolean false"),
    ("str-true", "true", "string 'true'"),
    ("str-false", "false", "string 'false'"),
    ("str-True", "True", "string 'True'"),
    ("str-FALSE", "FALSE", "string 'FALSE'"),
    ("str-TrUe", "TrUe", "mixed-case 'TrUe'"),
    ("str-pad-true", " true ", "'true' with surrounding whitespace"),
    ("str-pad-false", "  false  ", "'false' with surrounding whitespace"),
    ("str-1", "1", "numeric string 1 (truthy number)"),
    ("str-0", "0", "numeric string 0 (falsy number)"),
    ("str-minus-1", "-1", "negative numeric string (truthy number)"),
    ("str-0.0", "0.0", "decimal string 0.0 (falsy number)"),
    ("str-1.5", "1.5", "decimal string 1.5 (truthy number)"),
    ("str-empty", "", "empty string (falsy string)"),
    ("str-space", " ", "whitespace-only string (non-numeric, truthy)"),
    ("str-yes", "yes", "non-numeric string (truthy)"),
    ("str-no", "no", "non-numeric string (truthy — NOT read as false)"),
    ("str-off", "off", "non-numeric string (truthy — NOT read as false)"),
    ("str-null", "null", "non-numeric string (truthy — NOT read as null)"),
    ("str-None", "None", "non-numeric string (truthy)"),
    ("int-0", 0, "number 0"),
    ("int-1", 1, "number 1"),
    ("int-minus-1", -1, "number -1"),
    ("float-0.0", 0.0, "number 0.0"),
    ("float-1.0", 1.0, "number 1.0"),
    ("float-0.5", 0.5, "number 0.5"),
)

#: The coercion contract both halves implement, published with the
#: fixture so the TypeScript half is written against a specification
#: rather than against Python's ``int()``/``float()`` by accident.
COERCION_CONTRACT = [
    "A node is a Tunable_Node exactly when it is an Inspection_Node "
    "(node type 'bedrock_inference' or 'llm_inference', matched exactly, "
    "no trimming or case folding) in Anomaly_Mode (Requirement 1.5).",
    "Anomaly_Mode for 'bedrock_inference': the coerced anomaly_mode "
    "value, where an absent parameter and a stored null both mean "
    "'absent' and default to Anomaly_Mode; any other value is taken for "
    "its truth.",
    "Anomaly_Mode for 'llm_inference': the coerced anomaly_mode value "
    "taken for its truth — absent, null and false all keep the freeform "
    "path.",
    "Coercion of a string value (the executor's _coerce): trim and "
    "lowercase; 'true' means boolean true, 'false' means boolean false; "
    "otherwise, if the string matches /^[+-]?[0-9]+$/ read it as an "
    "integer, else if it contains '.' and matches "
    "/^[+-]?([0-9]+\\.[0-9]*|\\.[0-9]+)$/ read it as a decimal number; "
    "otherwise keep the string unchanged. Non-string values are never "
    "coerced.",
    "Truth of a coerced value: booleans as themselves; the numbers 0, "
    "-0 and 0.0 are false and every other number is true; the empty "
    "string is false and every other string — including a "
    "whitespace-only string — is true.",
]


def _derive_cases():
    """The case table, with expectations from the reference restatement."""
    cases = []
    for node_type, type_note in TABLE_NODE_TYPES:
        for value_id, value, value_note in TABLE_ANOMALY_MODES:
            case = {
                "id": "{0}#{1}".format(node_type or "<empty>", value_id),
                "nodeType": node_type,
                "nodeTypeNote": type_note,
                "anomalyModeId": value_id,
                "anomalyModePresent": value is not ABSENT,
                "anomalyMode": None if value is ABSENT else value,
                "anomalyModeNote": value_note,
                "expectedTunable": ref_is_tunable(node_type, value),
            }
            cases.append(case)
    return cases


def _derive_fixture():
    """The full fixture document published for the frontend half."""
    return {
        "feature": "quality-prompt-tuning",
        "property": 1,
        "propertyText": (
            "Tunable classification is one function used everywhere"),
        "validates": ["1.5"],
        "generatedBy": (
            "edge-cv-portal/backend/tests/"
            "test_property_anomaly_invocation_eligibility.py"),
        "regenerateWith": (
            "DDA_WRITE_ELIGIBILITY_FIXTURE=1 python -m pytest "
            "tests/test_property_anomaly_invocation_eligibility.py"),
        "consumedBy": [
            "edge-cv-portal/backend/tests/"
            "test_property_anomaly_invocation_eligibility.py",
            "edge-cv-portal/frontend/src/pages/workflow-tuning/"
            "eligibility.property.test.ts",
        ],
        "inspectionNodeTypes": [BEDROCK, LLM],
        "coercionContract": COERCION_CONTRACT,
        "notes": [
            "anomalyModePresent false means the anomaly_mode parameter is "
            "absent from the node's parameters; anomalyMode is then null "
            "and carries no meaning.",
            "Values whose coercion differs between Python and JavaScript "
            "(underscored, hex or exponent numeric strings, unicode "
            "digits, arrays, objects) are intentionally absent from this "
            "table; they are covered by the Python property test only.",
        ],
        "cases": _derive_cases(),
    }


def _serialize(document):
    return json.dumps(document, indent=2, ensure_ascii=False) + "\n"


# ---------------------------------------------------------------------------
# Hypothesis strategies: the table's domain plus values beyond it.
# ---------------------------------------------------------------------------

CATALOG_NODE_TYPES = tuple(
    descriptor.type_id for descriptor in NODE_CATALOG)

#: Node-type strings beyond the catalog: near misses, degenerate values
#: and free text.
NODE_TYPES = st.one_of(
    st.sampled_from(CATALOG_NODE_TYPES),
    st.sampled_from([node_type for node_type, _ in TABLE_NODE_TYPES]),
    st.sampled_from([
        "BEDROCK_INFERENCE", "bedrock", "llm", " llm_inference",
        "llm_inference\n", "bedrock_inference.v2", "LLM_Inference",
    ]),
    st.text(max_size=24),
    st.none(),
)

#: ``anomaly_mode`` values: the table's domain, plus shapes the table
#: deliberately excludes (language-specific numeric strings, containers)
#: and free text/numbers.
ANOMALY_MODES = st.one_of(
    st.sampled_from([value for _, value, _ in TABLE_ANOMALY_MODES]),
    st.just(ABSENT),
    st.none(),
    st.booleans(),
    st.integers(min_value=-5, max_value=5),
    st.floats(allow_nan=True, allow_infinity=True),
    st.sampled_from([
        "1_000", "0x0", "1e3", "0e0", "Infinity", "nan", "inf",
        "\u0663", "true\n", "\tFALSE\t", "+1", "-0.0", ".5", "00",
        "TRUE FALSE", "y", "n", "[]", "{}",
    ]),
    st.text(max_size=16),
    st.lists(st.integers(), max_size=2),
    st.dictionaries(st.text(max_size=3), st.integers(), max_size=2),
)

#: Values on which the rule's *decisions* turn — the shapes Property 1
#: names. Every generated example is checked against these too (crossed
#: with both Inspection_Node types and with the drawn node type), so a
#: regression in the coercion or in the per-type default fails on the
#: first example instead of waiting for a lucky draw.
SENSITIVE_VALUES = (
    ABSENT, None, True, False,
    "true", "false", "True", "FALSE", " true ", "  false  ",
    "0", "1", "0.0", "1.5", "", " ", "no",
    0, 1, 0.0,
)

#: Node types every example is checked against, whatever it drew.
SENSITIVE_NODE_TYPES = (BEDROCK, LLM, "model_inference", "", None)


# ---------------------------------------------------------------------------
# Property 1
# ---------------------------------------------------------------------------

def _check_one(node_type, anomaly_mode):
    """Assert the property for one (node type, ``anomaly_mode``) pair."""
    # How each caller reads the value: the Portal/frontend from a
    # parameters mapping (absent key), the executor from
    # `parameters.get("anomaly_mode")` (None). Both forms must agree.
    parameters = {}
    if anomaly_mode is not ABSENT:
        parameters["anomaly_mode"] = anomaly_mode
    frozen_parameters = repr(sorted(parameters.items(), key=repr))
    raw = parameters.get("anomaly_mode")

    expected = ref_is_tunable(node_type, anomaly_mode)

    tunable = ai.is_tunable_node(node_type, raw)
    assert tunable is expected, (
        "is_tunable_node({0!r}, {1!r}) = {2!r}, restatement says {3!r}"
        .format(node_type, raw, tunable, expected))

    # The executor-facing name is the same decision, not a second rule.
    assert ai.is_anomaly_mode(node_type, raw) is tunable

    # Total and boolean-valued: never None, never an exception, for any
    # value shape at all.
    assert isinstance(tunable, bool)

    # Only Inspection_Nodes can ever be tunable (Requirement 1.5's
    # "SHALL classify every other node as not tunable").
    if node_type not in (BEDROCK, LLM):
        assert tunable is False

    # Coercion is the executor's: the string form of a value classifies
    # exactly like the value it coerces to.
    assert ai.is_tunable_node(node_type, ref_coerce(raw)) is tunable

    # Pure and deterministic: no state, no mutation of the caller's
    # parameters mapping.
    assert ai.is_tunable_node(node_type, raw) is tunable
    assert repr(sorted(parameters.items(), key=repr)) == frozen_parameters


@given(node_type=NODE_TYPES, anomaly_mode=ANOMALY_MODES)
@settings(max_examples=100, deadline=None)
def test_property_tunable_classification_is_one_function(
        node_type, anomaly_mode):
    """**Feature: quality-prompt-tuning, Property 1: Tunable
    classification is one function used everywhere.**

    For any node type and any ``anomaly_mode`` value (absent, null,
    true, false, truthy/falsy strings), the shared module's
    ``is_tunable_node``, its executor-facing name ``is_anomaly_mode``
    and the independently restated executor decision agree, returning
    true exactly for ``bedrock_inference`` with ``anomaly_mode``
    absent/null/true and for ``llm_inference`` with ``anomaly_mode``
    true.

    **Validates: Requirements 1.5**
    """
    # The drawn pair, then the drawn value against every sensitive node
    # type and the drawn node type against every sensitive value, so each
    # example covers the whole decision surface and not one point of it.
    _check_one(node_type, anomaly_mode)
    for other_type in SENSITIVE_NODE_TYPES:
        _check_one(other_type, anomaly_mode)
    for other_value in SENSITIVE_VALUES:
        _check_one(node_type, other_value)
        _check_one(BEDROCK, other_value)
        _check_one(LLM, other_value)

    # The two Inspection_Node types differ only on absent/null/false, and
    # only in the documented direction (Bedrock defaults on, VLM off).
    assert ai.is_tunable_node(BEDROCK, None) is True
    assert ai.is_tunable_node(LLM, None) is False


# ---------------------------------------------------------------------------
# The shared fixture: published, matched by the module, and covering.
# ---------------------------------------------------------------------------

def test_published_fixture_matches_the_case_table():
    """The fixture on disk is exactly the derived case table.

    Task 8.5's frontend half asserts the same cases, so the table and
    the file may not drift apart.
    """
    document = _derive_fixture()
    serialized = _serialize(document)

    if os.environ.get("DDA_WRITE_ELIGIBILITY_FIXTURE") == "1":
        with open(FIXTURE_PATH, "w", encoding="utf-8") as handle:
            handle.write(serialized)

    assert os.path.exists(FIXTURE_PATH), (
        "missing shared fixture {0}; regenerate with "
        "DDA_WRITE_ELIGIBILITY_FIXTURE=1".format(FIXTURE_PATH))
    with open(FIXTURE_PATH, "r", encoding="utf-8") as handle:
        on_disk = handle.read()
    assert on_disk == serialized, (
        "the published eligibility fixture differs from the case table; "
        "regenerate it with DDA_WRITE_ELIGIBILITY_FIXTURE=1 and review "
        "the diff with the frontend half (task 8.5)")


def _load_fixture():
    with open(FIXTURE_PATH, "r", encoding="utf-8") as handle:
        return json.load(handle)


def test_fixture_cases_agree_with_the_shared_module():
    """Every published case's expectation is what the module returns."""
    document = _load_fixture()
    disagreements = []
    for case in document["cases"]:
        raw = case["anomalyMode"] if case["anomalyModePresent"] else None
        actual = ai.is_tunable_node(case["nodeType"], raw)
        if actual is not case["expectedTunable"]:
            disagreements.append(
                (case["id"], case["expectedTunable"], actual))
    assert not disagreements, (
        "module disagrees with the published cases: {0!r}"
        .format(disagreements))


def test_fixture_covers_the_shapes_the_property_names():
    """The shared table covers the value shapes Property 1 enumerates."""
    document = _load_fixture()
    cases = document["cases"]
    assert len(cases) == len(TABLE_NODE_TYPES) * len(TABLE_ANOMALY_MODES)
    assert len({case["id"] for case in cases}) == len(cases)

    by_type = {}
    for case in cases:
        by_type.setdefault(case["nodeType"], []).append(case)
    assert BEDROCK in by_type and LLM in by_type
    # Non-inspection node types are represented, and never tunable.
    others = [node_type for node_type in by_type
              if node_type not in (BEDROCK, LLM)]
    assert len(others) >= 5
    for node_type in others:
        assert all(case["expectedTunable"] is False
                   for case in by_type[node_type])

    def ids_of(node_type, expected):
        return {case["anomalyModeId"] for case in by_type[node_type]
                if case["expectedTunable"] is expected}

    # Requirement 1.5's exact rule, read off the published table.
    assert {"absent", "null", "true", "str-true", "str-True", "str-TrUe",
            "str-pad-true"} <= ids_of(BEDROCK, True)
    assert {"false", "str-false", "str-FALSE", "str-pad-false", "str-0",
            "str-0.0", "str-empty", "int-0", "float-0.0"} <= ids_of(
                BEDROCK, False)
    assert {"true", "str-true", "str-1", "str-space", "str-yes", "int-1",
            "float-0.5"} <= ids_of(LLM, True)
    assert {"absent", "null", "false", "str-false", "str-0", "str-empty",
            "int-0"} <= ids_of(LLM, False)

    # Both halves need the coercion contract to implement the rule.
    assert document["coercionContract"] == COERCION_CONTRACT
    assert document["inspectionNodeTypes"] == [BEDROCK, LLM]


# ---------------------------------------------------------------------------
# The catalog is the third statement of the same rule.
# ---------------------------------------------------------------------------

def _catalog_anomaly_mode_default(node_type):
    for descriptor in NODE_CATALOG:
        if descriptor.type_id == node_type:
            for parameter in descriptor.parameters:
                if parameter.name == "anomaly_mode":
                    return parameter.default
            pytest.fail(
                "{0} has no anomaly_mode parameter".format(node_type))
    pytest.fail("{0} is not a catalog node type".format(node_type))


def test_absent_matches_the_catalog_default_and_only_inspection_nodes():
    """"Absent" means the catalog default, and no other type is tunable."""
    assert _catalog_anomaly_mode_default(BEDROCK) is True
    assert _catalog_anomaly_mode_default(LLM) is False
    for node_type in (BEDROCK, LLM):
        assert ai.is_tunable_node(node_type, None) is (
            _catalog_anomaly_mode_default(node_type))

    # No other catalog node type declares anomaly_mode, and none is
    # tunable for any value in the table.
    for descriptor in NODE_CATALOG:
        if descriptor.type_id in (BEDROCK, LLM):
            continue
        assert not any(parameter.name == "anomaly_mode"
                       for parameter in descriptor.parameters)
        for _, value, _ in TABLE_ANOMALY_MODES:
            raw = None if value is ABSENT else value
            assert ai.is_tunable_node(descriptor.type_id, raw) is False
