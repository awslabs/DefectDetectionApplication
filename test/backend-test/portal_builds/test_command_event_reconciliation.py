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
Integration tests for terminal SSM event reconciliation in
``edge-cv-portal/backend/functions/build_events.py``
(build-fleet-execution-failures task 5.1).

**Validates: Requirements 2.1, 2.2, 2.4, 2.6, 2.10, 3.1**

The consumer now routes every terminal SSM command status (Success
included), resolves the correlated attempt/command/instance identity,
retrieves the final invocation READ-ONLY, sanitizes it immediately
through ``build_reconciliation``, and delegates classification to
``classify_attempt`` instead of the generic fallback:

- a Failed invocation with retained stderr/status details settles to the
  stable ``COMMAND_EXECUTION_FAILED`` code with a bounded, redacted
  ``execution_diagnostic`` persisted on the Build_Job (Req 2.1, 2.2);
- ``InvocationDoesNotExist`` is eventual consistency: a bounded
  retry/settlement state is persisted and the job stays nonterminal
  (Req 2.5 wiring; the scheduled tick re-drives it);
- ``Success`` without a terminal agent result stays nonterminal through
  the settlement window (AGENT_RESULT_MISSING is the dispatcher's
  post-settlement decision, Req 2.4);
- duplicate and callback-first deliveries converge without duplicate
  side effects, and a terminal job's outcome is never resurrected —
  late command evidence only increases diagnostic completeness
  (Req 2.6, 3.1);
- no raw invocation content or secret canary reaches the job item, the
  Audit_Log capture, or the Lambda log capture (Req 2.10).

Everything is moto/fake backed — no live AWS. Run from the repo root:

    PYTHONPATH=src/backend:test/backend-test python3 -m pytest \
        test/backend-test/portal_builds/test_command_event_reconciliation.py \
        --noconftest -q
