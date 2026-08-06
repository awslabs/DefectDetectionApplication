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
Property test for Structural_Error classification in
``edge-cv-portal/backend/functions/generation_gate.py``.

Spec: .kiro/specs/portal-build-fleet-and-workflow-gates (task 4.3)

**Validates: Requirements 8.2**

Design Property 17: for any generated findings list, ``classify`` marks
a finding as a Structural_Error if and only if its severity is error and
its code belongs to the structural code set; and marks a Structural_Error
as Unrepairable if and only if it is a missing-input/output-node finding
with no catalog node type of that category, or the total Structural_Error
count exceeds the threshold. Warnings and error findings with codes
outside the structural set are never Structural_Errors, and the complete
findings list is preserved in the decision.
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


_STRUCTURAL_CODES = sorted(generation_gate.STRUCTURAL_ERROR_CODES)

#: Real validator finding codes OUTSIDE the structural set (parameter
#: violations, unresolved model refs, mqtt target, unknown node type,
#: warning codes, ...), enumerated from the real module so the test
#: exercises genuine non-structural codes, plus made-up codes.
_NON_STRUCTURAL_CODES = sorted(
    {
        value
        for name, value in vars(validator).items()
        if name.startswith("CODE_") and isinstance(value, str)
    }
    - generation_gate.STRUCTURAL_ERROR_CODES
) + ["MADE_UP_CODE", "X9_NOT_A_VALIDATOR_CODE"]

_CODES = st.one_of(
    st.sampled_from(_STRUCTURAL_CODES),
    st.sampled_from(_NON_STRUCTURAL_CODES),
)

#: Error and warning are the validator's real severities; the extra
#: values check that only exactly-"error" severity classifies.
_SEVERITIES = st.sampled_from(
    [validator.SEVERITY_ERROR, validator.SEVERITY_WARNING, "info", "ERROR"]
)


def _finding(severity, code):
    return st.fixed_dictionaries({
        "severity": st.just(severity) if isinstance(severity, str) else severity,
        "code": st.just(code) if isinstance(code, str) else code,
        "message": st.text(max_size=30),
        "nodeId": st.one_of(st.none(), st.text(min_size=1, max_size=8)),
    })


_ANY_FINDING = _finding(_SEVERITIES, _CODES)

#: A guaranteed-structural finding, used to reach the generation-collapse
#: regime (Structural_Error count > UNREPAIRABLE_ERROR_THRESHOLD).
_STRUCTURAL_FINDING = _finding(validator.SEVERITY_ERROR,
                               st.sampled_from(_STRUCTURAL_CODES))

_FINDINGS_LISTS = st.one_of(
    # Ordinary mixed findings lists.
    st.lists(_ANY_FINDING, max_size=15),
    # Collapse regime: enough structural errors to cross the threshold,
    # mixed with arbitrary other findings.
    st.tuples(
        st.lists(_STRUCTURAL_FINDING,
                 min_size=generation_gate.UNREPAIRABLE_ERROR_THRESHOLD + 1,
                 max_size=generation_gate.UNREPAIRABLE_ERROR_THRESHOLD + 6),
        st.lists(_ANY_FINDING, max_size=5),
    ).map(lambda pair: pair[0] + pair[1]),
)

#: Catalogs in wire form: category is the only field ``classify``
#: consults. May be empty, may lack input and/or output categories.
_CATALOGS = st.lists(
    st.fixed_dictionaries({
        "category": st.sampled_from(
            [CATEGORY_INPUT, CATEGORY_OUTPUT, "processing", "detection"]),
        "type": st.text(min_size=1, max_size=8),
    }),
    max_size=6,
)


# Feature: portal-build-fleet-and-workflow-gates, Property 17: Structural error classification
@settings(max_examples=200)
@given(findings=_FINDINGS_LISTS, catalog=_CATALOGS)
def test_structural_error_classification(findings, catalog):
    """For any findings list and catalog, ``classify`` marks a finding
    structural iff severity is error AND code is in the structural code
    set; marks a Structural_Error unrepairable iff it is a missing
    input/output-node finding with no catalog node type of that category
    or the Structural_Error count exceeds the threshold; and preserves
    the complete findings list in the decision."""
    decision = generation_gate.classify(findings, catalog)

    # Structural iff error severity AND code in the structural set
    # (Req 8.2). Expected set computed independently of the gate.
    expected_structural = [
        finding for finding in findings
        if finding["severity"] == validator.SEVERITY_ERROR
        and finding["code"] in generation_gate.STRUCTURAL_ERROR_CODES
    ]
    assert decision.structural_errors == expected_structural

    # Warnings and unlisted error codes are never structural (implied by
    # the iff above; asserted explicitly for clarity).
    for finding in decision.structural_errors:
        assert finding["severity"] == validator.SEVERITY_ERROR
        assert finding["code"] in generation_gate.STRUCTURAL_ERROR_CODES

    # Unrepairable iff missing-input/output-node with no catalog node
    # type of that category, or count exceeds the threshold (design
    # Property 17, unrepairability rules).
    catalog_categories = {descriptor["category"] for descriptor in catalog}
    if len(expected_structural) > generation_gate.UNREPAIRABLE_ERROR_THRESHOLD:
        expected_unrepairable = list(expected_structural)
    else:
        expected_unrepairable = [
            finding for finding in expected_structural
            if (finding["code"] == validator.CODE_V1_NO_INPUT_NODE
                and CATEGORY_INPUT not in catalog_categories)
            or (finding["code"] == validator.CODE_V1_NO_OUTPUT_NODE
                and CATEGORY_OUTPUT not in catalog_categories)
        ]
    assert decision.unrepairable_errors == expected_unrepairable

    # The complete findings list is preserved in the decision, in order.
    assert decision.all_findings == findings
