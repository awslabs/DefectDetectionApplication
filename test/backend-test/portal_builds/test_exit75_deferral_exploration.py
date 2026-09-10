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
#
# Feature: build-agent-exit75-deferral, Property 1: exit-75 deferral and single-live-command
"""
BUG CONDITION EXPLORATION for build-agent-exit75-deferral (task 1).

**Property 1: Bug Condition** - Exit-75 deferral is hard-failed
post-dispatch and the duplicate-send path can double-execute.

**Validates: Requirements 1.1, 1.2, 1.3, 1.4, 1.5**

**CRITICAL**: every test in this file MUST FAIL on the current (unfixed)
code. The failures ARE the result: they reproduce ``isBugCondition(X)``
and ``isDoubleSendCondition(Y)`` from bugfix.md against the live
production incident of 2026-09-09 (Build_Job
``851042a7-434f-4f8a-9fd4-79b25d100150``, runner
``i-089a77f72bc558147``):

  (A) an honest agent lock-held deferral — terminal invocation
      ``Failed`` with ResponseCode 75, no terminal agent result, no
      preflight/ENOSPC evidence — falls through
      ``build_reconciliation.classify_attempt``'s terminal-``Failed``
      branch to a decided hard
      ``STATUS_FAILED``/``CODE_COMMAND_EXECUTION_FAILED``;

  (B) that classification settles an EPHEMERAL job through
      ``build_dispatcher.reconcile_running_command`` with a
      ``cleanup_required=True`` terminal ledger, making the runner
      termination-watchdog eligible while the genuine lock-holder build
      is still running on it;

  (C) the ambiguous/duplicate-send machinery cannot see a PRIOR
      attempt's command (``find_command_by_comment`` requires
      full-comment equality on ``dda-build:<job>:<attempt>``), the
      pre-SendCommand attempt claim is an unconditional SET, and no
      ``cancel_command`` exists — so the dispatch/recovery path can put
      TWO concurrently live agent commands on one instance for one job
      (incident commands ``1b538196``/``a13a0825``, attempts
      ``88dcd6b8``/``6c6b5483``, 4 s apart).

Do NOT weaken these assertions and do NOT change production code to
make them pass. Task 3.5 re-runs this exact file after the fixes land,
where the same assertions must then PASS.

Encoded expected behavior (bugfix.md "Fix Checking"):

    FOR ALL X WHERE isBugCondition(X):
        NOT (classify(X).decided AND classify(X).status = STATUS_FAILED)
        classify(X).error_code != CODE_COMMAND_EXECUTION_FAILED
        job_requeued_at_head(X) AND deferred_at_recorded(X)
        X.ephemeral IMPLIES NOT instance_terminated(X)
    FOR ALL Y WHERE isDoubleSendCondition possible:
        find_command_by_job'(Y) finds the prior attempt's command
        concurrent_live_agent_commands(Y) <= 1

--------------------------------------------------------------------------
Safety
--------------------------------------------------------------------------

No test launches EC2 compute, sends a real SSM command, calls real AWS,
deploys, publishes an artifact, or starts a real build. DynamoDB is
moto-backed with dummy credentials; SSM is a module-level recording fake
installed over ``boto3.client`` BEFORE any handler import (scripted
GetCommandInvocation / ListCommands, recorded SendCommand /
CancelCommand). ``send_agent`` is stubbed where a send would occur so no
generated command text is ever executed anywhere.

Run ONLY this file, from the repository root:

    HYPOTHESIS_PROFILE=ci PYTHONPATH=src/backend:test/backend-test \\
        ~/.dda-test-venv/bin/python -m pytest \\
        test/backend-test/portal_builds/test_exit75_deferral_exploration.py \\
        --noconftest -q -p no:cacheprovider

(--noconftest skips the conftest's profile registration, so this module
registers the ``ci`` hypothesis profile itself: >=100 examples.)
"""
import os
import sys
import types
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

