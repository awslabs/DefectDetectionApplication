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
Property test for session persistence gating in
``edge-cv-portal/backend/functions/workflow_generator.py``.

Spec: .kiro/specs/portal-build-fleet-and-workflow-gates (task 5.5)

**Validates: Requirements 8.1, 8.5, 8.7, 8.9, 8.10, 8.11**

For any generation flow outcome — the six outcome shapes:

1. clean accept                  (gate accepts the first result)
2. repaired accept               (repair pass yields a clean result, 8.1)
3. reject: unrepairable          (no Repair_Pass, 8.5)
4. reject: repair still broken / repair failed to complete (8.7)
5. reject: unparseable generator output (GENERATED_DEFINITION_INVALID, 8.10)
6. reject: validator failure     (GENERATION_VALIDATION_INCOMPLETE, 8.11)

— the chat session item and canvas snapshot are written if and only if
the outcome is an acceptance; every rejection performs ZERO writes to
the session table and snapshot store, leaving both in the state they had
before the rejected generation (8.9).

Unlike the sibling suites (test_repair_pass_invocation_count.py,
test_repair_outcome_shaping.py) which no-op ``put_snapshot`` /
``save_session``, here the REAL persistence functions run against a
moto-mocked WorkflowChatSessions DynamoDB table and portal-artifacts S3
bucket; the Bedrock client and Workflow_Validator are scripted stubs (the
sibling pattern) while the real Generation_Gate classification runs. The
property compares the full table scan and bucket listing before and after
each request. Both fresh sessions and pre-existing sessions (with a prior
snapshot and message history) are exercised, so rejections provably
preserve PRIOR state, not merely emptiness.
"""
import json
import os
import sys
import types

from botocore.exceptions import ClientError
from hypothesis import given, settings
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# Environment + sys.path so the generator Lambda bundle imports cleanly.
# The session table / snapshot bucket names and the module-level boto3
# resource/client bind at import time, so moto is STARTED and the table and
# bucket created BEFORE workflow_generator is imported. The workflow_core
# layer path is APPENDED, not prepended (mirroring the layer's own tests):
# python/ also carries vendored Lambda-runtime dependencies that must not
# shadow host packages.
# ---------------------------------------------------------------------------
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ["AWS_ACCESS_KEY_ID"] = "testing"
os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
os.environ["AWS_SECURITY_TOKEN"] = "testing"
os.environ["AWS_SESSION_TOKEN"] = "testing"

_SESSIONS_TABLE = "workflow-chat-sessions-p21"
_ARTIFACTS_BUCKET = "portal-artifacts-p21"
os.environ["WORKFLOW_CHAT_SESSIONS_TABLE"] = _SESSIONS_TABLE
os.environ["PORTAL_ARTIFACTS_BUCKET"] = _ARTIFACTS_BUCKET

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

# Fresh real modules (some standalone suites install a fake shared_utils),
# and force workflow_generator's module-level boto3 handles to be created
# under the moto mock below.
for _module in ("workflow_generator", "workflow_validation", "shared_utils",
                "bedrock_common", "code_assist", "node_catalog_resolution",
                "generation_gate"):
    sys.modules.pop(_module, None)

# The flask-app verification container's python3.9 is built without the
# _bz2 C extension, and moto's request path imports moto.s3 -> bz2 on
# every call (moto.core.authorization -> moto.iam.access_control ->
# moto.s3.models). bz2 is only used for S3-Select payload decompression,
# which this suite's plain put/get/list S3 traffic never exercises, so a
# minimal stdlib-shaped stub keeps the import chain intact where _bz2 is
# absent (same shim as the sibling test_build_history_ordering.py).
try:
    import bz2  # noqa: F401
except ImportError:  # pragma: no cover - depends on the runner's build
    _bz2_stub = types.ModuleType("_bz2")

    class _Bz2Unavailable:
        def __init__(self, *args, **kwargs):
            raise RuntimeError("bz2 is unavailable in this environment")

    _bz2_stub.BZ2Compressor = _Bz2Unavailable
    _bz2_stub.BZ2Decompressor = _Bz2Unavailable
    sys.modules["_bz2"] = _bz2_stub

from moto import mock_aws  # noqa: E402

# Module-scope moto: active for every import below and for the whole test
# run, so workflow_generator's import-time boto3 resource/client and every
# real put_snapshot/save_session call inside generate_workflow hit moto.
_MOCK = mock_aws()
_MOCK.start()

import boto3  # noqa: E402

_DDB = boto3.resource("dynamodb", region_name="us-east-1")
_DDB.create_table(
    TableName=_SESSIONS_TABLE,
    KeySchema=[{"AttributeName": "session_id", "KeyType": "HASH"}],
    AttributeDefinitions=[{"AttributeName": "session_id",
                           "AttributeType": "S"}],
    BillingMode="PAY_PER_REQUEST",
)
_TABLE = _DDB.Table(_SESSIONS_TABLE)
_S3 = boto3.client("s3", region_name="us-east-1")
_S3.create_bucket(Bucket=_ARTIFACTS_BUCKET)

import workflow_generator  # noqa: E402
import generation_gate  # noqa: E402
import workflow_core.validator as validator  # noqa: E402
from workflow_core.catalog import NODE_CATALOG  # noqa: E402


# ---------------------------------------------------------------------------
# Scripted collaborators (the sibling-suite pattern): the per-example
# script drives what each Bedrock invocation returns and what the
# validator reports; the REAL gate classification and the REAL session
# persistence run. When a script runs out the stubs answer benignly so a
# violation surfaces as a clean assertion, not an IndexError.
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
    """Per-example behavior scripts."""

    def __init__(self):
        self.converse_behaviors = []   # "error" | tool_input dict
        self.validator_behaviors = []  # "raise" | list of findings


_SCRIPT = _Script()


class _ScriptedBedrockClient:
    """Stub bedrock-runtime client answering per the example's script."""

    def converse(self, **kwargs):
        behavior = (_SCRIPT.converse_behaviors.pop(0)
                    if _SCRIPT.converse_behaviors else _GOOD_DEFINITION)
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


