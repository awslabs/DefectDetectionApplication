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
No-live-action validation contract
(build-fleet-execution-failures task 10.6).

**Property 15: Approval-Gated Timeout Validation** — _for any_ timeout
correction validation, safe mocked and property-based checks SHALL
complete before a costly live build, and no production timeout
configuration change or dedicated/ephemeral build launch SHALL occur
without explicit user approval. Local validation cannot authorize a
costly action.

**Validates: Requirements 2.12, 2.19**

Recording/FAILING adapters replace every AWS client the dispatcher
binds (SSM, EC2, SNS, DynamoDB): read-only operations needed for
evidence recovery return canned data and are recorded; EVERY other
operation — deployment, production setting write, SSM ``SendCommand``,
instance action (run/start/stop/terminate), artifact publication, live
build — is recorded as PROHIBITED and raises. Representative dispatch,
scheduled-reconciliation, timeout-watchdog, and terminal-effects flows
are then driven with mocked persistence/shell services, and the suite
asserts the recorders captured ZERO prohibited calls.

The raise alone is not sufficient proof: several flows deliberately
swallow per-job exceptions so one job cannot poison a tick — which
would also swallow an assertion. The PROHIBITED ledger is therefore the
authoritative oracle, asserted empty after every flow.

Run ONLY this file (with the sibling target-matrix properties), from
the repository root:

    PYTHONPATH=src/backend:test/backend-test python3 -m pytest \\
        test/backend-test/portal_builds/test_preflight_target_matrix_properties.py \\
        test/backend-test/portal_builds/test_no_live_validation_contract.py \\
        --noconftest -q

