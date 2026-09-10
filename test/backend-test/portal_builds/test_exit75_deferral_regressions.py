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
# Feature: build-agent-exit75-deferral, Property 3: the deferral is reachable and never terminates a live build
"""
REGRESSION suite for the design review of the exit-75 deferral fix
(semantic-review/2026-09-09-222949-pr-local.md, issues 1-12; this file
closes the coverage gaps of issue 13).

The two frozen suites — ``test_exit75_deferral_exploration.py`` (bug
condition, 6 tests) and ``test_exit75_deferral_preservation.py``
(immutable oracle, 34 tests) — are NOT modified and must keep passing
verbatim. This file adds the paths they never reached, each one an
executable statement of a review finding:

  Property 3 — the deferral is REACHABLE for every dispatch shape that
    can produce it, and the runner allocation/binding survives it:
      * a PROVISIONING ephemeral job (review issue 1: the agent takes
        ``flock -n`` in Step 1 and exits 75 BEFORE emitting
        ``phase=building``, so the common exit-75 shape is still
        ``provisioning`` — where nothing used to defer, fail, or watch);
      * a DEDICATED job post-dispatch, including the "allocation kept"
        clause of Req 2.1;
      * narrowness: a non-75 terminal failure on a provisioning job
        keeps today's behavior exactly.

  Property 4 — no agent command is ever re-sent while a build process is
    present on the runner (review issue 2: the command preamble runs
    ``chown -R`` + ``git fetch`` + ``git checkout --force -B`` BEFORE
    the agent reaches ``flock``, and ``portal-build.sh`` writes the
    TRACKED ``gdk-config.json``, so a re-send during the holder's build
    rewrites the tree it is building in), plus the runner-liveness
    verification of issue 9.

  Property 5 — deferral PRESSURE is bounded by the job's own resolved
    runtime ceiling (issue 7), an external backstop bounds the requeued
    state itself (issue 6), and NO exit from the deferral terminates a
    runner while a build may still be running on it (issue 8).

  Property 6 — the resend gate never authorizes a send on unproven
    non-execution: the incident's terminal-'Failed'-without-execution
    shape is INDETERMINATE (issue 3), an unreadable invocation is not
    absence (issue 4), an already-settled deferral command is never
    re-attached (issue 5), the attempt claim refuses a map without an
    ``attempt_id`` and reports WHY it refused (issue 11), and an adopted
    prior command is recorded explicitly instead of overwriting the
    current attempt's identity and clocks (issue 12).

--------------------------------------------------------------------------
Safety
--------------------------------------------------------------------------

No test launches EC2 compute, sends a real SSM command, calls real AWS,
deploys, publishes an artifact, or starts a real build. DynamoDB is
moto-backed with dummy credentials; SSM and EC2 are in-process recording
fakes; ``send_agent`` and ``run_runner_instance`` are stubbed wherever a
send/launch would occur, so no generated command text is ever executed
anywhere.

Run ONLY this file, from the repository root:

    HYPOTHESIS_PROFILE=ci PYTHONPATH=src/backend:test/backend-test \\
        ~/.dda-test-venv/bin/python -m pytest \\
        test/backend-test/portal_builds/test_exit75_deferral_regressions.py \\
        --noconftest -q -p no:cacheprovider

(--noconftest skips the conftest's profile registration, so this module
registers the ``ci`` hypothesis profile itself: >=100 examples.)
"""
import os
import sys
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

_SUFFIX = "exit75-deferral-regress"
_JOBS_TABLE = f"dda-portal-build-jobs-{_SUFFIX}"
_SERVERS_TABLE = f"dda-portal-build-servers-{_SUFFIX}"
os.environ["BUILD_JOBS_TABLE"] = _JOBS_TABLE
os.environ["BUILD_SERVERS_TABLE"] = _SERVERS_TABLE
os.environ.pop("BUILD_REPO_URL", None)
os.environ.pop("BUILD_ALERT_TOPIC_ARN", None)
os.environ.pop("BUILD_LOCK_DEFERRAL_WINDOW_MS", None)
os.environ.pop("BUILD_LOCK_DEFERRAL_STALL_MS", None)
os.environ.pop("BUILD_LOCK_DEFERRAL_UNCORROBORATED_CYCLES", None)

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
from hypothesis import (HealthCheck, given, settings,  # noqa: E402
                        strategies as st)

# --noconftest skips the conftest profile registration: register the ci
# profile here so the property tests run >=100 examples.
settings.register_profile(
    "ci", max_examples=100, deadline=None,
    suppress_health_check=[HealthCheck.too_slow,
                           HealthCheck.filter_too_much,
                           HealthCheck.function_scoped_fixture])
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "ci"))

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.normpath(os.path.join(_HERE, "..", "..", ".."))
_FUNCTIONS_DIR = os.path.join(
    _REPO_ROOT, "edge-cv-portal", "backend", "functions")
if _FUNCTIONS_DIR not in sys.path:
    sys.path.insert(0, _FUNCTIONS_DIR)

# ---------------------------------------------------------------------------
# Minimal stand-in for the Lambda layer module the handlers import.
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
_SERVERS = _DDB.Table(_SERVERS_TABLE)

import build_domain  # noqa: E402
import build_planner  # noqa: E402
import build_reconciliation as br  # noqa: E402
import build_dispatcher  # noqa: E402

# ---------------------------------------------------------------------------
# Incident constants (bugfix.md Introduction; 2026-09-09)
# ---------------------------------------------------------------------------
NOW = 1_788_800_043_000
MINUTE_MS = 60 * 1000
HOUR_MS = 60 * MINUTE_MS
CREATED_AT = NOW - 10 * MINUTE_MS

INSTANCE = "i-089a77f72bc558147"
#: The agent's deterministic exit-75 stdout marker
#: (scripts/portal-build-agent.sh line 242, verbatim).
LOCK_HELD_MARKER = ("Build lock /var/lock/dda-build.lock is held by "
                    "another build — deferring (exit 75).")
#: A pgrep -af line reporting a real build process on the runner.
BUILD_PROCESS_LINE = "4711 bash build-custom.sh aws.edgeml.dda.LocalServer"


# ---------------------------------------------------------------------------
# In-process fakes
# ---------------------------------------------------------------------------

class FakeSsm:
    """Scripted GetCommandInvocation / ListCommands, recorded
    SendCommand / CancelCommand. Nothing leaves the process."""

    def __init__(self):
        self.invocations = {}        # command_id -> invocation dict
        self.errors = {}             # command_id -> error code to raise
        self.commands = []           # ListCommands payload
        self.list_error = None
        self.sends = []
        self.cancels = []
        self.next_command_id = "cmd-fake"

    def get_command_invocation(self, **kwargs):
        command_id = kwargs.get("CommandId")
        code = self.errors.get(command_id)
        if code:
            raise ClientError({"Error": {"Code": code, "Message": code}},
                              "GetCommandInvocation")
        invocation = self.invocations.get(command_id)
        if invocation is None:
            raise ClientError(
                {"Error": {"Code": "InvocationDoesNotExist",
                           "Message": "no such invocation"}},
                "GetCommandInvocation")
        return dict(invocation)

    def list_commands(self, **kwargs):
        if self.list_error:
            raise ClientError(
                {"Error": {"Code": self.list_error,
                           "Message": self.list_error}}, "ListCommands")
        return {"Commands": [dict(c) for c in self.commands]}

    def send_command(self, **kwargs):
        self.sends.append(dict(kwargs))
        return {"Command": {"CommandId": self.next_command_id}}

    def cancel_command(self, **kwargs):
        self.cancels.append(dict(kwargs))
        return {}