# Stub the Bedrock/RBAC/catalog collaborators; the persistence functions
# (put_snapshot, save_session, get_session, load_snapshot) are NOT stubbed:
# they run for real against the moto table and bucket above — that is the
# subject of this property.
workflow_generator.get_bedrock_client = lambda region, timeout: _CLIENT
workflow_generator.get_bedrock_configuration = (
    lambda: dict(workflow_generator.DEFAULT_BEDROCK_CONFIG))
workflow_generator.has_workflow_permission = (
    lambda user, usecase_id, permission: True)
workflow_generator.get_usecase = lambda usecase_id: {"usecase_id": usecase_id}
workflow_generator.palette_catalog_for_usecase = (
    lambda usecase_id: (NODE_CATALOG, []))
workflow_generator.run_validator = _scripted_validator


# ---------------------------------------------------------------------------
# The six outcome shapes (design Property 21)
# ---------------------------------------------------------------------------

_ACCEPT_SHAPES = ("clean_accept", "repaired_accept")

_OUTCOME_SHAPES = st.sampled_from([
    "clean_accept",        # 1: gate accepts the first result
    "repaired_accept",     # 2: repair yields a clean result (accept)
    "reject_unrepairable",  # 3: collapse on the first result, no repair (8.5)
    "reject_repair_failed",  # 4: repair broken/failed -> reject (8.7)
    "unparseable_output",  # 5: GENERATED_DEFINITION_INVALID (8.10)
    "validator_failure",   # 6: GENERATION_VALIDATION_INCOMPLETE (8.11)
])

# Shape 4 covers both flavors: the Repair_Pass result still contains
# Structural_Errors, or the Repair_Pass fails to complete (invocation
# error / unparseable repair output / validator failure on the result).
_REPAIR_FAILURE_MODES = st.sampled_from([
    "still_broken", "invocation_error", "unparseable", "validator_raises",
])

