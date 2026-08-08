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
Integration tests for the terminal-effects ledger adapters of
``build_events.py`` / ``build_dispatcher.py`` / ``build_jobs.py``
(build-fleet-execution-failures tasks 6.1, 6.2, 6.3).

**Validates: Requirements 2.6, 2.7, 2.11, 3.1, 3.2, 3.3, 3.4, 3.8, 3.11**

- ONE terminal finalization write carries status, error-or-result,
  ``ended_at``, the sanitized-evidence digest, and the stable
  ``<job>:<attempt>:terminal`` effect ID; an absorbed terminal outcome
  is never rewritten by duplicate deliveries (task 6.1, Req 2.6);
- Audit_Log writes are deduplicated by the stable effect identity:
  retries may complete a PENDING audit but cannot create a second
  logical audit (task 6.1, Req 2.7);
- timeout/cancellation/interruption cleanup is retryable and VERIFIED:
  cleanup pending is recorded first, the stop is sent idempotently, the
  release happens only once protected processes are pgrep-confirmed
  absent — and ONLY for the job/attempt still owning the allocation;
  unknown process state keeps the slot and its followers blocked
  (task 6.2, Req 3.2/3.3/3.11);
- idempotent ephemeral termination counts as cleanup success (task 6.2);
- exactly one OLDEST eligible follower is promoted through the existing
  planner + conditional server lock; retries/races cannot promote a
  younger job or dispatch two followers (task 6.3, Req 3.4/3.11);
- races are simulated via conditional-write outcomes (pre-completed or
  mismatched ledger states) and repeated handler invocations.

Everything is moto/fake backed — no live AWS. Run from the repo root:

    PYTHONPATH=src/backend:test/backend-test python3 -m pytest \
        test/backend-test/portal_builds/test_terminal_effects_ledger.py \
        --noconftest -q
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

_SUFFIX = "terminal-effects"
_JOBS_TABLE = f"dda-portal-build-jobs-{_SUFFIX}"
_SERVERS_TABLE = f"dda-portal-build-servers-{_SUFFIX}"
os.environ["BUILD_JOBS_TABLE"] = _JOBS_TABLE
os.environ["BUILD_SERVERS_TABLE"] = _SERVERS_TABLE
# Unset: the events consumer's promotion wakeup must fall back to the
# scheduled tick (task 6.3 "retain the schedule fallback").
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
import build_reconciliation  # noqa: E402
import build_events  # noqa: E402
import build_dispatcher  # noqa: E402
import build_jobs  # noqa: E402

# Restore the real factory so other test modules collected in the same
# pytest process get untouched moto clients.
boto3.client = _REAL_BOTO3_CLIENT

NOW = 1_786_100_000_000
_MINUTE_MS = 60 * 1000

_ZERO_COUNT = "GDK_BUILD_COUNT=0\nCUSTOM_BUILD_COUNT=0\n"
_HEX_DIGEST = re.compile(r"^[0-9a-f]{64}$")


def _launch_instance():
    response = _EC2.run_instances(
        ImageId=_AMI_ID, InstanceType="m6g.4xlarge", MinCount=1, MaxCount=1)
    return response["Instances"][0]["InstanceId"]


def _clear_state():
    for item in _JOBS.scan().get("Items", []):
        _JOBS.delete_item(Key={"build_job_id": item["build_job_id"]})
    for item in _SERVERS.scan().get("Items", []):
        _SERVERS.delete_item(Key={"server_id": item["server_id"]})
    del AUDIT_EVENTS[:]
    del SSM_SEND_CALLS[:]
    SSM_INVOCATIONS.clear()


def _get_job(job_id):
    return build_dispatcher.to_native(
        _JOBS.get_item(Key={"build_job_id": job_id}).get("Item"))


def _get_server(server_id):
    return build_dispatcher.to_native(
        _SERVERS.get_item(Key={"server_id": server_id}).get("Item"))


def _audits(action):
    return [a for a in AUDIT_EVENTS if a["action"] == action]


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
    _JOBS.put_item(Item=job)
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