class FakeEc2:
    """describe_instances answers a scripted lifecycle state per
    instance; terminate_instances is recorded, never performed."""

    def __init__(self, states=None):
        self.states = dict(states or {})
        self.terminated = []
        self.default_state = "running"

    def describe_instances(self, **kwargs):
        ids = kwargs.get("InstanceIds")
        if ids is None:
            return {"Reservations": []}  # tag-filtered partial lookup
        reservations = []
        for instance_id in ids:
            state = self.states.get(instance_id, self.default_state)
            reservations.append({"Instances": [
                {"InstanceId": instance_id, "State": {"Name": state}}]})
        return {"Reservations": reservations}

    def terminate_instances(self, InstanceIds):
        self.terminated.append(list(InstanceIds))
        return {}


def shell_sync_fake(pgrep_output="", build_count=0, marker=True):
    """A ``run_shell_sync`` stand-in keyed on the command list, so the
    real pgrep/probe seams are exercised.

    ``pgrep_output`` feeds VERIFY_BUILD_PROCESS_COMMANDS (None = the
    check could not be positively completed); ``build_count`` feeds
    COUNT_BUILD_PROCESS_COMMANDS (None = unverifiable)."""
    def fake(instance_id, commands):
        text = "\n".join(commands)
        if "pgrep -cf" in text:
            if build_count is None:
                return None
            return (f"GDK_BUILD_COUNT={build_count}\n"
                    f"CUSTOM_BUILD_COUNT={build_count}")
        if "pgrep -af" in text:
            return pgrep_output
        if build_planner.BOOTSTRAP_MARKER_PATH in text:
            return (f"{build_planner.BOOTSTRAP_DONE_PROBE_KEY}="
                    f"{1 if marker else 0}\n"
                    f"{build_planner.BOOTSTRAP_LOG_PROBE_KEY}="
                    f"{build_planner.BOOTSTRAP_LOG_PATH}")
        return ""
    return fake


def get_job(job_id):
    return build_dispatcher.to_native(
        _JOBS.get_item(Key={"build_job_id": job_id}).get("Item"))


def get_server(server_id):
    return build_dispatcher.to_native(
        _SERVERS.get_item(Key={"server_id": server_id}).get("Item"))


def clear_state():
    for item in _JOBS.scan().get("Items", []):
        _JOBS.delete_item(Key={"build_job_id": item["build_job_id"]})
    for item in _SERVERS.scan().get("Items", []):
        _SERVERS.delete_item(Key={"server_id": item["server_id"]})
    del AUDIT_EVENTS[:]


def rc75_invocation(command_id="cmd-rc75", instance_id=INSTANCE,
                    marker=True, response_code=75, status="Failed"):
    return {
        "CommandId": command_id,
        "InstanceId": instance_id,
        "Status": status,
        "StatusDetails": status,
        "ResponseCode": response_code,
        "StandardOutputContent": (LOCK_HELD_MARKER + "\n") if marker
        else "gdk component build failed: inner tool exited 75\n",
        "StandardErrorContent": "",
    }


def seed_job(job_id, status, execution_mode="ephemeral", command_id="cmd-rc75",
             attempt_id=None, runner=True, server_id=None,
             lock_deferral=None, config_snapshot=None, deferred_at=None,
             dispatch_state=None, created_at=CREATED_AT):
    attempt_id = attempt_id or str(uuid.uuid4())
    job = {
        "build_job_id": job_id,
        "build_target": build_domain.TARGET_AMD64,
        "execution_mode": execution_mode,
        "status": status,
        "requested_by": "operator-1",
        "created_at": created_at,
        "dispatched_at": CREATED_AT + MINUTE_MS,
        "config_snapshot": config_snapshot or {"max_runtime_hours": 4},
    }
    if runner:
        job["runner"] = {"instance_id": INSTANCE,
                         "repo_dir": "/home/ubuntu/DefectDetectionApplication",
                         "instance_type": "c7i.4xlarge",
                         "arch": "x86_64",
                         "terminate_attempts": 0,
                         "terminate_first_failed_at": None}
    if server_id:
        job["server_id"] = server_id
    if command_id:
        job["ssm"] = {"command_id": command_id, "instance_id": INSTANCE}
    job["execution_attempt"] = {
        "attempt_id": attempt_id,
        "command_id": command_id,
        "instance_id": INSTANCE,
        "dispatch_state": dispatch_state or br.DISPATCH_SENT,
        "command_comment": br.command_comment(job_id, attempt_id),
        "claimed_at": CREATED_AT + MINUTE_MS,
        "sending_at": CREATED_AT + MINUTE_MS,
        "sent_at": CREATED_AT + MINUTE_MS,
    }
    if lock_deferral is not None:
        job["lock_deferral"] = build_dispatcher.to_dynamo(lock_deferral)
    if deferred_at is not None:
        job["deferred_at"] = deferred_at
    _JOBS.put_item(Item=build_dispatcher.to_dynamo(job))
    return build_dispatcher.to_native(job)


def cleanup_state(job):
    return (job.get("terminal_effects") or {}).get(
        br.EFFECT_COMPUTE_CLEANUP)


# ===========================================================================
# Property 3 — the deferral is REACHABLE for every dispatch shape, and
# the runner binding / server allocation survive it
# (review issues 1 and the Req 2.1 allocation-kept clause)
# ===========================================================================

