# Copyright 2026 Amazon Web Services, Inc.
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
Unit tests for the optional Build Log / detail ``diagnostic`` projection
of ``edge-cv-portal/backend/functions/build_jobs.py``
(build-fleet-execution-failures task 9.1; task 10.7 extends this file).

**Validates: Requirements 2.2, 2.3, 2.10, 2.18, 3.6**

- Legacy response: a job with NO persisted diagnostic evidence keeps the
  exact legacy `GET /builds/{id}/logs` body — `{'events': [...],
  'nextToken': ...}` with no `diagnostic` key (Req 3.6).
- Events + diagnostic: when both CloudWatch events and a persisted
  `execution_diagnostic` exist, `events`/`nextToken` are preserved
  exactly and the versioned camelCase `diagnostic` is added (Req 2.2).
- Diagnostic-only with missing stream: the diagnostic is returned
  independently of CloudWatch stream existence — never an empty-only
  page while useful evidence exists (Req 2.3).
- Pagination-token stability: `nextToken` semantics are untouched and
  the diagnostic repeats across pages as immutable metadata (Req 2.3).
- Projection reads ONLY persisted normalized/redacted/bounded records
  (Req 2.10) and marks unavailable evidence rather than fabricating it
  (Req 2.18).

Run from the repository root (finite, non-watch):

    PYTHONPATH=src/backend:test/backend-test python3 -m pytest \
        test/backend-test/portal_builds/test_build_diagnostic_api.py \
        --noconftest -q