_REPAIRABLE_COUNTS = st.integers(
    min_value=1, max_value=generation_gate.UNREPAIRABLE_ERROR_THRESHOLD)
_COLLAPSE_COUNTS = st.integers(
    min_value=generation_gate.UNREPAIRABLE_ERROR_THRESHOLD + 1,
    max_value=generation_gate.UNREPAIRABLE_ERROR_THRESHOLD + 5)


def _build_scripts(shape, repair_failure, repairable_count, collapse_count):
    """The converse/validator behavior scripts realizing the drawn shape."""
    converse, findings = [], []
    if shape == "unparseable_output":
        converse.append(_BROKEN_DEFINITION)
    elif shape == "validator_failure":
        converse.append(_GOOD_DEFINITION)
        findings.append("raise")
    elif shape == "clean_accept":
        converse.append(_GOOD_DEFINITION)
        findings.append([])
    elif shape == "reject_unrepairable":
        converse.append(_GOOD_DEFINITION)
        findings.append(_structural_findings(collapse_count))
    elif shape == "repaired_accept":
        converse.append(_GOOD_DEFINITION)
        findings.append(_structural_findings(repairable_count))
        converse.append(_GOOD_DEFINITION)
        findings.append([])
    else:  # reject_repair_failed
        converse.append(_GOOD_DEFINITION)
        findings.append(_structural_findings(repairable_count))
        if repair_failure == "invocation_error":
            converse.append("error")
        elif repair_failure == "unparseable":
            converse.append(_BROKEN_DEFINITION)
        else:
            converse.append(_GOOD_DEFINITION)
            if repair_failure == "validator_raises":
                findings.append("raise")
            else:  # still_broken
                findings.append(_structural_findings(repairable_count))
    return converse, findings


# ---------------------------------------------------------------------------
# Store snapshots (full table scan + bucket listing with content hashes)
# ---------------------------------------------------------------------------

def _table_state():
    """Every session item in the table, keyed by session_id."""
    items, kwargs = [], {}
    while True:
        page = _TABLE.scan(**kwargs)
        items.extend(page.get("Items", []))
        if "LastEvaluatedKey" not in page:
            break
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    return {item["session_id"]: item for item in items}


def _bucket_state():
    """Every object in the snapshot store: key -> body bytes."""
    state, kwargs = {}, {"Bucket": _ARTIFACTS_BUCKET}
    while True:
        page = _S3.list_objects_v2(**kwargs)
        for obj in page.get("Contents", []):
            body = _S3.get_object(Bucket=_ARTIFACTS_BUCKET,
                                  Key=obj["Key"])["Body"].read()
            state[obj["Key"]] = body
        if not page.get("IsTruncated"):
            break
        kwargs["ContinuationToken"] = page["NextContinuationToken"]
    return state


def _clear_stores():
    for session_id in _table_state():
        _TABLE.delete_item(Key={"session_id": session_id})
    for key in _bucket_state():
        _S3.delete_object(Bucket=_ARTIFACTS_BUCKET, Key=key)


_USECASE_ID = "uc-1"
_USER = {"user_id": "user-1", "email": "user-1@example.com",
         "username": "user-1", "role": "DataScientist"}


def _seed_existing_session(session_id):
    """A pre-existing session with a prior snapshot and message history,
    written through the module's own persistence helpers."""
    snapshot_key = workflow_generator.snapshot_s3_key(_USECASE_ID, session_id)
    workflow_generator.put_snapshot(
        snapshot_key, json.dumps(_GOOD_DEFINITION, sort_keys=True))
    workflow_generator.save_session({
        "session_id": session_id,
        "usecase_id": _USECASE_ID,
        "user_id": _USER["user_id"],
        "messages": [
            {"role": "user", "text": "prior prompt", "at": 1},
            {"role": "assistant", "text": "prior answer", "at": 1},
        ],
        "current_definition_key": snapshot_key,
        "created_at": 1,
    })