_SUFFIX = "exit75-deferral-explore"
_JOBS_TABLE = f"dda-portal-build-jobs-{_SUFFIX}"
_SERVERS_TABLE = f"dda-portal-build-servers-{_SUFFIX}"
os.environ["BUILD_JOBS_TABLE"] = _JOBS_TABLE
os.environ["BUILD_SERVERS_TABLE"] = _SERVERS_TABLE
os.environ.pop("BUILD_REPO_URL", None)
os.environ.pop("BUILD_ALERT_TOPIC_ARN", None)
os.environ.pop("BUILD_INSTANCE_PROFILE_ARN", None)
os.environ.pop("BUILD_INSTANCE_PROFILE_NAME", None)
os.environ.pop("BUILD_SECURITY_GROUP_ID", None)
os.environ.pop("BUILD_SUBNET_ID", None)

import boto3  # noqa: E402
from botocore.exceptions import ClientError  # noqa: E402

# Some verification containers ship a python build without the _bz2 C
# extension while moto's import path reaches bz2 (sibling shim in
# test_dispatcher_command_reconciliation.py).
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
from hypothesis import (HealthCheck, assume, given, settings,  # noqa: E402
                        strategies as st)

# --noconftest skips the conftest profile registration: register the ci
# profile here so the property runs >=100 examples (tasks.md convention).
settings.register_profile(
    "ci", max_examples=100, deadline=None,
    suppress_health_check=[HealthCheck.too_slow,
                           HealthCheck.filter_too_much])
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "ci"))

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.normpath(os.path.join(_HERE, "..", "..", ".."))
_FUNCTIONS_DIR = os.path.join(
    _REPO_ROOT, "edge-cv-portal", "backend", "functions")
if _FUNCTIONS_DIR not in sys.path:
    sys.path.insert(0, _FUNCTIONS_DIR)

# ---------------------------------------------------------------------------
# Minimal stand-in for the Lambda layer module the handlers import.
# Audit entries are captured in-process; nothing leaves the test.
# ---------------------------------------------------------------------------
AUDIT_EVENTS = []


def _fake_shared_utils():
    module = types.ModuleType("shared_utils")

    def log_audit_event(**kwargs):
        AUDIT_EVENTS.append(kwargs)

    module.log_audit_event = log_audit_event
    return module


for _module in ("build_dispatcher", "build_planner", "build_domain",
                "build_reconciliation", "build_source", "shared_utils"):
    sys.modules.pop(_module, None)
sys.modules["shared_utils"] = _fake_shared_utils()

_MOCK = mock_aws()
_MOCK.start()