"""
import json
import os
import sys
import types
from decimal import Decimal

# ---------------------------------------------------------------------------
# Environment + fake layer modules BEFORE build_jobs is imported (the
# handler binds boto3 handles and table names at import time).
# ---------------------------------------------------------------------------
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ["AWS_ACCESS_KEY_ID"] = "testing"
os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
os.environ["AWS_SECURITY_TOKEN"] = "testing"
os.environ["AWS_SESSION_TOKEN"] = "testing"

_SUFFIX = "diag-api"
_JOBS_TABLE_NAME = f"build-jobs-{_SUFFIX}"
_LOG_GROUP = f"/dda/portal-builds-{_SUFFIX}"
os.environ["BUILD_JOBS_TABLE"] = _JOBS_TABLE_NAME
os.environ["BUILD_SERVERS_TABLE"] = f"build-servers-{_SUFFIX}"
os.environ["SETTINGS_TABLE"] = f"settings-{_SUFFIX}"
os.environ["BUILD_LOG_GROUP"] = _LOG_GROUP

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.normpath(os.path.join(_HERE, "..", "..", ".."))
_FUNCTIONS_DIR = os.path.join(
    _REPO_ROOT, "edge-cv-portal", "backend", "functions")
if _FUNCTIONS_DIR not in sys.path:
    sys.path.insert(0, _FUNCTIONS_DIR)


def _decimal_default(obj):
    if isinstance(obj, Decimal):
        return int(obj) if obj % 1 == 0 else float(obj)
    raise TypeError(f"Object of type {type(obj)} is not JSON serializable")


def _fake_shared_utils():
    """shared_utils stub faithful to the real create_response envelope."""
    module = types.ModuleType("shared_utils")

    def create_response(status_code, body, headers=None):
        return {
            "statusCode": status_code,
            "headers": {"Content-Type": "application/json"},
            "body": (json.dumps(body, default=_decimal_default)
                     if not isinstance(body, str) else body),
        }

    module.create_response = create_response
    module.get_user_from_event = lambda event: {
        "user_id": "user-1", "role": "PortalAdmin"}
    module.log_audit_event = lambda *args, **kwargs: None
    return module


def _fake_rbac_middleware():
    module = types.ModuleType("rbac_middleware")

    def _passthrough_factory(*factory_args, **factory_kwargs):
        def decorator(handler):
            return handler
        return decorator

    module.require_builds_submit = _passthrough_factory
    module.require_builds_cancel = _passthrough_factory
    module.require_builds_read = _passthrough_factory
    return module


for _module in ("build_jobs", "build_domain", "build_reconciliation",
                "build_source", "shared_utils", "rbac_middleware"):
    sys.modules.pop(_module, None)
sys.modules["shared_utils"] = _fake_shared_utils()
sys.modules["rbac_middleware"] = _fake_rbac_middleware()

# Sibling-suite _bz2 shim (see test_build_history_ordering.py).
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

_MOCK = mock_aws()
_MOCK.start()

import boto3  # noqa: E402

_DDB = boto3.resource("dynamodb", region_name="us-east-1")
_DDB.create_table(
    TableName=_JOBS_TABLE_NAME,
    KeySchema=[{"AttributeName": "build_job_id", "KeyType": "HASH"}],
    AttributeDefinitions=[{"AttributeName": "build_job_id",
                           "AttributeType": "S"}],
    BillingMode="PAY_PER_REQUEST",
)
_TABLE = _DDB.Table(_JOBS_TABLE_NAME)
_LOGS = boto3.client("logs", region_name="us-east-1")
_LOGS.create_log_group(logGroupName=_LOG_GROUP)

import build_jobs  # noqa: E402

# ---------------------------------------------------------------------------
# Fixtures: persisted (already sanitized/bounded) diagnostic evidence, as
# build_reconciliation/build_events wrote it (tasks 4-6, 8).
# ---------------------------------------------------------------------------

T0 = 1_786_017_773_000

#: Persisted execution_diagnostic (snake_case, schema_version 1).
PERSISTED_DIAGNOSTIC = {
    "schema_version": 1,
    "attempt_id": "at-1",
    "command_id": "cmd-1",
    "instance_id": "i-0aa11bb22cc33dd44",
    "source": ["eventbridge"],
    "status": "Failed",
    "status_details": "Failed",
    "response_code": 127,
    "execution_start": "2026-08-06T12:02:54Z",
    "execution_end": "2026-08-06T12:03:02Z",
    "stdout": {"available": True, "text": "", "truncated": False,
               "original_bytes": 0},
    "stderr": {"available": True,
               "text": "bash: .../portal-build-agent.sh: "
                       "No such file or directory",
               "truncated": False, "original_bytes": 62},
    "classification": "COMMAND_EXECUTION_FAILED",
    "disk": {"available": False},
    "observed_at": T0 + 9_000,
    "complete": True,
}

#: Persisted terminal timeout evidence (task 8.2 shape).
PERSISTED_TIMEOUT_EVIDENCE = {
    "phase": "execution",
    "observed_ms": 4 * 60 * 60 * 1000 + 1,
    "queue_wait_ms": 120_000,
    "provisioning_ms": 300_000,
    "execution_runtime_ms": 4 * 60 * 60 * 1000 + 1,
    "budget_ms": 4 * 60 * 60 * 1000,
    "budget_source": "max_runtime_hours",
    "hard_runtime_ms": 4 * 60 * 60 * 1000,
    "last_heartbeat_at": T0 + 100_000,
    "last_progress_at": T0 + 90_000,
    "last_progress_kind": "output_growth",
    "execution_started_at": T0,
    "build_target": "AMD64",
    "execution_mode": "dedicated",
    "evaluated_at": T0 + 4 * 60 * 60 * 1000 + 1,
    "timeout_kind": "MAX_RUNTIME_EXCEEDED",
    "timeout_decided_at": T0 + 4 * 60 * 60 * 1000 + 1,
}


def _base_job(job_id, **extra):
    job = {
        "build_job_id": job_id,
        "build_target": "AMD64",
        "execution_mode": "dedicated",
        "status": "failed",
        "requested_by": "operator-1",
        "created_at": T0,
        "started_at": T0 + 1_000,
        "ended_at": T0 + 9_000,
        "error": {"kind": "build", "code": "COMMAND_EXECUTION_FAILED",
                  "message": "The build agent SSM command ended with "
                             "status 'Failed'."},
    }
    job.update(extra)
    return job


def _put_job(job):
    _TABLE.put_item(Item=job)


def _clear():
    for item in _TABLE.scan().get("Items", []):
        _TABLE.delete_item(Key={"build_job_id": item["build_job_id"]})


def _logs_body(job_id, params=None):
    response = build_jobs.get_build_logs(
        {"pathParameters": {"id": job_id},
         "queryStringParameters": params}, None)
    assert response["statusCode"] == 200, response
    return json.loads(response["body"])


def _write_stream(stream, messages, base_ts):
    _LOGS.create_log_stream(logGroupName=_LOG_GROUP, logStreamName=stream)
    _LOGS.put_log_events(
        logGroupName=_LOG_GROUP, logStreamName=stream,
        logEvents=[{"timestamp": base_ts + i, "message": m}
                   for i, m in enumerate(messages)])


class TestLegacyResponseUnchanged:
    """Jobs WITHOUT persisted diagnostics keep the byte-compatible
    legacy response (Req 3.6)."""

    def setup_method(self):
        _clear()

    def test_missing_stream_legacy_job_is_exact_legacy_body(self):
        _put_job(_base_job("legacy-1"))
        body = _logs_body("legacy-1")
        # Exact legacy shape: no diagnostic key, empty page.
        assert body == {"events": [], "nextToken": None}

    def test_stream_present_legacy_job_has_no_diagnostic_key(self):
        _put_job(_base_job("legacy-2"))
        _write_stream("legacy-2", ["line-1", "line-2"], T0 + 2_000)
        body = _logs_body("legacy-2")
        assert set(body.keys()) == {"events", "nextToken"}
        assert [e["message"] for e in body["events"]] == ["line-1", "line-2"]
        assert isinstance(body["nextToken"], str) and body["nextToken"]


class TestDiagnosticProjection:
    """Persisted evidence surfaces as the approved versioned camelCase
    `diagnostic` (Req 2.2, 2.18) independently of CloudWatch (Req 2.3)."""

    def setup_method(self):
        _clear()

    def test_diagnostic_only_with_missing_stream(self):
        _put_job(_base_job(
            "diag-1", execution_diagnostic=PERSISTED_DIAGNOSTIC))
        body = _logs_body("diag-1")
        # events/nextToken preserved exactly; diagnostic added.
        assert body["events"] == []
        assert body["nextToken"] is None
        diag = body["diagnostic"]
        assert diag["schemaVersion"] == 1
        assert diag["classification"] == "COMMAND_EXECUTION_FAILED"
        assert diag["status"] == "Failed"
        assert diag["statusDetails"] == "Failed"
        assert diag["responseCode"] == 127
        assert diag["stdout"] == {"available": True, "text": "",
                                  "truncated": False}
        assert diag["stderr"]["available"] is True
        assert "No such file or directory" in diag["stderr"]["text"]
        assert diag["stderr"]["truncated"] is False
        assert diag["observedAt"] == T0 + 9_000
        assert diag["complete"] is True
        # Unavailable disk measurement passes through truthfully.
        assert diag["disk"] == {"available": False}
        # Phase durations from the timing evidence; none was recorded
        # here beyond created/ended, so unavailable phases are None
        # (never fabricated, Req 2.18).
        assert set(diag["timing"].keys()) == {
            "queueMs", "provisioningMs", "executionMs"}

    def test_events_plus_diagnostic_preserves_events_exactly(self):
        _put_job(_base_job(
            "diag-2", execution_diagnostic=PERSISTED_DIAGNOSTIC))
        _write_stream("diag-2", ["build started", "build failed"],
                      T0 + 2_000)
        body = _logs_body("diag-2")
        assert [e["message"] for e in body["events"]] == [
            "build started", "build failed"]
        assert isinstance(body["nextToken"], str) and body["nextToken"]
        assert body["diagnostic"]["classification"] == \
            "COMMAND_EXECUTION_FAILED"

    def test_unavailable_provider_fields_marked_not_fabricated(self):
        diag = dict(PERSISTED_DIAGNOSTIC)
        diag["stdout"] = {"available": False}
        diag["stderr"] = {"available": False}
        _put_job(_base_job("diag-3", execution_diagnostic=diag))
        body = _logs_body("diag-3")
        assert body["diagnostic"]["stdout"] == {"available": False}
        assert body["diagnostic"]["stderr"] == {"available": False}

    def test_timeout_evidence_projects_kind_budget_source_and_activity(self):
        _put_job(_base_job(
            "diag-4",
            error={"kind": "build", "code": "MAX_RUNTIME_EXCEEDED",
                   "message": "Maximum runtime exceeded."},
            timeout_evidence=PERSISTED_TIMEOUT_EVIDENCE))
        body = _logs_body("diag-4")
        diag = body["diagnostic"]
        # No SSM invocation diagnostic: classification falls back to the
        # decided timeout kind; timing comes from the terminal record.
        assert diag["classification"] == "MAX_RUNTIME_EXCEEDED"
        assert diag["timing"] == {
            "queueMs": 120_000,
            "provisioningMs": 300_000,
            "executionMs": 4 * 60 * 60 * 1000 + 1,
        }
        timeout = diag["timeout"]
        assert timeout["kind"] == "MAX_RUNTIME_EXCEEDED"
        assert timeout["budgetMs"] == 4 * 60 * 60 * 1000
        assert timeout["budgetSource"] == "max_runtime_hours"
        assert timeout["lastHeartbeatAt"] == T0 + 100_000
        assert timeout["lastProgressAt"] == T0 + 90_000
        assert timeout["buildTarget"] == "AMD64"
        assert timeout["executionMode"] == "dedicated"

    def test_detail_exposes_the_persisted_structure(self):
        """GET /builds/{id} returns the job verbatim, so the persisted
        optional structure rides along unchanged (Req 3.6: additive)."""
        _put_job(_base_job(
            "diag-5", execution_diagnostic=PERSISTED_DIAGNOSTIC,
            timeout_evidence=PERSISTED_TIMEOUT_EVIDENCE))
        response = build_jobs.get_build(
            {"pathParameters": {"id": "diag-5"},
             "queryStringParameters": None}, None)
        assert response["statusCode"] == 200
        job = json.loads(response["body"])["job"]
        assert job["execution_diagnostic"]["classification"] == \
            "COMMAND_EXECUTION_FAILED"
        assert job["timeout_evidence"]["timeout_kind"] == \
            "MAX_RUNTIME_EXCEEDED"


class TestPaginationTokenStability:
    """`nextToken` semantics are untouched and the diagnostic repeats
    across pages as immutable metadata (Req 2.3, 3.6)."""

    def setup_method(self):
        _clear()

    def test_token_walk_is_stable_and_diagnostic_repeats(self):
        _put_job(_base_job(
            "page-1", execution_diagnostic=PERSISTED_DIAGNOSTIC))
        _write_stream("page-1", [f"line-{i}" for i in range(5)], T0 + 2_000)

        first = _logs_body("page-1", {"limit": "3"})
        assert [e["message"] for e in first["events"]] == [
            "line-0", "line-1", "line-2"]
        token = first["nextToken"]
        assert isinstance(token, str) and token
        assert first["diagnostic"]["classification"] == \
            "COMMAND_EXECUTION_FAILED"

        second = _logs_body("page-1", {"limit": "3", "nextToken": token})
        assert [e["message"] for e in second["events"]] == [
            "line-3", "line-4"]
        # The diagnostic repeats identically on every page.
        assert second["diagnostic"] == first["diagnostic"]

    def test_invalid_token_error_envelope_unchanged(self):
        _put_job(_base_job(
            "page-2", execution_diagnostic=PERSISTED_DIAGNOSTIC))
        _write_stream("page-2", ["line-0"], T0 + 2_000)
        response = build_jobs.get_build_logs(
            {"pathParameters": {"id": "page-2"},
             "queryStringParameters": {"nextToken": "not-a-token"}}, None)
        assert response["statusCode"] == 400
        error = json.loads(response["body"])["error"]
        assert error["code"] == "INVALID_PARAMETER"

    def test_unknown_job_error_envelope_unchanged(self):
        response = build_jobs.get_build_logs(
            {"pathParameters": {"id": "missing"},
             "queryStringParameters": None}, None)
        assert response["statusCode"] == 404
        error = json.loads(response["body"])["error"]
        assert error["code"] == "BUILD_JOB_NOT_FOUND"


# ===========================================================================
# Task 10.7 additive extensions. Everything above is the task 9.1
# baseline and is NOT rewritten.
# ===========================================================================

from hypothesis import HealthCheck, given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

#: Secret canary: never persisted anywhere; its absence from every API
#: body proves the projection cannot un-redact or fabricate (Req 2.10).
_CANARY_SECRET = "AKIA" + "CANARYDIAGAPI000"


class TestAvailabilityAndRedactionVariants:
    """Task 10.7 additive cases: available-empty vs unavailable
    variants, truncation flags, redaction passthrough, and
    diagnostics-only field combinations (Req 2.2, 2.10, 2.18)."""

    def setup_method(self):
        _clear()

    def test_available_empty_and_unavailable_distinguished_in_one_body(self):
        """Available-but-empty and unavailable stream fields are
        distinct, truthful states in the same response (Req 2.18)."""
        diag = dict(PERSISTED_DIAGNOSTIC)
        diag["stdout"] = {"available": True, "text": "",
                          "truncated": False, "original_bytes": 0}
        diag["stderr"] = {"available": False}
        _put_job(_base_job("var-1", execution_diagnostic=diag))
        body = _logs_body("var-1")
        assert body["diagnostic"]["stdout"] == {
            "available": True, "text": "", "truncated": False}
        assert body["diagnostic"]["stderr"] == {"available": False}

    def test_truncation_flags_pass_through_with_the_retained_text(self):
        """A persisted truncated excerpt keeps its truncation flag and
        retained text; nothing is silently completed (Req 2.2, 2.18)."""
        diag = dict(PERSISTED_DIAGNOSTIC)
        diag["stderr"] = {"available": True,
                          "text": "...[TRUNCATED 70000 bytes]... "
                                  "no space left on device",
                          "truncated": True, "original_bytes": 70_000}
        _put_job(_base_job("var-2", execution_diagnostic=diag))
        stderr = _logs_body("var-2")["diagnostic"]["stderr"]
        assert stderr["available"] is True
        assert stderr["truncated"] is True
        assert stderr["text"].endswith("no space left on device")

    def test_redaction_passthrough_keeps_persisted_redacted_text_intact(
            self):
        """The projection returns the persisted [REDACTED] text intact
        — it neither un-redacts nor re-processes — and no raw secret
        shape appears anywhere in the response body (Req 2.10)."""
        diag = dict(PERSISTED_DIAGNOSTIC)
        diag["stderr"] = {
            "available": True,
            "text": "aws_secret_access_key=[REDACTED] fetch failed for "
                    "https://[REDACTED]:[REDACTED]@github.com/org/repo",
            "truncated": False, "original_bytes": 96}
        diag["status_details"] = "authorization: [REDACTED] rejected"
        _put_job(_base_job("var-3", execution_diagnostic=diag))
        response = build_jobs.get_build_logs(
            {"pathParameters": {"id": "var-3"},
             "queryStringParameters": None}, None)
        raw_body = response["body"]
        body = json.loads(raw_body)
        assert body["diagnostic"]["stderr"]["text"] == \
            diag["stderr"]["text"]
        assert body["diagnostic"]["statusDetails"] == \
            "authorization: [REDACTED] rejected"
        assert _CANARY_SECRET not in raw_body
        assert "AKIA" not in raw_body.replace("[REDACTED]", "")

    def test_status_only_diagnostic_without_streams(self):
        """A diagnostics-only record carrying status/details/response
        code but NO stream fields marks both streams unavailable and
        still surfaces the classification (Req 2.2, 2.18)."""
        _put_job(_base_job("var-4", execution_diagnostic={
            "schema_version": 1, "attempt_id": "at-4",
            "command_id": "cmd-4", "instance_id": "i-4",
            "status": "TimedOut", "status_details": "DeliveryTimedOut",
            "response_code": -1,
            "classification": "COMMAND_TIMED_OUT",
            "observed_at": T0 + 5_000, "complete": False}))
        diag = _logs_body("var-4")["diagnostic"]
        assert diag["classification"] == "COMMAND_TIMED_OUT"
        assert diag["status"] == "TimedOut"
        assert diag["statusDetails"] == "DeliveryTimedOut"
        assert diag["responseCode"] == -1
        assert diag["stdout"] == {"available": False}
        assert diag["stderr"] == {"available": False}
        assert diag["complete"] is False

    def test_stderr_only_diagnostic_with_timing_map_combination(self):
        """A stderr-plus-timing combination (no stdout, no timeout
        decision) projects the stderr excerpt and computes execution
        duration from the persisted timing map (Req 2.2, 2.18)."""
        _put_job(_base_job(
            "var-5",
            execution_diagnostic={
                "schema_version": 1, "attempt_id": "at-5",
                "command_id": "cmd-5", "instance_id": "i-5",
                "status": "Failed", "response_code": 1,
                "stderr": {"available": True, "text": "gdk failed",
                           "truncated": False, "original_bytes": 10},
                "classification": "COMMAND_EXECUTION_FAILED",
                "observed_at": T0 + 8_000, "complete": True},
            timing={"execution_started_at": T0 + 2_000,
                    "execution_ended_at": T0 + 8_000}))
        diag = _logs_body("var-5")["diagnostic"]
        assert diag["stdout"] == {"available": False}
        assert diag["stderr"]["text"] == "gdk failed"
        assert diag["timing"]["executionMs"] == 6_000
        assert "timeout" not in diag


# ---------------------------------------------------------------------------
# Property 13: Build Log and Timeout Diagnostic Availability
# ---------------------------------------------------------------------------

_STREAM_VARIANTS = ("absent", "unavailable", "empty", "text", "truncated")

_texts = st.text(
    alphabet=st.characters(min_codepoint=32, max_codepoint=126),
    min_size=1, max_size=40)


def _stream_field(variant, text):
    if variant == "absent":
        return None
    if variant == "unavailable":
        return {"available": False}
    if variant == "empty":
        return {"available": True, "text": "", "truncated": False,
                "original_bytes": 0}
    if variant == "text":
        return {"available": True, "text": text, "truncated": False,
                "original_bytes": len(text.encode("utf-8"))}
    return {"available": True, "text": text, "truncated": True,
            "original_bytes": 70_000}


@st.composite
def _persisted_evidence(draw):
    """One persisted diagnostic field-availability combination: an
    optional execution_diagnostic (with independently absent/available
    status, details, response code, stdout, stderr) and an optional
    timeout_evidence subset. Both absent means a legacy job."""
    has_diag = draw(st.booleans())
    has_timeout = draw(st.booleans())
    diag = None
    if has_diag:
        diag = {"schema_version": 1, "attempt_id": "at-p13",
                "command_id": "cmd-p13", "instance_id": "i-p13",
                "complete": draw(st.booleans())}
        if draw(st.booleans()):
            diag["status"] = draw(st.sampled_from(
                ("Failed", "Success", "TimedOut", "Cancelled")))
        if draw(st.booleans()):
            diag["status_details"] = draw(_texts)
        if draw(st.booleans()):
            diag["response_code"] = draw(
                st.integers(min_value=-1, max_value=255))
        if draw(st.booleans()):
            diag["classification"] = draw(st.sampled_from(
                ("COMMAND_EXECUTION_FAILED", "AGENT_RESULT_MISSING",
                 "RUNNER_DISK_FULL")))
        if draw(st.booleans()):
            diag["observed_at"] = T0 + draw(
                st.integers(min_value=0, max_value=10_000))
        stdout = _stream_field(draw(st.sampled_from(_STREAM_VARIANTS)),
                               draw(_texts))
        stderr = _stream_field(draw(st.sampled_from(_STREAM_VARIANTS)),
                               draw(_texts))
        if stdout is not None:
            diag["stdout"] = stdout
        if stderr is not None:
            diag["stderr"] = stderr
        # Optional persisted disk-capacity evidence (task 7.5 shape):
        # an unavailable marker or an available measurement block.
        if draw(st.booleans()):
            if draw(st.booleans()):
                diag["disk"] = {"available": False}
            else:
                diag["disk"] = {
                    "available": True,
                    "path": "/var/snap/docker/common",
                    "free_bytes": draw(st.integers(
                        min_value=0, max_value=200 * 1024 ** 3)),
                    "total_bytes": 100 * 1024 ** 3,
                }
    timeout = None
    if has_timeout:
        timeout = {"timeout_kind": draw(st.sampled_from(
            ("MAX_RUNTIME_EXCEEDED", "AGENT_HEARTBEAT_EXPIRED",
             "BUILD_PROGRESS_STALLED")))}
        if draw(st.booleans()):
            timeout["budget_ms"] = draw(
                st.integers(min_value=1, max_value=24 * 3600 * 1000))
        if draw(st.booleans()):
            timeout["budget_source"] = draw(st.sampled_from(
                ("max_runtime_hours", "target_mode_override")))
        if draw(st.booleans()):
            timeout["last_heartbeat_at"] = T0 + draw(
                st.integers(min_value=0, max_value=10_000))
        if draw(st.booleans()):
            timeout["queue_wait_ms"] = draw(
                st.integers(min_value=0, max_value=3600 * 1000))
        if draw(st.booleans()):
            timeout["execution_runtime_ms"] = draw(
                st.integers(min_value=0, max_value=24 * 3600 * 1000))
        timeout["timeout_decided_at"] = T0 + 9_000
    return diag, timeout


_CLOUDWATCH_STATES = ("missing", "empty", "populated")


class TestProperty13DiagnosticAvailability:
    """**Property 13: Build Log and Timeout Diagnostic Availability**

    **Validates: Requirements 2.2, 2.3, 2.18, 3.6**

    _For any_ persisted diagnostic field-availability combination
    (present/absent status, details, response code, streams, truncation
    flags, timeout evidence, disk blocks) crossed with any CloudWatch
    stream state (missing, empty, populated), the log envelope stays
    backward compatible (`events`/`nextToken` preserved exactly) and
    the optional diagnostic shows every available field while marking
    unavailable evidence — the API returns useful evidence whenever any
    exists and never fabricates it."""

    _sequence = 0

    @classmethod
    def _next_job_id(cls):
        cls._sequence += 1
        return f"p13-{cls._sequence}"

    @settings(max_examples=60, deadline=None,
              suppress_health_check=[HealthCheck.too_slow])
    @given(case=_persisted_evidence(),
           stream_state=st.sampled_from(_CLOUDWATCH_STATES))
    def test_envelope_compatible_and_evidence_truthful(self, case,
                                                       stream_state):
        diag, timeout = case
        job_id = self._next_job_id()
        extra = {}
        if diag is not None:
            extra["execution_diagnostic"] = diag
        if timeout is not None:
            extra["timeout_evidence"] = timeout
        _put_job(_base_job(job_id, **extra))

        stream_messages = []
        if stream_state == "empty":
            _LOGS.create_log_stream(logGroupName=_LOG_GROUP,
                                    logStreamName=job_id)
        elif stream_state == "populated":
            stream_messages = ["step 1 ok", "step 2 failed"]
            _write_stream(job_id, stream_messages, T0 + 2_000)

        body = _logs_body(job_id)

        # Backward-compatible envelope (Req 3.6): `events`/`nextToken`
        # are preserved exactly for every stream state; `diagnostic` is
        # the ONLY optional addition and never replaces or reshapes the
        # event page.
        assert [e["message"] for e in body["events"]] == stream_messages
        if stream_state == "missing":
            assert body["nextToken"] is None
        else:
            # An existing stream keeps CloudWatch's own forward token
            # semantics untouched (a string page token).
            assert isinstance(body["nextToken"], str) and body["nextToken"]
        if diag is None and timeout is None:
            assert set(body.keys()) == {"events", "nextToken"}
            return
        assert set(body.keys()) == {"events", "nextToken", "diagnostic"}
        projected = body["diagnostic"]

        # The diagnostic is independent of CloudWatch stream existence
        # (Req 2.3): the projection equals what the persisted job alone
        # determines, regardless of the stream state generated above.
        assert projected == build_jobs.project_job_diagnostic(
            json.loads(json.dumps(_base_job(job_id, **extra),
                                  default=_decimal_default)))

        # Every available persisted field is represented; unavailable
        # evidence is explicitly marked, never fabricated (Req 2.2,
        # 2.18, and Req 2.3: no CloudWatch stream exists here at all).
        if diag is not None:
            assert projected["status"] == diag.get("status")
            assert projected["statusDetails"] == diag.get("status_details")
            assert projected["responseCode"] == diag.get("response_code")
            for stream_key in ("stdout", "stderr"):
                persisted_field = diag.get(stream_key)
                if persisted_field is None \
                        or not persisted_field.get("available"):
                    assert projected[stream_key] == {"available": False}
                else:
                    assert projected[stream_key] == {
                        "available": True,
                        "text": persisted_field.get("text") or "",
                        "truncated": bool(persisted_field.get("truncated")),
                    }
            # Disk evidence appears iff a persisted block exists —
            # unavailable markers pass through truthfully and a
            # measurement is never invented (Req 2.18, 2.23).
            if isinstance(diag.get("disk"), dict) and diag["disk"]:
                assert projected["disk"]["available"] == \
                    diag["disk"]["available"]
                if diag["disk"].get("available"):
                    assert projected["disk"]["free_bytes"] == \
                        diag["disk"]["free_bytes"]
            else:
                assert "disk" not in projected
        else:
            assert projected["stdout"] == {"available": False}
            assert projected["stderr"] == {"available": False}

        # Classification: the persisted classification, else the
        # decided timeout kind (never invented).
        expected_classification = (diag or {}).get("classification") \
            or (timeout or {}).get("timeout_kind")
        assert projected["classification"] == expected_classification

        # Timeout evidence appears iff it was decided, with available
        # fields shown and undetermined fields None (Req 2.18).
        if timeout is not None:
            assert projected["timeout"]["kind"] == timeout["timeout_kind"]
            assert projected["timeout"]["budgetMs"] == \
                timeout.get("budget_ms")
            assert projected["timeout"]["budgetSource"] == \
                timeout.get("budget_source")
            assert projected["timeout"]["lastHeartbeatAt"] == \
                timeout.get("last_heartbeat_at")
            assert projected["timing"]["queueMs"] == \
                timeout.get("queue_wait_ms")
            assert projected["timing"]["executionMs"] == \
                timeout.get("execution_runtime_ms")
        else:
            assert "timeout" not in projected

        # Nothing in the body carries a fabricated secret or canary.
        assert _CANARY_SECRET not in json.dumps(body)
