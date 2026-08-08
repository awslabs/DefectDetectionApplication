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
BUG CONDITION EXPLORATION for build-fleet-execution-failures
(task 14 — storage-exhaustion amendment; standalone pre-fix task).

**Property 17: Bug Condition** - ENOSPC evidence collapses into generic
failure with no disk evidence

**Property 18: Bug Condition** - Head-keeping truncation drops the
trailing root cause

**Validates: Requirements 1.19, 1.20, 1.21, 1.22**

**CRITICAL**: every test in this file MUST FAIL on the current (unfixed)
code. The failures ARE the deliverable: they reproduce the
``storageEvidenceLost`` form of ``isBugCondition(input)`` from the
design. Do NOT weaken these assertions and do NOT change production code
to make them pass. Task 10.9 re-runs this exact file unchanged after the
storage fixes (tasks 4.4, 7.4, 7.5, 8.5) land, where the same assertions
must then PASS. The frozen task-1 (test_execution_failure_exploration.py)
and task-2 (test_execution_failure_preservation.py) baselines are NOT
touched by this file.

Facets reproduced (design ``storageEvidenceLost``):

  (a) ENOSPC misclassification — agent/invocation output bearing
      disk-exhaustion evidence (``no space left on device``, ENOSPC, or
      an agent-reported ``error_kind=disk``) is fed through the CURRENT
      PRODUCTION classification path (``build_events.py``'s phase and
      SSM-fallback handling — deliberately NOT the new pure
      ``build_reconciliation.py`` module, which already carries
      ``RUNNER_DISK_FULL`` but is unintegrated). Fixed predicate
      asserted: a stable ``RUNNER_DISK_FULL`` error code with disk
      evidence recorded (``enospcEvidenceClassifiesAsRunnerDiskFull``,
      ``diskEvidenceRecordedOrMarkedUnavailable``). Unfixed outcome: the
      job collapses into generic ``BUILD_FAILED`` (phase path) or
      ``AGENT_COMMAND_FAILED`` (fallback path) with no disk evidence
      anywhere on the durable record — the assertions fail.

  (b) Head-keeping truncation loss — the durable error message is
      derived by executing the agent's ACTUAL derivation pipeline
      (``scripts/portal-build-agent.sh`` line ~224:
      ``tail -n 5 "$BUILD_LOG" | head -c 512``, extracted verbatim from
      the script and run against a fixture log in a temp dir) from an
      over-length buildkit failure line whose trailing content is
      ``no space left on device`` — matching the retained
      ``bd91c5d8-ac7e-4125-becc-711860660f2e`` record whose durable
      message ends at ``write /var/snap/docker/common/``. Fixed
      predicate asserted: the bounded message preserves the trailing
      root cause (``durableMessagePreservesRootCauseTail``). Unfixed
      outcome: ``head -c 512`` keeps only the head of the line and
      drops the cause — the assertion fails.

--------------------------------------------------------------------------
Incident being modeled (bugfix.md amendment, historical-evidence.md §2.3)
--------------------------------------------------------------------------

Ephemeral JP6 Build_Job ``bd91c5d8-ac7e-4125-becc-711860660f2e``
(``aws.edgeml.dda.LocalServer.arm64JP6``, ``m6g.4xlarge``,
``config_snapshot.volume_size_gb=100``, ``max_runtime_hours=4``): ran
~100 minutes, both JP6 image exports hit ENOSPC during final layer
extraction on the single 100 GB root volume, the agent's log tee itself
hit ENOSPC, ``gdk`` exited 1, and the agent emitted a normal failed
callback. Durable error: generic ``BUILD_FAILED`` with a message cut
mid-path at ``write /var/snap/docker/common/`` — the decisive
``no space left on device`` text survived only in CloudWatch.

--------------------------------------------------------------------------
Safety
--------------------------------------------------------------------------

Fixtures and injected outputs ONLY. No live AWS resource, SSM command,
instance action, deployment, or build:

* DynamoDB is moto-backed for the whole module (``mock_aws()`` started
  before every import that binds a boto3 handle), with dummy creds.
* SSM is intercepted by a module-level recording fake installed over
  ``boto3.client`` BEFORE any handler import; it serves scripted final
  ``GetCommandInvocation`` evidence. No SSM call can leave the process.
* Facet (b) executes the agent's error-tail shell pipeline (extracted
  from the script text) against a fixture log in a temp dir — no agent
  process, build, or instance is involved.
* No fixture contains a real credential or secret-shaped value.

Run ONLY this file, from the repository root:

    PYTHONPATH=src/backend:test/backend-test python3 -m pytest \\
        test/backend-test/portal_builds/test_storage_exhaustion_exploration.py \\
        --noconftest -q

(This run contains property-based tests and may generate/shrink
counterexamples.)
"""
import os
import re
import subprocess
import sys
import types
import uuid

import pytest

# ---------------------------------------------------------------------------
# Environment BEFORE any import: build_events binds its boto3 resources
# and env-derived table names at import time.
# ---------------------------------------------------------------------------
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ["AWS_ACCESS_KEY_ID"] = "testing"
os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
os.environ["AWS_SECURITY_TOKEN"] = "testing"
os.environ["AWS_SESSION_TOKEN"] = "testing"

_SUFFIX = "storage-exhaustion-explore"
_JOBS_TABLE = f"dda-portal-build-jobs-{_SUFFIX}"
_SERVERS_TABLE = f"dda-portal-build-servers-{_SUFFIX}"
os.environ["BUILD_JOBS_TABLE"] = _JOBS_TABLE
os.environ["BUILD_SERVERS_TABLE"] = _SERVERS_TABLE

# Import boto3 (and thus botocore/urllib3) from the test environment BEFORE
# the Lambda function directory joins sys.path.
import boto3  # noqa: E402
from botocore.exceptions import ClientError  # noqa: E402

# Some verification containers ship a python build without the _bz2 C
# extension while moto's request path imports moto.s3 -> bz2 (sibling
# shim in test_execution_failure_exploration.py).
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
from hypothesis import HealthCheck, given, settings, strategies as st  # noqa: E402

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.normpath(os.path.join(_HERE, "..", "..", ".."))
_FUNCTIONS_DIR = os.path.join(
    _REPO_ROOT, "edge-cv-portal", "backend", "functions")
if _FUNCTIONS_DIR not in sys.path:
    sys.path.insert(0, _FUNCTIONS_DIR)

_AGENT_SCRIPT = os.path.join(_REPO_ROOT, "scripts", "portal-build-agent.sh")

# ---------------------------------------------------------------------------
# Minimal stand-in for the Lambda layer module build_events imports.
# Audit entries are captured in-process; nothing leaves the test.
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

# Module-scope moto: active for every import below and for the whole run,
# so no boto3 handle can ever reach a real AWS endpoint.
_MOCK = mock_aws()
_MOCK.start()

# ---------------------------------------------------------------------------
# Recording SSM fake, installed over boto3.client BEFORE any handler
# import: serves the scripted final GetCommandInvocation evidence bearing
# the ENOSPC stderr/stdout a fixed implementation must consult.
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

# BuildJobs with the deployed schema (GSIs included so a fixed
# implementation that queries an index in the task-10.9 re-run still
# finds the same fixture — sibling exploration-test shape).
_DDB.create_table(
    TableName=_JOBS_TABLE,
    KeySchema=[{"AttributeName": "build_job_id", "KeyType": "HASH"}],
    AttributeDefinitions=[
        {"AttributeName": "build_job_id", "AttributeType": "S"},
        {"AttributeName": "status", "AttributeType": "S"},
        {"AttributeName": "created_at", "AttributeType": "N"},
        {"AttributeName": "server_id", "AttributeType": "S"},
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
    ],
    BillingMode="PAY_PER_REQUEST",
)
_DDB.create_table(
    TableName=_SERVERS_TABLE,
    KeySchema=[{"AttributeName": "server_id", "KeyType": "HASH"}],
    AttributeDefinitions=[
        {"AttributeName": "server_id", "AttributeType": "S"},
    ],
    BillingMode="PAY_PER_REQUEST",
)

import build_domain  # noqa: E402
import build_events  # noqa: E402

_JOBS = _DDB.Table(_JOBS_TABLE)

# ---------------------------------------------------------------------------
# Incident constants (historical-evidence.md §2.3, all retained evidence)
# ---------------------------------------------------------------------------
INCIDENT_JOB_ID = "bd91c5d8-ac7e-4125-becc-711860660f2e"
INCIDENT_COMMAND_ID = "2068c6dc-e0a0-4abd-a66c-e410c81950f2"
INCIDENT_INSTANCE_ID = "i-0dae725c73950757f"

#: The decisive trailing root cause the durable record must preserve.
ENOSPC_CAUSE = "no space left on device"
#: Where the retained bd91c5d8 durable message was cut (mid-path).
HEAD_KEPT_END = "write /var/snap/docker/common/"

#: The stable disk-exhaustion classification the design fixes toward.
RUNNER_DISK_FULL = "RUNNER_DISK_FULL"

#: Over-length buildkit failure line modeled on the retained CloudWatch
#: evidence: the layer-extraction error wraps the snap-docker snapshot
#: write and the containerd ingest write, and the ENOSPC cause sits at
#: the very END of a line far past the 512-byte durable-message bound.
_LAYER_SHA = "265a67" + "f" * 58  # 64-hex digest, retained-record prefix
BUILDKIT_ENOSPC_LINE = (
    f"#109 ERROR: failed to extract layer sha256:{_LAYER_SHA}: "
    f"{HEAD_KEPT_END}"
    "var-lib-docker/containerd/io.containerd.snapshotter.v1.overlayfs/"
    "snapshots/384/fs/usr/local/cuda-12.6/targets/aarch64-linux/lib/"
    "libcudnn_engines_precompiled.so.9.1.0: failed to write file: "
    "write /var/snap/docker/common/var-lib-docker/containerd/"
    f"io.containerd.content.v1.content/ingest/{_LAYER_SHA}/data: "
    + ENOSPC_CAUSE
)
assert len(BUILDKIT_ENOSPC_LINE.encode("utf-8")) > 512, \
    "fixture must model the retained over-length buildkit failure line"
#: The agent's own log tee hitting ENOSPC (retained 193-byte stderr tail).
AGENT_TEE_ENOSPC_LINE = (
    f"tee: /tmp/portal-build-agent-{INCIDENT_JOB_ID}.log: "
    "No space left on device"
)


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def _seed_job(job_id, command_id=None, status="building"):
    """Seed one ephemeral JP6 Build_Job shaped like the retained
    bd91c5d8 record (100 GB snapshot, snap-docker runner)."""
    item = {
        "build_job_id": job_id,
        "status": status,
        "build_target": "JP6",
        "execution_mode": build_domain.EXECUTION_MODE_EPHEMERAL,
        "component_name": "aws.edgeml.dda.LocalServer.arm64JP6",
        "source_ref": "feature/portal-build-fleet-and-workflow-gates",
        "created_at": 1786157485576,
        "started_at": 1786157657771,
        "config_snapshot": {"volume_size_gb": 100, "max_runtime_hours": 4},
        "runner": {"instance_id": INCIDENT_INSTANCE_ID},
    }
    if command_id:
        item["ssm"] = {"command_id": command_id}
    _JOBS.put_item(Item=item)
    return item


def _get_job(job_id):
    return build_events.to_native(
        _JOBS.get_item(Key={"build_job_id": job_id}).get("Item"))


def _deliver_agent_failed_callback(job_id, error_message, error_kind=None):
    """Deliver the agent's normal failed callback (the bd91c5d8 flow)
    through the production phase-classification path."""
    detail = {"build_job_id": job_id, "phase": "failed",
              "build_target": "JP6", "error_message": error_message}
    if error_kind is not None:
        detail["error_kind"] = error_kind
    build_events.handler(
        {"source": build_events.PHASE_EVENT_SOURCE,
         "detail-type": build_events.PHASE_EVENT_DETAIL_TYPE,
         "detail": detail},
        None)


def _deliver_ssm_failed(command_id):
    """Deliver the real SSM EventBridge terminal-Failed shape through the
    production fallback-classification path."""
    build_events.handler(
        {"source": build_events.SSM_EVENT_SOURCE,
         "detail-type": build_events.SSM_COMMAND_DETAIL_TYPES[0],
         "detail": {"command-id": command_id, "status": "Failed",
                    "instance-id": INCIDENT_INSTANCE_ID,
                    "document-name": "AWS-RunShellScript"}},
        None)


def _script_invocation(command_id, stdout, stderr):
    """Script the retained final GetCommandInvocation evidence."""
    SSM_INVOCATIONS[command_id] = {
        "CommandId": command_id,
        "InstanceId": INCIDENT_INSTANCE_ID,
        "Status": "Failed",
        "StatusDetails": "Failed",
        "ResponseCode": 1,
        "StandardOutputContent": stdout,
        "StandardErrorContent": stderr,
        "ExecutionStartDateTime": "2026-08-08T02:54:17.771Z",
        "ExecutionEndDateTime": "2026-08-08T04:34:25.176Z",
    }


def _find_disk_evidence(node):
    """Locate recorded disk evidence anywhere on the durable job record:
    a ``disk`` block carrying capacity measurements or an explicit
    ``available: False`` marker (design ``execution_diagnostic.disk``,
    ``diskEvidenceRecordedOrMarkedUnavailable``). Returns None when the
    record carries no disk evidence at all — the unfixed condition."""
    if isinstance(node, dict):
        disk = node.get("disk")
        if isinstance(disk, dict) and (
                "available" in disk or "available_gb" in disk
                or "docker_storage_path" in disk):
            return disk
        for value in node.values():
            found = _find_disk_evidence(value)
            if found is not None:
                return found
    elif isinstance(node, list):
        for value in node:
            found = _find_disk_evidence(value)
            if found is not None:
                return found
    return None


# ---------------------------------------------------------------------------
# Facet (b) helpers: execute the agent's ACTUAL durable-message
# derivation pipeline, extracted verbatim from the script text.
# ---------------------------------------------------------------------------

def _agent_error_tail_pipeline():
    """Extract the exact ``ERROR_TAIL=$( ... )`` shell pipeline from
    scripts/portal-build-agent.sh (line ~224) so this test derives the
    durable message with the SAME semantics production uses — including
    after the task-7.4 fix changes the pipeline."""
    with open(_AGENT_SCRIPT, encoding="utf-8") as handle:
        for line in handle:
            match = re.match(r"\s*ERROR_TAIL=\$\((.+)\)\s*$", line)
            if match:
                return match.group(1)
    raise AssertionError(
        "ERROR_TAIL derivation not found in scripts/portal-build-agent.sh")


def _derive_durable_message(build_log_path):
    """Run the agent's error-tail pipeline against a fixture log."""
    pipeline = _agent_error_tail_pipeline()
    completed = subprocess.run(
        ["bash", "-c", pipeline],
        env={**os.environ, "BUILD_LOG": build_log_path},
        capture_output=True, timeout=30, check=False)
    return completed.stdout.decode("utf-8", errors="replace")


