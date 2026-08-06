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
Property test for Repair_Pass outcome shaping in
``edge-cv-portal/backend/functions/workflow_generator.py``.

Spec: .kiro/specs/portal-build-fleet-and-workflow-gates (task 5.4)

**Validates: Requirements 8.6, 8.7**

For any repair pass over original Structural_Errors:

- a structurally clean repaired result is returned as a 200 carrying the
  REPAIRED definition, its complete findings list, ``gate.repaired ==
  true``, and ``gate.corrected_errors`` == the ORIGINAL Structural_Errors
  (Req 8.6);
- a repair that fails to complete (invocation error, unparseable output,
  or a validator failure on the repaired result) rejects with a 422
  GENERATION_REJECTED listing the ORIGINAL Structural_Errors (Req 8.7);
- a repaired result still containing Structural_Errors (whether still
  repairable or collapsed past the unrepairability threshold) rejects
  with a 422 GENERATION_REJECTED listing the REMAINING Structural_Errors
  from the repaired result (Req 8.7).

The generation flow is exercised through the real ``generate_workflow``
handler with a scripted stub Bedrock client injected through
``get_bedrock_client`` and a scripted Workflow_Validator (the pattern of
``test_repair_pass_invocation_count.py``); the real Generation_Gate
classification and ``user_readable_errors`` rendering run untouched.
Original and remaining errors carry distinct codes AND messages so the
assertions can tell which set a rejection lists.
"""
import json
import os
import sys

from botocore.exceptions import ClientError
from hypothesis import given, settings
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# Environment + sys.path so the generator Lambda bundle imports cleanly.
# boto3 resources are created at module import time (no AWS calls are made:
# every I/O collaborator is stubbed below), so a region and fake creds
# suffice. The workflow_core layer path is APPENDED, not prepended
# (mirroring the layer's own tests): python/ also carries vendored
# Lambda-runtime dependencies that must not shadow host packages.
# ---------------------------------------------------------------------------
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.normpath(os.path.join(_HERE, "..", "..", ".."))
_BACKEND = os.path.join(_REPO_ROOT, "edge-cv-portal", "backend")
_FUNCTIONS_DIR = os.path.join(_BACKEND, "functions")
_SHARED_LAYER = os.path.join(_BACKEND, "layers", "shared", "python")
_WORKFLOW_CORE_DIR = os.path.join(_BACKEND, "layers", "workflow_core", "python")
for _path in (_SHARED_LAYER, _FUNCTIONS_DIR):
    if _path not in sys.path:
        sys.path.insert(0, _path)
if _WORKFLOW_CORE_DIR not in sys.path:
    sys.path.append(_WORKFLOW_CORE_DIR)

# Fresh real modules (some standalone suites install a fake shared_utils).
for _module in ("workflow_generator", "workflow_validation", "shared_utils",
                "bedrock_common", "code_assist", "node_catalog_resolution",
                "generation_gate"):
    sys.modules.pop(_module, None)

import workflow_generator  # noqa: E402
import generation_gate  # noqa: E402
import workflow_core.validator as validator  # noqa: E402
from workflow_core.catalog import NODE_CATALOG  # noqa: E402
from workflow_core.serializer import (  # noqa: E402
    parse as parse_definition,
    serialize as serialize_graph,
)


# ---------------------------------------------------------------------------
# Scripted collaborators. The per-example script drives what the first and
# repair invocations return and what the validator reports for each result;
# the real gate classification and response shaping are what the property
# asserts. When a script runs out the stubs answer benignly so a violation
# surfaces as a clean assertion, not an IndexError.
# ---------------------------------------------------------------------------

# First-pass definition (parses; the validator is scripted, so catalog
# conformance is irrelevant — only parseability matters).
_ORIGINAL_DEFINITION = {
    "schemaVersion": 1,
    "nodes": [
        {"id": "n1", "type": "csi_camera_source",
         "position": {"x": 100, "y": 100}, "parameters": {}},
        {"id": "n2", "type": "capture",
         "position": {"x": 350, "y": 100},
         "parameters": {"output_path": "/data/original"}},
    ],
    "connections": [
        {"id": "c1",
         "from": {"node": "n1", "port": "out"},
         "to": {"node": "n2", "port": "in"}},
    ],
}

# Repair-pass definition: distinguishable from the original (different
# parameter value) so the 200 response provably carries the REPAIRED one.
_REPAIRED_DEFINITION = {
    "schemaVersion": 1,
    "nodes": [
        {"id": "n1", "type": "csi_camera_source",
         "position": {"x": 100, "y": 100}, "parameters": {}},
        {"id": "n2", "type": "capture",
         "position": {"x": 350, "y": 100},
         "parameters": {"output_path": "/data/repaired"}},
    ],
    "connections": [
        {"id": "c1",
         "from": {"node": "n1", "port": "out"},
         "to": {"node": "n2", "port": "in"}},
    ],
}

# The canonical wire form generate_workflow returns for the repaired
# definition (serializer round-trip, computed once).
_REPAIRED_CANONICAL = json.loads(serialize_graph(
    parse_definition(json.dumps(_REPAIRED_DEFINITION)).graph))

# Fails Workflow_Serializer parsing.
_BROKEN_DEFINITION = {"schemaVersion": 1, "nodes": "not-a-list"}


def _tool_response(tool_input):
    """A Converse API response calling create_workflow with tool_input."""
    return {
        "output": {"message": {"role": "assistant", "content": [
            {"toolUse": {"toolUseId": "tool-1",
                         "name": workflow_generator.TOOL_NAME,
                         "input": tool_input}},
        ]}},
        "stopReason": "tool_use",
    }


def _original_errors(count):
    """`count` ORIGINAL Structural_Errors in wire form: cycle findings
    (a Structural_Error under any catalog, never unrepairable by rule 1)
    with messages unique to the first pass."""
    return [
        {"severity": validator.SEVERITY_ERROR,
         "code": validator.CODE_V3_CYCLE,
         "message": f"original cycle {i}"}
        for i in range(count)
    ]


def _remaining_errors(count):
    """`count` REMAINING Structural_Errors in wire form: unreachable-node
    findings (distinct code AND messages from the originals) reported on
    the repaired result."""
    return [
        {"severity": validator.SEVERITY_ERROR,
         "code": validator.CODE_V5_UNREACHABLE_NODE,
         "message": f"remaining unreachable {i}"}
        for i in range(count)
    ]


def _warning_findings(count):
    """`count` warning-severity findings: never Structural_Errors, so a
    clean repaired result may still carry them in its complete findings
    list (Req 8.3/8.6)."""
    return [
        {"severity": validator.SEVERITY_WARNING,
         "code": "V6_DEFAULT_PARAM",
         "message": f"warning {i}"}
        for i in range(count)
    ]


class _Script:
    """Per-example behavior scripts."""

    def __init__(self):
        self.converse_behaviors = []   # "error" | tool_input dict
        self.validator_behaviors = []  # "raise" | list of findings


_SCRIPT = _Script()


class _ScriptedBedrockClient:
    """Stub bedrock-runtime client answering per the example's script."""

    def converse(self, **kwargs):
        behavior = (_SCRIPT.converse_behaviors.pop(0)
                    if _SCRIPT.converse_behaviors else _REPAIRED_DEFINITION)
        if behavior == "error":
            raise ClientError(
                {"Error": {"Code": "ThrottlingException",
                           "Message": "Rate exceeded"}},
                "Converse")
        return _tool_response(behavior)


