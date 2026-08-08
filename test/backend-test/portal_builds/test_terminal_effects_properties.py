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
Terminal-effects and race property/integration tests
(build-fleet-execution-failures task 10.4).

**Property 6: Exactly-Once Terminal Effects and Promotion** — _for any_
terminal cause and any retries/races among callback, command event,
timeout watchdog, cancellation, and scheduled tick, there SHALL be one
terminal outcome, one deduplicated audit effect, one effective dedicated
release or ephemeral cleanup, one oldest-eligible queue promotion, and
at most one effective build execution for each attempt.

**Property 10: Timeout Retry and Race Convergence** — _for any_ timeout
decision concurrent with a terminal callback, SSM status event, cleanup
retry, or queue-promotion tick, a valid result completed before the
applicable deadline SHALL retain precedence, otherwise the deterministic
timeout class SHALL remain terminal, and all repeated effects SHALL
converge without duplicate dispatch or cleanup.

**Validates: Requirements 2.6, 2.7, 2.11, 3.2, 3.3, 3.4, 3.11, 3.12**

Hypothesis generates callback/SSM/timeout/cancel/tick races,
conditional-write loss, service retries, cleanup outcomes, and queued
followers. The integration properties run through the ACTUAL
``build_events.py`` / ``build_dispatcher.py`` persistence adapters
against moto DynamoDB and a recording SSM fake (the
``test_terminal_effects_ledger.py`` harness pattern — those frozen
example tests are NOT duplicated here); no live AWS client is used.

Run ONLY this file, from the repository root:

    PYTHONPATH=src/backend:test/backend-test python3 -m pytest \\
        test/backend-test/portal_builds/test_terminal_effects_properties.py \\
        --noconftest -q