def _write_incident_shaped_log(tmp_path):
    """Fixture BUILD_LOG whose last five lines reproduce the retained
    bd91c5d8 shape: the first 512 bytes of ``tail -n 5`` end EXACTLY at
    ``write /var/snap/docker/common/`` (the observed durable cut), and
    the over-length final line carries ``no space left on device`` at
    its END."""
    fixed_lines = [
        "#108 exporting layers",
        "#108 exporting layers 94.9s done",
        "#109 extracting sha256:" + _LAYER_SHA,
    ]
    consumed = sum(len(line) + 1 for line in fixed_lines)
    head_len = len(BUILDKIT_ENOSPC_LINE.split(HEAD_KEPT_END)[0]) \
        + len(HEAD_KEPT_END)
    filler_len = 512 - consumed - head_len - 1  # -1: filler's newline
    assert filler_len > len("#109 "), "fixture arithmetic broke"
    filler = "#109 " + "." * (filler_len - len("#109 "))
    tail_five = fixed_lines + [filler, BUILDKIT_ENOSPC_LINE]

    stream = "".join(line + "\n" for line in tail_five)
    kept = stream.encode("utf-8")[:512].decode("utf-8")
    assert kept.endswith(HEAD_KEPT_END), \
        "fixture must reproduce the retained mid-path cut"
    assert len(BUILDKIT_ENOSPC_LINE.encode("utf-8")) > 512, \
        "the failure line must exceed the durable-message byte bound"

    log_path = os.path.join(str(tmp_path),
                            f"portal-build-agent-{INCIDENT_JOB_ID}.log")
    earlier = [f"#{i} sha256:{_LAYER_SHA} 400MB / 400MB done"
               for i in range(100, 108)]
    with open(log_path, "w", encoding="utf-8") as handle:
        for line in earlier + tail_five:
            handle.write(line + "\n")
    return log_path