"""
import json
import logging
import os
import sys
import types
import uuid

# ---------------------------------------------------------------------------
# Environment BEFORE any import (the handlers bind boto3 at import time).
# ---------------------------------------------------------------------------
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ["AWS_ACCESS_KEY_ID"] = "testing"
os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
os.environ["AWS_SECURITY_TOKEN"] = "testing"
os.environ["AWS_SESSION_TOKEN"] = "testing"

_SUFFIX = "cmd-event-reconcile"
_JOBS_TABLE = f"dda-portal-build-jobs-{_SUFFIX}"
_SERVERS_TABLE = f"dda-portal-build-servers-{_SUFFIX}"
os.environ["BUILD_JOBS_TABLE"] = _JOBS_TABLE
os.environ["BUILD_SERVERS_TABLE"] = _SERVERS_TABLE

import boto3  # noqa: E402
from botocore.exceptions import ClientError  # noqa: E402

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

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.normpath(os.path.join(_HERE, "..", "..", ".."))
_FUNCTIONS_DIR = os.path.join(
    _REPO_ROOT, "edge-cv-portal", "backend", "functions")
if _FUNCTIONS_DIR not in sys.path:
    sys.path.insert(0, _FUNCTIONS_DIR)

# ---------------------------------------------------------------------------
# Fake shared_utils capturing Audit_Log entries.
# ---------------------------------------------------------------------------
AUDIT_EVENTS = []


def _fake_shared_utils():
    module = types.ModuleType("shared_utils")

    def log_audit_event(**kwargs):
        AUDIT_EVENTS.append(kwargs)

    module.log_audit_event = log_audit_event
    return module


for _module in ("build_events", "build_domain", "build_reconciliation",
                "shared_utils"):
    sys.modules.pop(_module, None)
sys.modules["shared_utils"] = _fake_shared_utils()

_MOCK = mock_aws()
_MOCK.start()

# ---------------------------------------------------------------------------
# Recording fake SSM installed over boto3.client BEFORE the handler
# import: scripted final GetCommandInvocation evidence + retrieval
# observability. Unknown operations delegate to moto (never real AWS).
# ---------------------------------------------------------------------------
SSM_INVOCATIONS = {}
SSM_GET_CALLS = []

_REAL_BOTO3_CLIENT = boto3.client


class _FakeSsm:
    def __init__(self, inner):
        self._inner = inner

    def get_command_invocation(self, **kwargs):
        SSM_GET_CALLS.append(dict(kwargs))
        invocation = SSM_INVOCATIONS.get(kwargs.get("CommandId"))
        if invocation is not None:
            return dict(invocation)
        raise ClientError(
            {"Error": {"Code": "InvocationDoesNotExist",
                       "Message": "no such invocation"}},
            "GetCommandInvocation")

    def __getattr__(self, name):
        return getattr(self._inner, name)


def _intercepting_client(service_name, *args, **kwargs):
    inner = _REAL_BOTO3_CLIENT(service_name, *args, **kwargs)
    if service_name == "ssm":
        return _FakeSsm(inner)
    return inner


boto3.client = _intercepting_client

_DDB = boto3.resource("dynamodb", region_name="us-east-1")
for _name, _key in ((_JOBS_TABLE, "build_job_id"),
                    (_SERVERS_TABLE, "server_id")):
    _DDB.create_table(
        TableName=_name,
        KeySchema=[{"AttributeName": _key, "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": _key, "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
_JOBS = _DDB.Table(_JOBS_TABLE)
_SERVERS = _DDB.Table(_SERVERS_TABLE)

import build_domain  # noqa: E402
import build_reconciliation  # noqa: E402
import build_events  # noqa: E402

# The handlers above captured their (fake-wrapped) clients at import;
# restore the real factory so OTHER test modules collected in the same
# pytest process get untouched moto clients.
boto3.client = _REAL_BOTO3_CLIENT

# Captured Lambda log output: a durable surface no raw invocation
# content or canary may ever reach (Req 2.10).
LOG_RECORDS = []


class _CaptureHandler(logging.Handler):
    def emit(self, record):
        try:
            LOG_RECORDS.append(record.getMessage())
        except Exception:  # pragma: no cover
            pass


logging.getLogger().addHandler(_CaptureHandler())

NOW = 1_786_017_773_000


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _clear_state():
    for item in _JOBS.scan().get("Items", []):
        _JOBS.delete_item(Key={"build_job_id": item["build_job_id"]})
    for item in _SERVERS.scan().get("Items", []):
        _SERVERS.delete_item(Key={"server_id": item["server_id"]})
    del AUDIT_EVENTS[:]
    del LOG_RECORDS[:]
    del SSM_GET_CALLS[:]
    SSM_INVOCATIONS.clear()


def _get_job(job_id):
    return build_events.to_native(
        _JOBS.get_item(Key={"build_job_id": job_id}).get("Item"))


def _seed_job(job_id, command_id, instance_id, status="building",
              execution_mode="dedicated", server_id="srv-1", **extra):
    attempt_id = str(uuid.uuid4())
    job = {
        "build_job_id": job_id,
        "build_target": build_domain.TARGET_AMD64,
        "execution_mode": execution_mode,
        "status": status,
        "requested_by": "operator-1",
        "created_at": NOW - 10_000,
        "started_at": NOW - 9_000,
        "config_snapshot": {"max_runtime_hours": 4},
        "ssm": {"command_id": command_id, "instance_id": instance_id},
        "execution_attempt": {
            "attempt_id": attempt_id,
            "command_id": command_id,
            "instance_id": instance_id,
        },
    }
    if execution_mode == "dedicated":
        job["server_id"] = server_id
        _SERVERS.put_item(Item={
            "server_id": server_id,
            "instance_id": instance_id,
            "lifecycle_state": "running",
            "running_build_job_id": job_id,
        })
    job.update(extra)
    _JOBS.put_item(Item=job)
    return job


def _script_invocation(command_id, instance_id, status="Failed",
                       response_code=127, stderr=None, stdout=""):
    invocation = {
        "CommandId": command_id,
        "InstanceId": instance_id,
        "Status": status,
        "StatusDetails": status,
        "ResponseCode": response_code,
        "ExecutionStartDateTime": "2026-08-06T17:02:55Z",
        "ExecutionEndDateTime": "2026-08-06T17:02:58Z",
        "StandardOutputContent": stdout,
    }
    if stderr is not None:
        invocation["StandardErrorContent"] = stderr
    SSM_INVOCATIONS[command_id] = invocation
    return invocation


def _deliver(command_id, instance_id, status):
    return build_events.handler({
        "detail-type": "EC2 Command Status-change Notification",
        "source": "aws.ssm",
        "detail": {
            "command-id": command_id,
            "instance-id": instance_id,
            "status": status,
        },
    }, None)


# ---------------------------------------------------------------------------
# Terminal Failed reconciliation (Req 2.1, 2.2, 2.4)
# ---------------------------------------------------------------------------

class TestFailedCommandReconciliation:

    def setup_method(self):
        _clear_state()

    def test_failed_invocation_settles_to_stable_code_with_diagnostic(self):
        command_id = str(uuid.uuid4())
        canary = f"wJalrXUtnFEMI/CANARY/{uuid.uuid4().hex}"
        _seed_job("job-1", command_id, "i-abc123")
        _script_invocation(
            command_id, "i-abc123",
            stderr=("bash: /opt/dda/DefectDetectionApplication/scripts/"
                    "portal-build-agent.sh: No such file or directory\n"
                    f"AWS_SECRET_ACCESS_KEY={canary}"))

        _deliver(command_id, "i-abc123", "Failed")

        # The final invocation was retrieved (Req 2.1).
        assert [c for c in SSM_GET_CALLS
                if c.get("CommandId") == command_id]

        job = _get_job("job-1")
        assert job["status"] == build_domain.STATUS_FAILED
        assert job["error"]["code"] == \
            build_reconciliation.CODE_COMMAND_EXECUTION_FAILED
        assert job["ended_at"]

        # Bounded, redacted, truthful diagnostic persisted (Req 2.2).
        diag = job["execution_diagnostic"]
        assert diag["status"] == "Failed"
        assert diag["status_details"] == "Failed"
        assert diag["response_code"] == 127
        assert diag["command_id"] == command_id
        assert diag["instance_id"] == "i-abc123"
        assert diag["stdout"] == {"available": True, "text": "",
                                  "truncated": False, "original_bytes": 0}
        assert diag["stderr"]["available"] is True
        assert "portal-build-agent.sh" in diag["stderr"]["text"]
        assert diag["classification"] == \
            build_reconciliation.CODE_COMMAND_EXECUTION_FAILED
        assert diag["complete"] is True

        # Redaction on every application-controlled surface (Req 2.10).
        surfaces = {
            "job_item": json.dumps(job, default=str),
            "audit": json.dumps(AUDIT_EVENTS, default=str),
            "logs": "\n".join(LOG_RECORDS),
        }
        for name, text in surfaces.items():
            assert canary not in text, f"canary leaked into {name}"
        # Raw invocation content is never logged (Req 2.10).
        assert "No such file or directory" not in surfaces["logs"]

        # Exactly one audit; the dedicated allocation was released.
        failures = [a for a in AUDIT_EVENTS
                    if a["action"] == "build_failed"]
        assert len(failures) == 1
        assert failures[0]["details"]["error_code"] == \
            build_reconciliation.CODE_COMMAND_EXECUTION_FAILED
        server = build_events.to_native(
            _SERVERS.get_item(Key={"server_id": "srv-1"}).get("Item"))
        assert "running_build_job_id" not in server

    def test_timed_out_and_cancelled_classifications(self):
        for status, expected_status, expected_code in (
                ("TimedOut", build_domain.STATUS_FAILED,
                 build_reconciliation.CODE_COMMAND_TIMED_OUT),
                ("Cancelled", build_domain.STATUS_INTERRUPTED,
                 build_reconciliation.CODE_COMMAND_CANCELLED)):
            _clear_state()
            command_id = str(uuid.uuid4())
            _seed_job("job-t", command_id, "i-abc123")
            _script_invocation(command_id, "i-abc123", status=status,
                               response_code=1, stderr="")

            _deliver(command_id, "i-abc123", status)

            job = _get_job("job-t")
            assert job["status"] == expected_status, status
            assert job["execution_diagnostic"]["classification"] == \
                expected_code
            if expected_status == build_domain.STATUS_FAILED:
                assert job["error"]["code"] == expected_code
            else:
                # Interrupted keeps its existing shape: no error record
                # (preservation Req 3.8); the code lives in the
                # diagnostic.
                assert "error" not in job

    def test_duplicate_delivery_is_a_noop(self):
        command_id = str(uuid.uuid4())
        _seed_job("job-2", command_id, "i-abc123")
        _script_invocation(command_id, "i-abc123", stderr="boom")

        _deliver(command_id, "i-abc123", "Failed")
        first = _get_job("job-2")
        _deliver(command_id, "i-abc123", "Failed")
        second = _get_job("job-2")

        assert second["status"] == build_domain.STATUS_FAILED
        assert second["ended_at"] == first["ended_at"]
        assert second["execution_diagnostic"] == \
            first["execution_diagnostic"]
        assert len([a for a in AUDIT_EVENTS
                    if a["action"] == "build_failed"]) == 1, \
            "duplicate delivery must not duplicate audit side effects"


# ---------------------------------------------------------------------------
# Success routing, settlement state, eventual consistency (Req 2.4, 2.5)
# ---------------------------------------------------------------------------

class TestSuccessAndEventualConsistency:

    def setup_method(self):
        _clear_state()

    def test_success_without_callback_stays_nonterminal_with_settlement(self):
        command_id = str(uuid.uuid4())
        _seed_job("job-3", command_id, "i-abc123")
        _script_invocation(command_id, "i-abc123", status="Success",
                           response_code=0, stderr="")

        _deliver(command_id, "i-abc123", "Success")

        job = _get_job("job-3")
        # Not failed, not succeeded: the in-flight agent result gets its
        # settlement window (Req 2.4; the dispatcher settles it).
        assert job["status"] == build_domain.STATUS_BUILDING
        recon = job["reconciliation"]
        assert recon["command_status"] == "Success"
        assert recon["settlement_deadline"] > recon["first_observed_at"]
        assert AUDIT_EVENTS == []

    def test_invocation_does_not_exist_is_eventual_consistency(self):
        command_id = str(uuid.uuid4())
        _seed_job("job-4", command_id, "i-abc123")
        # No scripted invocation: GetCommandInvocation raises
        # InvocationDoesNotExist.

        _deliver(command_id, "i-abc123", "Failed")

        job = _get_job("job-4")
        assert job["status"] == build_domain.STATUS_BUILDING, \
            "eventual consistency must not fabricate a command failure"
        recon = job["reconciliation"]
        assert recon["lookup_state"] == build_reconciliation.LOOKUP_PENDING
        assert recon["command_status"] == "Failed"
        assert recon["first_observed_at"]
        assert AUDIT_EVENTS == []


# ---------------------------------------------------------------------------
# Convergence and terminal absorption (Req 2.6, 3.1)
# ---------------------------------------------------------------------------

class TestOrderingConvergence:

    def setup_method(self):
        _clear_state()

    def test_callback_first_terminal_job_is_never_resurrected(self):
        """A valid correlated callback already recorded the outcome; the
        later command event only increases diagnostic completeness."""
        command_id = str(uuid.uuid4())
        _seed_job("job-5", command_id, "i-abc123",
                  status=build_domain.STATUS_SUCCEEDED,
                  ended_at=NOW - 1_000,
                  result={"component_name": "aws.edgeml.dda.LocalServer"})
        _script_invocation(command_id, "i-abc123", status="Success",
                           response_code=0, stderr="")

        _deliver(command_id, "i-abc123", "Success")

        job = _get_job("job-5")
        assert job["status"] == build_domain.STATUS_SUCCEEDED
        assert job["ended_at"] == NOW - 1_000
        assert job["result"] == {
            "component_name": "aws.edgeml.dda.LocalServer"}
        # Late diagnostics enriched the terminal record (Req 2.6).
        assert job["execution_diagnostic"]["status"] == "Success"
        assert AUDIT_EVENTS == []

    def test_stale_mismatched_command_evidence_is_rejected(self):
        """Evidence carrying a different command id than the job's
        current attempt can never affect it (Req 2.6)."""
        command_id = str(uuid.uuid4())
        stale_command = str(uuid.uuid4())
        _seed_job("job-6", command_id, "i-abc123")
        # The job's ssm marker still names the stale command (a retry
        # overwrote the attempt but not the marker).
        _JOBS.update_item(
            Key={"build_job_id": "job-6"},
            UpdateExpression="SET #s.command_id = :c",
            ExpressionAttributeNames={"#s": "ssm"},
            ExpressionAttributeValues={":c": stale_command})
        _script_invocation(stale_command, "i-abc123", stderr="stale")

        _deliver(stale_command, "i-abc123", "Failed")

        job = _get_job("job-6")
        assert job["status"] == build_domain.STATUS_BUILDING
        assert "execution_diagnostic" not in job
        assert AUDIT_EVENTS == []

    def test_normal_phase_path_is_untouched(self):
        """The apply_phase_event path stays byte-compatible: a normal
        succeeded phase event applies exactly as before (Req 3.1)."""
        command_id = str(uuid.uuid4())
        _seed_job("job-7", command_id, "i-abc123",
                  status=build_domain.STATUS_PUBLISHING)
        build_events.handler({
            "source": "dda.portal.builds",
            "detail-type": "BuildPhaseChange",
            "detail": {
                "build_job_id": "job-7",
                "phase": "succeeded",
                "result": {"component_name": "c", "published_version": "1"},
            },
        }, None)
        job = _get_job("job-7")
        assert job["status"] == build_domain.STATUS_SUCCEEDED
        assert job["result"]["published_version"] == "1"
        assert [a for a in AUDIT_EVENTS
                if a["action"] == "build_published"]
