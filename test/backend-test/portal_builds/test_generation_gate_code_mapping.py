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
Unit test for the Generation_Gate structural code mapping in
``edge-cv-portal/backend/functions/generation_gate.py``.

Spec: .kiro/specs/portal-build-fleet-and-workflow-gates (task 4.2)

**Validates: Requirements 8.2**

Requirement 8.2 names eight Structural_Error categories. This test
asserts that ``generation_gate.STRUCTURAL_ERROR_CATEGORIES`` covers
exactly those eight categories and that every mapped code is a finding
code that actually exists in the real ``workflow_core.validator``
module (enumerated here by value, so a hardcoded or stale code string
in the gate cannot silently drift from the validator).
"""
import os
import sys

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


#: The eight Structural_Error categories Requirement 8.2 names, in the
#: requirement's own order. Transcribed from requirements.md, not from
#: the implementation, so a missing or renamed category in the gate's
#: map fails this test.
REQ_8_2_CATEGORIES = frozenset({
    "incompatible_port_types",   # connections joining incompatible port types
    "backwards_edge",            # endpoints not output-port -> input-port
    "cycle",                     # cycles in the node graph
    "unreachable_node",          # nodes unreachable from any input node
    "unknown_reference",         # connections referencing nonexistent nodes/ports
    "missing_input_node",        # absence of an input-category node
    "missing_output_node",       # absence of an output-category node
    "coexistence_conflict",      # node types that cannot coexist
})


def _real_validator_codes():
    """Every finding code the real ``workflow_core.validator`` module
    defines, enumerated by value from its ``CODE_*`` string constants."""
    return {
        value
        for name, value in vars(validator).items()
        if name.startswith("CODE_") and isinstance(value, str)
    }


def test_every_req_8_2_category_maps_to_a_real_validator_code():
    """Every one of the eight Req 8.2 categories is present in the gate's
    category map, is mapped to at least one code, and every mapped code
    exists in the real ``workflow_core.validator`` finding codes."""
    real_codes = _real_validator_codes()
    assert real_codes, "no CODE_* constants found in workflow_core.validator"

    categories = generation_gate.STRUCTURAL_ERROR_CATEGORIES

    missing = REQ_8_2_CATEGORIES - set(categories)
    assert not missing, (
        "Req 8.2 categories missing from STRUCTURAL_ERROR_CATEGORIES: "
        "{0}".format(sorted(missing)))

    extra = set(categories) - REQ_8_2_CATEGORIES
    assert not extra, (
        "unexpected categories in STRUCTURAL_ERROR_CATEGORIES (not named "
        "by Req 8.2): {0}".format(sorted(extra)))

    for category in sorted(REQ_8_2_CATEGORIES):
        codes = categories[category]
        assert codes, "category '{0}' maps to no codes".format(category)
        unknown = set(codes) - real_codes
        assert not unknown, (
            "category '{0}' maps to codes absent from the real "
            "workflow_core.validator: {1}".format(category, sorted(unknown)))


def test_structural_error_codes_is_the_union_of_the_category_map():
    """``STRUCTURAL_ERROR_CODES`` (what ``classify`` consults) is exactly
    the union of the category map, so every Req 8.2 category actually
    participates in classification."""
    expected = {
        code
        for codes in generation_gate.STRUCTURAL_ERROR_CATEGORIES.values()
        for code in codes
    }
    assert generation_gate.STRUCTURAL_ERROR_CODES == frozenset(expected)