(This run contains property-based tests and may generate/shrink
counterexamples.)
"""
import os
import re
import sys
import types
import uuid
from unittest import mock

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ["AWS_ACCESS_KEY_ID"] = "testing"
os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
os.environ["AWS_SECURITY_TOKEN"] = "testing"
os.environ["AWS_SESSION_TOKEN"] = "testing"

_SUFFIX = "terminal-effects-props"
_JOBS_TABLE = f"dda-portal-build-jobs-{_SUFFIX}"
_SERVERS_TABLE = f"dda-portal-build-servers-{_SUFFIX}"
os.environ["BUILD_JOBS_TABLE"] = _JOBS_TABLE
os.environ["BUILD_SERVERS_TABLE"] = _SERVERS_TABLE
# The promotion wakeup must fall back to the scheduled tick (task 6.3
# "retain the schedule fallback") — no dispatcher function configured.
os.environ.pop("BUILD_DISPATCHER_FUNCTION_NAME", None)
os.environ.pop("BUILD_REPO_URL", None)
os.environ.pop("BUILD_REPO_DIR", None)
os.environ.pop("BUILD_ALERT_TOPIC_ARN", None)
os.environ.pop("BUILD_INSTANCE_PROFILE_ARN", None)
os.environ.pop("BUILD_INSTANCE_PROFILE_NAME", None)
os.environ.pop("BUILD_SECURITY_GROUP_ID", None)
os.environ.pop("BUILD_SUBNET_ID", None)

import boto3  # noqa: E402
from botocore.exceptions import ClientError  # noqa: E402
from hypothesis import HealthCheck, given, settings, strategies as st  # noqa: E402

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

AUDIT_EVENTS = []


def _fake_shared_utils():
    module = types.ModuleType("shared_utils")

    def log_audit_event(**kwargs):
        AUDIT_EVENTS.append(kwargs)

    def create_response(status_code, body):
        return {"statusCode": status_code, "body": body}

    def get_user_from_event(event):
        return {"user_id": "test-user"}

    module.log_audit_event = log_audit_event
    module.create_response = create_response
    module.get_user_from_event = get_user_from_event
    return module


def _fake_rbac_middleware():
    module = types.ModuleType("rbac_middleware")

    def _passthrough(*dargs, **dkwargs):
        def decorator(fn):
            return fn
        return decorator

    module.require_builds_submit = _passthrough
    module.require_builds_cancel = _passthrough
    module.require_builds_read = _passthrough
    return module


for _module in ("build_jobs", "build_dispatcher", "build_events",
                "build_planner", "build_domain", "build_reconciliation",
                "build_source", "rbac_middleware", "shared_utils"):
    sys.modules.pop(_module, None)
sys.modules["shared_utils"] = _fake_shared_utils()
sys.modules["rbac_middleware"] = _fake_rbac_middleware()

_MOCK = mock_aws()
_MOCK.start()

# ---------------------------------------------------------------------------
# Recording fake SSM over boto3.client BEFORE the handler imports:
# scripted GetCommandInvocation, recorded SendCommand (delegated to moto
# so command ids are real). No call leaves the process.
# ---------------------------------------------------------------------------
SSM_INVOCATIONS = {}
SSM_SEND_CALLS = []

_REAL_BOTO3_CLIENT = boto3.client


class _FakeSsm:
    def __init__(self, inner):
        self._inner = inner

    def get_command_invocation(self, **kwargs):
        invocation = SSM_INVOCATIONS.get(kwargs.get("CommandId"))
        if invocation is not None:
            return dict(invocation)
        raise ClientError(
            {"Error": {"Code": "InvocationDoesNotExist",
                       "Message": "no such invocation"}},
            "GetCommandInvocation")

    def list_commands(self, **kwargs):
        return {"Commands": []}

    def send_command(self, **kwargs):
        SSM_SEND_CALLS.append(dict(kwargs))
        return self._inner.send_command(**kwargs)

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

_EC2 = boto3.client("ec2", region_name="us-east-1")
_AMI_ID = _EC2.describe_images(Owners=["amazon"])["Images"][0]["ImageId"]
os.environ["BUILD_ARM64_AMI_ID"] = _AMI_ID
os.environ["BUILD_X86_64_AMI_ID"] = _AMI_ID

import build_domain  # noqa: E402
import build_planner  # noqa: E402
import build_reconciliation  # noqa: E402
import build_events  # noqa: E402
import build_dispatcher  # noqa: E402
import build_jobs  # noqa: E402

# Restore the real factory so other test modules collected in the same
# pytest process get untouched moto clients.
boto3.client = _REAL_BOTO3_CLIENT

NOW = 1_786_200_000_000
_MINUTE_MS = 60 * 1000
_HOUR_MS = 60 * _MINUTE_MS

_ZERO_COUNT = "GDK_BUILD_COUNT=0\nCUSTOM_BUILD_COUNT=0\n"
_ONE_COUNT = "GDK_BUILD_COUNT=1\nCUSTOM_BUILD_COUNT=1\n"
_HEX_DIGEST = re.compile(r"^[0-9a-f]{64}$")

#: send_agent recordings across ticks: the effective build executions.
DISPATCHES = []

#: Terminal Audit_Log actions the system writers may record.
_TERMINAL_AUDIT_ACTIONS = (
    "build_failed", "build_interrupted", "build_timeout",
    "build_published", "build_publishing_failed", "build_cancelled",
)


def _clear_state():
    for item in _JOBS.scan().get("Items", []):
        _JOBS.delete_item(Key={"build_job_id": item["build_job_id"]})
    for item in _SERVERS.scan().get("Items", []):
        _SERVERS.delete_item(Key={"server_id": item["server_id"]})
    del AUDIT_EVENTS[:]
    del SSM_SEND_CALLS[:]
    del DISPATCHES[:]
    SSM_INVOCATIONS.clear()


def _get_job(job_id):
    return build_dispatcher.to_native(
        _JOBS.get_item(Key={"build_job_id": job_id}).get("Item"))


def _get_server(server_id):
    return build_dispatcher.to_native(
        _SERVERS.get_item(Key={"server_id": server_id}).get("Item"))


def _terminal_audits():
    return [a for a in AUDIT_EVENTS
            if a["action"] in _TERMINAL_AUDIT_ACTIONS]


def _stop_sends():
    return [c for c in SSM_SEND_CALLS
            if c.get("Parameters", {}).get("commands") ==
            build_dispatcher.STOP_BUILD_COMMANDS]


def _seed_server(server_id, instance_id=None, running_build_job_id=None,
                 **extra):
    item = {
        "server_id": server_id,
        "name": server_id,
        "lifecycle_state": "running",
    }
    if instance_id:
        item["instance_id"] = instance_id
    if running_build_job_id:
        item["running_build_job_id"] = running_build_job_id
    item.update(extra)
    _SERVERS.put_item(Item=item)
    return item


def _seed_job(job_id, status, mode=build_domain.EXECUTION_MODE_DEDICATED,
              server_id=None, command_id=None, instance_id=None,
              attempt_id=None, **extra):
    job = {
        "build_job_id": job_id,
        "build_target": build_domain.TARGET_JP5,
        "execution_mode": mode,
        "status": status,
        "requested_by": "operator-1",
        "created_at": NOW - 10 * _MINUTE_MS,
        "config_snapshot": {"max_runtime_hours": 4},
    }
    if server_id:
        job["server_id"] = server_id
    if status in ("building", "publishing"):
        job["started_at"] = NOW - 9 * _MINUTE_MS
    if command_id:
        job["ssm"] = {"command_id": command_id, "instance_id": instance_id}
    if attempt_id:
        job["execution_attempt"] = {
            "attempt_id": attempt_id,
            "command_id": command_id,
            "instance_id": instance_id,
        }
    job.update(extra)
    _JOBS.put_item(Item=build_dispatcher.to_dynamo(job))
    return job


def _script_invocation(command_id, instance_id, status="Failed",
                       response_code=127, stderr="agent exited 127"):
    SSM_INVOCATIONS[command_id] = {
        "CommandId": command_id,
        "InstanceId": instance_id,
        "Status": status,
        "StatusDetails": status,
        "ResponseCode": response_code,
        "StandardOutputContent": "",
        "StandardErrorContent": stderr,
    }


def _deliver_ssm_event(command_id, instance_id, status="Failed"):
    return build_events.handler({
        "source": "aws.ssm",
        "detail-type": "EC2 Command Status-change Notification",
        "detail": {
            "command-id": command_id,
            "instance-id": instance_id,
            "status": status,
        },
    }, None)


def _deliver_phase_event(job_id, phase, **detail):
    return build_events.handler({
        "source": "dda.portal.builds",
        "detail-type": "BuildPhaseChange",
        "detail": {"build_job_id": job_id, "phase": phase, **detail},
    }, None)


def _shell_router(pgrep_output):
    """run_shell_sync fake: the pgrep/stop-verification count commands
    get the scripted output; every other synchronous probe (pre-dispatch
    verification, bootstrap marker) gets the clean empty output the
    frozen ledger harness dispatches with."""
    def _run(instance_id, commands, **kwargs):
        if commands == build_dispatcher.COUNT_BUILD_PROCESS_COMMANDS:
            return pgrep_output
        return ""
    return _run


def _tick(now, pgrep_output=_ZERO_COUNT):
    """One dispatcher tick with routed shell probes and a recording
    send_agent (the effective-execution counter): no real dispatch."""
    def _record_send(job, instance_id, repo_dir, **kwargs):
        DISPATCHES.append(job["build_job_id"])
        return ("cmd-follow-%d" % len(DISPATCHES), "stream")

    with mock.patch.object(build_dispatcher, "run_shell_sync",
                           side_effect=_shell_router(pgrep_output)), \
            mock.patch.object(build_dispatcher, "send_agent",
                              side_effect=_record_send):
        build_dispatcher.run_tick(now=now)


def _cancel_job(job_id):
    """The cancellation adapter route (conflict-preserving conditional
    write, task 6.2): returns False on a lost/duplicate cancel."""
    job = _get_job(job_id)
    if not job or job.get("status") != build_domain.STATUS_BUILDING:
        return False
    ledger = build_jobs.cancellation_ledger(job, dispatched=True,
                                            stop_verified=True)
    return build_jobs.apply_job_cancellation(
        job_id, build_domain.STATUS_BUILDING, NOW,
        terminal_effects=ledger)


_LEDGER_EFFECTS = (build_reconciliation.EFFECT_AUDIT,
                   build_reconciliation.EFFECT_COMPUTE_CLEANUP,
                   build_reconciliation.EFFECT_ALLOCATION_RELEASE,
                   build_reconciliation.EFFECT_PROMOTION_WAKEUP)

_INTEGRATION_SETTINGS = dict(
    max_examples=20, deadline=None,
    suppress_health_check=[HealthCheck.too_slow,
                           HealthCheck.filter_too_much,
                           HealthCheck.data_too_large])


# ===========================================================================
# Property 6 — Exactly-Once Terminal Effects and Promotion
# ===========================================================================

#: The generated terminal causes and their deterministic expectations:
#: (terminal status, audit action or None, expected error code or None).
_CAUSES = {
    "ssm_failed": (build_domain.STATUS_FAILED, "build_failed",
                   build_reconciliation.CODE_COMMAND_EXECUTION_FAILED),
    "ssm_timedout": (build_domain.STATUS_FAILED, "build_failed",
                     build_reconciliation.CODE_COMMAND_TIMED_OUT),
    "callback_failed": (build_domain.STATUS_FAILED, "build_failed",
                        build_events.ERROR_BUILD_FAILED),
    "watchdog_timeout": (build_domain.STATUS_FAILED, "build_timeout",
                         build_dispatcher.ERROR_TIMEOUT),
    "cancel": (build_domain.STATUS_CANCELLED, None, None),
}


class TestProperty6ExactlyOnceTerminalEffectsAndPromotion:
    """**Property 6: Exactly-Once Terminal Effects and Promotion**

    **Validates: Requirements 2.6, 2.7, 2.11, 3.2, 3.3, 3.4, 3.11**
    """

    @settings(**_INTEGRATION_SETTINGS)
    @given(cause=st.sampled_from(sorted(_CAUSES)),
           order=st.permutations(["deliver", "deliver", "tick"]),
           follower_offsets=st.lists(st.integers(1, 240), min_size=2,
                                     max_size=3, unique=True))
    def test_terminal_races_yield_one_outcome_audit_cleanup_promotion(
            self, cause, order, follower_offsets):
        """For ANY terminal cause (SSM Failed/TimedOut event, agent
        callback, timeout watchdog, cancellation) raced against
        duplicate deliveries and scheduled ticks, through the ACTUAL
        event/dispatcher adapters: ONE terminal status/``ended_at``,
        ONE logical audit under the stable effect ID, verified stop
        before release, one effective cleanup, oldest-eligible
        promotion, at most one effective dispatch, and preserved
        serialization (Req 2.6, 2.7, 2.11, 3.2, 3.4, 3.11)."""
        _clear_state()
        expected_status, audit_action, error_code = _CAUSES[cause]
        command_id, attempt_id = str(uuid.uuid4()), str(uuid.uuid4())
        server_id, job_id = "srv-race", "job-race"
        _seed_server(server_id, instance_id="i-race",
                     running_build_job_id=job_id)
        extra = {}
        if cause == "watchdog_timeout":
            # Legacy-shaped running job strictly past its snapshotted
            # 4-hour maximum runtime (frozen legacy decision, Req 3.12).
            extra["started_at"] = NOW - 5 * _HOUR_MS
        _seed_job(job_id, build_domain.STATUS_BUILDING,
                  server_id=server_id, command_id=command_id,
                  instance_id="i-race", attempt_id=attempt_id, **extra)
        if cause == "ssm_failed":
            _script_invocation(command_id, "i-race", status="Failed")
        elif cause == "ssm_timedout":
            _script_invocation(command_id, "i-race", status="TimedOut",
                               response_code=-1, stderr="")

        offsets = sorted(follower_offsets)
        followers = []
        for index, offset in enumerate(offsets):
            follower_id = "job-follow-%d" % index
            _seed_job(follower_id, build_domain.STATUS_QUEUED,
                      server_id=server_id,
                      created_at=NOW - offset * _MINUTE_MS)
            followers.append((follower_id, offset))
        oldest_id = max(followers, key=lambda pair: pair[1])[0]

        def _deliver():
            if cause in ("ssm_failed", "ssm_timedout"):
                _deliver_ssm_event(command_id, "i-race",
                                   status=("TimedOut"
                                           if cause == "ssm_timedout"
                                           else "Failed"))
            elif cause == "callback_failed":
                _deliver_phase_event(job_id, "failed",
                                     error_kind="building",
                                     error_message="gdk build failed")
            elif cause == "watchdog_timeout":
                _tick(NOW)
            else:
                _cancel_job(job_id)

        # The generated race schedule, then bounded convergence ticks.
        for op in order:
            if op == "deliver":
                _deliver()
            else:
                _tick(NOW)
        _tick(NOW + 1 * _MINUTE_MS)
        _tick(NOW + 2 * _MINUTE_MS)

        job = _get_job(job_id)
        # ONE terminal outcome with one ended_at (Req 2.6).
        assert job["status"] == expected_status
        assert job.get("ended_at") is not None
        if error_code is not None:
            assert job["error"]["code"] == error_code
        # ONE logical audit under the ONE stable effect ID (Req 2.7):
        # the cancellation adapter's audit lives with the cancel
        # handler, so the system writers record none for it.
        audits = _terminal_audits()
        if audit_action is None:
            assert audits == []
        else:
            assert len(audits) == 1
            assert audits[0]["action"] == audit_action
        ledger = job["terminal_effects"]
        assert ledger["effect_id"] == \
            build_reconciliation.terminal_effect_id(job_id, attempt_id)
        # The retried effects all converged: nothing pending.
        assert build_reconciliation.pending_effects(ledger) == []
        # ONE effective cleanup (Req 3.11/3.3): only the watchdog cause
        # needs a stop send, and even under duplicate ticks it is sent
        # exactly once; event-settled causes need none (the terminal
        # invocation means the shell exited).
        stops = _stop_sends()
        if cause == "watchdog_timeout":
            assert len(stops) == 1
        else:
            assert stops == []
        # Oldest-eligible promotion, exactly once, original created_at
        # retained; every younger follower stays queued (Req 3.2/3.4).
        assert DISPATCHES == [oldest_id]
        promoted = _get_job(oldest_id)
        assert promoted["status"] == build_domain.STATUS_BUILDING
        assert promoted["created_at"] == \
            NOW - max(offsets) * _MINUTE_MS
        for follower_id, _ in followers:
            if follower_id != oldest_id:
                assert _get_job(follower_id)["status"] == \
                    build_domain.STATUS_QUEUED
        # Serialization preserved: the slot holds exactly the promoted
        # job (Req 3.2/3.11).
        assert _get_server(server_id)["running_build_job_id"] == oldest_id

        # Idempotency phase (service retries): re-deliver every
        # duplicate and tick again — the absorbed outcome, its audit
        # set, and the single dispatch are all unchanged (Req 2.6/2.7).
        snapshot = (job["status"], job.get("ended_at"),
                    job.get("error"), ledger["effect_id"])
        _deliver()
        _tick(NOW + 3 * _MINUTE_MS)
        after = _get_job(job_id)
        assert (after["status"], after.get("ended_at"), after.get("error"),
                after["terminal_effects"]["effect_id"]) == snapshot
        assert len(_terminal_audits()) == len(audits)
        assert DISPATCHES == [oldest_id]
        assert len(_stop_sends()) == len(stops)

    @settings(max_examples=200, deadline=None)
    @given(mode=st.sampled_from([build_domain.EXECUTION_MODE_DEDICATED,
                                 build_domain.EXECUTION_MODE_EPHEMERAL]),
           cleanup_required=st.booleans(),
           attempts=st.lists(st.sampled_from(_LEDGER_EFFECTS),
                             max_size=24))
    def test_ledger_races_complete_each_effect_at_most_once_in_order(
            self, mode, cleanup_required, attempts):
        """For ANY interleaving of effect-completion retries over the
        pure ledger: each effect completes at most once (duplicates are
        refused), the release is refused before verified cleanup, the
        promotion is refused before the release, and driving the
        remaining pending effects in order settles everything under the
        unchanged stable effect ID (Req 2.7, 3.11)."""
        ledger = build_reconciliation.plan_terminal_effects(
            "job-x", "att-x", mode, cleanup_required=cleanup_required)
        effect_id = ledger["effect_id"]
        assert effect_id == \
            build_reconciliation.terminal_effect_id("job-x", "att-x")
        completions = {effect: 0 for effect in _LEDGER_EFFECTS}
        for effect in attempts:
            before = dict(ledger)
            advance = build_reconciliation.advance_effect(ledger, effect)
            if advance.allowed:
                completions[effect] += 1
                # Ordering: verified stop/cleanup before release,
                # release before promotion (Req 3.11).
                if effect == build_reconciliation.EFFECT_ALLOCATION_RELEASE:
                    assert before[
                        build_reconciliation.EFFECT_COMPUTE_CLEANUP] != \
                        build_reconciliation.EFFECT_PENDING
                if effect == build_reconciliation.EFFECT_PROMOTION_WAKEUP:
                    assert before[
                        build_reconciliation.EFFECT_ALLOCATION_RELEASE] != \
                        build_reconciliation.EFFECT_PENDING
            else:
                assert advance.ledger == before  # refused = unchanged
            ledger = advance.ledger
            assert ledger["effect_id"] == effect_id
        # At most one completion per effect ever succeeded (Req 2.7).
        assert all(count <= 1 for count in completions.values())
        # Convergence: retrying the pending effects in required order
        # settles the outcome exactly once each.
        for effect in build_reconciliation.pending_effects(ledger):
            advance = build_reconciliation.advance_effect(ledger, effect)
            assert advance.allowed is True
            ledger = advance.ledger
        assert build_reconciliation.pending_effects(ledger) == []
        # Every further retry is a refused duplicate/not-applicable.
        for effect in _LEDGER_EFFECTS:
            assert build_reconciliation.advance_effect(
                ledger, effect).allowed is False

    @settings(**_INTEGRATION_SETTINGS)
    @given(attempts=st.lists(st.sampled_from(_LEDGER_EFFECTS),
                             min_size=1, max_size=12),
           stale_effect=st.sampled_from(_LEDGER_EFFECTS))
    def test_conditional_write_loss_arbitrates_concurrent_completions(
            self, attempts, stale_effect):
        """For ANY interleaving of concurrent effect completions through
        the ACTUAL conditional-write adapter (moto DynamoDB), the
        condition is the arbiter: each effect's write succeeds at most
        once, out-of-order release/promotion writes lose, and a stale
        effect identity from another attempt can never advance the
        ledger (conditional-write loss, Req 2.6/2.7/3.11)."""
        _clear_state()
        job_id = "job-cw"
        ledger = build_reconciliation.plan_terminal_effects(
            job_id, "att-cw", build_domain.EXECUTION_MODE_DEDICATED)
        effect_id = ledger["effect_id"]
        _seed_job(job_id, build_domain.STATUS_FAILED, ended_at=NOW,
                  terminal_effects=ledger,
                  error={"code": "X", "message": "x"})
        successes = {effect: 0 for effect in _LEDGER_EFFECTS}
        for effect in attempts:
            before = _get_job(job_id)["terminal_effects"]
            if build_dispatcher.complete_effect(job_id, effect_id, effect):
                successes[effect] += 1
                if effect == build_reconciliation.EFFECT_ALLOCATION_RELEASE:
                    assert before[
                        build_reconciliation.EFFECT_COMPUTE_CLEANUP] != \
                        build_reconciliation.EFFECT_PENDING
                if effect == build_reconciliation.EFFECT_PROMOTION_WAKEUP:
                    assert before[
                        build_reconciliation.EFFECT_ALLOCATION_RELEASE] != \
                        build_reconciliation.EFFECT_PENDING
        assert all(count <= 1 for count in successes.values())
        # A stale identity (another attempt's effect ID) always loses.
        stale_id = build_reconciliation.terminal_effect_id(job_id,
                                                           "att-old")
        assert build_dispatcher.complete_effect(
            job_id, stale_id, stale_effect) is False
        # Convergence: the pending effects complete exactly once each.
        stored = _get_job(job_id)["terminal_effects"]
        for effect in build_reconciliation.pending_effects(stored):
            assert build_dispatcher.complete_effect(
                job_id, effect_id, effect) is True
        stored = _get_job(job_id)["terminal_effects"]
        assert build_reconciliation.pending_effects(stored) == []
        for effect in _LEDGER_EFFECTS:
            assert build_dispatcher.complete_effect(
                job_id, effect_id, effect) is False

    @settings(**_INTEGRATION_SETTINGS)
    @given(cleanup_outcome=st.sampled_from(["unknown", "still_running",
                                            "verified"]),
           retries=st.integers(1, 3))
    def test_cleanup_outcome_gates_release_and_blocks_followers(
            self, cleanup_outcome, retries):
        """For ANY cleanup outcome and retry count: only a positively
        VERIFIED stop (pgrep count zero) completes the cleanup and
        releases the slot; unknown or still-running process state keeps
        the slot held and every follower blocked across retries, while
        the idempotent stop is re-sent — then convergence promotes
        exactly one oldest follower (Req 3.2, 3.3, 3.4, 3.11)."""
        _clear_state()
        server_id, job_id = "srv-cl", "job-cl"
        attempt_id = str(uuid.uuid4())
        ledger = build_reconciliation.plan_terminal_effects(
            job_id, attempt_id, build_domain.EXECUTION_MODE_DEDICATED)
        ledger = build_reconciliation.advance_effect(
            ledger, build_reconciliation.EFFECT_AUDIT).ledger
        _seed_server(server_id, instance_id="i-cl",
                     running_build_job_id=job_id)
        _seed_job(job_id, build_domain.STATUS_FAILED, server_id=server_id,
                  ended_at=NOW - _MINUTE_MS, terminal_effects=ledger,
                  error={"code": "TIMEOUT", "message": "timed out"})
        _seed_job("job-old", build_domain.STATUS_QUEUED,
                  server_id=server_id, created_at=NOW - 5 * _MINUTE_MS)
        _seed_job("job-young", build_domain.STATUS_QUEUED,
                  server_id=server_id, created_at=NOW - 1 * _MINUTE_MS)
        pgrep = {"unknown": None, "still_running": _ONE_COUNT,
                 "verified": _ZERO_COUNT}[cleanup_outcome]

        for attempt in range(retries):
            _tick(NOW + attempt * _MINUTE_MS, pgrep_output=pgrep)

        job = _get_job(job_id)
        if cleanup_outcome == "verified":
            assert job["terminal_effects"]["compute_cleanup"] == \
                build_reconciliation.EFFECT_DONE
        else:
            # Fail closed: unknown/running process state keeps the slot
            # and its followers blocked (Req 3.11).
            assert job["terminal_effects"]["compute_cleanup"] == \
                build_reconciliation.EFFECT_PENDING
            assert job["terminal_effects"]["allocation_release"] == \
                build_reconciliation.EFFECT_PENDING
            assert _get_server(server_id)["running_build_job_id"] == job_id
            assert DISPATCHES == []
            assert _get_job("job-old")["status"] == \
                build_domain.STATUS_QUEUED
            assert _get_job("job-young")["status"] == \
                build_domain.STATUS_QUEUED
            # The idempotent stop was re-sent while blocked (Req 3.3).
            assert len(_stop_sends()) >= 1

        # Convergence: a verified stop settles the ledger and the NEXT
        # ticks promote exactly the oldest follower once (Req 3.4).
        _tick(NOW + (retries + 1) * _MINUTE_MS, pgrep_output=_ZERO_COUNT)
        _tick(NOW + (retries + 2) * _MINUTE_MS, pgrep_output=_ZERO_COUNT)
        _tick(NOW + (retries + 3) * _MINUTE_MS, pgrep_output=_ZERO_COUNT)
        job = _get_job(job_id)
        assert build_reconciliation.pending_effects(
            job["terminal_effects"]) == []
        assert DISPATCHES == ["job-old"]
        assert _get_job("job-old")["status"] == build_domain.STATUS_BUILDING
        assert _get_job("job-young")["status"] == build_domain.STATUS_QUEUED
        assert _get_server(server_id)["running_build_job_id"] == "job-old"

    @settings(**_INTEGRATION_SETTINGS)
    @given(same_owner=st.booleans())
    def test_stale_release_cannot_free_a_slot_another_job_owns(
            self, same_owner):
        """For ANY retried release: the conditional server write frees
        the slot ONLY for the job still owning the allocation; a stale
        release for a job that lost the slot is rejected and the current
        owner keeps running (stale-release rejection, Req 3.2/3.11)."""
        _clear_state()
        server_id = "srv-st"
        attempt_id = str(uuid.uuid4())
        ledger = build_reconciliation.plan_terminal_effects(
            "job-stale", attempt_id, build_domain.EXECUTION_MODE_DEDICATED)
        for effect in (build_reconciliation.EFFECT_AUDIT,
                       build_reconciliation.EFFECT_COMPUTE_CLEANUP):
            ledger = build_reconciliation.advance_effect(
                ledger, effect).ledger
        owner = "job-stale" if same_owner else "job-current"
        _seed_server(server_id, instance_id="i-st",
                     running_build_job_id=owner)
        _seed_job("job-stale", build_domain.STATUS_FAILED,
                  server_id=server_id, ended_at=NOW - _MINUTE_MS,
                  terminal_effects=ledger,
                  error={"code": "TIMEOUT", "message": "timed out"})
        if not same_owner:
            _seed_job("job-current", build_domain.STATUS_BUILDING,
                      server_id=server_id, command_id="cmd-cur",
                      instance_id="i-st")

        released = build_dispatcher.release_server(server_id, "job-stale")
        assert released is same_owner
        # Driving the full release path converges the stale job's
        # ledger but can never free the slot the CURRENT job owns.
        terminal_job = _get_job("job-stale")
        build_dispatcher.release_and_promote(
            terminal_job, terminal_job["terminal_effects"])
        server = _get_server(server_id)
        if same_owner:
            assert "running_build_job_id" not in server
        else:
            assert server["running_build_job_id"] == "job-current"
            assert _get_job("job-current")["status"] == \
                build_domain.STATUS_BUILDING
        # A second direct release retry is always a refused duplicate.
        assert build_dispatcher.release_server(
            server_id, "job-stale") is False

    @settings(**_INTEGRATION_SETTINGS)
    @given(offsets=st.lists(st.integers(1, 600), min_size=2, max_size=4,
                            unique=True),
           extra_ticks=st.integers(1, 3))
    def test_repeated_ticks_promote_exactly_one_oldest_follower(
            self, offsets, extra_ticks):
        """For ANY set of queued followers with distinct ages and ANY
        number of racing promotion ticks over a freed slot: exactly the
        OLDEST eligible follower is dispatched exactly once through the
        existing planner + conditional server lock; younger followers
        stay queued with their original created_at (Req 3.2, 3.4,
        3.11)."""
        _clear_state()
        server_id = "srv-oldest"
        _seed_server(server_id, instance_id="i-old")
        followers = []
        for index, offset in enumerate(sorted(offsets)):
            follower_id = "job-f%d" % index
            _seed_job(follower_id, build_domain.STATUS_QUEUED,
                      server_id=server_id,
                      created_at=NOW - offset * _MINUTE_MS)
            followers.append((follower_id, offset))
        oldest_id = max(followers, key=lambda pair: pair[1])[0]

        for attempt in range(1 + extra_ticks):
            _tick(NOW + attempt * _MINUTE_MS)

        assert DISPATCHES == [oldest_id]
        assert _get_job(oldest_id)["status"] == build_domain.STATUS_BUILDING
        assert _get_server(server_id)["running_build_job_id"] == oldest_id
        for follower_id, offset in followers:
            follower = _get_job(follower_id)
            assert follower["created_at"] == NOW - offset * _MINUTE_MS
            if follower_id != oldest_id:
                assert follower["status"] == build_domain.STATUS_QUEUED
        # The dispatch claim was recorded with the send (task 6.3):
        # at most one effective execution for the promoted attempt.
        attempt = _get_job(oldest_id)["execution_attempt"]
        assert attempt["dispatch_state"] == \
            build_reconciliation.DISPATCH_SENT
        assert attempt["command_comment"] == \
            build_reconciliation.command_comment(oldest_id,
                                                 attempt["attempt_id"])


# ===========================================================================
# Property 10 — Timeout Retry and Race Convergence
# ===========================================================================

class TestProperty10TimeoutRetryAndRaceConvergence:
    """**Property 10: Timeout Retry and Race Convergence**

    **Validates: Requirements 2.6, 2.7, 2.11, 3.11, 3.12**
    """

    @settings(max_examples=200, deadline=None)
    @given(start=st.integers(0, 10 ** 12),
           budget_hours=st.integers(1, 12),
           completed_offset=st.integers(-3 * _HOUR_MS, 3 * _HOUR_MS),
           after_deadline=st.integers(1, _HOUR_MS),
           phase=st.sampled_from(["succeeded", "failed"]))
    def test_pre_deadline_terminal_result_retains_precedence(
            self, start, budget_hours, completed_offset, after_deadline,
            phase):
        """For ANY deadline race settled by the pure precedence rules:
        a valid correlated agent result completed AT or BEFORE the hard
        deadline wins (authority 1) even when reconciled after the
        deadline passed; a result completed after it does not qualify
        and the deterministic MAX_RUNTIME_EXCEEDED class decides
        (Req 2.6, 2.11, 3.12)."""
        deadline = start + budget_hours * _HOUR_MS
        completed_at = deadline + completed_offset
        now = deadline + after_deadline  # the watchdog fires strictly after
        outcome = build_reconciliation.classify_attempt(
            build_domain.STATUS_BUILDING,
            agent_result={"phase": phase, "completed_at": completed_at,
                          "message": "done"},
            hard_deadline_ms=deadline,
            now=now)
        assert outcome.decided is True
        if completed_at <= deadline:
            # The valid pre-deadline result retains precedence.
            assert outcome.authority == 1
            expected = (build_domain.STATUS_SUCCEEDED
                        if phase == "succeeded"
                        else build_domain.STATUS_FAILED)
            assert outcome.status == expected
            assert outcome.error_code != \
                build_reconciliation.CODE_MAX_RUNTIME_EXCEEDED
        else:
            # Not qualifying: the deterministic timeout class decides.
            assert outcome.authority == 3
            assert outcome.status == build_domain.STATUS_FAILED
            assert outcome.error_code == \
                build_reconciliation.CODE_MAX_RUNTIME_EXCEEDED

    @settings(max_examples=200, deadline=None)
    @given(start=st.integers(0, 10 ** 12),
           budget_hours=st.integers(1, 12),
           heartbeat_fresh=st.booleans())
    def test_strict_boundary_and_non_extendable_hard_ceiling(
            self, start, budget_hours, heartbeat_fresh):
        """For ANY snapshotted hard budget: ``now == deadline`` does NOT
        expire, ``now == deadline + 1`` does, and fresh heartbeat or
        progress activity cannot extend the hard ceiling (strict
        boundary preserved, Req 3.12; non-extendable ceiling feeding the
        deterministic race outcome, Req 2.11)."""
        deadline = start + budget_hours * _HOUR_MS
        timing = {"execution_started_at": start}
        if heartbeat_fresh:
            timing["last_heartbeat_at"] = deadline
            timing["last_progress_at"] = deadline
        job = {
            "build_job_id": "job-b",
            "build_target": build_domain.TARGET_JP5,
            "execution_mode": build_domain.EXECUTION_MODE_DEDICATED,
            "status": build_domain.STATUS_BUILDING,
            "config_snapshot": {"max_runtime_hours": budget_hours},
            "timing": timing,
        }
        at_boundary = build_reconciliation.decide_timing(job, deadline)
        assert at_boundary.timed_out is False
        past_boundary = build_reconciliation.decide_timing(job, deadline + 1)
        assert past_boundary.timed_out is True
        assert past_boundary.classification == \
            build_reconciliation.CODE_MAX_RUNTIME_EXCEEDED
        assert build_reconciliation.hard_deadline_ms(job) == deadline

    @settings(**_INTEGRATION_SETTINGS)
    @given(winner=st.sampled_from(["callback_first", "watchdog_first"]),
           callback_phase=st.sampled_from(["succeeded", "failed"]),
           duplicate_ticks=st.integers(1, 3),
           race_ssm_event=st.booleans())
    def test_deadline_races_settle_deterministically_and_converge(
            self, winner, callback_phase, duplicate_ticks, race_ssm_event):
        """For ANY race among the timeout watchdog, a terminal agent
        callback, an SSM status event, cleanup retries, and promotion
        ticks over a job past its hard deadline, through the ACTUAL
        adapters: a valid terminal result applied before the timeout
        decision retains precedence; otherwise the deterministic
        timeout class remains terminal (absorbing, no resurrection);
        repeated effects converge to one audit, one effective cleanup,
        and one promotion without duplicate dispatch (Req 2.6, 2.7,
        2.11, 3.11, 3.12)."""
        _clear_state()
        server_id, job_id = "srv-dl", "job-dl"
        command_id, attempt_id = str(uuid.uuid4()), str(uuid.uuid4())
        _seed_server(server_id, instance_id="i-dl",
                     running_build_job_id=job_id)
        # Phase-clock shape: execution started 5h ago under a 4h hard
        # ceiling, so the deadline is already strictly crossed at NOW.
        _seed_job(job_id, build_domain.STATUS_BUILDING,
                  server_id=server_id, command_id=command_id,
                  instance_id="i-dl", attempt_id=attempt_id,
                  timing={"execution_started_at": NOW - 5 * _HOUR_MS})
        _seed_job("job-next", build_domain.STATUS_QUEUED,
                  server_id=server_id, created_at=NOW - 8 * _MINUTE_MS)

        def _callback():
            if callback_phase == "succeeded":
                _deliver_phase_event(
                    job_id, "succeeded",
                    result={"component_name": "c",
                            "published_version": "1.0.0",
                            "pushed_image_refs": ["ref:1"]})
            else:
                _deliver_phase_event(job_id, "failed",
                                     error_kind="building",
                                     error_message="gdk build failed")

        if winner == "callback_first":
            _callback()  # valid result lands before any timeout decision
        else:
            _tick(NOW)  # the watchdog decides first (now > deadline)
            _callback()  # the late callback must not resurrect
        if race_ssm_event:
            _script_invocation(command_id, "i-dl", status="Failed")
            _deliver_ssm_event(command_id, "i-dl")
        for attempt in range(duplicate_ticks):
            _tick(NOW + (attempt + 1) * _MINUTE_MS)

        job = _get_job(job_id)
        if winner == "callback_first":
            # The valid pre-deadline result retained precedence: the
            # timeout class never overwrote it.
            expected = (build_domain.STATUS_SUCCEEDED
                        if callback_phase == "succeeded"
                        else build_domain.STATUS_FAILED)
            assert job["status"] == expected
            if callback_phase == "failed":
                assert job["error"]["code"] == \
                    build_events.ERROR_BUILD_FAILED
            expected_audit = ("build_published"
                              if callback_phase == "succeeded"
                              else "build_failed")
        else:
            # The deterministic timeout class remains terminal: the
            # late callback and duplicate events cannot resurrect or
            # rewrite the absorbed outcome (Req 2.6, 3.12).
            assert job["status"] == build_domain.STATUS_FAILED
            assert job["error"]["code"] == \
                build_reconciliation.CODE_MAX_RUNTIME_EXCEEDED
            expected_audit = "build_timeout"
        assert job.get("ended_at") is not None
        # ONE logical audit for the settled outcome (Req 2.7).
        audits = _terminal_audits()
        assert len(audits) == 1
        assert audits[0]["action"] == expected_audit
        # All repeated effects converged: nothing pending, one
        # effective cleanup at most (the watchdog's single stop).
        assert build_reconciliation.pending_effects(
            job["terminal_effects"]) == []
        assert len(_stop_sends()) == (1 if winner == "watchdog_first"
                                      else 0)
        # One oldest-eligible promotion, no duplicate dispatch.
        assert DISPATCHES == ["job-next"]
        assert _get_job("job-next")["status"] == \
            build_domain.STATUS_BUILDING
        assert _get_server(server_id)["running_build_job_id"] == "job-next"

        # Retry convergence: repeat every racing writer after
        # settlement — the outcome, audit set, and dispatch are stable.
        snapshot = (job["status"], job.get("ended_at"), job.get("error"))
        _callback()
        if race_ssm_event:
            _deliver_ssm_event(command_id, "i-dl")
        _tick(NOW + (duplicate_ticks + 2) * _MINUTE_MS)
        after = _get_job(job_id)
        assert (after["status"], after.get("ended_at"),
                after.get("error")) == snapshot
        assert len(_terminal_audits()) == 1
        assert DISPATCHES == ["job-next"]