# ---------------------------------------------------------------------------
# Recording fake SSM over boto3.client BEFORE the handler import:
# scripted GetCommandInvocation / ListCommands, recorded SendCommand and
# CancelCommand. No call leaves the process (sibling pattern:
# test_dispatcher_command_reconciliation.py).
# ---------------------------------------------------------------------------
SSM_INVOCATIONS = {}
SSM_GET_CALLS = []
SSM_LIST_COMMANDS = []
SSM_SEND_CALLS = []
SSM_CANCEL_CALLS = []

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

    def list_commands(self, **kwargs):
        return {"Commands": [dict(c) for c in SSM_LIST_COMMANDS]}

    def send_command(self, **kwargs):
        SSM_SEND_CALLS.append(dict(kwargs))
        return self._inner.send_command(**kwargs)

    def cancel_command(self, **kwargs):
        SSM_CANCEL_CALLS.append(dict(kwargs))
        return {}

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
        AttributeDefinitions=[{"AttributeName": _key,
                               "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
_JOBS = _DDB.Table(_JOBS_TABLE)

_EC2 = boto3.client("ec2", region_name="us-east-1")


def _default_ami_id():
    images = _EC2.describe_images(Owners=["amazon"]).get("Images", [])
    if images:
        return images[0]["ImageId"]
    return _EC2.register_image(  # pragma: no cover
        Name="dda-test-ami", RootDeviceName="/dev/sda1",
        VirtualizationType="hvm")["ImageId"]


_AMI_ID = _default_ami_id()
os.environ["BUILD_ARM64_AMI_ID"] = _AMI_ID
os.environ["BUILD_X86_64_AMI_ID"] = _AMI_ID

import build_domain  # noqa: E402
import build_planner  # noqa: E402
import build_reconciliation as br  # noqa: E402
import build_dispatcher  # noqa: E402

# The handler above captured its (fake-wrapped) clients at import;
# restore the real factory so other modules in the same pytest process
# get untouched clients.
boto3.client = _REAL_BOTO3_CLIENT

# ---------------------------------------------------------------------------
# Incident constants (bugfix.md Introduction; 2026-09-09)
# ---------------------------------------------------------------------------
NOW = 1_788_800_043_000
_MINUTE_MS = 60 * 1000
CREATED_AT = NOW - 10 * _MINUTE_MS

INCIDENT_JOB = "851042a7-434f-4f8a-9fd4-79b25d100150"
INCIDENT_INSTANCE = "i-089a77f72bc558147"
#: First send (12:54:03) — SSM-reported 'Failed/Undeliverable', rc -1,
#: no recorded execution start — but it ACTUALLY executed and held the
#: build lock.
PRIOR_ATTEMPT = "88dcd6b8"
PRIOR_COMMAND = "1b538196"
#: Second send (12:54:07) — executed 12:54:08-12:54:12, exited 75 with
#: the deterministic lock-held stdout marker.
CURRENT_ATTEMPT = "6c6b5483"
INCIDENT_COMMAND = "a13a0825"

#: The agent's deterministic exit-75 stdout marker
#: (scripts/portal-build-agent.sh lock contract, verbatim).
LOCK_HELD_MARKER = ("Build lock /var/lock/dda-build.lock is held by "
                    "another build — deferring (exit 75).")


def _get_job(job_id):
    return build_dispatcher.to_native(
        _JOBS.get_item(Key={"build_job_id": job_id}).get("Item"))


def _clear_state():
    for item in _JOBS.scan().get("Items", []):
        _JOBS.delete_item(Key={"build_job_id": item["build_job_id"]})
    del AUDIT_EVENTS[:]
    del SSM_GET_CALLS[:]
    del SSM_LIST_COMMANDS[:]
    del SSM_SEND_CALLS[:]
    del SSM_CANCEL_CALLS[:]
    SSM_INVOCATIONS.clear()


def _incident_invocation(response_code=75, stdout=None, stderr="",
                         status_details="Failed"):
    return {
        "CommandId": INCIDENT_COMMAND,
        "InstanceId": INCIDENT_INSTANCE,
        "Status": "Failed",
        "StatusDetails": status_details,
        "ResponseCode": response_code,
        "StandardOutputContent": (LOCK_HELD_MARKER + "\n"
                                  if stdout is None else stdout),
        "StandardErrorContent": stderr,
    }


# ===========================================================================
# Part A — classification (Defect A): rc-75 lock-held deferral must not
# classify as a decided hard COMMAND_EXECUTION_FAILED (Req 1.1 -> 2.1)
# ===========================================================================

def _assert_not_hard_failed(classification, evidence_note):
    """The bugfix.md Fix Checking predicate for one classification."""
    assert not (classification.decided
                and classification.status == build_domain.STATUS_FAILED), (
        "BUG CONDITION (Defect A): an honest agent exit-75 lock-held "
        "deferral was DECIDED as a hard failure instead of being "
        "deferred (agent contract: 'dispatcher should defer and "
        "retry').\n"
        f"  evidence: {evidence_note}\n"
        f"  observed classification: decided={classification.decided} "
        f"status={classification.status!r} "
        f"error_code={classification.error_code!r} "
        f"authority={classification.authority} "
        f"reason={classification.reason!r}")
    assert classification.error_code != br.CODE_COMMAND_EXECUTION_FAILED, (
        "BUG CONDITION (Defect A): the exit-75 deferral was labelled "
        "with the hard CODE_COMMAND_EXECUTION_FAILED stable code.\n"
        f"  evidence: {evidence_note}\n"
        f"  observed classification: {classification!r}")


class TestPartAClassification:
    """``classify_attempt`` on the deterministic bug condition
    isBugCondition(X): Status='Failed' AND ResponseCode=75 AND
    agent_result=None AND no preflight/ENOSPC evidence."""

    def test_incident_verbatim_exit75_invocation_is_not_hard_failed(self):
        """The verbatim incident case: command a13a0825, attempt
        6c6b5483, terminal 'Failed', rc 75, the deterministic lock-held
        stdout marker, no terminal agent result."""
        classification = br.classify_attempt(
            current_status=build_domain.STATUS_BUILDING,
            invocation=_incident_invocation(),
            agent_result=None)
        _assert_not_hard_failed(
            classification,
            f"command {INCIDENT_COMMAND} attempt {CURRENT_ATTEMPT} "
            f"Status=Failed ResponseCode=75 "
            f"stdout={LOCK_HELD_MARKER!r}")

    # Feature: build-agent-exit75-deferral, Property 1: exit-75 deferral and single-live-command
    # **Validates: Requirements 1.1**
    @given(
        response_code=st.sampled_from([75, "75"]),
        noise_before=st.text(max_size=200),
        noise_after=st.text(max_size=200),
        stderr=st.text(max_size=200),
        status_details=st.sampled_from(
            ["Failed", "Failed: exit status 75", ""]),
        current_status=st.sampled_from(
            [build_domain.STATUS_BUILDING,
             build_domain.STATUS_PROVISIONING]),
    )
    def test_property_rc75_terminal_failed_is_never_command_execution_failed(
            self, response_code, noise_before, noise_after, stderr,
            status_details, current_status):
        """For ALL terminal 'Failed' invocations of the incident shape
        (rc 75, the deterministic lock marker in stdout, generated
        non-preflight/non-ENOSPC text, no agent result),
        classify_attempt must NOT return the decided
        STATUS_FAILED / CODE_COMMAND_EXECUTION_FAILED outcome."""
        stdout = noise_before + LOCK_HELD_MARKER + noise_after
        # isBugCondition(X) requires NO preflight and NO ENOSPC
        # evidence — enforced with the production predicates themselves.
        texts = (stdout, stderr, status_details)
        assume(not br.is_preflight_failure_evidence(*texts))
        assume(not br.is_disk_exhaustion_evidence(*texts))

        invocation = {
            "CommandId": INCIDENT_COMMAND,
            "InstanceId": INCIDENT_INSTANCE,
            "Status": "Failed",
            "StatusDetails": status_details,
            "ResponseCode": response_code,
            "StandardOutputContent": stdout,
            "StandardErrorContent": stderr,
        }
        classification = br.classify_attempt(
            current_status=current_status,
            invocation=invocation,
            agent_result=None)
        _assert_not_hard_failed(
            classification,
            f"generated rc={response_code!r} "
            f"status_details={status_details!r} "
            f"stdout_len={len(stdout)} stderr_len={len(stderr)}")


# ===========================================================================
# Part B — settlement side effect (Defect A): an ephemeral job settling
# an rc-75 'Failed' invocation must be DEFERRED (requeued at head,
# deferred_at recorded, NO cleanup_required ledger / termination
# eligibility) — not hard-failed with the runner marked for termination
# while the lock-holder build runs (Req 1.1, 1.2 -> 2.1, 2.2)
# ===========================================================================

class TestPartBSettlement:

    def setup_method(self):
        _clear_state()

    def _seed_incident_job(self):
        job = {
            "build_job_id": INCIDENT_JOB,
            "build_target": build_domain.TARGET_AMD64,
            "execution_mode": build_domain.EXECUTION_MODE_EPHEMERAL,
            "status": build_domain.STATUS_BUILDING,
            "requested_by": "operator-1",
            "created_at": CREATED_AT,
            "started_at": NOW - 9 * _MINUTE_MS,
            "config_snapshot": {"max_runtime_hours": 4},
            "runner": {"instance_id": INCIDENT_INSTANCE,
                       "repo_dir": "/home/ubuntu/DefectDetectionApplication",
                       "terminate_attempts": 0,
                       "terminate_first_failed_at": None},
            "ssm": {"command_id": INCIDENT_COMMAND,
                    "instance_id": INCIDENT_INSTANCE},
            "execution_attempt": {
                "attempt_id": CURRENT_ATTEMPT,
                "command_id": INCIDENT_COMMAND,
                "instance_id": INCIDENT_INSTANCE,
                "dispatch_state": br.DISPATCH_SENT,
                "command_comment": br.command_comment(
                    INCIDENT_JOB, CURRENT_ATTEMPT),
            },
        }
        _JOBS.put_item(Item=job)
        return job

    def test_rc75_settlement_defers_and_never_plans_runner_termination(self):
        """reconcile_running_command on the incident's rc-75 'Failed'
        invocation: the fixed settlement requeues at the head of the
        queue (original created_at, deferred_at recorded) and plans NO
        cleanup_required=True terminal ledger — the runner must not
        become termination-watchdog eligible while the lock-holder
        build may be running."""
        self._seed_incident_job()
        SSM_INVOCATIONS[INCIDENT_COMMAND] = _incident_invocation()

        with mock.patch.object(build_dispatcher, "run_shell_sync",
                               return_value=None):
            build_dispatcher.run_tick(now=NOW)

        job = _get_job(INCIDENT_JOB)
        error = job.get("error") or {}
        ledger = job.get("terminal_effects") or {}
        cleanup = ledger.get(br.EFFECT_COMPUTE_CLEANUP)

        violations = []
        if job["status"] == build_domain.STATUS_FAILED \
                and error.get("code") == br.CODE_COMMAND_EXECUTION_FAILED:
            violations.append(
                f"hard-failed: status={job['status']!r} "
                f"error={error!r} — the honest exit-75 deferral became "
                f"a terminal COMMAND_EXECUTION_FAILED")
        if cleanup not in (None, br.EFFECT_NOT_APPLICABLE):
            violations.append(
                f"cleanup_required ledger planned: "
                f"terminal_effects.compute_cleanup={cleanup!r} — the "
                f"ephemeral runner {INCIDENT_INSTANCE} is "
                f"termination-watchdog eligible while the lock-holder "
                f"build (command {PRIOR_COMMAND}) is still running")
        if job["status"] != build_domain.STATUS_QUEUED:
            violations.append(
                f"not requeued: status={job['status']!r} (expected "
                f"'{build_domain.STATUS_QUEUED}' at the head of its "
                f"queue, mirroring PREDISPATCH_DEFER)")
        if job.get("deferred_at") is None:
            violations.append("deferred_at was not recorded")
        if job.get("created_at") != CREATED_AT:
            violations.append(
                f"created_at not retained: {job.get('created_at')!r} "
                f"!= {CREATED_AT} (head-of-queue position lost)")

        assert not violations, (
            "BUG CONDITION (Defect A settlement, Req 1.1/1.2): an "
            "EPHEMERAL job settling a terminal 'Failed' rc-75 "
            "invocation must be deferred, not terminally failed with "
            "runner cleanup planned. Violated fixed-predicate "
            "clauses:\n- " + "\n- ".join(violations))


# ===========================================================================
# Part C — recovery lookup and single-live-command (Defect B): a prior
# attempt's command must be findable by job id, and the dispatch/
# recovery path must never put two concurrently live agent commands on
# one instance for one job (Req 1.3, 1.4, 1.5 -> 2.3, 2.4, 2.5)
# ===========================================================================

class TestPartCRecoveryAndSingleLiveCommand:

    def setup_method(self):
        _clear_state()

    def test_find_command_by_comment_finds_prior_attempt_by_job_id(self):
        """Req 1.4 -> 2.4: given only the job id (via the CURRENT
        attempt's comment ``dda-build:<job>:6c6b5483``), the recovery
        lookup must find the PRIOR attempt's command whose comment is
        ``dda-build:<job>:88dcd6b8``. Today's full-comment equality can
        never match a different attempt id and returns None."""
        current_comment = br.command_comment(INCIDENT_JOB, CURRENT_ATTEMPT)
        prior_comment = br.command_comment(INCIDENT_JOB, PRIOR_ATTEMPT)
        SSM_LIST_COMMANDS.append({
            "CommandId": PRIOR_COMMAND,
            "InstanceId": INCIDENT_INSTANCE,
            "Comment": prior_comment,
            "Status": "Failed",
            "StatusDetails": "Undeliverable",
        })

        found = build_dispatcher.find_command_by_comment(
            INCIDENT_INSTANCE, current_comment)

        assert found and PRIOR_COMMAND in str(found), (
            "BUG CONDITION (Defect B lookup, Req 1.4): "
            "find_command_by_comment could not find the prior "
            "attempt's command for the SAME job.\n"
            f"  recent commands contained: CommandId={PRIOR_COMMAND!r} "
            f"Comment={prior_comment!r}\n"
            f"  current attempt's comment: {current_comment!r}\n"
            f"  observed lookup result: {found!r} — full-comment "
            "equality guarantees a different attempt's command never "
            "matches, so the recovery proceeds as if no command exists")

    def test_overlapping_tick_cannot_mint_a_second_live_command(self):
        """Req 1.3 -> 2.3: the pre-SendCommand attempt claim must be a
        CONDITIONAL one-writer-wins write. Modeled exactly as the
        incident: tick A already claimed attempt 88dcd6b8 and sent
        command 1b538196 (persisted); tick B still holds the STALE
        pre-claim job snapshot (scanned before tick A's writes) and
        runs the ephemeral send loop. Tick B must lose the claim and
        NOT send — never a second concurrently live agent command."""
        persisted = {
            "build_job_id": INCIDENT_JOB,
            "build_target": build_domain.TARGET_AMD64,
            "execution_mode": build_domain.EXECUTION_MODE_EPHEMERAL,
            "status": build_domain.STATUS_PROVISIONING,
            "requested_by": "operator-1",
            "created_at": CREATED_AT,
            "config_snapshot": {"max_runtime_hours": 4},
            "runner": {"instance_id": INCIDENT_INSTANCE,
                       "repo_dir": "/home/ubuntu/DefectDetectionApplication"},
            # Tick A's writes, already persisted:
            "ssm": {"command_id": PRIOR_COMMAND,
                    "instance_id": INCIDENT_INSTANCE},
            "execution_attempt": {
                "attempt_id": PRIOR_ATTEMPT,
                "command_id": PRIOR_COMMAND,
                "instance_id": INCIDENT_INSTANCE,
                "dispatch_state": br.DISPATCH_SENT,
                "command_comment": br.command_comment(
                    INCIDENT_JOB, PRIOR_ATTEMPT),
                "claimed_at": NOW - 5_000,
                "sent_at": NOW - 4_000,
            },
        }
        _JOBS.put_item(Item=persisted)

        # Tick B's STALE in-memory snapshot: scanned BEFORE tick A
        # persisted the claim/command (the 4-second overlap window).
        stale_snapshot = {
            k: v for k, v in persisted.items()
            if k not in ("ssm", "execution_attempt")}

        ready = types.SimpleNamespace(
            readiness=build_planner.READINESS_READY,
            log_path="/var/log/dda-build-server-bootstrap.log")
        send_mock = mock.Mock(return_value=(INCIDENT_COMMAND,
                                            "stream-second-send"))
        with mock.patch.object(build_dispatcher, "instance_ssm_online",
                               lambda _instance: True), \
                mock.patch.object(build_dispatcher,
                                  "probe_bootstrap_marker",
                                  lambda _instance: "marker-present"), \
                mock.patch.object(build_planner, "decide_runner_readiness",
                                  lambda *_a, **_k: ready), \
                mock.patch.object(
                    build_dispatcher, "decide_preflight",
                    lambda *_a, **_k: build_dispatcher.PreflightDecision(
                        ok=True, failures=(), checks={})), \
                mock.patch.object(build_dispatcher, "send_agent",
                                  send_mock):
            build_dispatcher.provision_ephemeral([stale_snapshot], NOW)

        job = _get_job(INCIDENT_JOB)
        attempt = job.get("execution_attempt") or {}

        violations = []
        if send_mock.called:
            violations.append(
                f"a SECOND agent SendCommand was issued for the same "
                f"job on {INCIDENT_INSTANCE} "
                f"({send_mock.call_count} send(s)) — the incident's "
                f"commands {PRIOR_COMMAND}/{INCIDENT_COMMAND} 4 s "
                f"apart; the losing tick must skip the send")
        if attempt.get("attempt_id") != PRIOR_ATTEMPT:
            violations.append(
                f"the first tick's attempt claim was clobbered by an "
                f"unconditional write: persisted attempt_id="
                f"{attempt.get('attempt_id')!r} (expected the prior "
                f"attempt {PRIOR_ATTEMPT!r}); command "
                f"{PRIOR_COMMAND} is now orphaned")
        if (job.get("ssm") or {}).get("command_id") != PRIOR_COMMAND:
            violations.append(
                f"ssm.command_id was rewritten to "
                f"{(job.get('ssm') or {}).get('command_id')!r} "
                f"(expected {PRIOR_COMMAND!r})")

        assert not violations, (
            "BUG CONDITION (Defect B double-send, Req 1.3): the "
            "execution-attempt claim before SendCommand must be a "
            "conditional write exactly one concurrent tick can win. "
            "Violated fixed-predicate clauses:\n- "
            + "\n- ".join(violations))

    def test_recovery_never_resends_while_prior_command_may_be_live(self):
        """Req 1.4/1.5 -> 2.4/2.5: an ambiguous-send recovery past the
        visibility bound, with the PRIOR attempt's command visible in
        recent commands (SSM-reported 'Undeliverable' but actually
        executed — the incident's 1b538196), must attach that command
        or cancel it before any resend. Never two concurrently live
        agent commands for one job on one instance."""
        sending_at = (NOW
                      - build_dispatcher.AMBIGUOUS_SEND_VISIBILITY_MS
                      - _MINUTE_MS)
        current_comment = br.command_comment(INCIDENT_JOB, CURRENT_ATTEMPT)
        prior_comment = br.command_comment(INCIDENT_JOB, PRIOR_ATTEMPT)
        job = {
            "build_job_id": INCIDENT_JOB,
            "build_target": build_domain.TARGET_AMD64,
            "execution_mode": build_domain.EXECUTION_MODE_EPHEMERAL,
            "status": build_domain.STATUS_BUILDING,
            "requested_by": "operator-1",
            "created_at": CREATED_AT,
            "started_at": NOW - 9 * _MINUTE_MS,
            "config_snapshot": {"max_runtime_hours": 4},
            "runner": {"instance_id": INCIDENT_INSTANCE,
                       "repo_dir": "/home/ubuntu/DefectDetectionApplication"},
            "execution_attempt": {
                "attempt_id": CURRENT_ATTEMPT,
                "dispatch_state": br.DISPATCH_SENDING,
                "instance_id": INCIDENT_INSTANCE,
                "command_id": None,
                "command_comment": current_comment,
                "claimed_at": sending_at,
                "sending_at": sending_at,
                "sent_at": None,
            },
        }
        _JOBS.put_item(Item=job)
        # The prior attempt's command: SSM-REPORTED terminal
        # 'Failed/Undeliverable' — but it actually executed and holds
        # the build lock (delivery-status race on a just-bootstrapped
        # instance). 'Undeliverable' is NOT proof of non-execution.
        SSM_LIST_COMMANDS.append({
            "CommandId": PRIOR_COMMAND,
            "InstanceId": INCIDENT_INSTANCE,
            "Comment": prior_comment,
            "Status": "Failed",
            "StatusDetails": "Undeliverable",
        })
        SSM_INVOCATIONS[PRIOR_COMMAND] = {
            "CommandId": PRIOR_COMMAND,
            "InstanceId": INCIDENT_INSTANCE,
            "Status": "InProgress",
            "StatusDetails": "InProgress",
            "ResponseCode": -1,
            # Execution evidence exists: the command IS running.
            "ExecutionStartDateTime": "2026-09-09T12:54:04Z",
            "StandardOutputContent": "",
            "StandardErrorContent": "",
        }

        resent_command = "cmd-conditional-resend"
        send_mock = mock.Mock(return_value=(resent_command,
                                            "stream-resend"))
        with mock.patch.object(build_dispatcher, "send_agent", send_mock):
            build_dispatcher.recover_ambiguous_send(
                _get_job(INCIDENT_JOB), {}, NOW)

        settled = _get_job(INCIDENT_JOB)
        resent = send_mock.called
        cancelled = any(call.get("CommandId") == PRIOR_COMMAND
                        for call in SSM_CANCEL_CALLS)
        attached = ((settled.get("ssm") or {}).get("command_id")
                    == PRIOR_COMMAND)

        live_commands = set()
        if not cancelled:
            live_commands.add(PRIOR_COMMAND)
        if resent:
            live_commands.add(resent_command)

        assert len(live_commands) <= 1 and (attached or not resent), (
            "BUG CONDITION (Defect B recovery, Req 1.4/1.5): the "
            "resend was neither preceded by cancellation of the prior "
            "attempt's command nor gated on proof of its terminal "
            "non-execution, and the prior command was not attached.\n"
            f"  prior command {PRIOR_COMMAND!r}: reported "
            f"'Failed/Undeliverable' but actually executing "
            f"(ExecutionStartDateTime present), cancelled={cancelled}, "
            f"attached={attached}\n"
            f"  resend issued: {resent} "
            f"({send_mock.call_count} send(s), new command "
            f"{resent_command!r})\n"
            f"  concurrently live agent commands for job "
            f"{INCIDENT_JOB}: {sorted(live_commands)} (must be <= 1)")


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "--noconftest", "-q",
                          "-p", "no:cacheprovider"]))