# ===========================================================================
# Facet (a) — ENOSPC misclassification (Req 1.19, 1.20, 1.22)
# ===========================================================================

class TestEnospcMisclassification:
    """Fixed predicate: ``enospcEvidenceClassifiesAsRunnerDiskFull`` AND
    ``diskEvidenceRecordedOrMarkedUnavailable``. MUST FAIL unfixed."""

    def test_a1_incident_agent_callback_enospc_collapses_to_generic(self):
        """bd91c5d8 flow: the agent emits a NORMAL failed callback whose
        message carries the disk-exhaustion evidence. The production
        phase path must classify it RUNNER_DISK_FULL with disk evidence
        recorded — unfixed it records generic BUILD_FAILED and nothing
        about the disk."""
        job_id = INCIDENT_JOB_ID
        _seed_job(job_id)
        message = (BUILDKIT_ENOSPC_LINE + "\n" + AGENT_TEE_ENOSPC_LINE
                   + "\ngdk exited 1")
        _deliver_agent_failed_callback(job_id, message,
                                       error_kind="building")

        job = _get_job(job_id)
        assert job["status"] == build_domain.STATUS_FAILED  # sanity
        error = job.get("error") or {}

        failed_clauses = []
        if error.get("code") != RUNNER_DISK_FULL:
            failed_clauses.append(
                f"ENOSPC-bearing agent output collapsed into generic "
                f"'{error.get('code')}' instead of the stable "
                f"'{RUNNER_DISK_FULL}' classification "
                f"(enospcEvidenceClassifiesAsRunnerDiskFull)")
        if _find_disk_evidence(job) is None:
            failed_clauses.append(
                "no disk evidence was recorded (or marked unavailable) "
                "anywhere on the durable job record "
                "(diskEvidenceRecordedOrMarkedUnavailable)")
        assert not failed_clauses, (
            "storageEvidenceLost reproduced (Req 1.19, 1.20, 1.22):\n- "
            + "\n- ".join(failed_clauses))

    def test_a2_agent_error_kind_disk_shortcut_is_not_honored(self):
        """An agent-reported ``error_kind=disk`` (the task-7.4 local
        detection shortcut the classification must honor without pattern
        matching) must classify RUNNER_DISK_FULL — unfixed it falls into
        the generic BUILD_FAILED branch."""
        job_id = str(uuid.uuid4())
        _seed_job(job_id)
        _deliver_agent_failed_callback(
            job_id,
            "Build failed on runner; local detector reported storage "
            "exhaustion.",
            error_kind="disk")

        job = _get_job(job_id)
        assert job["status"] == build_domain.STATUS_FAILED  # sanity
        code = (job.get("error") or {}).get("code")
        assert code == RUNNER_DISK_FULL, (
            f"agent-reported error_kind=disk collapsed into generic "
            f"'{code}' instead of '{RUNNER_DISK_FULL}' (Req 1.20)")

    def test_a3_ssm_fallback_discards_enospc_invocation_evidence(self):
        """Terminal SSM Failed with no callback, while the retained final
        invocation stderr/stdout carries the ENOSPC evidence (the
        retained tee line + buildkit extract). The production fallback
        path must classify from that evidence — unfixed it never reads
        the invocation and records generic AGENT_COMMAND_FAILED with no
        disk evidence."""
        job_id = str(uuid.uuid4())
        command_id = INCIDENT_COMMAND_ID
        _seed_job(job_id, command_id=command_id)
        _script_invocation(
            command_id,
            stdout=BUILDKIT_ENOSPC_LINE + "\ntee: /tmp/gdk-build-"
                   "1786157659.log: No space left on device",
            stderr=AGENT_TEE_ENOSPC_LINE)
        _deliver_ssm_failed(command_id)

        job = _get_job(job_id)
        assert job["status"] == build_domain.STATUS_FAILED  # sanity
        error = job.get("error") or {}

        failed_clauses = []
        if error.get("code") != RUNNER_DISK_FULL:
            failed_clauses.append(
                f"ENOSPC-bearing invocation output collapsed into "
                f"generic '{error.get('code')}' instead of "
                f"'{RUNNER_DISK_FULL}' "
                f"(enospcEvidenceClassifiesAsRunnerDiskFull)")
        if _find_disk_evidence(job) is None:
            failed_clauses.append(
                "no disk evidence was recorded (or marked unavailable) "
                "anywhere on the durable job record "
                "(diskEvidenceRecordedOrMarkedUnavailable)")
        assert not failed_clauses, (
            "storageEvidenceLost reproduced (Req 1.19, 1.20, 1.22):\n- "
            + "\n- ".join(failed_clauses))

    @settings(max_examples=25, deadline=None,
              suppress_health_check=[HealthCheck.too_slow])
    @given(
        pattern=st.sampled_from([
            "no space left on device",
            "No space left on device",
            "ENOSPC",
        ]),
        channel=st.sampled_from([
            "agent_message", "agent_error_kind_disk",
            "invocation_stdout", "invocation_stderr",
        ]),
        noise_before=st.text(
            alphabet=st.characters(
                whitelist_categories=("L", "N", "P", "Zs")),
            max_size=60),
        noise_after=st.text(
            alphabet=st.characters(
                whitelist_categories=("L", "N", "P", "Zs")),
            max_size=60),
    )
    def test_property17_bug_condition_enospc_evidence_classification(
            self, pattern, channel, noise_before, noise_after):
        """**Property 17: Bug Condition** — for ANY agent error message,
        stderr, or invocation output containing disk-exhaustion patterns
        (or an agent-reported ``error_kind=disk``), the production
        classification path SHALL yield the stable ``RUNNER_DISK_FULL``
        code and never generic ``BUILD_FAILED``.

        **Validates: Requirements 1.19, 1.20**

        MUST FAIL on unfixed code: every generated case collapses into
        BUILD_FAILED (phase path) or AGENT_COMMAND_FAILED (fallback)."""
        job_id = str(uuid.uuid4())
        evidence_text = f"{noise_before}{pattern}{noise_after}"

        if channel == "agent_message":
            _seed_job(job_id)
            _deliver_agent_failed_callback(job_id, evidence_text,
                                           error_kind="building")
        elif channel == "agent_error_kind_disk":
            _seed_job(job_id)
            _deliver_agent_failed_callback(
                job_id, "build failed", error_kind="disk")
        else:
            command_id = str(uuid.uuid4())
            _seed_job(job_id, command_id=command_id)
            _script_invocation(
                command_id,
                stdout=evidence_text if channel == "invocation_stdout"
                else "build output",
                stderr=evidence_text if channel == "invocation_stderr"
                else "")
            _deliver_ssm_failed(command_id)

        job = _get_job(job_id)
        code = (job.get("error") or {}).get("code")
        assert code == RUNNER_DISK_FULL, (
            f"channel={channel!r} pattern={pattern!r}: disk-exhaustion "
            f"evidence classified as generic '{code}' instead of "
            f"'{RUNNER_DISK_FULL}' (Property 17 bug condition, "
            f"Req 1.19, 1.20)")