def _tick(now, shell_output=None):
    """One dispatcher tick with a scripted run_shell_sync outcome and no
    real agent dispatch."""
    with mock.patch.object(build_dispatcher, "run_shell_sync",
                           return_value=shell_output), \
            mock.patch.object(build_dispatcher, "send_agent",
                              return_value=("cmd-follow", "st")) as agent:
        build_dispatcher.run_tick(now=now)
    return agent


# ===========================================================================
# Task 6.1 — one terminal outcome, one ledger, one logical audit
# ===========================================================================

class TestEventTerminalFinalizationLedger:

    def setup_method(self):
        _clear_state()

    def test_ssm_failed_event_finalizes_once_with_ledger_and_digest(self):
        """The terminal finalization write carries the stable effect ID,
        the evidence digest, and the ledger; a DUPLICATE delivery cannot
        rewrite the absorbed outcome or create a second logical audit
        (Req 2.6, 2.7, 3.1)."""
        command_id, attempt_id = str(uuid.uuid4()), str(uuid.uuid4())
        _seed_server("srv-1", instance_id="i-led1",
                     running_build_job_id="job-1")
        _seed_job("job-1", build_domain.STATUS_BUILDING,
                  server_id="srv-1", command_id=command_id,
                  instance_id="i-led1", attempt_id=attempt_id)
        _script_invocation(command_id, "i-led1")

        _deliver_ssm_event(command_id, "i-led1")
        first = _get_job("job-1")
        _deliver_ssm_event(command_id, "i-led1")  # duplicate delivery
        second = _get_job("job-1")

        assert first["status"] == build_domain.STATUS_FAILED
        assert second["status"] == first["status"]
        assert second["ended_at"] == first["ended_at"]
        assert second["error"] == first["error"]

        effects = second["terminal_effects"]
        assert effects["effect_id"] == \
            build_reconciliation.terminal_effect_id("job-1", attempt_id)
        assert effects["audit"] == build_reconciliation.EFFECT_DONE
        # A terminal invocation means the command shell exited: no stop
        # verification applies on the dedicated server.
        assert effects["compute_cleanup"] == \
            build_reconciliation.EFFECT_NOT_APPLICABLE
        assert effects["allocation_release"] == \
            build_reconciliation.EFFECT_DONE
        assert _HEX_DIGEST.match(second["evidence_digest"])

        # ONE logical audit carrying the effect identity (Req 2.7).
        failures = _audits("build_failed")
        assert len(failures) == 1
        assert failures[0]["details"]["terminal_effect_id"] == \
            effects["effect_id"]
        # The allocation was released for promotion (Req 3.2).
        assert "running_build_job_id" not in _get_server("srv-1")

    def test_promotion_wakeup_falls_back_to_the_scheduled_tick(self):
        """With no dispatcher function configured, the events consumer
        leaves promotion_wakeup PENDING; the scheduled tick — the
        retained fallback (task 6.3) — completes it."""
        command_id, attempt_id = str(uuid.uuid4()), str(uuid.uuid4())
        _seed_server("srv-2", instance_id="i-led2",
                     running_build_job_id="job-2")
        _seed_job("job-2", build_domain.STATUS_BUILDING,
                  server_id="srv-2", command_id=command_id,
                  instance_id="i-led2", attempt_id=attempt_id)
        _script_invocation(command_id, "i-led2")

        _deliver_ssm_event(command_id, "i-led2")
        assert _get_job("job-2")["terminal_effects"]["promotion_wakeup"] \
            == build_reconciliation.EFFECT_PENDING

        _tick(NOW)
        assert _get_job("job-2")["terminal_effects"]["promotion_wakeup"] \
            == build_reconciliation.EFFECT_DONE
        # The tick added no second logical audit (Req 2.7).
        assert len(_audits("build_failed")) == 1

    def test_phase_failure_ledger_and_single_audit(self):
        """An agent-reported terminal failure finalizes with the ledger
        (cleanup not applicable on a dedicated server: the agent exited
        on its own) and duplicate delivery is a no-op (Req 2.6, 3.1)."""
        _seed_server("srv-3", instance_id="i-led3",
                     running_build_job_id="job-3")
        _seed_job("job-3", build_domain.STATUS_BUILDING, server_id="srv-3")

        _deliver_phase_event("job-3", "failed", error_kind="building",
                             error_message="gdk component build failed")
        first = _get_job("job-3")
        _deliver_phase_event("job-3", "failed", error_kind="building",
                             error_message="gdk component build failed")
        second = _get_job("job-3")

        assert first["status"] == build_domain.STATUS_FAILED
        assert second["ended_at"] == first["ended_at"]
        effects = second["terminal_effects"]
        assert effects["audit"] == build_reconciliation.EFFECT_DONE
        assert effects["compute_cleanup"] == \
            build_reconciliation.EFFECT_NOT_APPLICABLE
        assert effects["allocation_release"] == \
            build_reconciliation.EFFECT_DONE
        assert len(_audits("build_failed")) == 1
        assert "running_build_job_id" not in _get_server("srv-3")

    def test_instance_loss_completes_cleanup_from_observed_state(self):
        """An observed stopped/terminated instance IS the cleanup
        evidence (design rule): the interruption finalizes with cleanup
        done, releases the slot, and audits once (Req 3.8)."""
        _seed_server("srv-4", instance_id="i-gone4",
                     running_build_job_id="job-4")
        _seed_job("job-4", build_domain.STATUS_BUILDING, server_id="srv-4")

        event = {
            "source": "aws.ec2",
            "detail-type": "EC2 Instance State-change Notification",
            "detail": {"instance-id": "i-gone4", "state": "stopped"},
        }
        build_events.handler(event, None)
        build_events.handler(event, None)  # duplicate delivery

        job = _get_job("job-4")
        assert job["status"] == build_domain.STATUS_INTERRUPTED
        effects = job["terminal_effects"]
        assert effects["compute_cleanup"] == \
            build_reconciliation.EFFECT_DONE
        assert effects["allocation_release"] == \
            build_reconciliation.EFFECT_DONE
        assert len(_audits("build_interrupted")) == 1
        assert "running_build_job_id" not in _get_server("srv-4")