class TestProvisioningStatusDeferral:
    """**Property 3** — an ephemeral agent exits 75 in the script's
    Step 1, BEFORE ``phase=building``, so its job is still
    ``provisioning``. That route must defer.

    **Validates: Requirements 2.1, 2.2**
    """

    def setup_method(self):
        clear_state()

    def _reconcile(self, job, invocation, ssm=None, ec2=None,
                   shell=None, now=NOW):
        ssm = ssm or FakeSsm()
        ssm.invocations[invocation["CommandId"]] = invocation
        ec2 = ec2 or FakeEc2()
        with mock.patch.object(build_dispatcher, "ssm", ssm), \
                mock.patch.object(build_dispatcher, "ec2", ec2), \
                mock.patch.object(build_dispatcher, "run_shell_sync",
                                  shell or shell_sync_fake()), \
                mock.patch.object(build_dispatcher, "send_agent",
                                  mock.Mock(side_effect=AssertionError(
                                      "no send from reconciliation"))):
            build_dispatcher.command_reconciliation([job], {}, now)
        return ssm, ec2

    def test_provisioning_ephemeral_exit75_defers_and_keeps_the_runner(self):
        """Review issue 1: before this route existed the job wedged in
        ``provisioning`` — never deferred, never failed, ``ssm.command_id``
        still set (the ephemeral send gate skips it), the `sent` attempt
        invisible to ambiguous-send recovery, and no watchdog covering
        the status. The runner billed forever."""
        job_id = f"prov-defer-{uuid.uuid4()}"
        job = seed_job(job_id, build_domain.STATUS_PROVISIONING)
        _, ec2 = self._reconcile(job, rc75_invocation())

        stored = get_job(job_id)
        assert stored["status"] == build_domain.STATUS_QUEUED
        assert stored["deferred_at"] == NOW
        assert stored["created_at"] == CREATED_AT      # head of queue
        assert stored["lock_deferral"]["count"] == 1
        assert stored["lock_deferral"]["last_command_id"] == "cmd-rc75"
        assert stored["lock_deferral"]["marker_corroborated"] is True
        # The runner binding survives, nothing is planned for cleanup and
        # nothing was terminated.
        assert stored["runner"]["instance_id"] == INSTANCE
        assert cleanup_state(stored) in (None, br.EFFECT_NOT_APPLICABLE)
        assert ec2.terminated == []
        # The command bookkeeping is cleared so the send gate can
        # re-dispatch through the conditional claim only.
        assert (stored.get("ssm") or {}).get("command_id") is None
        assert stored["execution_attempt"]["dispatch_state"] == \
            br.DISPATCH_TERMINAL
        assert any(e["action"] == "build_deferred_lock_held"
                   for e in AUDIT_EVENTS)

    def test_provisioning_non75_failure_keeps_todays_behavior(self):
        """Narrowness guard: the provisioning route acts ONLY on the
        lock-held deferral. Every other terminal invocation on a
        provisioning job is left where the design put it ("a queued/
        provisioning job has no agent outcome to lose")."""
        job_id = f"prov-non75-{uuid.uuid4()}"
        job = seed_job(job_id, build_domain.STATUS_PROVISIONING)
        self._reconcile(job, rc75_invocation(response_code=1, marker=False))

        stored = get_job(job_id)
        assert stored["status"] == build_domain.STATUS_PROVISIONING
        assert "lock_deferral" not in stored
        assert stored.get("deferred_at") is None
        assert (stored.get("ssm") or {}).get("command_id") == "cmd-rc75"

    def test_provisioning_nonterminal_invocation_is_untouched(self):
        job_id = f"prov-running-{uuid.uuid4()}"
        job = seed_job(job_id, build_domain.STATUS_PROVISIONING)
        self._reconcile(job, rc75_invocation(status="InProgress"))
        stored = get_job(job_id)
        assert stored["status"] == build_domain.STATUS_PROVISIONING
        assert "lock_deferral" not in stored

    # Feature: build-agent-exit75-deferral, Property 3: the deferral is reachable and never terminates a live build
    # **Validates: Requirements 2.1, 2.2**
    @given(status=st.sampled_from([build_domain.STATUS_PROVISIONING,
                                   build_domain.STATUS_BUILDING]),
           response_code=st.sampled_from([75, "75"]),
           status_details=st.sampled_from(["Failed", "Undeliverable",
                                           "Failed: exit status 75"]))
    def test_property_every_dispatch_shape_defers_and_keeps_the_runner(
            self, status, response_code, status_details):
        """For EVERY status an rc-75 ephemeral settlement can arrive in,
        the job is requeued at the head of its queue with its runner
        binding intact and no cleanup planned — never terminal."""
        clear_state()
        job_id = f"prop-shape-{uuid.uuid4()}"
        invocation = rc75_invocation(response_code=response_code)
        invocation["StatusDetails"] = status_details
        job = seed_job(job_id, status)
        _, ec2 = self._reconcile(job, invocation)

        stored = get_job(job_id)
        assert not build_domain.is_terminal(stored["status"])
        assert stored["status"] == build_domain.STATUS_QUEUED
        assert stored["deferred_at"] == NOW
        assert stored["created_at"] == CREATED_AT
        assert cleanup_state(stored) in (None, br.EFFECT_NOT_APPLICABLE)
        assert ec2.terminated == []


class TestDedicatedPostDispatchDeferral:
    """**Property 3** — the dedicated post-dispatch deferral, including
    the "allocation kept for dedicated servers" clause of Req 2.1 that
    no frozen test reached.

    **Validates: Requirements 2.1, 2.2, 3.4**
    """

    def setup_method(self):
        clear_state()

    def test_dedicated_deferral_requeues_at_head_and_keeps_allocation(self):
        job_id = f"ded-defer-{uuid.uuid4()}"
        server_id = f"srv-{uuid.uuid4()}"
        _SERVERS.put_item(Item={
            "server_id": server_id,
            "instance_id": INSTANCE,
            "name": "amd64-builder-1",
            "lifecycle_state": build_domain.SERVER_STATE_RUNNING,
            "running_build_job_id": job_id,
        })
        job = seed_job(job_id, build_domain.STATUS_BUILDING,
                       execution_mode="dedicated", runner=False,
                       server_id=server_id)
        ssm = FakeSsm()
        ssm.invocations["cmd-rc75"] = rc75_invocation()
        ec2 = FakeEc2()
        servers_by_id = {server_id: get_server(server_id)}
        with mock.patch.object(build_dispatcher, "ssm", ssm), \
                mock.patch.object(build_dispatcher, "ec2", ec2):
            build_dispatcher.command_reconciliation(
                [job], servers_by_id, NOW)

        stored = get_job(job_id)
        assert stored["status"] == build_domain.STATUS_QUEUED
        assert stored["deferred_at"] == NOW
        assert stored["created_at"] == CREATED_AT
        assert stored["lock_deferral"]["count"] == 1
        # Req 2.1 allocation-kept clause: the server slot stays held, so
        # no other job can slip onto the busy server while this one waits.
        assert get_server(server_id)["running_build_job_id"] == job_id
        assert cleanup_state(stored) in (None, br.EFFECT_NOT_APPLICABLE)
        assert ec2.terminated == []
        # And no terminal ledger/effects exist at all.
        assert "terminal_effects" not in stored
        assert "error" not in stored


# ===========================================================================
# Property 4 — the defer -> re-dispatch cycle, gated on the lock holder
# (review issues 2 and 9)
# ===========================================================================

def _deferred_job(job_id, count=1, deferred_at=None, first=None,
                  corroborated=True):
    deferred_at = NOW - 6 * MINUTE_MS if deferred_at is None else deferred_at
    return seed_job(
        job_id, build_domain.STATUS_QUEUED, command_id=None,
        dispatch_state=br.DISPATCH_TERMINAL,
        deferred_at=deferred_at,
        lock_deferral={
            "count": count,
            "first_deferred_at": first if first is not None
            else deferred_at - 2 * MINUTE_MS,
            "last_deferred_at": deferred_at,
            "last_command_id": "cmd-rc75",
            "settled_command_ids": ["cmd-rc75"],
            "marker_corroborated": corroborated,
        })