_CLIENT = _ScriptedBedrockClient()


def _scripted_validator(graph, catalog=None):
    behavior = (_SCRIPT.validator_behaviors.pop(0)
                if _SCRIPT.validator_behaviors else [])
    if behavior == "raise":
        raise RuntimeError("validator cannot complete")
    return behavior


# Stub every I/O collaborator in the generation flow; the control flow
# under test (invoke -> parse -> validate -> gate -> repair -> shape) and
# the real Generation_Gate classification/rendering stay untouched.
workflow_generator.get_bedrock_client = lambda region, timeout: _CLIENT
workflow_generator.get_bedrock_configuration = (
    lambda: dict(workflow_generator.DEFAULT_BEDROCK_CONFIG))
workflow_generator.has_workflow_permission = (
    lambda user, usecase_id, permission: True)
workflow_generator.get_usecase = lambda usecase_id: {"usecase_id": usecase_id}
workflow_generator.palette_catalog_for_usecase = (
    lambda usecase_id: (NODE_CATALOG, []))
workflow_generator.run_validator = _scripted_validator
workflow_generator.put_snapshot = lambda key, body: None
workflow_generator.save_session = lambda session: None


# ---------------------------------------------------------------------------
# Repair outcome shape generators
# ---------------------------------------------------------------------------

# How the single Repair_Pass turns out. The first pass always yields a
# 'repair' gate decision (1..threshold repairable Structural_Errors).
_REPAIR_SHAPES = st.sampled_from([
    "clean",               # repaired result structurally clean -> 200 (8.6)
    "invocation_error",    # repair invocation fails -> original errors (8.7)
    "unparseable",         # repair output unparseable -> original errors (8.7)
    "validator_raises",    # repaired result not validatable -> original (8.7)
    "still_repairable",    # errors remain -> remaining errors (8.7)
    "still_unrepairable",  # collapse on repaired result -> remaining (8.7)
])

