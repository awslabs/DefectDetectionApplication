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
Property test for the Repair_Pass invocation count in
``edge-cv-portal/backend/functions/workflow_generator.py``.

Spec: .kiro/specs/portal-build-fleet-and-workflow-gates (task 5.3)

**Validates: Requirements 8.4, 8.5**

For any generation outcome shape, the total number of Bedrock generation
invocations issued by one POST /workflows/generate request is 1 (the
initial invocation) plus at most 1 (the single Repair_Pass), and the
repair invocation happens if and only if the Generation_Gate decision on
the first result is ``repair``:

- accept  -> exactly 1 invocation (no repair needed);
- repair  -> exactly 2 invocations, regardless of how the Repair_Pass
  turns out (clean, still repairable, still unrepairable, invocation
  failure, unparseable output, validator failure) — never a second
  Repair_Pass (Req 8.4);
- reject  -> exactly 1 invocation: unrepairable generations are rejected
  WITHOUT any Repair_Pass (Req 8.5);
- pre-gate failures (invocation error, unparseable output, validator
  exception) -> exactly 1 invocation and no repair.

The generation flow is exercised through the real ``generate_workflow``
handler function with a counting stub Bedrock client injected through
``get_bedrock_client`` (the pattern of
``edge-cv-portal/backend/tests/test_workflow_generation.py``); the
Workflow_Validator is scripted per invocation so every gate decision
shape is reachable, while the real Generation_Gate classification runs.
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


# ---------------------------------------------------------------------------
# Scripted collaborators. The per-example script drives which gate decision
# each invocation's result receives; call COUNTS are what the property
# asserts. When a script runs out (i.e. the implementation invokes more
# than expected) the stubs answer benignly so the violation surfaces as a
# clean count assertion, not an IndexError.
# ---------------------------------------------------------------------------

# Parses into a valid Workflow_Definition (the validator is scripted, so
# catalog conformance is irrelevant — only parseability matters).
_GOOD_DEFINITION = {
    "schemaVersion": 1,
    "nodes": [
        {"id": "n1", "type": "csi_camera_source",
         "position": {"x": 100, "y": 100}, "parameters": {}},
        {"id": "n2", "type": "capture",
         "position": {"x": 350, "y": 100},
         "parameters": {"output_path": "/data/captures"}},
    ],
    "connections": [
        {"id": "c1",
         "from": {"node": "n1", "port": "out"},
         "to": {"node": "n2", "port": "in"}},
    ],
}

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


def _structural_findings(count):
    """`count` structural-error findings in wire form (cycle code: a
    Structural_Error under any catalog, never unrepairable by rule 1)."""
    return [
        {"severity": validator.SEVERITY_ERROR,
         "code": validator.CODE_V3_CYCLE,
         "message": f"cycle {i}"}
        for i in range(count)
    ]


class _Script:
    """Per-example behavior scripts and invocation counters."""

    def __init__(self):
        self.converse_calls = 0
        self.converse_behaviors = []   # "error" | tool_input dict
        self.validator_behaviors = []  # "raise" | list of findings


_SCRIPT = _Script()


class _CountingBedrockClient:
    """Stub bedrock-runtime client counting every generation invocation."""

    def converse(self, **kwargs):
        _SCRIPT.converse_calls += 1
        behavior = (_SCRIPT.converse_behaviors.pop(0)
                    if _SCRIPT.converse_behaviors else _GOOD_DEFINITION)
        if behavior == "error":
            raise ClientError(
                {"Error": {"Code": "ThrottlingException",
                           "Message": "Rate exceeded"}},
                "Converse")
        return _tool_response(behavior)


_CLIENT = _CountingBedrockClient()


def _scripted_validator(graph, catalog=None):
    behavior = (_SCRIPT.validator_behaviors.pop(0)
                if _SCRIPT.validator_behaviors else [])
    if behavior == "raise":
        raise RuntimeError("validator cannot complete")
    return behavior


# Stub every I/O collaborator in the generation flow; the control flow
# under test (invoke -> parse -> validate -> gate -> repair/reject/accept)
# and the real Generation_Gate classification stay untouched.
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
# Outcome-shape generators
# ---------------------------------------------------------------------------

# What the FIRST invocation's result looks like. The three pre-gate shapes
# never reach a gate decision; the three gate shapes are decided by the
# real generation_gate.classify over the scripted findings.
_FIRST_SHAPES = st.sampled_from([
    "accept",            # no structural errors
    "repair",            # 1..threshold structural errors, all repairable
    "reject",            # > threshold structural errors (collapse rule)
    "invocation_error",  # Bedrock invocation fails
    "unparseable",       # tool output fails to parse
    "validator_raises",  # validator cannot complete (fail closed, 8.11)
])