class TestRedispatchLockHolderGate:
    """**Property 4** — the ephemeral re-dispatch must carry the same
    pgrep verification the dedicated path runs through
    ``decide_predispatch``. The agent command's preamble (``chown -R``,
    ``git fetch``, ``git checkout --force -B <ref> origin/<ref>``) runs
    BEFORE ``portal-build-agent.sh`` reaches ``flock``, and
    ``portal-build.sh`` writes the TRACKED ``gdk-config.json``, so
    re-sending during the holder's build rewrites the tree it builds in.

    **Validates: Requirements 2.2, 3.4**
    """

    def setup_method(self):
        clear_state()

    def _provision(self, job, pgrep_output="", states=None,
                   run_runner=None, now=NOW):
        send = mock.Mock(return_value=("cmd-redispatch", "stream"))
        launch = run_runner or mock.Mock(
            side_effect=AssertionError("no new compute expected"))
        ec2 = FakeEc2(states)
        with mock.patch.object(build_dispatcher, "ec2", ec2), \
                mock.patch.object(build_dispatcher, "run_shell_sync",
                                  shell_sync_fake(
                                      pgrep_output=pgrep_output)), \
                mock.patch.object(build_dispatcher, "instance_ssm_online",
                                  lambda _i: True), \
                mock.patch.object(
                    build_dispatcher, "decide_preflight",
                    lambda *_a, **_k: build_dispatcher.PreflightDecision(
                        ok=True, failures=(), checks={})), \
                mock.patch.object(build_dispatcher, "run_runner_instance",
                                  launch), \
                mock.patch.object(build_dispatcher, "send_agent", send):
            build_dispatcher.provision_ephemeral([job], now)
        return send, launch, ec2

    def test_no_send_while_a_build_process_is_present(self):
        """The review's blocking finding: the re-dispatch used to send
        with no lock-holder verification at all."""
        job_id = f"redispatch-blocked-{uuid.uuid4()}"
        job = _deferred_job(job_id)
        send, _, _ = self._provision(job, pgrep_output=BUILD_PROCESS_LINE)

        assert send.call_count == 0, (
            "an agent command was re-sent while a build process was "
            "running on the runner: its preamble would git-checkout "
            "--force the tree the lock holder is building in")
        stored = get_job(job_id)
        assert stored["status"] == build_domain.STATUS_QUEUED
        assert stored["deferred_at"] == NOW   # cadence advanced
        assert (stored.get("ssm") or {}).get("command_id") is None

    def test_no_send_when_the_verification_is_unverifiable(self):
        """Fail closed on an unverifiable check, exactly as the
        dedicated path does (``run_shell_sync`` None convention)."""
        job_id = f"redispatch-unknown-{uuid.uuid4()}"
        job = _deferred_job(job_id)
        send, _, _ = self._provision(job, pgrep_output=None)
        assert send.call_count == 0
        assert get_job(job_id)["status"] == build_domain.STATUS_QUEUED

    def test_send_proceeds_on_the_same_runner_once_the_lock_frees(self):
        """The other half of the cycle: a clean verification re-dispatches
        on the SAME runner (no new compute) with a FRESH attempt claimed
        conditionally over the settled one."""
        job_id = f"redispatch-clean-{uuid.uuid4()}"
        job = _deferred_job(job_id)
        prior_attempt = job["execution_attempt"]["attempt_id"]
        send, launch, _ = self._provision(
            job, pgrep_output="1234 sshd: ubuntu@pts/0")

        assert send.call_count == 1
        assert send.call_args[0][1] == INSTANCE
        assert launch.call_count == 0
        stored = get_job(job_id)
        assert stored["status"] == build_domain.STATUS_PROVISIONING
        assert stored["runner"]["instance_id"] == INSTANCE
        assert stored["ssm"]["command_id"] == "cmd-redispatch"
        assert stored["execution_attempt"]["attempt_id"] != prior_attempt
        assert stored["execution_attempt"]["dispatch_state"] == \
            br.DISPATCH_SENT

    def test_not_due_inside_the_reverification_interval(self):
        job_id = f"redispatch-early-{uuid.uuid4()}"
        job = _deferred_job(job_id, deferred_at=NOW - MINUTE_MS)
        send, _, _ = self._provision(job, pgrep_output="")
        assert send.call_count == 0
        stored = get_job(job_id)
        assert stored["status"] == build_domain.STATUS_QUEUED
        assert stored["deferred_at"] == NOW - MINUTE_MS  # untouched

    def test_a_gone_runner_provisions_replacement_compute(self):
        """Review issue 9: an absent ``runner.terminated_at`` only records
        that THIS dispatcher never terminated the instance. A
        spot-reclaimed runner must not be treated as live."""
        job_id = f"redispatch-gone-{uuid.uuid4()}"
        job = _deferred_job(job_id)
        launch = mock.Mock(return_value="i-replacement")
        send, launch, _ = self._provision(
            job, pgrep_output="", states={INSTANCE: "terminated"},
            run_runner=launch)
        assert launch.call_count == 1, (
            "the deferred job kept waiting on an instance that no longer "
            "exists instead of provisioning replacement compute")
        assert send.call_count == 0   # the new runner is not ready yet
        stored = get_job(job_id)
        assert stored["runner"]["instance_id"] == "i-replacement"
        # The deferral clock is refreshed so the stalled-deferral
        # backstop does not judge the replacement runner's bootstrap by
        # the abandoned runner's stale clock.
        assert stored["deferred_at"] == NOW

    # Feature: build-agent-exit75-deferral, Property 4: no re-send while a build process is present
    # **Validates: Requirements 2.2, 3.4**
    @given(pids=st.lists(st.integers(min_value=100, max_value=999_999),
                         min_size=1, max_size=3),
           pattern=st.sampled_from(
               sorted(build_planner.BUILD_PROCESS_PATTERNS)),
           noise=st.lists(st.sampled_from(
               ["1234 sshd: ubuntu@pts/0", "901 [kworker/0:1]", "55 bash"]),
               max_size=3))
    def test_property_a_running_build_always_blocks_the_resend(
            self, pids, pattern, noise):
        """For ANY pgrep output reporting at least one build process, the
        deferred job is never re-dispatched."""
        clear_state()
        job_id = f"prop-gate-{uuid.uuid4()}"
        job = _deferred_job(job_id)
        lines = noise + [f"{pid} bash {pattern} arg" for pid in pids]
        send, _, _ = self._provision(job, pgrep_output="\n".join(lines))
        assert send.call_count == 0
        assert get_job(job_id)["status"] == build_domain.STATUS_QUEUED


# ===========================================================================
# Property 5 — bounded deferral pressure, an external backstop, and no
# runner termination while a build may still be running
# (review issues 6, 7, 8, and the issue-10 corroboration decision)
# ===========================================================================

