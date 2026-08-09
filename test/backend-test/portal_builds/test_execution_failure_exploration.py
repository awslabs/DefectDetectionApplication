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
BUG CONDITION EXPLORATION for build-fleet-execution-failures (task 1).

**Property 1: Bug Condition** - Terminal SSM evidence recovery and
runtime-evidence sufficiency

**Validates: Requirements 1.1, 1.2, 1.3, 1.4, 1.5, 1.9, 1.10, 1.11,
1.13, 1.14, 1.15, 1.16, 1.17, 1.18**

**CRITICAL**: every test in this file (except the two explicitly marked
boundary-preservation checks) MUST FAIL on the current (unfixed) code.
The failures ARE the result: they reproduce `isBugCondition(input)`'s two
facets from the design:

  (A) ``commandEvidenceLost`` — a dedicated AMD64 Build_Job's agent SSM
      command reaches terminal ``Failed`` before any terminal agent
      callback; the final ``GetCommandInvocation`` evidence
      (StatusDetails, ResponseCode, stdout, stderr, timestamps) exists
      but is never retrieved, never persisted as a bounded redacted
      ``execution_diagnostic``, the outcome collapses into generic
      ``AGENT_COMMAND_FAILED``, and with the CloudWatch stream missing
      the Build Log API returns only an empty page.

  (B) ``runtimeEvidenceInsufficient`` — the runtime watchdog decision
      (`build_planner.decide_runtime_timeout`) uses only status,
      ``started_at``, and one snapshotted ``max_runtime_hours``; it can
      neither renew on fresh progress below a hard ceiling, classify
      stale-heartbeat vs progress-stall vs hard-ceiling expiry, nor
      expose separate queue/provisioning/execution phase accounting.

Do NOT weaken these assertions and do NOT change production code to make
them pass. Task 10 re-runs this exact file after the fixes land, where
the same assertions must then PASS.

Encoded expected behavior (design "Correct Result Predicate"):

    result.outcome = classifyDeterministically(result.settledEvidence)
    result.execution_diagnostic IS bounded AND redacted
    everyAvailableInvocationFieldIsRepresentedOrMarkedUnavailable(result)
    buildLogShowsUsefulEvidenceWhenAnyExists(result)
    queueWaitDoesNotConsumeExecutionRuntime(result)
    provisioningDoesNotConsumeExecutionRuntime(result)
    freshProgressPreventsSoftStallTimeout(result)
    hardSafetyCeilingCannotBeExtended(result)

--------------------------------------------------------------------------
Incident being modeled (bugfix.md Introduction)
--------------------------------------------------------------------------

Dedicated AMD64 Build_Job ``06c9a7ac-6b65-49ee-acdd-db8bf6d0cc03`` on
Dedicated_Build_Server ``srv-5b214096-91a9-41b7-9d62-cc03ba205c15``:
submitted 12:02:53, started 12:02:54, failed 12:03:02 (~8s). Build
Details showed only "The build agent SSM command ended with status
'Failed' before reporting a build result." and "No log output was
recorded for this build job."

--------------------------------------------------------------------------
Safety
--------------------------------------------------------------------------

ABSOLUTE constraint, honored here: **no test launches EC2 compute, sends
a real SSM command, calls real AWS, deploys, publishes an artifact, or
starts a real build.**

* DynamoDB / CloudWatch Logs are moto-backed for the whole module
  (``mock_aws()`` started before every import that binds a boto3
  handle), with dummy credentials.
* SSM is intercepted by a module-level recording fake installed over
  ``boto3.client`` BEFORE any handler import: it serves the scripted
  final ``GetCommandInvocation`` evidence and records every retrieval
  attempt. No SSM call can leave the process.
* The repository-path contract case models both candidate clone
  locations purely inside a temporary filesystem.
* All canary "secrets" are synthetic per-test values; none is a real
  credential, and every failure message redacts them.

Run ONLY this file, from the repository root:

    python3 -m pytest \\
        test/backend-test/portal_builds/test_execution_failure_exploration.py \\
        --noconftest -q