# How the single Repair_Pass turns out (drawn always; used only when the
# first decision is repair).
_REPAIR_SHAPES = st.sampled_from([
    "clean",               # repaired result accepted
    "still_repairable",    # structural errors remain -> reject, NO 2nd pass
    "still_unrepairable",  # collapse on the repaired result -> reject
    "invocation_error",    # repair invocation fails -> reject
    "unparseable",         # repair output unparseable -> reject
    "validator_raises",    # repaired result not validatable -> reject
])

_REPAIRABLE_COUNTS = st.integers(
    min_value=1, max_value=generation_gate.UNREPAIRABLE_ERROR_THRESHOLD)
_COLLAPSE_COUNTS = st.integers(
    min_value=generation_gate.UNREPAIRABLE_ERROR_THRESHOLD + 1,
    max_value=generation_gate.UNREPAIRABLE_ERROR_THRESHOLD + 5)


def _build_scripts(first_shape, repair_shape, repairable_count,
                   collapse_count):
    """The converse/validator behavior scripts realizing the drawn shape."""
    converse, findings = [], []
    if first_shape == "invocation_error":
        converse.append("error")
    elif first_shape == "unparseable":
        converse.append(_BROKEN_DEFINITION)
    else:
        converse.append(_GOOD_DEFINITION)
        if first_shape == "validator_raises":
            findings.append("raise")
        elif first_shape == "accept":
            findings.append([])
        elif first_shape == "reject":
            findings.append(_structural_findings(collapse_count))
        else:  # repair
            findings.append(_structural_findings(repairable_count))
            if repair_shape == "invocation_error":
                converse.append("error")
            elif repair_shape == "unparseable":
                converse.append(_BROKEN_DEFINITION)
            else:
                converse.append(_GOOD_DEFINITION)
                if repair_shape == "validator_raises":
                    findings.append("raise")
                elif repair_shape == "clean":
                    findings.append([])
                elif repair_shape == "still_repairable":
                    findings.append(_structural_findings(repairable_count))
                else:  # still_unrepairable
                    findings.append(_structural_findings(collapse_count))
    return converse, findings


def _generate(prompt="Camera to capture"):
    """Run the preserved synchronous generation body directly.

    The workflow-manager-gaps async split moved the old generate_workflow
    body (the entire Bedrock invocation flow, including the single
    Repair_Pass) verbatim into run_generation_core, which the background
    worker executes; the HTTP submit path only queues the job and never
    invokes Bedrock. Driving the core with a fresh session replicates
    exactly what a fresh-session POST /workflows/generate produced before
    the split, so the invocation-count property still asserts the
    preserved semantics (Req 3/8 of that spec)."""
    session_id = "session-under-test"
    session = {
        "session_id": session_id,
        "usecase_id": "uc-1",
        "user_id": "user-1",
        "messages": [],
        "current_definition_key": None,
        "created_at": 1,
    }
    status_code, body = workflow_generator.run_generation_core(
        "uc-1", session_id, session, prompt, None, None)
    return {"statusCode": status_code, "body": json.dumps(body)}


# Feature: portal-build-fleet-and-workflow-gates, Property 19: At most one Repair_Pass per generation request
# Validates: Requirements 8.4, 8.5
@settings(max_examples=200)
@given(
    first_shape=_FIRST_SHAPES,
    repair_shape=_REPAIR_SHAPES,
    repairable_count=_REPAIRABLE_COUNTS,
    collapse_count=_COLLAPSE_COUNTS,
)
def test_at_most_one_repair_pass_per_generation_request(
        first_shape, repair_shape, repairable_count, collapse_count):
    """For any generation outcome shape: the total generation invocation
    count is 1 (initial) plus at most 1 (repair), and the repair
    invocation happens if and only if the gate decision on the first
    result is 'repair' (Req 8.4) — in particular, an unrepairable first
    result is rejected without any Repair_Pass (Req 8.5), and no repair
    outcome ever triggers a second pass."""
    converse_script, validator_script = _build_scripts(
        first_shape, repair_shape, repairable_count, collapse_count)

    _SCRIPT.converse_calls = 0
    _SCRIPT.converse_behaviors = list(converse_script)
    _SCRIPT.validator_behaviors = list(validator_script)

    response = _generate()

    # The request always completes with a well-formed HTTP response.
    assert isinstance(response, dict) and "statusCode" in response

    # Repair invocation iff the first gate decision is 'repair' (8.4, 8.5):
    # 1 initial invocation, plus exactly 1 repair on a repair decision.
    expected_invocations = 2 if first_shape == "repair" else 1
    assert _SCRIPT.converse_calls == expected_invocations, (
        f"first_shape={first_shape!r}, repair_shape={repair_shape!r}: "
        f"expected {expected_invocations} generation invocation(s), "
        f"got {_SCRIPT.converse_calls}"
    )

    # Never more than one Repair_Pass per generation request (8.4).
    assert _SCRIPT.converse_calls <= 2