def _generate(session_id=None, prompt="Camera to capture"):
    """Run generate_workflow directly with a synthetic event and user."""
    request = {"usecase_id": _USECASE_ID, "prompt": prompt}
    if session_id is not None:
        request["session_id"] = session_id
    event = {
        "httpMethod": "POST",
        "resource": "/workflows/generate",
        "path": "/workflows/generate",
        "body": json.dumps(request),
    }
    return workflow_generator.generate_workflow(event, dict(_USER))


# Feature: portal-build-fleet-and-workflow-gates, Property 21: Session persistence if and only if the gate accepts
# Validates: Requirements 8.1, 8.5, 8.7, 8.9, 8.10, 8.11
@settings(max_examples=100, deadline=None)
@given(
    shape=_OUTCOME_SHAPES,
    repair_failure=_REPAIR_FAILURE_MODES,
    repairable_count=_REPAIRABLE_COUNTS,
    collapse_count=_COLLAPSE_COUNTS,
    existing_session=st.booleans(),
)
def test_persistence_iff_accept(shape, repair_failure, repairable_count,
                                collapse_count, existing_session):
    """For any of the six generation outcome shapes: the chat session item
    and canvas snapshot are written if and only if the gate accepts (clean
    or repaired accept, 8.1); every rejection — unrepairable (8.5), failed
    repair (8.7), unparseable output (8.10), validator failure (8.11) —
    performs zero writes, leaving the session table and snapshot store in
    the exact state they had before the request (8.9, 8.10, 8.11)."""
    _clear_stores()

    session_id = None
    if existing_session:
        session_id = "session-prior"
        _seed_existing_session(session_id)

    converse_script, validator_script = _build_scripts(
        shape, repair_failure, repairable_count, collapse_count)
    _SCRIPT.converse_behaviors = list(converse_script)
    _SCRIPT.validator_behaviors = [
        list(b) if isinstance(b, list) else b for b in validator_script]

    table_before = _table_state()
    bucket_before = _bucket_state()

    response = _generate(session_id=session_id)
    body = json.loads(response["body"])

    table_after = _table_state()
    bucket_after = _bucket_state()

    if shape in _ACCEPT_SHAPES:
        # Accept path: the request succeeds and the session item plus the
        # canvas snapshot were persisted (8.1: gate ran before persistence
        # and accepted; persistence happens only now).
        assert response["statusCode"] == 200, body
        result_session_id = body["session_id"]
        expected_key = workflow_generator.snapshot_s3_key(
            _USECASE_ID, result_session_id)

        assert result_session_id in table_after, (
            f"shape={shape!r}: accepted generation did not persist the "
            f"chat session item")
        session_item = table_after[result_session_id]
        assert session_item["current_definition_key"] == expected_key
        # The accepted turn was appended to the message history.
        prior_messages = (table_before.get(result_session_id, {})
                          .get("messages", []))
        assert len(session_item["messages"]) == len(prior_messages) + 2

        assert expected_key in bucket_after, (
            f"shape={shape!r}: accepted generation did not persist the "
            f"canvas snapshot")
        # The snapshot IS the returned (accepted) definition.
        assert json.loads(bucket_after[expected_key].decode("utf-8")) \
            == body["definition"]
    else:
        # Reject path: a user-readable error and ZERO writes — the table
        # and the snapshot store are byte-identical to their prior state
        # (8.5, 8.7, 8.9, 8.10, 8.11).
        assert response["statusCode"] == 422, body
        expected_codes = {
            "reject_unrepairable": "GENERATION_REJECTED",
            "reject_repair_failed": "GENERATION_REJECTED",
            "unparseable_output": "GENERATED_DEFINITION_INVALID",
            "validator_failure": "GENERATION_VALIDATION_INCOMPLETE",
        }
        assert body["error"]["code"] == expected_codes[shape], body
        assert body["error"]["message"]

        assert table_after == table_before, (
            f"shape={shape!r} (repair_failure={repair_failure!r}): "
            f"rejected generation mutated the session table")
        assert bucket_after == bucket_before, (
            f"shape={shape!r} (repair_failure={repair_failure!r}): "
            f"rejected generation mutated the snapshot store")