(This run contains property-based tests and may generate/shrink
counterexamples.)
"""
import json
import logging
import os
import sys
import tempfile
import shutil
import types
import uuid
from unittest import mock

import pytest

# ---------------------------------------------------------------------------
# Environment BEFORE any import: the handlers bind boto3 clients and
# env-derived settings at import time.
# ---------------------------------------------------------------------------
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ["AWS_ACCESS_KEY_ID"] = "testing"
os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
os.environ["AWS_SECURITY_TOKEN"] = "testing"
os.environ["AWS_SESSION_TOKEN"] = "testing"

_SUFFIX = "execution-failure-explore"
_JOBS_TABLE = f"dda-portal-build-jobs-{_SUFFIX}"
_SERVERS_TABLE = f"dda-portal-build-servers-{_SUFFIX}"
_SETTINGS_TABLE = f"dda-portal-settings-{_SUFFIX}"

os.environ["BUILD_JOBS_TABLE"] = _JOBS_TABLE
os.environ["BUILD_SERVERS_TABLE"] = _SERVERS_TABLE
os.environ["SETTINGS_TABLE"] = _SETTINGS_TABLE
os.environ["BUILD_LOG_GROUP"] = f"/dda/portal-builds-{_SUFFIX}"
os.environ.pop("BUILD_REPO_DIR", None)
os.environ.pop("BUILD_ALERT_TOPIC_ARN", None)

# Import boto3 (and thus botocore/urllib3) from the test environment BEFORE
# the Lambda function directory joins sys.path.
import boto3  # noqa: E402
from botocore.exceptions import ClientError  # noqa: E402

# Some verification containers ship a python build without the _bz2 C
# extension while moto's request path imports moto.s3 -> bz2 (sibling
# shim in test_source_selection_exploration.py).
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
from hypothesis import given, settings, strategies as st, HealthCheck  # noqa: E402

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.normpath(os.path.join(_HERE, "..", "..", ".."))
_FUNCTIONS_DIR = os.path.join(
    _REPO_ROOT, "edge-cv-portal", "backend", "functions")
if _FUNCTIONS_DIR not in sys.path:
    sys.path.insert(0, _FUNCTIONS_DIR)

# ---------------------------------------------------------------------------
# Minimal stand-ins for the Lambda layer modules the handlers import.
# Audit entries are captured in-process; nothing leaves the test.
# ---------------------------------------------------------------------------
AUDIT_EVENTS = []


def _fake_shared_utils():
    module = types.ModuleType("shared_utils")

    def log_audit_event(**kwargs):
        AUDIT_EVENTS.append(kwargs)

    def create_response(status_code, body):
        return {"statusCode": status_code, "body": body}

    def get_user_from_event(event):
        return {"user_id": "explore-user", "role": "PortalAdmin"}

    module.log_audit_event = log_audit_event
    module.create_response = create_response
    module.get_user_from_event = get_user_from_event
    return module


def _fake_rbac_middleware():
    module = types.ModuleType("rbac_middleware")

    def _identity_decorator_factory(*d_args, **d_kwargs):
        def decorator(func):
            return func
        return decorator

    def super_user_only(func):
        return func

    module.require_builds_read = _identity_decorator_factory
    module.require_builds_submit = _identity_decorator_factory
    module.require_builds_cancel = _identity_decorator_factory
    module.super_user_only = super_user_only
    return module


for _module in ("build_events", "build_jobs", "build_dispatcher",
                "build_planner", "build_domain", "build_source",
                "build_fleet", "shared_utils", "rbac_middleware"):
    sys.modules.pop(_module, None)
sys.modules["shared_utils"] = _fake_shared_utils()
sys.modules["rbac_middleware"] = _fake_rbac_middleware()

# Module-scope moto: active for every import below and for the whole run,
# so no boto3 handle can ever reach a real AWS endpoint.
_MOCK = mock_aws()
_MOCK.start()

# ---------------------------------------------------------------------------
# Recording SSM fake, installed over boto3.client BEFORE any handler
# import. It scripts the final GetCommandInvocation evidence a fixed
# implementation must retrieve, and records every retrieval attempt so
# "invocation retrieval" is directly observable. On the unfixed code
# SSM_GET_CALLS stays empty — the first counterexample.
# ---------------------------------------------------------------------------

#: command_id -> scripted final invocation (the retained SSM evidence).
SSM_INVOCATIONS = {}
#: Every GetCommandInvocation kwargs observed (any client, any caller).
SSM_GET_CALLS = []

_REAL_BOTO3_CLIENT = boto3.client


class _RecordingSsm:
    """SSM client stand-in: serves scripted final invocation evidence and
    records retrieval attempts. Unknown operations delegate to the moto
    client (which can never reach real AWS)."""

    def __init__(self, inner):
        self._inner = inner

    def get_command_invocation(self, **kwargs):
        SSM_GET_CALLS.append(dict(kwargs))
        invocation = SSM_INVOCATIONS.get(kwargs.get("CommandId"))
        if invocation is not None:
            return dict(invocation)
        raise ClientError(
            {"Error": {"Code": "InvocationDoesNotExist",
                       "Message": "The command ID and instance ID you "
                                  "specified did not match any invocations."}},
            "GetCommandInvocation")

    def __getattr__(self, name):
        return getattr(self._inner, name)


def _intercepting_client(service_name, *args, **kwargs):
    inner = _REAL_BOTO3_CLIENT(service_name, *args, **kwargs)
    if service_name == "ssm":
        return _RecordingSsm(inner)
    return inner


boto3.client = _intercepting_client

_DDB = boto3.resource("dynamodb", region_name="us-east-1")

# BuildJobs with the deployed schema, GSIs included (same fixture shape as
# the sibling exploration/preservation tests, which document why the GSIs
# matter).
_DDB.create_table(
    TableName=_JOBS_TABLE,
    KeySchema=[{"AttributeName": "build_job_id", "KeyType": "HASH"}],
    AttributeDefinitions=[
        {"AttributeName": "build_job_id", "AttributeType": "S"},
        {"AttributeName": "status", "AttributeType": "S"},
        {"AttributeName": "created_at", "AttributeType": "N"},
        {"AttributeName": "server_id", "AttributeType": "S"},
        {"AttributeName": "request_id", "AttributeType": "S"},
        {"AttributeName": "request_order", "AttributeType": "N"},
    ],
    GlobalSecondaryIndexes=[
        {
            "IndexName": "status-index",
            "KeySchema": [
                {"AttributeName": "status", "KeyType": "HASH"},
                {"AttributeName": "created_at", "KeyType": "RANGE"},
            ],
            "Projection": {"ProjectionType": "ALL"},
        },
        {
            "IndexName": "server-index",
            "KeySchema": [
                {"AttributeName": "server_id", "KeyType": "HASH"},
                {"AttributeName": "created_at", "KeyType": "RANGE"},
            ],
            "Projection": {"ProjectionType": "ALL"},
        },
        {
            "IndexName": "request-index",
            "KeySchema": [
                {"AttributeName": "request_id", "KeyType": "HASH"},
                {"AttributeName": "request_order", "KeyType": "RANGE"},
            ],
            "Projection": {"ProjectionType": "ALL"},
        },
    ],
    BillingMode="PAY_PER_REQUEST",
)
for _name, _key in ((_SERVERS_TABLE, "server_id"),
                    (_SETTINGS_TABLE, "setting_key")):
    _DDB.create_table(
        TableName=_name,
        KeySchema=[{"AttributeName": _key, "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": _key, "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )

_JOBS = _DDB.Table(_JOBS_TABLE)
_SERVERS = _DDB.Table(_SERVERS_TABLE)

import build_domain  # noqa: E402
import build_source  # noqa: E402
import build_planner  # noqa: E402
import build_dispatcher  # noqa: E402
import build_events  # noqa: E402
import build_jobs  # noqa: E402

# ---------------------------------------------------------------------------
# Lambda/CloudWatch log surface capture: every log line the handlers emit
# is a durable surface the canary secrets must never reach (Req 1.4/2.10).
# ---------------------------------------------------------------------------
LOG_RECORDS = []


class _CaptureHandler(logging.Handler):
    def emit(self, record):
        try:
            LOG_RECORDS.append(record.getMessage())
        except Exception:  # pragma: no cover
            pass


logging.getLogger().addHandler(_CaptureHandler())

# ---------------------------------------------------------------------------
# Incident identities (bugfix.md) and fixture constants
# ---------------------------------------------------------------------------

INCIDENT_JOB_ID = "06c9a7ac-6b65-49ee-acdd-db8bf6d0cc03"
INCIDENT_SERVER_ID = "srv-5b214096-91a9-41b7-9d62-cc03ba205c15"
INCIDENT_INSTANCE_ID = "i-0aa11bb22cc33dd44"  # modeled, not live data

#: 2026-08-06 12:02:53 (ms epoch anchor; exact value irrelevant to logic).
T_SUBMITTED = 1_786_017_773_000
T_STARTED = T_SUBMITTED + 1_000          # 12:02:54
T_FAILED = T_SUBMITTED + 9_000           # 12:03:02 (~8s execution)

#: Design classification table: stable safe codes for a Failed invocation.
STABLE_COMMAND_FAILURE_CODES = frozenset({"COMMAND_EXECUTION_FAILED"})
#: The generic code the unfixed fallback writes (the defect, Req 1.1/1.5).
GENERIC_CODE = "AGENT_COMMAND_FAILED"

#: Post-redaction byte bounds from the design data model.
STDOUT_STDERR_LIMIT = 16 * 1024
DETAIL_FIELD_LIMIT = 4 * 1024
TOTAL_DIAGNOSTIC_LIMIT = 48 * 1024

_MS_PER_MINUTE = 60 * 1000
_MS_PER_HOUR = 60 * _MS_PER_MINUTE


def _canaries():
    """Unique synthetic secret canaries for one test case. Never real
    credentials; every failure message redacts them."""
    unique = uuid.uuid4().hex
    return {
        "aws_secret": f"wJalrXUtnFEMI/CANARY/{unique}",
        "token": f"ghp_canary{unique}",
    }


def _redact(text, canaries):
    for value in canaries.values():
        text = text.replace(value, "[CANARY-REDACTED]")
    return text


# ---------------------------------------------------------------------------
# Job / event helpers
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


def _put_dedicated_amd64_job(job_id, command_id, instance_id,
                             server_id=INCIDENT_SERVER_ID):
    """A dispatched dedicated AMD64 Build_Job carrying correlated
    job/attempt/command/instance identities, in `building`, with NO
    terminal agent callback recorded (the incident shape)."""
    attempt_id = str(uuid.uuid4())
    job = {
        "build_job_id": job_id,
        "build_target": build_domain.TARGET_AMD64,
        "execution_mode": build_domain.EXECUTION_MODE_DEDICATED,
        "status": build_domain.STATUS_BUILDING,
        "requested_by": "operator-1",
        "created_at": T_SUBMITTED,
        "started_at": T_STARTED,
        "server_id": server_id,
        "config_snapshot": {
            "arm64_instance_type": "m6g.4xlarge",
            "x86_64_instance_type": "m6i.4xlarge",
            "volume_size_gb": 100,
            "region": "us-east-1",
            "max_runtime_hours": 4,
            "use_spot_for_ephemeral": False,
        },
        # Correlated command/instance/attempt identity, as persisted by
        # the dispatcher after SendCommand.
        "ssm": {
            "command_id": command_id,
            "instance_id": instance_id,
            "log_stream": job_id,
        },
        "execution_attempt": {
            "attempt_id": attempt_id,
            "command_id": command_id,
            "instance_id": instance_id,
        },
    }
    _JOBS.put_item(Item=job)
    _SERVERS.put_item(Item={
        "server_id": server_id,
        "name": "amd64-dedicated-1",
        "instance_id": instance_id,
        "lifecycle_state": build_domain.SERVER_STATE_RUNNING,
        "cpu_architecture": build_domain.ARCH_X86_64,
        "running_build_job_id": job_id,
    })
    return job


def _register_invocation(command_id, instance_id, *, status_details,
                         response_code, stdout, stderr, canaries):
    """Script the retained final GetCommandInvocation evidence for one
    command. ``stdout``/``stderr`` are (kind, text) where kind is one of
    'present' | 'empty' | 'unavailable'. Timestamps and identities are
    always included (Req 1.2/2.2)."""
    invocation = {
        "CommandId": command_id,
        "InstanceId": instance_id,
        "Comment": f"dda-build:{command_id}",
        "DocumentName": "AWS-RunShellScript",
        "Status": "Failed",
        "StatusDetails": status_details,
        "ResponseCode": response_code,
        "ExecutionStartDateTime": "2026-08-06T12:02:54Z",
        "ExecutionEndDateTime": "2026-08-06T12:03:02Z",
    }
    for field, (kind, text) in (("StandardOutputContent", stdout),
                                ("StandardErrorContent", stderr)):
        if kind == "present":
            invocation[field] = text
        elif kind == "empty":
            invocation[field] = ""
        # 'unavailable': field genuinely absent from the provider response
    SSM_INVOCATIONS[command_id] = invocation
    return invocation


def _deliver_ssm_failure_event(command_id, instance_id):
    """The real SSM EventBridge terminal-failure shape, delivered through
    build_events.handler (the deployed rule's payload)."""
    return build_events.handler({
        "version": "0",
        "id": str(uuid.uuid4()),
        "detail-type": "EC2 Command Status-change Notification",
        "source": "aws.ssm",
        "account": "111111111111",
        "time": "2026-08-06T12:03:02Z",
        "region": "us-east-1",
        "resources": [],
        "detail": {
            "command-id": command_id,
            "document-name": "AWS-RunShellScript",
            "instance-id": instance_id,
            "requested-date-time": "2026-08-06T12:02:54Z",
            "status": "Failed",
        },
    }, None)


def _get_build_logs(job_id):
    """GET /builds/{id}/logs with the configured CloudWatch stream
    MISSING (the moto log group/stream was never created), i.e. the
    incident's 'No log output was recorded' condition."""
    response = build_jobs.get_build_logs(
        {"pathParameters": {"id": job_id}, "queryStringParameters": None},
        None)
    return response["body"]


def _get_build_detail(job_id):
    response = build_jobs.get_build(
        {"pathParameters": {"id": job_id}, "queryStringParameters": None},
        None)
    return response["body"]


# ---------------------------------------------------------------------------
# Fixed-predicate assertion helpers (design "Correct Result Predicate")
# ---------------------------------------------------------------------------

def _surfaces(job_id):
    """Every durable/API/audit/log surface the evidence and the canaries
    can reach (Req 1.4/2.10): the DynamoDB job item, the Build Log API
    page (CloudWatch stream missing), the detail API, the Audit_Log
    capture, and the captured Lambda log output."""
    return {
        "dynamodb_job_item": json.dumps(_get_job(job_id), default=str,
                                        sort_keys=True),
        "build_logs_api": json.dumps(_get_build_logs(job_id), default=str,
                                     sort_keys=True),
        "build_detail_api": json.dumps(_get_build_detail(job_id),
                                       default=str, sort_keys=True),
        "audit_log": json.dumps(AUDIT_EVENTS, default=str, sort_keys=True),
        "lambda_logs": "\n".join(LOG_RECORDS),
    }


def _field_represented(diag_field, kind):
    """design data model: a missing provider field is {available: false};
    an empty but available field is {available: true, text: ''}."""
    if not isinstance(diag_field, dict):
        return False
    if kind == "unavailable":
        return diag_field.get("available") is False
    if diag_field.get("available") is not True:
        return False
    return isinstance(diag_field.get("text"), str)


def _find_diagnostic(container):
    """The persisted/projected execution diagnostic on a job record or an
    API body, under either naming convention of the design."""
    if not isinstance(container, dict):
        return None
    for key in ("execution_diagnostic", "diagnostic"):
        value = container.get(key)
        if isinstance(value, dict):
            return value
    return None


def _diag_get(diag, *names):
    for name in names:
        if name in diag:
            return diag[name]
    return None


def assert_fixed_command_evidence_predicate(job_id, invocation, canaries,
                                            stdout_kind, stderr_kind):
    """The fixed predicate for facet (A): invocation retrieval, bounded/
    redacted persistence, stable classification, and a useful Build Log
    diagnostic instead of generic AGENT_COMMAND_FAILED plus an empty
    log. EXPECTED TO FAIL on unfixed code."""
    findings = []
    job = _get_job(job_id)
    command_id = invocation["CommandId"]

    # --- Redaction first: canary secrets must NEVER survive on any
    # durable/API/audit/log surface (Req 1.4/1.11/2.10). This must hold
    # both before and after the fix.
    surfaces = _surfaces(job_id)
    for surface, text in surfaces.items():
        for name, value in canaries.items():
            if value in text:
                findings.append(
                    f"canary '{name}' LEAKED into surface '{surface}'")

    # --- 1. Invocation retrieval (Req 1.2/2.1): the final
    # GetCommandInvocation evidence was scripted and observable; the
    # consumer must have retrieved it.
    retrieved = [c for c in SSM_GET_CALLS
                 if c.get("CommandId") == command_id]
    if not retrieved:
        findings.append(
            "final GetCommandInvocation evidence was NEVER retrieved "
            "(build_events.py consumes only the event's command-id and "
            "status; SSM_GET_CALLS is empty for this command)")

    # --- 2. Bounded, redacted, truthful persistence (Req 1.2/2.2): a
    # structured execution diagnostic on the Build_Job with every
    # available field represented and unavailable fields identified.
    diag = _find_diagnostic(job)
    if diag is None:
        findings.append(
            "no execution diagnostic was persisted on the Build_Job "
            "(terminal StatusDetails / ResponseCode / stdout / stderr "
            "and identities were discarded)")
    else:
        if _diag_get(diag, "status_details", "statusDetails") is None:
            findings.append("diagnostic lost StatusDetails")
        if _diag_get(diag, "response_code", "responseCode") \
                != invocation["ResponseCode"]:
            findings.append("diagnostic lost ResponseCode "
                            f"{invocation['ResponseCode']}")
        for field, kind in (("stdout", stdout_kind), ("stderr", stderr_kind)):
            if not _field_represented(diag.get(field), kind):
                findings.append(
                    f"diagnostic does not represent {field} "
                    f"(provider kind: {kind}) as available/empty/"
                    f"unavailable truthfully")
        for field in ("command_id", "instance_id"):
            if not (diag.get(field) or _diag_get(
                    diag, field.replace("_i", "I").replace("_", ""))):
                findings.append(f"diagnostic lost {field}")
        for field, limit in (("stdout", STDOUT_STDERR_LIMIT),
                             ("stderr", STDOUT_STDERR_LIMIT)):
            text = (diag.get(field) or {}).get("text") \
                if isinstance(diag.get(field), dict) else None
            if isinstance(text, str) \
                    and len(text.encode("utf-8")) > limit:
                findings.append(f"diagnostic {field} exceeds the "
                                f"{limit}-byte post-redaction bound")
        if len(json.dumps(diag, default=str).encode("utf-8")) \
                > TOTAL_DIAGNOSTIC_LIMIT:
            findings.append("diagnostic exceeds the 48 KiB total bound")

    # --- 3. Stable deterministic classification (Req 1.1/1.5/2.4): a
    # Failed invocation with a non-zero response code is
    # COMMAND_EXECUTION_FAILED, never the generic collapse.
    error = (job or {}).get("error") or {}
    code = error.get("code")
    if code not in STABLE_COMMAND_FAILURE_CODES:
        findings.append(
            f"outcome collapsed into generic/unstable code {code!r} "
            f"(expected one of {sorted(STABLE_COMMAND_FAILURE_CODES)})")

    # --- 4. Useful Build Log diagnostic despite the missing CloudWatch
    # stream (Req 1.3/2.3): the API page must carry the retained
    # evidence, not only an empty event list.
    logs_body = _get_build_logs(job_id)
    log_diag = _find_diagnostic(logs_body)
    if not logs_body.get("events") and log_diag is None:
        findings.append(
            "Build Log API returned ONLY an empty page "
            f"({json.dumps(logs_body, default=str)}) although useful "
            "terminal evidence exists — the portal can render nothing "
            "but 'No log output was recorded for this build job.'")

    assert not findings, _redact(
        "COMMAND EVIDENCE LOST (isBugCondition facet A, Property 1)\n"
        f"  job                : {job_id}\n"
        f"  command / instance : {command_id} / "
        f"{invocation['InstanceId']}\n"
        f"  scripted invocation: Status=Failed, "
        f"StatusDetails={invocation['StatusDetails']!r}, "
        f"ResponseCode={invocation['ResponseCode']}, "
        f"stdout={stdout_kind}, stderr={stderr_kind}\n"
        f"  observed job error : {json.dumps(error, default=str)}\n"
        f"  observed job keys  : {sorted((job or {}).keys())}\n"
        f"  observed logs body : "
        f"{json.dumps(logs_body, default=str)}\n"
        "  failed fixed-predicate clauses:\n"
        + "\n".join(f"    - {f}" for f in findings), canaries)


# ===========================================================================
# Facet A — Terminal SSM evidence recovery (Req 1.1, 1.2, 1.3, 1.4, 1.5,
# 1.10, 1.11). EXPECTED TO FAIL on unfixed code.
# ===========================================================================

class TestTerminalSsmEvidenceRecovery:
    """`commandEvidenceLost` — the incident shape: terminal SSM `Failed`
    before any agent callback, final invocation evidence available but
    lost, CloudWatch stream missing."""

    def setup_method(self):
        _clear_state()

    def test_incident_dedicated_amd64_failed_before_callback(self):
        """The observed 2026-08-06 dedicated AMD64 failure, modeled
        exactly: ~8s execution, SSM `Failed`, ResponseCode 127, stderr
        naming the missing agent path, empty-but-available stdout, no
        callback, no CloudWatch stream.

        EXPECTED COUNTEREXAMPLE on unfixed code: the job carries only
        `AGENT_COMMAND_FAILED` / "The build agent SSM command ended with
        status 'Failed' before reporting a build result.", no
        execution diagnostic, and the Build Log API returns
        {'events': [], 'nextToken': None}.
        """
        canaries = _canaries()
        command_id = str(uuid.uuid4())
        _put_dedicated_amd64_job(INCIDENT_JOB_ID, command_id,
                                 INCIDENT_INSTANCE_ID)
        invocation = _register_invocation(
            command_id, INCIDENT_INSTANCE_ID,
            status_details="Failed",
            response_code=127,
            stdout=("empty", ""),
            stderr=("present",
                    "bash: /opt/dda/DefectDetectionApplication/scripts/"
                    "portal-build-agent.sh: No such file or directory\n"
                    f"AWS_SECRET_ACCESS_KEY={canaries['aws_secret']}\n"
                    f"remote: token {canaries['token']}"),
            canaries=canaries)

        _deliver_ssm_failure_event(command_id, INCIDENT_INSTANCE_ID)

        assert_fixed_command_evidence_predicate(
            INCIDENT_JOB_ID, invocation, canaries,
            stdout_kind="empty", stderr_kind="present")

    # Hypothesis: generated invocation-field variants. Available-empty
    # and unavailable stdout/stderr are both generated, but at least one
    # useful status/detail/output field is ALWAYS present, so every
    # generated case proves currently available diagnostics are lost
    # (Req 1.10/1.11/2.11).
    @settings(max_examples=15, deadline=None,
              suppress_health_check=list(HealthCheck))
    @given(
        response_code=st.integers(min_value=1, max_value=255),
        status_details=st.sampled_from(
            ["Failed", "Failed: exit status 127", "NonZeroExitCode"]),
        stdout_kind=st.sampled_from(["present", "empty", "unavailable"]),
        stderr_kind=st.sampled_from(["present", "empty", "unavailable"]),
        output_noise=st.text(min_size=0, max_size=200),
        secret_in_details=st.booleans(),
    )
    def test_property_generated_invocation_evidence_is_preserved(
            self, response_code, status_details, stdout_kind, stderr_kind,
            output_noise, secret_in_details):
        """**Property 1: Bug Condition** — for ANY final Failed
        invocation carrying at least one useful status/detail/output
        field and unique secret canaries, reconciliation must retrieve
        the invocation, persist a bounded redacted diagnostic with
        available fields represented and unavailable fields identified,
        classify deterministically, and surface useful Build Log
        evidence with every canary redacted.

        EXPECTED TO FAIL on unfixed code for every generated example.
        """
        canaries = _canaries()
        job_id = str(uuid.uuid4())
        command_id = str(uuid.uuid4())
        instance_id = f"i-{uuid.uuid4().hex[:17]}"
        _put_dedicated_amd64_job(job_id, command_id, instance_id,
                                 server_id=f"srv-{uuid.uuid4()}")

        # StatusDetails is always non-empty: at least one useful field.
        details = status_details
        if secret_in_details:
            details += f" token={canaries['token']}"
        body = (f"{output_noise}\n"
                f"AWS_SECRET_ACCESS_KEY={canaries['aws_secret']}\n"
                "gdk build failed: see above")
        invocation = _register_invocation(
            command_id, instance_id,
            status_details=details,
            response_code=response_code,
            stdout=(stdout_kind, body),
            stderr=(stderr_kind, body),
            canaries=canaries)

        _deliver_ssm_failure_event(command_id, instance_id)

        assert_fixed_command_evidence_predicate(
            job_id, invocation, canaries,
            stdout_kind=stdout_kind, stderr_kind=stderr_kind)


# ===========================================================================
# Repository-path contract (Req 1.9, 2.8). Deterministic; EXPECTED TO
# FAIL on unfixed code. Proves the contract defect, not historical
# causation.
# ===========================================================================

class TestRepositoryPathContract:
    """The dedicated dispatcher's effective/default repository and
    agent-script path versus the fleet bootstrap/registration clone
    path, modeled in a temporary filesystem."""

    def setup_method(self):
        _clear_state()
        self.root = tempfile.mkdtemp(prefix="dda-exec-failure-explore-")
        # The fleet bootstrap/registration clone (the tree that actually
        # exists on the server and carries the agent script).
        self.registered_clone = os.path.join(
            self.root, "home", "ubuntu", "DefectDetectionApplication")
        os.makedirs(os.path.join(self.registered_clone, "scripts"))
        for name, rel in (("portal-build-agent.sh", "scripts"),
                          ("portal-build.sh", "")):
            path = os.path.join(self.registered_clone, rel, name)
            with open(path, "w") as handle:
                handle.write("#!/bin/bash\n# inert stub — never a build\n"
                             "exit 0\n")
            os.chmod(path, 0o755)
        # The dispatcher's historical default: the directory exists (the
        # clone there once succeeded) but carries NO agent script —
        # matching the live inspection recorded for the 127 failures.
        self.dispatcher_default = os.path.join(
            self.root, "opt", "dda", "DefectDetectionApplication")
        os.makedirs(self.dispatcher_default)

    def teardown_method(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_preflight_selects_registered_clone_and_finds_script(self):
        """A legacy Dedicated_Build_Server record (registered before
        clone-path persistence, like the incident's
        srv-5b214096-...cc03ba205c15: it carries NO repo_dir field)
        plus a dispatcher whose effective/default repository is pinned
        to the historical `/opt/dda/DefectDetectionApplication`.

        Fixed predicate (design "AMD64 Command Preflight", Req 2.8): a
        preflight before costly work selects the repository directory
        where the registered clone and `scripts/portal-build-agent.sh`
        actually exist, and the effective agent-script path exists.

        EXPECTED COUNTEREXAMPLE on unfixed code: resolution lands on the
        dispatcher default (`/opt/dda/...`), the script does not exist
        there, no preflight detects it, and the costly SSM command would
        be sent anyway — reaching exit 127 only after dispatch.
        """
        job = {
            "build_job_id": INCIDENT_JOB_ID,
            "build_target": build_domain.TARGET_AMD64,
            "execution_mode": build_domain.EXECUTION_MODE_DEDICATED,
            "status": build_domain.STATUS_QUEUED,
            "server_id": INCIDENT_SERVER_ID,
            "config_snapshot": {"max_runtime_hours": 4},
        }
        legacy_server = {
            "server_id": INCIDENT_SERVER_ID,
            "name": "amd64-dedicated-1",
            "instance_id": INCIDENT_INSTANCE_ID,
            "cpu_architecture": build_domain.ARCH_X86_64,
            # NO repo_dir: registered before clone-path persistence.
        }

        with mock.patch.object(build_dispatcher, "BUILD_REPO_DIR",
                               self.dispatcher_default):
            effective_dir = build_source.resolve_repo_dir(
                job, legacy_server,
                env_default=build_dispatcher.BUILD_REPO_DIR)
            effective_script = build_source.agent_script_path(effective_dir)

        registered_script = os.path.join(
            self.registered_clone, "scripts", "portal-build-agent.sh")
        preflight_seam = any(
            hasattr(build_dispatcher, name) for name in
            ("preflight_agent_contract", "agent_preflight",
             "decide_preflight", "preflight"))

        assert (effective_dir == self.registered_clone
                and os.path.isfile(effective_script)
                and preflight_seam), (
            "REPOSITORY-PATH CONTRACT MISMATCH (isBugCondition facet A, "
            "Req 1.9/2.8)\n"
            f"  dispatcher effective repo dir : {effective_dir}\n"
            f"  effective agent script        : {effective_script}\n"
            f"  effective script exists       : "
            f"{os.path.isfile(effective_script)}\n"
            f"  registered clone (has script) : {self.registered_clone}\n"
            f"  registered script exists      : "
            f"{os.path.isfile(registered_script)}\n"
            f"  preflight seam in dispatcher  : {preflight_seam}\n"
            "  expected (Req 2.8): preflight validates the repository/"
            "script contract BEFORE costly work and selects the "
            "registered clone that actually carries "
            "scripts/portal-build-agent.sh.\n"
            "  This case proves the contract defect only; historical "
            "causation is task 3's read-only evidence question.")


# ===========================================================================
# Facet B — Runtime-evidence sufficiency (Req 1.13-1.18). Deterministic
# cases (a)-(f); EXPECTED TO FAIL on unfixed code except the two strict-
# boundary preservation checks (Req 3.12).
# ===========================================================================

#: Explicitly snapshotted target/mode budgets used by the runtime cases.
#: These live in the JOB'S OWN config snapshot (design: snapshotted,
#: never current config) — no production timeout value is asserted or
#: changed anywhere in this file.
RUNTIME_BUDGETS = {
    "AMD64": {
        "dedicated": {
            "heartbeat_lease_minutes": 30,
            "progress_stall_minutes": 60,
            "hard_runtime_hours": 8,
        },
    },
}

T0 = 1_786_017_773_000  # arbitrary ms-epoch anchor


def _runtime_job(status, *, created_at=T0, dispatched_at=None,
                 started_at=None, execution_started_at=None,
                 last_heartbeat_at=None, last_progress_at=None,
                 max_runtime_hours=4, budgets=None):
    job = {
        "build_job_id": str(uuid.uuid4()),
        "build_target": build_domain.TARGET_AMD64,
        "execution_mode": build_domain.EXECUTION_MODE_DEDICATED,
        "status": status,
        "created_at": created_at,
        "config_snapshot": {"max_runtime_hours": max_runtime_hours},
    }
    if budgets is not None:
        job["config_snapshot"]["runtime_budgets"] = budgets
    if dispatched_at is not None:
        job["dispatched_at"] = dispatched_at
    if started_at is not None:
        job["started_at"] = started_at
    timing = {}
    for key, value in (("execution_started_at", execution_started_at),
                       ("last_heartbeat_at", last_heartbeat_at),
                       ("last_progress_at", last_progress_at)):
        if value is not None:
            timing[key] = value
    if timing:
        job["timing"] = timing
    return job


def _decision_evidence(decision):
    """The evidence-rich diagnostic the fixed decision must expose
    (design: phase, observed durations, budget + source, last activity,
    target/mode). None on the current TimeoutDecision."""
    for attr in ("evidence", "diagnostic", "timing"):
        value = getattr(decision, attr, None)
        if isinstance(value, dict):
            return value
    return None


def _classification(decision):
    """The stable timeout classification carried by a decision, under
    either a dedicated field or embedded in the error text."""
    for attr in ("classification", "error_code", "timeout_kind"):
        value = getattr(decision, attr, None)
        if isinstance(value, str):
            return value
    return decision.error or ""


class TestRuntimeEvidenceSufficiency:
    """`runtimeEvidenceInsufficient` — the current single
    started_at + max_runtime_hours model cannot represent phase
    accounting, leases, or hard-ceiling semantics (Req 1.13-1.18)."""

    # ---- (a) active beyond a soft interval, fresh progress, below the
    # snapshotted hard ceiling: must CONTINUE. -----------------------------
    def test_a_active_fresh_progress_below_hard_ceiling_continues(self):
        """5h of active execution with heartbeats/progress seconds old
        and an explicitly snapshotted 8h hard ceiling: the fixed model
        renews the soft leases and continues (Req 1.16/2.16).

        EXPECTED COUNTEREXAMPLE on unfixed code: decide_runtime_timeout
        fails the job at wall-clock 5h > max_runtime_hours=4, ignoring
        fresh progress and the job's own snapshotted hard budget.
        """
        now = T0 + 5 * _MS_PER_HOUR
        job = _runtime_job(
            build_domain.STATUS_BUILDING,
            started_at=T0, execution_started_at=T0,
            last_heartbeat_at=now - _MS_PER_MINUTE,
            last_progress_at=now - 2 * _MS_PER_MINUTE,
            budgets=RUNTIME_BUDGETS)
        decision = build_planner.decide_runtime_timeout(job, now)
        assert not decision.timed_out, (
            "ACTIVE PROGRESS IGNORED (isBugCondition facet B, Req 1.16)\n"
            f"  decision: {decision}\n"
            "  observed: the job is failed on elapsed wall clock alone "
            "(started_at + max_runtime_hours) although heartbeat and "
            "meaningful progress are minutes fresh and the job's own "
            "snapshotted hard ceiling (8h, runtime_budgets.AMD64."
            "dedicated) is not crossed.\n"
            "  anchor note: only created_at/started_at exist in the "
            "current model; no heartbeat, progress, or hard/soft "
            "distinction is read at all.")

    def test_a_boundary_now_equals_legacy_deadline_not_expired(self):
        """PRESERVATION (Req 3.12, must PASS unfixed): at exactly
        now == started_at + max_runtime_hours the strict boundary keeps
        the job running."""
        now = T0 + 4 * _MS_PER_HOUR
        job = _runtime_job(build_domain.STATUS_BUILDING, started_at=T0)
        decision = build_planner.decide_runtime_timeout(job, now)
        assert not decision.timed_out, (
            f"strict `now == deadline` boundary regressed: {decision}")

    # ---- (b) heartbeat-fresh but progress-stalled ------------------------
    def test_b_progress_stall_is_classified(self):
        """Heartbeats seconds old but no meaningful progress for 2h
        against a snapshotted 60-minute progress-stall budget: the fixed
        model classifies BUILD_PROGRESS_STALLED with the last observed
        evidence (Req 1.16/2.16).

        EXPECTED COUNTEREXAMPLE on unfixed code: no stall concept exists;
        the decision is a silent CONTINUE until the global wall clock.
        """
        now = T0 + 3 * _MS_PER_HOUR
        job = _runtime_job(
            build_domain.STATUS_BUILDING,
            started_at=T0, execution_started_at=T0,
            last_heartbeat_at=now - _MS_PER_MINUTE,
            last_progress_at=now - 2 * _MS_PER_HOUR,
            budgets=RUNTIME_BUDGETS)
        decision = build_planner.decide_runtime_timeout(job, now)
        assert decision.timed_out and \
            "BUILD_PROGRESS_STALLED" in _classification(decision), (
            "PROGRESS STALL NOT DISTINGUISHED (facet B, Req 1.16)\n"
            f"  decision: {decision}\n"
            "  observed: liveness-without-progress cannot be classified; "
            "the model has no last_progress_at/progress lease input.")

    # ---- (c) stale heartbeat ---------------------------------------------
    def test_c_stale_heartbeat_is_classified(self):
        """No heartbeat for 2h against a snapshotted 30-minute heartbeat
        lease, well below the wall-clock limit: the fixed model
        classifies AGENT_HEARTBEAT_EXPIRED (Req 1.16/2.16).

        EXPECTED COUNTEREXAMPLE on unfixed code: a hung/lost agent is
        invisible until the 4h global limit, then reported with the same
        undifferentiated message as every other timeout.
        """
        now = T0 + 3 * _MS_PER_HOUR
        job = _runtime_job(
            build_domain.STATUS_BUILDING,
            started_at=T0, execution_started_at=T0,
            last_heartbeat_at=now - 2 * _MS_PER_HOUR,
            last_progress_at=now - 2 * _MS_PER_HOUR,
            budgets=RUNTIME_BUDGETS)
        decision = build_planner.decide_runtime_timeout(job, now)
        assert decision.timed_out and \
            "AGENT_HEARTBEAT_EXPIRED" in _classification(decision), (
            "STALE HEARTBEAT NOT DISTINGUISHED (facet B, Req 1.16)\n"
            f"  decision: {decision}\n"
            "  observed: no heartbeat lease exists in the decision "
            "inputs; hung executions and healthy ones are identical "
            "until the global wall clock.")

    # ---- (d) queued behind an occupied server ----------------------------
    def test_d_queue_wait_is_accounted_separately(self):
        """Six hours queued behind an occupied Dedicated_Build_Server:
        the job must remain queued (no active-runtime charge) AND the
        decision must expose separate queue-wait accounting so a later
        maximum-runtime report can prove the wait was legitimate
        (Req 1.14/1.15/2.14/2.15).

        EXPECTED COUNTEREXAMPLE on unfixed code: the job is (correctly)
        not failed, but the decision carries NO phase evidence at all —
        queue wait, budgets, and activity are unobservable, which is
        exactly the insufficiency that made the reported timeout
        undiagnosable.
        """
        now = T0 + 6 * _MS_PER_HOUR
        job = _runtime_job(build_domain.STATUS_QUEUED, created_at=T0,
                           budgets=RUNTIME_BUDGETS)
        decision = build_planner.decide_runtime_timeout(job, now)
        assert not decision.timed_out, (
            f"queued job must never fail under the active-execution "
            f"budget (Req 2.15): {decision}")
        evidence = _decision_evidence(decision)
        assert evidence is not None and \
            evidence.get("phase") == "queue_wait" and \
            evidence.get("queue_wait_ms") == 6 * _MS_PER_HOUR and \
            evidence.get("execution_runtime_ms", 0) == 0, (
            "QUEUE WAIT UNACCOUNTED (facet B, Req 1.14/1.15)\n"
            f"  decision fields: {decision._asdict()}\n"
            f"  extracted evidence: {evidence}\n"
            "  observed: TimeoutDecision exposes no phase, queue-wait "
            "duration, budget/source, or activity evidence; six hours "
            "of legitimate queueing cannot be distinguished from six "
            "hours of execution after the fact.")

    # ---- (e) provisioning delay -------------------------------------------
    def test_e_provisioning_time_is_isolated(self):
        """45 minutes in provisioning with no explicit provisioning
        budget: never charged to execution runtime, timed out only under
        an explicitly snapshotted provisioning budget, and the phase
        duration must be exposed (Req 1.14/2.14).

        EXPECTED COUNTEREXAMPLE on unfixed code: not failed (correct)
        but zero provisioning evidence is recorded or exposed.
        """
        now = T0 + 45 * _MS_PER_MINUTE
        job = _runtime_job(build_domain.STATUS_PROVISIONING, created_at=T0,
                           dispatched_at=T0, budgets=RUNTIME_BUDGETS)
        decision = build_planner.decide_runtime_timeout(job, now)
        assert not decision.timed_out, (
            f"provisioning without an explicit snapshotted budget must "
            f"not time out (Req 2.14): {decision}")
        evidence = _decision_evidence(decision)
        assert evidence is not None and \
            evidence.get("phase") == "provisioning" and \
            evidence.get("provisioning_ms") == 45 * _MS_PER_MINUTE and \
            evidence.get("execution_runtime_ms", 0) == 0, (
            "PROVISIONING TIME UNACCOUNTED (facet B, Req 1.14)\n"
            f"  decision fields: {decision._asdict()}\n"
            f"  extracted evidence: {evidence}\n"
            "  observed: no provisioning duration/budget evidence exists "
            "in the decision; a slow-provisioning failure and an "
            "execution timeout are indistinguishable.")

    # ---- (f) strict hard-ceiling expiry ------------------------------------
    def test_f_boundary_now_equals_hard_deadline_not_expired(self):
        """PRESERVATION (Req 3.12, must PASS unfixed): at exactly
        now == execution start + hard budget (here the legacy 4h
        fallback with started_at == execution_started_at) the job is
        still running."""
        now = T0 + 4 * _MS_PER_HOUR
        job = _runtime_job(build_domain.STATUS_BUILDING,
                           started_at=T0, execution_started_at=T0)
        decision = build_planner.decide_runtime_timeout(job, now)
        assert not decision.timed_out, (
            f"strict hard-ceiling boundary regressed: {decision}")

    def test_f_hard_ceiling_expiry_is_classified_with_evidence(self):
        """One millisecond past the hard deadline with NO fresher
        activity: the fixed model classifies MAX_RUNTIME_EXCEEDED and
        exposes phase, configured budget and source, observed duration,
        last heartbeat/progress, and target/mode (Req 1.18/2.18).

        EXPECTED COUNTEREXAMPLE on unfixed code: timed_out is True but
        the outcome is one undifferentiated prose string with no stable
        code and no evidence fields — the newly reported failure could
        not be diagnosed from it.
        """
        now = T0 + 4 * _MS_PER_HOUR + 1
        job = _runtime_job(
            build_domain.STATUS_BUILDING,
            started_at=T0, execution_started_at=T0,
            last_heartbeat_at=T0 + 3 * _MS_PER_HOUR,
            last_progress_at=T0 + 3 * _MS_PER_HOUR)
        decision = build_planner.decide_runtime_timeout(job, now)
        assert decision.timed_out, (
            f"strict `now > deadline` expiry regressed: {decision}")
        evidence = _decision_evidence(decision)
        assert "MAX_RUNTIME_EXCEEDED" in _classification(decision) and \
            evidence is not None and \
            evidence.get("phase") == "execution" and \
            evidence.get("budget_source") is not None and \
            evidence.get("observed_ms") == 4 * _MS_PER_HOUR + 1 and \
            evidence.get("last_heartbeat_at") == T0 + 3 * _MS_PER_HOUR and \
            evidence.get("build_target") == build_domain.TARGET_AMD64, (
            "HARD-CEILING EXPIRY WITHOUT EVIDENCE (facet B, Req 1.18)\n"
            f"  decision fields: {decision._asdict()}\n"
            f"  extracted evidence: {evidence}\n"
            "  observed: the only output is the prose string "
            f"{decision.error!r} — no stable MAX_RUNTIME_EXCEEDED code, "
            "no phase/budget/source/duration/activity/target evidence "
            "(Req 2.18).")

    def test_f_anchor_counterexample_execution_start_not_started_at(self):
        """EXACT-DEADLINE ANCHOR COUNTEREXAMPLE (Req 1.13/2.14): the
        agent's positive execution start is 30 minutes AFTER the job's
        `started_at` transition (dispatch/setup gap). At
        started_at + 4h + 1ms the active execution is only 3h30m —
        below the 4h hard budget — so the fixed model must NOT expire.

        EXPECTED COUNTEREXAMPLE on unfixed code: decide_runtime_timeout
        anchors expiry on `started_at` (recorded at the `building`
        transition), not on positive execution-start evidence, and fails
        the job 30 minutes early. RECORDED: `started_at` is
        insufficiently used as the execution anchor; `created_at` and
        `dispatched_at` are not separately accounted at all. No longer
        production timeout is asserted anywhere in this file.
        """
        execution_started = T0 + 30 * _MS_PER_MINUTE
        now = T0 + 4 * _MS_PER_HOUR + 1
        job = _runtime_job(
            build_domain.STATUS_BUILDING,
            created_at=T0 - 10 * _MS_PER_MINUTE,
            dispatched_at=T0 - _MS_PER_MINUTE,
            started_at=T0,
            execution_started_at=execution_started,
            last_heartbeat_at=now - _MS_PER_MINUTE,
            last_progress_at=now - _MS_PER_MINUTE)
        decision = build_planner.decide_runtime_timeout(job, now)
        assert not decision.timed_out, (
            "WRONG EXECUTION-RUNTIME ANCHOR (facet B, Req 1.13)\n"
            f"  created_at / dispatched_at / started_at / "
            f"execution_started_at: {job['created_at']} / "
            f"{job['dispatched_at']} / {job['started_at']} / "
            f"{execution_started}\n"
            f"  now: {now} (started_at + 4h + 1ms; active execution "
            f"only 3h30m)\n"
            f"  decision: {decision}\n"
            "  observed: expiry is computed from started_at, not from "
            "positive execution-start evidence; created_at and "
            "dispatched_at are never consulted for separate phase "
            "accounting.")