# ===========================================================================
# Conditional-write outcomes: duplicate/out-of-order effect completion
# ===========================================================================

class TestConditionalEffectAdapters:

    def setup_method(self):
        _clear_state()

    def _seed_terminal(self, job_id, ledger):
        _seed_job(job_id, build_domain.STATUS_FAILED,
                  ended_at=NOW, terminal_effects=ledger)
        return ledger["effect_id"]

    def test_duplicate_audit_completion_is_refused(self):
        """A retry may complete a PENDING audit exactly once; the second
        completion loses the conditional write (Req 2.7)."""
        ledger = build_reconciliation.plan_terminal_effects(
            "job-a", "att-a", build_domain.EXECUTION_MODE_DEDICATED)
        effect_id = self._seed_terminal("job-a", ledger)
        assert build_dispatcher.complete_effect(
            "job-a", effect_id, build_reconciliation.EFFECT_AUDIT) is True
        assert build_dispatcher.complete_effect(
            "job-a", effect_id, build_reconciliation.EFFECT_AUDIT) is False

    def test_release_requires_verified_cleanup_first(self):
        """The allocation release conditional write refuses to complete
        before the compute cleanup is done (stop-before-release,
        Req 3.11); promotion refuses before the release."""
        ledger = build_reconciliation.plan_terminal_effects(
            "job-b", "att-b", build_domain.EXECUTION_MODE_DEDICATED)
        effect_id = self._seed_terminal("job-b", ledger)
        release = build_reconciliation.EFFECT_ALLOCATION_RELEASE
        promote = build_reconciliation.EFFECT_PROMOTION_WAKEUP
        cleanup = build_reconciliation.EFFECT_COMPUTE_CLEANUP

        assert build_dispatcher.complete_effect(
            "job-b", effect_id, release) is False
        assert build_dispatcher.complete_effect(
            "job-b", effect_id, promote) is False
        assert build_dispatcher.complete_effect(
            "job-b", effect_id, cleanup) is True
        assert build_dispatcher.complete_effect(
            "job-b", effect_id, release) is True
        assert build_dispatcher.complete_effect(
            "job-b", effect_id, promote) is True

    def test_mismatched_effect_identity_is_rejected(self):
        """Evidence/retries carrying another attempt's effect identity
        can never advance this outcome's ledger (Req 2.6)."""
        ledger = build_reconciliation.plan_terminal_effects(
            "job-c", "att-c", build_domain.EXECUTION_MODE_DEDICATED)
        self._seed_terminal("job-c", ledger)
        stale = build_reconciliation.terminal_effect_id("job-c", "att-old")
        assert build_dispatcher.complete_effect(
            "job-c", stale, build_reconciliation.EFFECT_AUDIT) is False

    def test_evidence_digest_is_stable_and_order_independent(self):
        digest = build_dispatcher.evidence_digest(
            {"b": 2, "a": 1, "nested": {"y": [1, 2]}})
        assert digest == build_dispatcher.evidence_digest(
            {"nested": {"y": [1, 2]}, "a": 1, "b": 2})
        assert _HEX_DIGEST.match(digest)
        assert build_dispatcher.evidence_digest(None) is None
        assert build_events.evidence_digest(
            {"a": 1, "b": 2, "nested": {"y": [1, 2]}}) == digest