(This run contains property-based tests and may generate/shrink
counterexamples.)
"""
import json
import os
import sys
import types
from unittest import mock

import pytest
from hypothesis import given, settings, strategies as st

# ---------------------------------------------------------------------------
# Environment BEFORE any import: the handlers bind their boto3 handles
# and env-derived configuration at import time. Nothing live is called.
# ---------------------------------------------------------------------------
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ["AWS_ACCESS_KEY_ID"] = "testing"
os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
os.environ["AWS_SECURITY_TOKEN"] = "testing"
os.environ["AWS_SESSION_TOKEN"] = "testing"
os.environ.setdefault("BUILD_JOBS_TABLE", "dda-portal-build-jobs-nolive")
os.environ.setdefault("BUILD_SERVERS_TABLE",
                      "dda-portal-build-servers-nolive")
os.environ.pop("BUILD_REPO_DIR", None)

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.normpath(os.path.join(_HERE, "..", "..", ".."))
_FUNCTIONS_DIR = os.path.join(
    _REPO_ROOT, "edge-cv-portal", "backend", "functions")
if _FUNCTIONS_DIR not in sys.path:
    sys.path.insert(0, _FUNCTIONS_DIR)

# Fake shared_utils capturing Audit_Log entries.
AUDIT_EVENTS = []
_stub = types.ModuleType("shared_utils")
_stub.log_audit_event = lambda **kwargs: AUDIT_EVENTS.append(kwargs)
sys.modules["shared_utils"] = _stub

import build_domain  # noqa: E402
import build_reconciliation as br  # noqa: E402
import build_dispatcher  # noqa: E402

# Order-independence within one pytest process: a sibling suite may
# already have imported build_dispatcher with ITS OWN shared_utils
# stub, in which case `from shared_utils import log_audit_event` bound
# that earlier stub. Rebind the dispatcher's name to THIS file's
# recording stub so AUDIT_EVENTS is authoritative regardless of which
# suite imported the handler first.
build_dispatcher.log_audit_event = _stub.log_audit_event

NOW = 1_754_500_000_000
_HOUR_MS = 60 * 60 * 1000

#: Secret-shaped canary planted in provider evidence: it must never
#: survive into any captured sink (Req 2.10 supports the safe-local
#: validation posture of Req 2.19).
SECRET_CANARY = "AKIAAAAABBBBCCCCDDDD"

# ---------------------------------------------------------------------------
# Recording/failing AWS client adapters (Property 15)
# ---------------------------------------------------------------------------

#: The authoritative prohibited-call ledger: (client, operation) pairs.
PROHIBITED_CALLS = []

#: Operations that constitute a live/costly action if any validation
#: flow ever reached a real client with them (the design's prohibition:
#: deployment, production setting write, SSM send, instance action,
#: artifact publication, live build).
PROHIBITED_VOCABULARY = frozenset({
    ("ssm", "send_command"),          # live build / SSM send
    ("ssm", "cancel_command"),
    ("ssm", "put_parameter"),         # production setting write
    ("ec2", "run_instances"),         # instance action
    ("ec2", "start_instances"),
    ("ec2", "stop_instances"),
    ("ec2", "terminate_instances"),
    ("sns", "publish"),
    ("events", "put_events"),
    ("cloudformation", "create_stack"),   # deployment
    ("cloudformation", "update_stack"),
    ("s3", "put_object"),             # artifact publication
})


class RecordingFailingClient:
    """Stands in for one live AWS client: allow-listed READ operations
    return canned data and are recorded; everything else is recorded on
    the PROHIBITED ledger and raises."""

    def __init__(self, name, reads=None):
        self._name = name
        self._reads = dict(reads or {})
        self.read_calls = []

    def __getattr__(self, operation):
        if operation.startswith("_"):
            raise AttributeError(operation)

        def _call(**kwargs):
            if operation in self._reads:
                self.read_calls.append((operation, kwargs))
                handler = self._reads[operation]
                return handler(**kwargs) if callable(handler) else handler
            PROHIBITED_CALLS.append((self._name, operation))
            raise AssertionError(
                f"PROHIBITED live action: {self._name}.{operation} — "
                f"local validation cannot authorize a costly action "
                f"(Property 15, Req 2.19)")
        return _call


class FailingTableFactory:
    """DynamoDB table accessor stand-in: any direct table use during
    these flows is a write path this suite did not mock — recorded as
    prohibited (production/persistence writes go through the patched
    in-memory persistence seams instead)."""

    def __call__(self):
        PROHIBITED_CALLS.append(("dynamodb", "table_access"))
        raise AssertionError(
            "PROHIBITED direct DynamoDB access during no-live validation")


# ---------------------------------------------------------------------------
# In-memory persistence (mocked services; conditional semantics kept)
# ---------------------------------------------------------------------------

class MemoryPersistence:
    """In-memory conditional persistence for the dispatcher's seam
    functions, recording every write so sinks can be canary-scanned."""

    def __init__(self, jobs, servers=()):
        self.jobs = {job["build_job_id"]: dict(job) for job in jobs}
        self.servers = {server["server_id"]: dict(server)
                        for server in servers}
        self.writes = []

    def transition_job(self, build_job_id, expected_status, new_status,
                       extra=None):
        job = self.jobs.get(build_job_id)
        if job is None or job.get("status") != expected_status:
            return False
        job["status"] = new_status
        job.update(extra or {})
        self.writes.append(("transition", build_job_id, new_status,
                            extra or {}))
        return True

    def update_job_fields(self, build_job_id, fields):
        job = self.jobs.setdefault(build_job_id,
                                   {"build_job_id": build_job_id})
        job.update(fields)
        self.writes.append(("update", build_job_id, fields))

    def complete_effect(self, build_job_id, effect_id, effect):
        job = self.jobs.get(build_job_id) or {}
        ledger = job.get("terminal_effects")
        if not effect_id or not isinstance(ledger, dict):
            return False
        if ledger.get("effect_id") != effect_id:
            return False
        if ledger.get(effect) != br.EFFECT_PENDING:
            return False
        ledger[effect] = br.EFFECT_DONE
        self.writes.append(("effect", build_job_id, effect))
        return True

    def release_server(self, server_id, build_job_id):
        server = self.servers.get(server_id) or {}
        if server.get("running_build_job_id") != build_job_id:
            return False
        server.pop("running_build_job_id", None)
        self.writes.append(("release", server_id, build_job_id))
        return True

    def sink_text(self):
        return json.dumps({"writes": self.writes, "jobs": self.jobs,
                           "audits": AUDIT_EVENTS},
                          default=str, sort_keys=True)


def _job(job_id, status, mode=build_domain.EXECUTION_MODE_DEDICATED,
         target="AMD64", **extra):
    job = {
        "build_job_id": job_id,
        "build_target": target,
        "execution_mode": mode,
        "component_name":
            build_domain.BUILD_TARGETS[target]["component_name"],
        "status": status,
        "created_at": NOW - 10 * _HOUR_MS,
        "config_snapshot": {"max_runtime_hours": 4},
    }
    job.update(extra)
    return job


_SERVER = {"server_id": "srv-1", "instance_id": "i-0000000000000000a",
           "lifecycle_state": "running",
           "repo_dir": "/home/ubuntu/DefectDetectionApplication"}


class _Flow:
    """One representative flow driven under the failing adapters."""

    def __init__(self, ssm_reads=None):
        self.ssm = RecordingFailingClient("ssm", reads=ssm_reads)
        self.ec2 = RecordingFailingClient("ec2")
        self.sns = RecordingFailingClient("sns")
        self.shell_commands = []  # mocked stop/pgrep seam captures

    def run(self, memory, callable_, *args, shell_output=""):
        def _send_shell(instance_id, commands, **kwargs):
            self.shell_commands.append((instance_id, tuple(commands)))
            return "cmd-mocked"

        with mock.patch.multiple(
                build_dispatcher,
                ssm=self.ssm, ec2=self.ec2, sns=self.sns,
                jobs_table=FailingTableFactory(),
                servers_table=FailingTableFactory(),
                transition_job=memory.transition_job,
                update_job_fields=memory.update_job_fields,
                complete_effect=memory.complete_effect,
                release_server=memory.release_server,
                # the audit sink is captured here (not via the module
                # stub: build_dispatcher binds log_audit_event at its
                # FIRST import, which another test file may own)
                log_audit_event=lambda **kwargs:
                    AUDIT_EVENTS.append(kwargs),
                send_shell_command=_send_shell,
                run_shell_sync=mock.Mock(return_value=shell_output)):
            callable_(*args)


class _CleanLedgers:
    """Per-test ledger reset via setup_method (NOT a function-scoped
    pytest fixture: those trip Hypothesis's health check on the @given
    tests, which reset the ledgers in-body instead)."""

    def setup_method(self, method):
        del PROHIBITED_CALLS[:]
        del AUDIT_EVENTS[:]


def _assert_no_prohibited(flow):
    assert PROHIBITED_CALLS == [], PROHIBITED_CALLS
    # every captured provider interaction was an allow-listed READ
    for operation, _ in flow.ssm.read_calls:
        assert operation in ("get_command_invocation", "list_commands",
                             "describe_instance_information")
    assert flow.ec2.read_calls == []
    assert flow.sns.read_calls == []


# ===========================================================================
# The adapters themselves fail-close on the prohibited vocabulary
# ===========================================================================

class TestFailingAdapters:

    @settings(max_examples=50, deadline=None)
    @given(action=st.sampled_from(sorted(PROHIBITED_VOCABULARY)))
    def test_every_prohibited_operation_is_recorded_and_raises(
            self, action):
        """For any operation in the prohibited vocabulary, the adapter
        records it and raises — a validation flow that reached it could
        never complete silently (Property 15)."""
        del PROHIBITED_CALLS[:]
        client_name, operation = action
        client = RecordingFailingClient(client_name)
        with pytest.raises(AssertionError, match="PROHIBITED"):
            getattr(client, operation)()
        assert PROHIBITED_CALLS == [(client_name, operation)]

    def test_direct_table_access_is_prohibited(self):
        with pytest.raises(AssertionError, match="PROHIBITED"):
            FailingTableFactory()()
        assert ("dynamodb", "table_access") in PROHIBITED_CALLS


# ===========================================================================
# Representative dispatch flow (preflight-refused dedicated dispatch)
# ===========================================================================

class TestDispatchFlowNoLiveAction(_CleanLedgers):

    def test_preflight_refused_dispatch_performs_zero_live_calls(self):
        """The dispatch flow for an invalid startup contract settles the
        job terminally (COMMAND_PREFLIGHT_FAILED) with ZERO prohibited
        calls: no SendCommand, no instance action, no publication
        (Req 2.12, 2.19)."""
        job = _job("job-dispatch-1", build_domain.STATUS_QUEUED,
                   # cross-wired component identity: invalid contract
                   component_name=build_domain
                   .BUILD_TARGETS["JP5"]["component_name"])
        server = dict(_SERVER, running_build_job_id="job-dispatch-1")
        memory = MemoryPersistence([job], [server])
        flow = _Flow()
        flow.run(memory, build_dispatcher.verify_and_start_dedicated,
                 job, server, NOW)

        _assert_no_prohibited(flow)
        settled = memory.jobs["job-dispatch-1"]
        assert settled["status"] == build_domain.STATUS_FAILED
        assert settled["error"]["code"] == \
            build_dispatcher.ERROR_COMMAND_PREFLIGHT_FAILED
        # the only shell interaction was the mocked pgrep verification
        assert flow.shell_commands == []
        assert flow.ssm.read_calls == []

    def test_valid_dispatch_reaches_only_the_mocked_send_seam(self):
        """A VALID dedicated dispatch driven the same way sends the
        agent exclusively through the mocked seam — the failing boto
        adapter proves the flow cannot reach a live SendCommand from
        this suite (Req 2.19: a real launch needs the approval-gated
        tasks, not this validation)."""
        job = _job("job-dispatch-2", build_domain.STATUS_QUEUED)
        server = dict(_SERVER, running_build_job_id="job-dispatch-2")
        memory = MemoryPersistence([job], [server])
        flow = _Flow()
        flow.run(memory, build_dispatcher.verify_and_start_dedicated,
                 job, server, NOW)

        _assert_no_prohibited(flow)
        started = memory.jobs["job-dispatch-2"]
        assert started["status"] == build_domain.STATUS_BUILDING
        # exactly one agent send, through the MOCKED seam only
        agent_sends = [commands for _, commands in flow.shell_commands
                       if any("portal-build-agent" in line
                              for line in commands)]
        assert len(agent_sends) == 1


# ===========================================================================
# Representative scheduled-reconciliation flow (terminal SSM evidence)
# ===========================================================================

def _failed_invocation(**overrides):
    invocation = {
        "CommandId": "cmd-1", "InstanceId": _SERVER["instance_id"],
        "Status": "Failed", "StatusDetails": "Failed",
        "ResponseCode": 127,
        "StandardErrorContent":
            f"bash: /opt/dda/x/scripts/portal-build-agent.sh: "
            f"No such file or directory token={SECRET_CANARY}",
        "StandardOutputContent": "",
    }
    invocation.update(overrides)
    return invocation


class TestReconciliationFlowNoLiveAction(_CleanLedgers):

    def test_command_reconciliation_uses_read_only_evidence_recovery(self):
        """Scheduled reconciliation of a terminal Failed invocation
        settles the job from READ-ONLY GetCommandInvocation evidence
        with zero prohibited calls, and the planted secret canary never
        survives into any captured sink (Req 2.10, 2.19)."""
        job = _job("job-recon-1", build_domain.STATUS_BUILDING,
                   server_id="srv-1",
                   ssm={"command_id": "cmd-1",
                        "instance_id": _SERVER["instance_id"]},
                   execution_attempt={"attempt_id": "att-1",
                                      "command_id": "cmd-1",
                                      "instance_id":
                                          _SERVER["instance_id"],
                                      "dispatch_state": br.DISPATCH_SENT})
        server = dict(_SERVER, running_build_job_id="job-recon-1")
        memory = MemoryPersistence([job], [server])
        flow = _Flow(ssm_reads={
            "get_command_invocation": _failed_invocation()})
        flow.run(memory, build_dispatcher.command_reconciliation,
                 [job], {"srv-1": server}, NOW)

        _assert_no_prohibited(flow)
        assert [op for op, _ in flow.ssm.read_calls] == \
            ["get_command_invocation"]
        settled = memory.jobs["job-recon-1"]
        assert settled["status"] == build_domain.STATUS_FAILED
        assert settled["error"]["code"] in br.STABLE_ERROR_CODES
        # redaction before every sink: the canary is gone everywhere
        assert SECRET_CANARY not in memory.sink_text()
        assert flow.shell_commands == []

    def test_ambiguous_send_recovery_is_read_only_within_the_bound(self):
        """Ambiguous-send recovery inside the visibility bound uses only
        the READ-ONLY recent-command lookup — never a resend — so zero
        prohibited calls are possible from this validation (Req 2.19)."""
        job = _job("job-recon-2", build_domain.STATUS_BUILDING,
                   server_id="srv-1",
                   execution_attempt={
                       "attempt_id": "att-2",
                       "instance_id": _SERVER["instance_id"],
                       "dispatch_state": br.DISPATCH_SENDING,
                       "sending_at": NOW - 1000,
                       "command_comment": br.command_comment(
                           "job-recon-2", "att-2")})
        memory = MemoryPersistence([job], [_SERVER])
        flow = _Flow(ssm_reads={"list_commands": {"Commands": []}})
        flow.run(memory, build_dispatcher.command_reconciliation,
                 [job], {"srv-1": dict(_SERVER)}, NOW)

        _assert_no_prohibited(flow)
        assert [op for op, _ in flow.ssm.read_calls] == ["list_commands"]
        # still building, no resend, no live action
        assert memory.jobs["job-recon-2"]["status"] == \
            build_domain.STATUS_BUILDING
        assert flow.shell_commands == []


# ===========================================================================
# Representative watchdog flow (runtime timeout past the hard deadline)
# ===========================================================================

_CLEAN_COUNT_OUTPUT = "GDK_BUILD_COUNT=0\nCUSTOM_BUILD_COUNT=0\n"


class TestWatchdogFlowNoLiveAction(_CleanLedgers):

    def test_timeout_watchdog_stops_only_through_the_mocked_seam(self):
        """The runtime timeout watchdog on a job past its snapshotted
        budget finalizes the timeout, requests the stop exclusively
        through the MOCKED shell seam, verifies cleanup via the mocked
        pgrep count, and performs zero prohibited calls — validating a
        timeout locally can never change a production timeout setting
        or launch/stop real compute (Property 15, Req 2.19)."""
        job = _job("job-watchdog-1", build_domain.STATUS_BUILDING,
                   server_id="srv-1",
                   started_at=NOW - 10 * _HOUR_MS,
                   ssm={"command_id": "cmd-9",
                        "instance_id": _SERVER["instance_id"]})
        server = dict(_SERVER, running_build_job_id="job-watchdog-1")
        memory = MemoryPersistence([job], [server])
        flow = _Flow()
        flow.run(memory, build_dispatcher.runtime_timeout_watchdog,
                 [job], {"srv-1": server},
                 NOW, shell_output=_CLEAN_COUNT_OUTPUT)

        _assert_no_prohibited(flow)
        settled = memory.jobs["job-watchdog-1"]
        assert settled["status"] == build_domain.STATUS_FAILED
        assert settled["error"]["code"] in (
            build_dispatcher.ERROR_TIMEOUT, "MAX_RUNTIME_EXCEEDED")
        # the stop request went to the mocked seam, never a client
        assert flow.shell_commands == [
            (_SERVER["instance_id"],
             tuple(build_dispatcher.STOP_BUILD_COMMANDS))]
        # the snapshotted budget itself was not mutated by validation
        assert settled["config_snapshot"]["max_runtime_hours"] == 4

    def test_terminal_effects_reconciliation_re_drive_is_local(self):
        """Re-driving pending terminal effects (audit + verified cleanup
        + release + promotion wakeup) for an absorbed terminal job uses
        only mocked seams: one audit, cleanup pgrep-confirmed through
        the mocked shell, allocation released, zero prohibited calls."""
        ledger = br.plan_terminal_effects(
            "job-effects-1", "att-9",
            build_domain.EXECUTION_MODE_DEDICATED, cleanup_required=True)
        job = _job("job-effects-1", build_domain.STATUS_FAILED,
                   server_id="srv-1",
                   error={"code": "TIMEOUT", "message": "timed out"},
                   ended_at=NOW - 1000,
                   ssm={"command_id": "cmd-9",
                        "instance_id": _SERVER["instance_id"]},
                   terminal_effects=ledger)
        server = dict(_SERVER, running_build_job_id="job-effects-1")
        memory = MemoryPersistence([job], [server])
        flow = _Flow()
        flow.run(memory,
                 build_dispatcher.terminal_effects_reconciliation,
                 [memory.jobs["job-effects-1"]], {"srv-1": server},
                 NOW, shell_output=_CLEAN_COUNT_OUTPUT)

        _assert_no_prohibited(flow)
        final_ledger = memory.jobs["job-effects-1"]["terminal_effects"]
        assert final_ledger[br.EFFECT_AUDIT] == br.EFFECT_DONE
        assert final_ledger[br.EFFECT_COMPUTE_CLEANUP] == br.EFFECT_DONE
        assert final_ledger[br.EFFECT_ALLOCATION_RELEASE] == \
            br.EFFECT_DONE
        assert len(AUDIT_EVENTS) == 1
        assert ("release", "srv-1", "job-effects-1") in memory.writes
        # the cleanup stop/pgrep ran only through the mocked seams
        assert all(instance == _SERVER["instance_id"]
                   for instance, _ in flow.shell_commands)


# ===========================================================================
# **Property 15** — for ANY interleaving of the representative flows,
# the suite captures zero prohibited calls
# ===========================================================================

_FLOW_NAMES = ("dispatch_invalid", "dispatch_valid", "reconciliation",
               "ambiguous_send", "watchdog", "terminal_effects")


def _drive(flow_name, index):
    """Build fixtures and drive one named flow; returns the _Flow."""
    suffix = f"{flow_name}-{index}"
    if flow_name == "dispatch_invalid":
        job = _job(f"job-{suffix}", build_domain.STATUS_QUEUED,
                   component_name=build_domain
                   .BUILD_TARGETS["JP6"]["component_name"])
        server = dict(_SERVER, running_build_job_id=job["build_job_id"])
        memory = MemoryPersistence([job], [server])
        flow = _Flow()
        flow.run(memory, build_dispatcher.verify_and_start_dedicated,
                 job, server, NOW)
    elif flow_name == "dispatch_valid":
        job = _job(f"job-{suffix}", build_domain.STATUS_QUEUED)
        server = dict(_SERVER, running_build_job_id=job["build_job_id"])
        memory = MemoryPersistence([job], [server])
        flow = _Flow()
        flow.run(memory, build_dispatcher.verify_and_start_dedicated,
                 job, server, NOW)
    elif flow_name == "reconciliation":
        job = _job(f"job-{suffix}", build_domain.STATUS_BUILDING,
                   server_id="srv-1",
                   ssm={"command_id": "cmd-1",
                        "instance_id": _SERVER["instance_id"]})
        memory = MemoryPersistence([job], [_SERVER])
        flow = _Flow(ssm_reads={
            "get_command_invocation": _failed_invocation()})
        flow.run(memory, build_dispatcher.command_reconciliation,
                 [job], {"srv-1": dict(_SERVER)}, NOW)
    elif flow_name == "ambiguous_send":
        job = _job(f"job-{suffix}", build_domain.STATUS_BUILDING,
                   server_id="srv-1",
                   execution_attempt={
                       "attempt_id": "att-1",
                       "instance_id": _SERVER["instance_id"],
                       "dispatch_state": br.DISPATCH_SENDING,
                       "sending_at": NOW - 1000,
                       "command_comment": br.command_comment(
                           f"job-{suffix}", "att-1")})
        memory = MemoryPersistence([job], [_SERVER])
        flow = _Flow(ssm_reads={"list_commands": {"Commands": []}})
        flow.run(memory, build_dispatcher.command_reconciliation,
                 [job], {"srv-1": dict(_SERVER)}, NOW)
    elif flow_name == "watchdog":
        job = _job(f"job-{suffix}", build_domain.STATUS_BUILDING,
                   server_id="srv-1", started_at=NOW - 10 * _HOUR_MS,
                   ssm={"command_id": "cmd-9",
                        "instance_id": _SERVER["instance_id"]})
        server = dict(_SERVER, running_build_job_id=job["build_job_id"])
        memory = MemoryPersistence([job], [server])
        flow = _Flow()
        flow.run(memory, build_dispatcher.runtime_timeout_watchdog,
                 [job], {"srv-1": server}, NOW,
                 shell_output=_CLEAN_COUNT_OUTPUT)
    else:  # terminal_effects
        ledger = br.plan_terminal_effects(
            f"job-{suffix}", "att-9",
            build_domain.EXECUTION_MODE_DEDICATED, cleanup_required=True)
        job = _job(f"job-{suffix}", build_domain.STATUS_FAILED,
                   server_id="srv-1",
                   error={"code": "TIMEOUT", "message": "timed out"},
                   ended_at=NOW - 1000,
                   ssm={"command_id": "cmd-9",
                        "instance_id": _SERVER["instance_id"]},
                   terminal_effects=ledger)
        server = dict(_SERVER, running_build_job_id=job["build_job_id"])
        memory = MemoryPersistence([job], [server])
        flow = _Flow()
        flow.run(memory,
                 build_dispatcher.terminal_effects_reconciliation,
                 [memory.jobs[job["build_job_id"]]], {"srv-1": server},
                 NOW, shell_output=_CLEAN_COUNT_OUTPUT)
    return flow


class TestProperty15ApprovalGatedValidation(_CleanLedgers):
    """**Property 15: Approval-Gated Timeout Validation**

    **Validates: Requirements 2.12, 2.19**
    """

    @settings(max_examples=25, deadline=None)
    @given(sequence=st.lists(st.sampled_from(_FLOW_NAMES),
                             min_size=1, max_size=6))
    def test_any_flow_interleaving_captures_zero_prohibited_calls(
            self, sequence):
        """For ANY generated interleaving of the representative
        dispatch/reconciliation/watchdog/terminal-effects flows, the
        recording adapters capture ZERO prohibited calls: no
        deployment, no production setting write, no SSM SendCommand,
        no instance action, no artifact publication, no live build
        (Property 15, Req 2.19)."""
        del PROHIBITED_CALLS[:]
        del AUDIT_EVENTS[:]
        flows = [_drive(name, index)
                 for index, name in enumerate(sequence)]
        assert PROHIBITED_CALLS == [], PROHIBITED_CALLS
        for flow in flows:
            _assert_no_prohibited(flow)

    def test_local_validation_never_mutates_a_snapshotted_budget(self):
        """Driving the full watchdog validation leaves every job's
        snapshotted runtime budget byte-identical: validating a timeout
        locally cannot change a production timeout configuration
        (Req 2.19); a real change requires the approval-gated task 12
        deployment."""
        job = _job("job-budget-1", build_domain.STATUS_BUILDING,
                   server_id="srv-1", started_at=NOW - 10 * _HOUR_MS,
                   ssm={"command_id": "cmd-9",
                        "instance_id": _SERVER["instance_id"]})
        snapshot_before = json.dumps(job["config_snapshot"],
                                     sort_keys=True)
        server = dict(_SERVER, running_build_job_id="job-budget-1")
        memory = MemoryPersistence([job], [server])
        flow = _Flow()
        flow.run(memory, build_dispatcher.runtime_timeout_watchdog,
                 [job], {"srv-1": server}, NOW,
                 shell_output=_CLEAN_COUNT_OUTPUT)
        _assert_no_prohibited(flow)
        assert json.dumps(
            memory.jobs["job-budget-1"]["config_snapshot"],
            sort_keys=True) == snapshot_before