class TestDeferralPressureBudget:
    """**Property 5** — the valve bounds deferral CYCLES using the job's
    OWN resolved runtime ceiling.

    **Validates: Requirements 2.1, 3.8**
    """

    def test_budget_uses_effective_budget_not_max_runtime_ms(self):
        """Review issue 7: ``build_planner.max_runtime_ms`` reads only
        the snapshot's ``max_runtime_hours`` and would size an 8-hour
        target's valve at the 4-hour compatibility default."""
        job = {
            "build_job_id": "budget-job",
            "build_target": build_domain.TARGET_AMD64,
            "execution_mode": build_domain.EXECUTION_MODE_EPHEMERAL,
            "config_snapshot": {"runtime_budgets": {
                build_domain.TARGET_AMD64: {
                    build_domain.EXECUTION_MODE_EPHEMERAL: {
                        "hard_runtime_hours": 8}}}},
        }
        interval = build_planner.PREDISPATCH_RETRY_INTERVAL_MS
        assert br.effective_budget(job).hard_runtime_ms == 8 * HOUR_MS
        assert build_planner.max_runtime_ms(job["config_snapshot"]) == \
            4 * HOUR_MS      # the wrong number the review flagged
        assert build_dispatcher.lock_deferral_window_ms(job) == 8 * HOUR_MS
        assert build_dispatcher.lock_deferral_cycle_budget(job) == \
            (8 * HOUR_MS) // interval

    def test_budget_falls_back_to_snapshot_max_runtime_hours(self):
        job = {"build_job_id": "b", "config_snapshot":
               {"max_runtime_hours": 4}}
        interval = build_planner.PREDISPATCH_RETRY_INTERVAL_MS
        assert build_dispatcher.lock_deferral_cycle_budget(job) == \
            (4 * HOUR_MS) // interval

    def test_uncorroborated_rc75_gets_a_single_cycle(self):
        """Review issue 10, adopted as a PRESSURE bound rather than a
        classification gate: an rc 75 whose invocation text does not
        carry the agent's lock-held marker (an inner tool that itself
        exited 75) may defer once, never loop through rebuilds."""
        job = {"build_job_id": "b", "config_snapshot":
               {"max_runtime_hours": 4}}
        assert build_dispatcher.lock_deferral_cycle_budget(
            job, corroborated=False) == 1
        assert br.is_lock_held_evidence(rc75_invocation()) is True
        assert br.is_lock_held_evidence(
            rc75_invocation(marker=False)) is False
        # The CLASSIFICATION still keys on the response code alone, so a
        # genuine deferral whose stdout was truncated/re-encoded is never
        # re-hard-failed (bugfix.md 3.1 stays intact both ways).
        assert br.is_lock_held_deferral(br.classify_attempt(
            build_domain.STATUS_BUILDING,
            invocation=rc75_invocation(marker=False)))

    def test_pressure_not_wall_clock_a_late_first_deferral_still_defers(self):
        """Review issue 7's second half: a job that waited hours in a
        healthy pre-dispatch pgrep deferral must not hard-fail on the
        FIRST exit-75 it then sees."""
        clear_state()
        job_id = f"late-defer-{uuid.uuid4()}"
        job = seed_job(job_id, build_domain.STATUS_BUILDING,
                       lock_deferral={"count": 1,
                                      "first_deferred_at": NOW - 10 * HOUR_MS,
                                      "last_deferred_at": NOW - 10 * HOUR_MS,
                                      "last_command_id": "cmd-old"})
        assert build_dispatcher.defer_lock_held_job(
            job, "cmd-rc75", None, NOW) is True
        stored = get_job(job_id)
        assert stored["status"] == build_domain.STATUS_QUEUED
        assert stored["lock_deferral"]["count"] == 2

    def test_exhausted_budget_stops_deferring(self):
        clear_state()
        job_id = f"exhausted-{uuid.uuid4()}"
        budget = build_dispatcher.lock_deferral_cycle_budget(
            {"config_snapshot": {"max_runtime_hours": 4}})
        job = seed_job(job_id, build_domain.STATUS_BUILDING,
                       lock_deferral={"count": budget,
                                      "first_deferred_at": NOW - HOUR_MS,
                                      "last_deferred_at": NOW - MINUTE_MS,
                                      "last_command_id": "cmd-old"})
        assert build_dispatcher.defer_lock_held_job(
            job, "cmd-rc75", None, NOW) is False
        assert get_job(job_id)["status"] == build_domain.STATUS_BUILDING


class TestValveExitNeverKillsALiveBuild:
    """**Property 5** — review issue 8: the exhausted path used to settle
    FAILED with ``cleanup_required=True``, and ``termination_watchdog``
    keys on terminal status ALONE, so it terminated a runner that could
    still be running the lock-holder build — the incident harm, merely
    delayed by the window.

    **Validates: Requirements 2.2, 3.6**
    """

    def setup_method(self):
        clear_state()

    def _settle(self, job, build_count, states=None, now=NOW):
        ssm = FakeSsm()
        ssm.invocations["cmd-rc75"] = rc75_invocation()
        ec2 = FakeEc2(states)
        with mock.patch.object(build_dispatcher, "ssm", ssm), \
                mock.patch.object(build_dispatcher, "ec2", ec2), \
                mock.patch.object(build_dispatcher, "run_shell_sync",
                                  shell_sync_fake(build_count=build_count)):
            build_dispatcher.command_reconciliation([job], {}, now)
            # The watchdog would terminate a terminal ephemeral runner.
            build_dispatcher.termination_watchdog([get_job(
                job["build_job_id"])], now)
        return ec2

    def _exhausted_job(self, job_id, status=build_domain.STATUS_BUILDING):
        budget = build_dispatcher.lock_deferral_cycle_budget(
            {"config_snapshot": {"max_runtime_hours": 4}})
        return seed_job(job_id, status,
                        lock_deferral={"count": budget,
                                       "first_deferred_at": NOW - 5 * HOUR_MS,
                                       "last_deferred_at": NOW - MINUTE_MS,
                                       "last_command_id": "cmd-old",
                                       "settled_command_ids": ["cmd-old"]})

    def test_a_running_build_holds_the_job_nonterminal(self):
        job_id = f"valve-hold-{uuid.uuid4()}"
        job = self._exhausted_job(job_id)
        ec2 = self._settle(job, build_count=1)

        stored = get_job(job_id)
        assert not build_domain.is_terminal(stored["status"]), (
            "the exhausted deferral settled terminally while a build was "
            "still running on the runner")
        assert ec2.terminated == [], (
            "the runner was terminated under a live build — the exact "
            "incident harm (i-089a77f72bc558147 / command 1b538196)")
        assert stored["lock_deferral"]["valve_exhausted_at"] == NOW
        assert stored["lock_deferral"]["build_process_absent"] is False
        assert any(e["action"] == "build_lock_deferral_valve_held"
                   for e in AUDIT_EVENTS)

    def test_an_unverifiable_process_check_also_holds(self):
        """Fail closed: unknown process state is NOT absence."""
        job_id = f"valve-unknown-{uuid.uuid4()}"
        job = self._exhausted_job(job_id)
        ec2 = self._settle(job, build_count=None)
        assert not build_domain.is_terminal(get_job(job_id)["status"])
        assert ec2.terminated == []

    def test_settles_and_terminates_once_no_build_remains(self):
        """The valve must still be able to END a wedged deferral."""
        job_id = f"valve-settle-{uuid.uuid4()}"
        job = self._exhausted_job(job_id)
        ec2 = self._settle(job, build_count=0)

        stored = get_job(job_id)
        assert stored["status"] == build_domain.STATUS_FAILED
        assert stored["error"]["code"] == br.CODE_BUILD_LOCK_HELD
        # Cleanup was planned and the watchdog completed it on the
        # confirmed-idle runner.
        assert ec2.terminated == [[INSTANCE]]
        assert cleanup_state(stored) == br.EFFECT_DONE

    def test_a_gone_instance_settles_without_a_pgrep_answer(self):
        """A spot-reclaimed runner can host no build, and pgrep on it can
        only ever return unknown — the exit must not wedge there."""
        job_id = f"valve-gone-{uuid.uuid4()}"
        job = self._exhausted_job(job_id)
        ec2 = self._settle(job, build_count=None,
                           states={INSTANCE: "terminated"})
        stored = get_job(job_id)
        assert stored["status"] == build_domain.STATUS_FAILED
        assert stored["error"]["code"] == br.CODE_BUILD_LOCK_HELD

    def test_provisioning_valve_exit_obeys_the_same_gate(self):
        job_id = f"valve-prov-{uuid.uuid4()}"
        job = self._exhausted_job(job_id, build_domain.STATUS_PROVISIONING)
        ec2 = self._settle(job, build_count=2)
        assert not build_domain.is_terminal(get_job(job_id)["status"])
        assert ec2.terminated == []