# ===========================================================================
# Facet (b) — Head-keeping truncation loss (Req 1.21)
# ===========================================================================

class TestHeadKeepingTruncationLoss:
    """Fixed predicate: ``durableMessagePreservesRootCauseTail``.
    MUST FAIL unfixed (``tail -n 5 | head -c 512`` keeps the head)."""

    def test_b1_incident_overlength_buildkit_line_drops_trailing_cause(
            self, tmp_path):
        """Derive the durable message via the agent's ACTUAL pipeline
        from the incident-shaped fixture log. The retained bd91c5d8
        record ends at ``write /var/snap/docker/common/``; the fixed
        derivation must preserve the trailing
        ``no space left on device`` root cause instead."""
        log_path = _write_incident_shaped_log(tmp_path)
        derived = _derive_durable_message(log_path)

        # Bound sanity (holds both before and after the fix).
        assert len(derived.encode("utf-8")) <= 512

        # Fixed predicate — FAILS on unfixed code (head-keeping cut).
        assert ENOSPC_CAUSE in derived, (
            "durableMessageDroppedTrailingCause reproduced (Req 1.21): "
            "the agent's derivation "
            f"({_agent_error_tail_pipeline()!r}) kept only the head of "
            f"the over-length buildkit failure line and dropped the "
            f"trailing root cause {ENOSPC_CAUSE!r}. Derived durable "
            f"message ends: ...{derived[-80:]!r} — matching the "
            f"retained record cut at {HEAD_KEPT_END!r}.")

    @settings(max_examples=15, deadline=None,
              suppress_health_check=[HealthCheck.too_slow])
    @given(
        overshoot=st.integers(min_value=1, max_value=1600),
        pad_char=st.sampled_from(["a", "x", "/", ".", "0"]),
        previous_lines=st.lists(
            st.text(alphabet="abcdefghij #:/.-0123456789",
                    min_size=0, max_size=40),
            min_size=0, max_size=4),
    )
    def test_property18_bug_condition_trailing_cause_survives(
            self, tmp_path_factory, overshoot, pad_char, previous_lines):
        """**Property 18: Bug Condition** — for ANY failure line
        exceeding the durable-message byte bound whose trailing content
        is the root cause, the derived bounded message SHALL contain the
        line's trailing content (the root-cause end).

        **Validates: Requirements 1.21**

        MUST FAIL on unfixed code: the head-keeping ``head -c 512``
        derivation drops the cause for every over-length line."""
        suffix = ": " + ENOSPC_CAUSE
        # The cause starts beyond the 512-byte bound: over-length line.
        prefix = ("#7 ERROR: failed to commit snapshot: write /"
                  + pad_char * (512 + overshoot))
        line = prefix + suffix
        assert len(line.encode("utf-8")) > 512

        tmp_dir = tmp_path_factory.mktemp("p18")
        log_path = os.path.join(str(tmp_dir), "build.log")
        with open(log_path, "w", encoding="utf-8") as handle:
            for previous in previous_lines:
                handle.write(previous + "\n")
            handle.write(line + "\n")

        derived = _derive_durable_message(log_path)
        assert len(derived.encode("utf-8")) <= 512  # bound sanity
        assert ENOSPC_CAUSE in derived, (
            f"over-length line (len={len(line)}, "
            f"previous_lines={len(previous_lines)}): the derivation "
            f"({_agent_error_tail_pipeline()!r}) dropped the trailing "
            f"root cause {ENOSPC_CAUSE!r} (Property 18 bug condition, "
            f"Req 1.21). Derived ends: ...{derived[-60:]!r}")