# ===========================================================================
# Task 6.2/6.3 — verified cleanup gates release; one oldest follower
# ===========================================================================

class TestVerifiedCleanupAndPromotion:

    def setup_method(self):
        _clear_state()

    def _seed_blocked_terminal(self, server_id, instance_id, job_id):
        """A terminal failed dedicated job holding its slot with cleanup
        PENDING (audit already done), exactly as the timeout watchdog
        records before verification."""
        attempt_id = str(uuid.uuid4())
        ledger = build_reconciliation.plan_terminal_effects(
            job_id, attempt_id, build_domain.EXECUTION_MODE_DEDICATED)
        ledger = build_reconciliation.advance_effect(
            ledger, build_reconciliation.EFFECT_AUDIT).ledger
        _seed_server(server_id, instance_id=instance_id,
                     running_build_job_id=job_id)
        _seed_job(job_id, build_domain.STATUS_FAILED, server_id=server_id,
                  ended_at=NOW - _MINUTE_MS, terminal_effects=ledger,
                  error={"code": "TIMEOUT", "message": "timed out"})
        return ledger

    def test_unknown_process_state_blocks_release_and_promotion(self):
        """While the pgrep confirmation cannot positively complete, the
        slot stays held and NO follower is dispatched (Req 3.11); the
        idempotent stop is re-sent by the reconciliation."""
        self._seed_blocked_terminal("srv-p", "i-prom1", "job-dead")
        _seed_job("job-old", build_domain.STATUS_QUEUED,
                  server_id="srv-p", created_at=NOW - 5 * _MINUTE_MS)
        _seed_job("job-young", build_domain.STATUS_QUEUED,
                  server_id="srv-p", created_at=NOW - 1 * _MINUTE_MS)

        agent = _tick(NOW, shell_output=None)  # process state UNKNOWN

        agent.assert_not_called()
        assert _get_server("srv-p")["running_build_job_id"] == "job-dead"
        job = _get_job("job-dead")
        assert job["terminal_effects"]["compute_cleanup"] == \
            build_reconciliation.EFFECT_PENDING
        assert job["terminal_effects"]["allocation_release"] == \
            build_reconciliation.EFFECT_PENDING
        # The stop was (re-)sent idempotently while blocked.
        stop_sends = [c for c in SSM_SEND_CALLS
                      if c.get("Parameters", {}).get("commands") ==
                      build_dispatcher.STOP_BUILD_COMMANDS]
        assert len(stop_sends) == 1
        # Both followers stay queued in original order (Req 3.4).
        assert _get_job("job-old")["status"] == build_domain.STATUS_QUEUED
        assert _get_job("job-young")["status"] == build_domain.STATUS_QUEUED

    def test_verified_cleanup_releases_then_exactly_one_oldest_follower(self):
        """Once the stop is pgrep-confirmed, the release completes and
        the NEXT ticks promote exactly the OLDEST eligible follower via
        the existing planner + conditional lock; repeated ticks cannot
        dispatch a second follower or the younger job first
        (task 6.3, Req 3.2/3.4/3.11)."""
        self._seed_blocked_terminal("srv-p", "i-prom2", "job-dead")
        _seed_job("job-old", build_domain.STATUS_QUEUED,
                  server_id="srv-p", created_at=NOW - 5 * _MINUTE_MS)
        _seed_job("job-young", build_domain.STATUS_QUEUED,
                  server_id="srv-p", created_at=NOW - 1 * _MINUTE_MS)

        # Tick 1: cleanup verified (pgrep count 0) -> release completes.
        agent = _tick(NOW, shell_output=_ZERO_COUNT)
        agent.assert_not_called()  # dispatch step ran before the release
        job = _get_job("job-dead")
        assert job["terminal_effects"]["compute_cleanup"] == \
            build_reconciliation.EFFECT_DONE
        assert job["terminal_effects"]["allocation_release"] == \
            build_reconciliation.EFFECT_DONE
        assert "running_build_job_id" not in _get_server("srv-p")

        # Tick 2: the freed slot promotes the OLDEST follower only.
        agent = _tick(NOW + _MINUTE_MS, shell_output="")
        assert agent.call_count == 1
        assert agent.call_args.args[0]["build_job_id"] == "job-old"
        assert _get_job("job-old")["status"] == build_domain.STATUS_BUILDING
        assert _get_job("job-young")["status"] == build_domain.STATUS_QUEUED
        # Original submission time retained through the wait (Req 3.4).
        assert _get_job("job-old")["created_at"] == NOW - 5 * _MINUTE_MS
        # The dispatch claim was recorded with the send (task 6.3).
        attempt = _get_job("job-old")["execution_attempt"]
        assert attempt["dispatch_state"] == \
            build_reconciliation.DISPATCH_SENT
        assert attempt["command_comment"] == \
            build_reconciliation.command_comment("job-old",
                                                 attempt["attempt_id"])

        # Tick 3: the occupied slot admits NO second dispatch (Req 3.2).
        agent = _tick(NOW + 2 * _MINUTE_MS, shell_output="")
        agent.assert_not_called()
        assert _get_job("job-young")["status"] == build_domain.STATUS_QUEUED
        assert _get_server("srv-p")["running_build_job_id"] == "job-old"

    def test_stale_release_cannot_free_another_jobs_allocation(self):
        """A retried release for a job that no longer owns the slot is
        REJECTED by the conditional server write: the ledger effect may
        complete on the stale job's record, but the slot another job
        took stays held (stale-release rejection, Req 3.2/3.11)."""
        attempt_id = str(uuid.uuid4())
        ledger = build_reconciliation.plan_terminal_effects(
            "job-stale", attempt_id, build_domain.EXECUTION_MODE_DEDICATED)
        ledger = build_reconciliation.advance_effect(
            ledger, build_reconciliation.EFFECT_AUDIT).ledger
        ledger = build_reconciliation.advance_effect(
            ledger, build_reconciliation.EFFECT_COMPUTE_CLEANUP).ledger
        # The slot has already moved on to another job.
        _seed_server("srv-s", instance_id="i-stale",
                     running_build_job_id="job-current")
        _seed_job("job-stale", build_domain.STATUS_FAILED,
                  server_id="srv-s", ended_at=NOW - _MINUTE_MS,
                  terminal_effects=ledger,
                  error={"code": "TIMEOUT", "message": "timed out"})
        _seed_job("job-current", build_domain.STATUS_BUILDING,
                  server_id="srv-s", command_id="cmd-cur",
                  instance_id="i-stale")

        # Direct adapter retry: the conditional slot release loses.
        assert build_dispatcher.release_server(
            "srv-s", "job-stale") is False
        # Driving the full release path converges the ledger but never
        # frees the slot the CURRENT job owns.
        terminal_job = _get_job("job-stale")
        build_dispatcher.release_and_promote(
            terminal_job, terminal_job["terminal_effects"])
        assert _get_server("srv-s")["running_build_job_id"] == \
            "job-current"

    def test_ephemeral_termination_completes_cleanup_effect(self):
        """Idempotent ephemeral termination is cleanup success: the
        termination watchdog terminates the runner and completes the
        ledger's cleanup effect in the same tick (task 6.2, Req 3.8)."""
        instance_id = _launch_instance()
        attempt_id = str(uuid.uuid4())
        ledger = build_reconciliation.plan_terminal_effects(
            "job-eph", attempt_id, build_domain.EXECUTION_MODE_EPHEMERAL)
        ledger = build_reconciliation.advance_effect(
            ledger, build_reconciliation.EFFECT_AUDIT).ledger
        _seed_job("job-eph", build_domain.STATUS_FAILED,
                  mode=build_domain.EXECUTION_MODE_EPHEMERAL,
                  ended_at=NOW - _MINUTE_MS, terminal_effects=ledger,
                  error={"code": "BUILD_FAILED", "message": "x"},
                  runner={"instance_id": instance_id,
                          "terminate_attempts": 0})

        _tick(NOW)

        job = _get_job("job-eph")
        assert job["runner"]["terminated_at"]
        assert job["terminal_effects"]["compute_cleanup"] == \
            build_reconciliation.EFFECT_DONE
        state = _EC2.describe_instances(InstanceIds=[instance_id])[
            "Reservations"][0]["Instances"][0]["State"]["Name"]
        assert state in ("shutting-down", "terminated")