_REPAIRABLE_COUNTS = st.integers(
    min_value=1, max_value=generation_gate.UNREPAIRABLE_ERROR_THRESHOLD)
_COLLAPSE_COUNTS = st.integers(
    min_value=generation_gate.UNREPAIRABLE_ERROR_THRESHOLD + 1,
    max_value=generation_gate.UNREPAIRABLE_ERROR_THRESHOLD + 5)
_WARNING_COUNTS = st.integers(min_value=0, max_value=3)


def _generate(prompt="Camera to capture"):
    """Run generate_workflow directly with a synthetic event and user."""
    event = {
        "httpMethod": "POST",
        "resource": "/workflows/generate",
        "path": "/workflows/generate",
        "body": json.dumps({"usecase_id": "uc-1", "prompt": prompt}),
    }
    user = {"user_id": "user-1", "email": "user-1@example.com",
            "username": "user-1", "role": "DataScientist"}
    return workflow_generator.generate_workflow(event, user)


def _listed_errors(body):
    """(code, message) pairs of the rejection's structural error list."""
    listed = body["error"]["details"]["structural_errors"]
    return [(e.get("code"), e.get("message")) for e in listed]


def _pairs(findings):
    return [(f["code"], f["message"]) for f in findings]


# Feature: portal-build-fleet-and-workflow-gates, Property 20: Repair outcome shaping
# Validates: Requirements 8.6, 8.7
@settings(max_examples=150)
@given(
    repair_shape=_REPAIR_SHAPES,
    original_count=_REPAIRABLE_COUNTS,
    remaining_count=_REPAIRABLE_COUNTS,
    collapse_count=_COLLAPSE_COUNTS,
    warning_count=_WARNING_COUNTS,
)
def test_repair_outcome_shaping(repair_shape, original_count,
                                remaining_count, collapse_count,
                                warning_count):
    """For any repair pass over original Structural_Errors: a clean
    repaired result is a 200 with the repaired definition, its complete
    findings list, gate.repaired == true and gate.corrected_errors ==
    the ORIGINAL Structural_Errors (Req 8.6); a repair that fails to
    complete rejects listing the ORIGINAL Structural_Errors; a repaired
    result still containing Structural_Errors rejects listing the
    REMAINING errors from the repaired result (Req 8.7)."""
    original = _original_errors(original_count)
    clean_findings = _warning_findings(warning_count)

    # First pass: parseable definition whose findings force a 'repair'
    # gate decision, then the drawn repair outcome.
    converse = [_ORIGINAL_DEFINITION]
    findings_script = [list(original)]
    if repair_shape == "invocation_error":
        converse.append("error")
    elif repair_shape == "unparseable":
        converse.append(_BROKEN_DEFINITION)
    else:
        converse.append(_REPAIRED_DEFINITION)
        if repair_shape == "validator_raises":
            findings_script.append("raise")
        elif repair_shape == "clean":
            findings_script.append(list(clean_findings))
        elif repair_shape == "still_repairable":
            findings_script.append(_remaining_errors(remaining_count))
        else:  # still_unrepairable
            findings_script.append(_remaining_errors(collapse_count))

    _SCRIPT.converse_behaviors = list(converse)
    _SCRIPT.validator_behaviors = [
        list(b) if isinstance(b, list) else b for b in findings_script]

    response = _generate()
    body = json.loads(response["body"])

    if repair_shape == "clean":
        # Req 8.6: 200 with the REPAIRED definition, its complete
        # findings list, the repair indication, and the original
        # Structural_Errors as corrected_errors.
        assert response["statusCode"] == 200, body
        assert body["definition"] == _REPAIRED_CANONICAL
        assert _pairs(body["findings"]) == _pairs(clean_findings)
        gate = body["gate"]
        assert gate["passed"] is True
        assert gate["repaired"] is True
        assert _pairs(gate["corrected_errors"]) == _pairs(original)
    else:
        # Req 8.7: every non-clean repair outcome is a 422 rejection.
        assert response["statusCode"] == 422, body
        assert body["error"]["code"] == "GENERATION_REJECTED"
        assert body["error"]["details"]["repair_attempted"] is True

        if repair_shape in ("invocation_error", "unparseable",
                            "validator_raises"):
            # Repair did not complete: the ORIGINAL Structural_Errors.
            expected = original
        elif repair_shape == "still_repairable":
            # The REMAINING errors from the repaired result.
            expected = _remaining_errors(remaining_count)
        else:  # still_unrepairable
            expected = _remaining_errors(collapse_count)

        assert _listed_errors(body) == _pairs(expected), (
            f"repair_shape={repair_shape!r}: rejection lists the wrong "
            f"error set")