class TestDeferralBackstop:
    """**Property 5** — review issue 6: the valve is only evaluated
    inside ``defer_lock_held_job``, the runtime watchdog skips
    queued/provisioning, and ``queue_wait_ms`` is frozen by the
    pre-existing ``dispatched_at``. A job that stops being re-dispatched
    had no bound at all.

    **Validates: Requirements 2.2, 3.8**
    """

    def setup_method(self):
        clear_state()

    def _run(self, job, build_count, states=None, now=NOW):
        ec2 = FakeEc2(states)
        with mock.patch.object(build_dispatcher, "ec2", ec2), \
                mock.patch.object(build_dispatcher, "run_shell_sync",
                                  shell_sync_fake(build_count=build_count)):
            build_dispatcher.lock_deferral_watchdog([job], {}, now)
        return ec2

    def test_a_stalled_deferral_is_settled_after_the_bound(self):
        job_id = f"stall-settle-{uuid.uuid4()}"
        stalled_at = NOW - build_dispatcher.LOCK_DEFERRAL_STALL_MS \
            - MINUTE_MS
        job = _deferred_job(job_id, deferred_at=stalled_at)
        self._run(job, build_count=0)

        stored = get_job(job_id)
        assert stored["status"] == build_domain.STATUS_FAILED
        assert stored["error"]["code"] == br.CODE_BUILD_LOCK_HELD
        assert cleanup_state(stored) == br.EFFECT_PENDING
        assert any(e["action"] == "build_lock_deferral_stalled"
                   for e in AUDIT_EVENTS)

    def test_a_stalled_deferral_with_a_live_build_is_held_not_killed(self):
        job_id = f"stall-hold-{uuid.uuid4()}"
        stalled_at = NOW - build_dispatcher.LOCK_DEFERRAL_STALL_MS \
            - MINUTE_MS
        job = _deferred_job(job_id, deferred_at=stalled_at)
        ec2 = self._run(job, build_count=1)

        stored = get_job(job_id)
        assert stored["status"] == build_domain.STATUS_QUEUED
        assert ec2.terminated == []
        assert stored["lock_deferral"]["valve_hold_cause"] == \
            "deferral_stalled"

    def test_a_progressing_deferral_is_untouched(self):
        job_id = f"stall-fresh-{uuid.uuid4()}"
        job = _deferred_job(job_id, deferred_at=NOW - 2 * MINUTE_MS)
        self._run(job, build_count=0)
        stored = get_job(job_id)
        assert stored["status"] == build_domain.STATUS_QUEUED
        assert "error" not in stored

    def test_jobs_without_a_deferral_are_untouched(self):
        job_id = f"stall-none-{uuid.uuid4()}"
        job = seed_job(job_id, build_domain.STATUS_QUEUED, command_id=None,
                       deferred_at=NOW - 10 * HOUR_MS)
        self._run(job, build_count=0)
        assert get_job(job_id)["status"] == build_domain.STATUS_QUEUED

    def test_a_stalled_provisioning_deferral_is_also_covered(self):
        job_id = f"stall-prov-{uuid.uuid4()}"
        stalled_at = NOW - build_dispatcher.LOCK_DEFERRAL_STALL_MS \
            - MINUTE_MS
        job = _deferred_job(job_id, deferred_at=stalled_at)
        job["status"] = build_domain.STATUS_PROVISIONING
        _JOBS.update_item(
            Key={"build_job_id": job_id},
            UpdateExpression="SET #s = :s",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":s": build_domain.STATUS_PROVISIONING})
        self._run(job, build_count=0)
        assert get_job(job_id)["status"] == build_domain.STATUS_FAILED


# ===========================================================================
# Property 6 — the resend gate never authorizes a send on unproven
# non-execution (review issues 3, 4, 5, 11, 12)
# ===========================================================================

class TestResendGatePredicates:
    """**Property 6** — ``cancel_command_confirmed`` and
    ``prior_command_proven_never_executed``, which the frozen suites
    exercised only in the ``InProgress`` shape.

    **Validates: Requirements 2.5**
    """

    def _match(self, command_id="cmd-prior", list_status="Failed"):
        return build_dispatcher.CommandMatch(
            command_id=command_id, attempt_id="88dcd6b8",
            comment=br.command_comment("job-1", "88dcd6b8"),
            list_status=list_status, exact=False)

    def test_incident_shape_is_indeterminate_not_proof(self):
        """Review issue 3 (blocking): terminal 'Failed'/'Undeliverable'
        with no ``ExecutionStartDateTime`` is EXACTLY command 1b538196,
        which was executing and holding the build lock. It must never
        read as proof of non-execution."""
        ssm = FakeSsm()
        ssm.invocations["cmd-prior"] = {
            "CommandId": "cmd-prior", "InstanceId": INSTANCE,
            "Status": "Failed", "StatusDetails": "Undeliverable",
            "ResponseCode": -1,
            "StandardOutputContent": "", "StandardErrorContent": "",
        }
        with mock.patch.object(build_dispatcher, "ssm", ssm):
            assert build_dispatcher.prior_command_proven_never_executed(
                self._match(), INSTANCE) is None

    def test_execution_evidence_means_attach(self):
        ssm = FakeSsm()
        ssm.invocations["cmd-prior"] = {
            "CommandId": "cmd-prior", "InstanceId": INSTANCE,
            "Status": "Failed", "StatusDetails": "Failed",
            "ExecutionStartDateTime": "2026-09-09T12:54:04Z",
        }
        with mock.patch.object(build_dispatcher, "ssm", ssm):
            assert build_dispatcher.prior_command_proven_never_executed(
                self._match(), INSTANCE) is False

    def test_unreadable_invocation_is_not_absence(self):
        """Review issue 4: throttling/AccessDenied/5xx used to collapse
        into the same None as InvocationDoesNotExist."""
        ssm = FakeSsm()
        ssm.errors["cmd-prior"] = "ThrottlingException"
        with mock.patch.object(build_dispatcher, "ssm", ssm):
            assert build_dispatcher.prior_command_proven_never_executed(
                self._match(), INSTANCE) is None
            read = build_dispatcher.read_invocation("cmd-prior", INSTANCE)
        assert read.state == build_dispatcher.INVOCATION_READ_UNREADABLE

    def test_absent_invocation_with_terminal_delivery_is_proof(self):
        ssm = FakeSsm()   # no invocation recorded -> InvocationDoesNotExist
        with mock.patch.object(build_dispatcher, "ssm", ssm):
            assert build_dispatcher.prior_command_proven_never_executed(
                self._match(), INSTANCE) is True
            read = build_dispatcher.read_invocation("cmd-prior", INSTANCE)
        assert read.state == build_dispatcher.INVOCATION_READ_ABSENT

    def test_absent_invocation_without_terminal_delivery_proves_nothing(self):
        ssm = FakeSsm()
        with mock.patch.object(build_dispatcher, "ssm", ssm):
            assert build_dispatcher.prior_command_proven_never_executed(
                self._match(list_status="InProgress"), INSTANCE) is None

    def test_cancel_confirmed_when_the_invocation_reads_terminal(self):
        ssm = FakeSsm()
        ssm.invocations["cmd-prior"] = {
            "CommandId": "cmd-prior", "InstanceId": INSTANCE,
            "Status": "Cancelled", "StatusDetails": "Cancelled"}
        with mock.patch.object(build_dispatcher, "ssm", ssm):
            assert build_dispatcher.cancel_command_confirmed(
                "cmd-prior", INSTANCE) is True
        assert ssm.cancels and ssm.cancels[0]["CommandId"] == "cmd-prior"

    def test_cancel_confirmed_when_no_invocation_exists(self):
        ssm = FakeSsm()
        with mock.patch.object(build_dispatcher, "ssm", ssm):
            assert build_dispatcher.cancel_command_confirmed(
                "cmd-prior", INSTANCE) is True

    def test_cancel_not_confirmed_when_the_read_fails(self):
        """Review issue 4: the gate used to return True here — a cheap
        CancelCommand plus a throttled read authorized a resend over a
        live command."""
        ssm = FakeSsm()
        ssm.errors["cmd-prior"] = "ThrottlingException"
        with mock.patch.object(build_dispatcher, "ssm", ssm):
            assert build_dispatcher.cancel_command_confirmed(
                "cmd-prior", INSTANCE) is False

    def test_cancel_not_confirmed_when_still_in_progress(self):
        ssm = FakeSsm()
        ssm.invocations["cmd-prior"] = {
            "CommandId": "cmd-prior", "InstanceId": INSTANCE,
            "Status": "InProgress"}
        with mock.patch.object(build_dispatcher, "ssm", ssm):
            assert build_dispatcher.cancel_command_confirmed(
                "cmd-prior", INSTANCE) is False

    def test_cancel_not_confirmed_when_the_cancel_call_fails(self):
        class _Failing(FakeSsm):
            def cancel_command(self, **kwargs):
                raise ClientError(
                    {"Error": {"Code": "AccessDeniedException"}},
                    "CancelCommand")
        with mock.patch.object(build_dispatcher, "ssm", _Failing()):
            assert build_dispatcher.cancel_command_confirmed(
                "cmd-prior", INSTANCE) is False