# ===========================================================================
# Task 6.2 — cancellation cleanup routed through the same ledger
# ===========================================================================

class TestCancellationLedgerRouting:

    def setup_method(self):
        _clear_state()

    def test_running_cancellation_records_verified_cleanup(self):
        """A pgrep-confirmed running cancellation writes the ledger in
        the same conditional cancellation write: audit done (the cancel
        handler writes its own build_cancelled entry), cleanup VERIFIED
        done, release pending for the dispatcher (Req 3.3, 3.11)."""
        attempt_id = str(uuid.uuid4())
        _seed_server("srv-c", instance_id="i-can1",
                     running_build_job_id="job-can")
        job = _seed_job("job-can", build_domain.STATUS_BUILDING,
                        server_id="srv-c", command_id="cmd-can",
                        instance_id="i-can1", attempt_id=attempt_id)

        ledger = build_jobs.cancellation_ledger(job, dispatched=True,
                                                stop_verified=True)
        assert build_jobs.apply_job_cancellation(
            "job-can", build_domain.STATUS_BUILDING, NOW,
            terminal_effects=ledger) is True

        stored = _get_job("job-can")
        assert stored["status"] == build_domain.STATUS_CANCELLED
        effects = stored["terminal_effects"]
        assert effects["effect_id"] == \
            build_reconciliation.terminal_effect_id("job-can", attempt_id)
        assert effects["audit"] == build_reconciliation.EFFECT_DONE
        assert effects["compute_cleanup"] == \
            build_reconciliation.EFFECT_DONE
        assert effects["allocation_release"] == \
            build_reconciliation.EFFECT_PENDING

        # The dispatcher completes the release/promotion effects without
        # inventing a second audit (Req 2.7).
        _tick(NOW + _MINUTE_MS)
        stored = _get_job("job-can")
        assert stored["terminal_effects"]["allocation_release"] == \
            build_reconciliation.EFFECT_DONE
        assert stored["terminal_effects"]["promotion_wakeup"] == \
            build_reconciliation.EFFECT_DONE
        assert "running_build_job_id" not in _get_server("srv-c")
        assert AUDIT_EVENTS == []  # no system audit was added

    def test_queued_cancellation_marks_cleanup_not_applicable(self):
        """A queued job never dispatched compute: its cancellation
        ledger records cleanup not applicable and the conflict/fail-
        closed semantics stay with the cancel handler (Req 3.3)."""
        job = _seed_job("job-q", build_domain.STATUS_QUEUED)
        ledger = build_jobs.cancellation_ledger(job, dispatched=False,
                                                stop_verified=False)
        assert ledger["compute_cleanup"] == \
            build_reconciliation.EFFECT_NOT_APPLICABLE
        assert ledger["audit"] == build_reconciliation.EFFECT_DONE
        assert build_jobs.apply_job_cancellation(
            "job-q", build_domain.STATUS_QUEUED, NOW,
            terminal_effects=ledger) is True
        # A stale writer still cannot double-cancel (conflict preserved).
        assert build_jobs.apply_job_cancellation(
            "job-q", build_domain.STATUS_QUEUED, NOW,
            terminal_effects=ledger) is False
