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
Property test for the Generation_Gate decision function in
``edge-cv-portal/backend/functions/generation_gate.py``.

Spec: .kiro/specs/portal-build-fleet-and-workflow-gates (task 4.4)

**Validates: Requirements 8.3, 8.5**

The expected decision below is computed by an independent oracle
transcribed from the DESIGN (structural code set enumerated from the
real ``workflow_core.validator`` constants, the two unrepairability
rules, and the threshold value 10 written out literally), not by
consulting the implementation's classification, so a drift in the
gate's decision logic fails this test.
"""
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
import workflow_core.validator as validator  # noqa: E402
from workflow_core.catalog.models import CATEGORY_INPUT, CATEGORY_OUTPUT  # noqa: E402


# ---------------------------------------------------------------------------
# Independent oracle, transcribed from the design (not the implementation)
# ---------------------------------------------------------------------------

#: The structural code set, enumerated from the real validator constants
#: per the eight Req 8.2 categories (design section 8).
_STRUCTURAL_CODES = frozenset({
    validator.CODE_V2_INCOMPATIBLE_TYPES,   # incompatible port types
    validator.CODE_V2_SOURCE_NOT_OUTPUT,    # backwards edge (source side)
    validator.CODE_V2_TARGET_NOT_INPUT,     # backwards edge (target side)
    validator.CODE_V3_CYCLE,                # cycle
    validator.CODE_V5_UNREACHABLE_NODE,     # unreachable node
    validator.CODE_V2_UNKNOWN_NODE,         # unknown node reference
    validator.CODE_V2_UNKNOWN_PORT,         # unknown port reference
    validator.CODE_V1_NO_INPUT_NODE,        # missing input node
    validator.CODE_V1_NO_OUTPUT_NODE,       # missing output node
    validator.CODE_V7_COEXISTENCE_CONFLICT, # coexistence conflict
})

#: Design threshold (design section 8, unrepairability rule 2): more
#: Structural_Errors than this is generation collapse -> reject. Written
#: literally so a changed constant in the implementation fails the test.
_DESIGN_THRESHOLD = 10


def _oracle(findings, catalog):
    """Expected (structural_errors, unrepairable_errors, action) per the
    design decision function."""
    structural = [
        f for f in findings
        if f["severity"] == validator.SEVERITY_ERROR
        and f["code"] in _STRUCTURAL_CODES
    ]
    categories = {entry["category"] for entry in catalog}

    if len(structural) > _DESIGN_THRESHOLD:
        # Rule 2: generation collapse — every Structural_Error unrepairable.
        unrepairable = list(structural)
    else:
        # Rule 1: catalog impossibility for missing input/output node.
        unrepairable = [
            f for f in structural
            if (f["code"] == validator.CODE_V1_NO_INPUT_NODE
                and CATEGORY_INPUT not in categories)
            or (f["code"] == validator.CODE_V1_NO_OUTPUT_NODE
                and CATEGORY_OUTPUT not in categories)
        ]

    if not structural:
        action = "accept"
    elif unrepairable:
        action = "reject"
    else:
        action = "repair"
    return structural, unrepairable, action


# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------

_ALL_REAL_CODES = sorted({
    value for name, value in vars(validator).items()
    if name.startswith("CODE_") and isinstance(value, str)
})
_NON_STRUCTURAL_CODES = (
    sorted(set(_ALL_REAL_CODES) - _STRUCTURAL_CODES) + ["X_NOT_A_REAL_CODE"])

_SEVERITIES = [validator.SEVERITY_ERROR, "warning", "info"]

_MESSAGES = st.text(min_size=0, max_size=20)

# Wire-form findings (the camelCase dict shape of ValidationFinding.to_dict).
# Structural error-severity findings are generated as a dedicated branch so
# lists frequently cross the >10 collapse threshold.
_structural_error_finding = st.fixed_dictionaries({
    "severity": st.just(validator.SEVERITY_ERROR),
    "code": st.sampled_from(sorted(_STRUCTURAL_CODES)),
    "message": _MESSAGES,
})
_arbitrary_finding = st.fixed_dictionaries({
    "severity": st.sampled_from(_SEVERITIES),
    "code": st.sampled_from(_ALL_REAL_CODES + _NON_STRUCTURAL_CODES),
    "message": _MESSAGES,
})
_FINDINGS = st.lists(
    st.one_of(_structural_error_finding, _arbitrary_finding),
    min_size=0, max_size=25)

# Effective catalogs: wire-form descriptors with a category; frequently
# missing the input and/or output category so unrepairability rule 1 fires.
_CATALOGS = st.lists(
    st.fixed_dictionaries({
        "category": st.sampled_from(
            [CATEGORY_INPUT, CATEGORY_OUTPUT, "processing", "logic"]),
    }),
    min_size=0, max_size=6)


# Feature: portal-build-fleet-and-workflow-gates, Property 18: Gate decision function
@settings(max_examples=300)
@given(findings=_FINDINGS, catalog=_CATALOGS)
def test_gate_decision_function(findings, catalog):
    """For any classified findings: the decision is accept if and only if
    there are no Structural_Errors (and then the complete findings list is
    passed through unmodified); reject-without-repair if and only if at
    least one Structural_Error is Unrepairable; and repair otherwise."""
    expected_structural, expected_unrepairable, expected_action = _oracle(
        findings, catalog)

    decision = generation_gate.classify(findings, catalog)

    # The action is always exactly one of the three defined values.
    assert decision.action in ("accept", "repair", "reject")

    # accept <=> no Structural_Errors (Req 8.3).
    assert (decision.action == "accept") == (not expected_structural)

    # reject <=> at least one Unrepairable_Error — no Repair_Pass (Req 8.5).
    assert (decision.action == "reject") == bool(expected_unrepairable)

    # repair <=> Structural_Errors exist and none is unrepairable (Req 8.3).
    assert (decision.action == "repair") == (
        bool(expected_structural) and not expected_unrepairable)

    # The action matches the oracle's decision outright.
    assert decision.action == expected_action

    # The classification the decision is based on matches the oracle.
    assert decision.structural_errors == expected_structural
    assert decision.unrepairable_errors == expected_unrepairable

    # On accept, the complete findings list is passed through unmodified.
    if decision.action == "accept":
        assert decision.all_findings == findings