class TestRecoverAmbiguousSendGate:
    """**Property 6** — the same gate through ``recover_ambiguous_send``:
    the incident's evidence shape must not produce a send.

    **Validates: Requirements 2.4, 2.5**
    """

    def setup_method(self):
        clear_state()

    def _sending_job(self, job_id, attempt_id="6c6b5483",
                     lock_deferral=None):
        sending_at = NOW - build_dispatcher.AMBIGUOUS_SEND_VISIBILITY_MS \
            - MINUTE_MS
        job = {
            "build_job_id": job_id,
            "build_target": build_domain.TARGET_AMD64,
            "execution_mode": build_domain.EXECUTION_MODE_EPHEMERAL,
            "status": build_domain.STATUS_BUILDING,
            "requested_by": "operator-1",
            "created_at": CREATED_AT,
            "config_snapshot": {"max_runtime_hours": 4},
            "runner": {"instance_id": INSTANCE},
            "execution_attempt": {
                "attempt_id": attempt_id,
                "dispatch_state": br.DISPATCH_SENDING,
                "instance_id": INSTANCE,
                "command_id": None,
                "command_comment": br.command_comment(job_id, attempt_id),
                "claimed_at": sending_at,
                "sending_at": sending_at,
                "sent_at": None,
            },
        }
        if lock_deferral is not None:
            job["lock_deferral"] = lock_deferral
        _JOBS.put_item(Item=build_dispatcher.to_dynamo(job))
        return build_dispatcher.to_native(job)

    def test_indeterminate_prior_command_with_unreadable_recheck_waits(self):
        job_id = f"gate-wait-{uuid.uuid4()}"
        job = self._sending_job(job_id)
        prior_comment = br.command_comment(job_id, "88dcd6b8")
        ssm = FakeSsm()
        ssm.commands = [{"CommandId": "cmd-prior", "InstanceId": INSTANCE,
                         "Comment": prior_comment, "Status": "Failed",
                         "StatusDetails": "Undeliverable"}]
        # The incident shape: terminal delivery, no execution evidence.
        ssm.invocations["cmd-prior"] = {
            "CommandId": "cmd-prior", "InstanceId": INSTANCE,
            "Status": "Failed", "StatusDetails": "Undeliverable",
            "ResponseCode": -1}
        send = mock.Mock(return_value=("cmd-new", "stream"))

        def _cancel_then_unreadable(**kwargs):
            ssm.cancels.append(dict(kwargs))
            ssm.errors["cmd-prior"] = "ThrottlingException"
            return {}
        ssm.cancel_command = _cancel_then_unreadable
        with mock.patch.object(build_dispatcher, "ssm", ssm), \
                mock.patch.object(build_dispatcher, "send_agent", send):
            build_dispatcher.recover_ambiguous_send(job, {}, NOW)

        assert send.call_count == 0, (
            "a resend went out while the prior command's execution was "
            "unproven and its cancellation unconfirmed")
        stored = get_job(job_id)
        assert stored["execution_attempt"]["dispatch_state"] == \
            br.DISPATCH_SENDING
        assert (stored.get("ssm") or {}).get("command_id") is None

    def test_confirmed_cancellation_releases_exactly_one_resend(self):
        """The gate must not wedge forever: once the prior command's
        cancellation IS confirmed, one conditional resend proceeds."""
        job_id = f"gate-resend-{uuid.uuid4()}"
        job = self._sending_job(job_id)
        prior_comment = br.command_comment(job_id, "88dcd6b8")
        ssm = FakeSsm()
        ssm.commands = [{"CommandId": "cmd-prior", "InstanceId": INSTANCE,
                         "Comment": prior_comment, "Status": "Failed",
                         "StatusDetails": "Undeliverable"}]
        ssm.invocations["cmd-prior"] = {
            "CommandId": "cmd-prior", "InstanceId": INSTANCE,
            "Status": "Failed", "StatusDetails": "Undeliverable",
            "ResponseCode": -1}

        def _cancel_then_terminal(**kwargs):
            ssm.cancels.append(dict(kwargs))
            ssm.invocations["cmd-prior"] = {
                "CommandId": "cmd-prior", "InstanceId": INSTANCE,
                "Status": "Cancelled", "StatusDetails": "Cancelled"}
            return {}
        ssm.cancel_command = _cancel_then_terminal
        send = mock.Mock(return_value=("cmd-new", "stream"))
        with mock.patch.object(build_dispatcher, "ssm", ssm), \
                mock.patch.object(build_dispatcher, "send_agent", send):
            build_dispatcher.recover_ambiguous_send(job, {}, NOW)

        assert ssm.cancels and send.call_count == 1
        assert get_job(job_id)["ssm"]["command_id"] == "cmd-new"

    def test_a_settled_deferral_command_is_never_reattached(self):
        """Review issue 5: a previous deferral cycle's rc-75 command is
        findable by job id; attaching it re-defers the job while the
        command the current attempt sent stays orphaned."""
        job_id = f"gate-stale-{uuid.uuid4()}"
        job = self._sending_job(
            job_id, lock_deferral={"count": 1,
                                   "last_command_id": "cmd-settled",
                                   "settled_command_ids": ["cmd-settled"]})
        settled_comment = br.command_comment(job_id, "88dcd6b8")
        ssm = FakeSsm()
        ssm.commands = [{"CommandId": "cmd-settled", "InstanceId": INSTANCE,
                         "Comment": settled_comment, "Status": "Failed",
                         "StatusDetails": "Failed"}]
        ssm.invocations["cmd-settled"] = {
            "CommandId": "cmd-settled", "InstanceId": INSTANCE,
            "Status": "Failed", "ResponseCode": 75,
            "ExecutionStartDateTime": "2026-09-09T12:54:08Z"}
        send = mock.Mock(return_value=("cmd-new", "stream"))
        with mock.patch.object(build_dispatcher, "ssm", ssm), \
                mock.patch.object(build_dispatcher, "send_agent", send):
            found = build_dispatcher.find_command_by_comment(
                INSTANCE, job["execution_attempt"]["command_comment"],
                exclude_command_ids=build_dispatcher.settled_command_ids(
                    job))
            build_dispatcher.recover_ambiguous_send(job, {}, NOW)

        assert found is None, (
            "an already-settled deferral command was offered for "
            "re-attachment")
        assert (get_job(job_id).get("ssm") or {}).get("command_id") != \
            "cmd-settled"

    def test_prior_command_adoption_is_recorded_explicitly(self):
        """Review issue 12: ``_attach`` used to revive a settled attempt
        id from terminal to sent while KEEPING the current attempt's
        clocks, so the attempt's timeline described a different dispatch
        than the command it pointed at."""
        job_id = f"gate-adopt-{uuid.uuid4()}"
        current = "6c6b5483"
        job = self._sending_job(job_id, attempt_id=current)
        superseded_claimed = job["execution_attempt"]["claimed_at"]
        prior_comment = br.command_comment(job_id, "88dcd6b8")
        ssm = FakeSsm()
        ssm.commands = [{"CommandId": "cmd-prior", "InstanceId": INSTANCE,
                         "Comment": prior_comment, "Status": "InProgress"}]
        send = mock.Mock(side_effect=AssertionError("no resend expected"))
        with mock.patch.object(build_dispatcher, "ssm", ssm), \
                mock.patch.object(build_dispatcher, "send_agent", send):
            build_dispatcher.recover_ambiguous_send(job, {}, NOW)

        attempt = get_job(job_id)["execution_attempt"]
        adopted = attempt["adopted_command"]
        # The command's OWN attempt identity is adopted, because the agent
        # was started with it and every phase event correlates by
        # attempt_id (evidence_matches_attempt).
        assert attempt["attempt_id"] == "88dcd6b8"
        assert attempt["command_id"] == "cmd-prior"
        # ...and the adoption is recorded, with the abandoned dispatch's
        # clocks moved out of the live fields.
        assert adopted["command_id"] == "cmd-prior"
        assert adopted["adopted_at"] == NOW
        assert adopted["superseded_attempt_id"] == current
        assert adopted["superseded_claimed_at"] == superseded_claimed
        assert attempt["claimed_at"] is None
        assert attempt["sending_at"] is None


class TestExecutionAttemptClaimEdges:
    """**Property 6** — review issue 11: the claim condition and the
    diagnostic that explains a refusal.

    **Validates: Requirements 2.3**
    """

    def setup_method(self):
        clear_state()

    def _attempt(self, job_id, attempt_id="fresh"):
        attempt = br.new_execution_attempt(job_id, attempt_id, INSTANCE, NOW)
        attempt["dispatch_state"] = br.DISPATCH_SENDING
        attempt["sending_at"] = NOW
        return attempt

    def test_claim_wins_when_no_attempt_exists(self):
        job_id = f"claim-fresh-{uuid.uuid4()}"
        _JOBS.put_item(Item={"build_job_id": job_id, "status": "provisioning"})
        assert build_dispatcher.claim_execution_attempt(
            job_id, self._attempt(job_id), prior_attempt=None) == \
            build_dispatcher.CLAIM_WON
        assert get_job(job_id)["execution_attempt"]["attempt_id"] == "fresh"

    def test_claim_wins_when_the_attempt_attribute_is_null(self):
        """The tightened condition still admits an explicitly NULL
        attribute (writers in this module do persist None values)."""
        job_id = f"claim-null-{uuid.uuid4()}"
        _JOBS.put_item(Item={"build_job_id": job_id,
                             "status": "provisioning",
                             "execution_attempt": None})
        assert build_dispatcher.claim_execution_attempt(
            job_id, self._attempt(job_id), prior_attempt=None) == \
            build_dispatcher.CLAIM_WON

    def test_claim_refuses_an_attempt_map_without_an_attempt_id(self):
        """``attribute_not_exists(execution_attempt.attempt_id)`` also
        passed for a map that merely lacks ``attempt_id`` — the claim
        could overwrite it."""
        job_id = f"claim-partial-{uuid.uuid4()}"
        _JOBS.put_item(Item={
            "build_job_id": job_id, "status": "provisioning",
            "execution_attempt": {"dispatch_state": br.DISPATCH_SENT,
                                  "command_id": "cmd-live"}})
        assert build_dispatcher.claim_execution_attempt(
            job_id, self._attempt(job_id), prior_attempt=None) == \
            build_dispatcher.CLAIM_LOST
        stored = get_job(job_id)["execution_attempt"]
        assert stored == {"dispatch_state": br.DISPATCH_SENT,
                          "command_id": "cmd-live"}

    def test_claim_reports_a_live_attempt_distinctly_from_a_lost_race(self):
        """The loser's log line used to blame "a concurrent tick" even
        when the local live-attempt pre-check refused."""
        job_id = f"claim-live-{uuid.uuid4()}"
        _JOBS.put_item(Item={"build_job_id": job_id, "status": "provisioning"})
        live = {"attempt_id": "live-1", "dispatch_state": br.DISPATCH_SENT}
        assert build_dispatcher.claim_execution_attempt(
            job_id, self._attempt(job_id), prior_attempt=live) == \
            build_dispatcher.CLAIM_LIVE_ATTEMPT
        assert "execution_attempt" not in get_job(job_id)  # no write at all

    def test_claim_supersedes_exactly_one_settled_attempt(self):
        job_id = f"claim-settled-{uuid.uuid4()}"
        settled = {"attempt_id": "settled-1",
                   "dispatch_state": br.DISPATCH_TERMINAL,
                   "settled_as": "lock_held_deferral"}
        _JOBS.put_item(Item={"build_job_id": job_id,
                             "status": "provisioning",
                             "execution_attempt": settled})
        assert build_dispatcher.claim_execution_attempt(
            job_id, self._attempt(job_id, "second"),
            prior_attempt=settled) == build_dispatcher.CLAIM_WON
        # A second writer holding the SAME stale snapshot now loses.
        assert build_dispatcher.claim_execution_attempt(
            job_id, self._attempt(job_id, "third"),
            prior_attempt=settled) == build_dispatcher.CLAIM_LOST
        assert get_job(job_id)["execution_attempt"]["attempt_id"] == "second"


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "--noconftest", "-q",
                          "-p", "no:cacheprovider"]))
