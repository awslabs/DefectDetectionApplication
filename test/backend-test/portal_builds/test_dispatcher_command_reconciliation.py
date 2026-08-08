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
Integration tests for the scheduled command reconciliation step of
``edge-cv-portal/backend/functions/build_dispatcher.py``
(build-fleet-execution-failures task 5.2).

**Validates: Requirements 2.5, 2.6, 2.7, 2.11, 3.2, 3.4**

On the existing one-minute tick the dispatcher now inspects
command-bearing nonterminal jobs, settlement waits, ambiguous `sending`
dispatch attempts, and terminal jobs with incomplete diagnostics:

- a missing/omitted EventBridge event costs only latency: the tick
  retrieves the final invocation and settles the same deterministic
  outcome the event path would have (Req 2.5);
- a genuinely nonterminal invocation keeps the job nonterminal;
- `Success` without a callback becomes AGENT_RESULT_MISSING only AFTER
  the settlement window (Req 2.4/2.5);
- an ambiguous SendCommand is recovered through the deterministic
  job/attempt command comment and a recent-command lookup BEFORE any
  resend; only a conditional attempt after the visibility bound may
  send anew (Req 2.7, at most one effective dispatch);
- dispatch records the execution attempt with dispatch_state
  transitions around SendCommand and the deterministic comment;
- terminal jobs gain diagnostic completeness without any change to
  their absorbed status/error/ended_at (Req 2.6).

Everything is moto/fake backed — no live AWS. Run from the repo root:

    PYTHONPATH=src/backend:test/backend-test python3 -m pytest \
        test/backend-test/portal_builds/test_dispatcher_command_reconciliation.py \
        --noconftest -q
"""
import os
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

_SUFFIX = "cmd-tick-reconcile"
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
# scripted GetCommandInvocation / ListCommands, recorded SendCommand
# (delegated to moto so command ids are real). No call leaves the
# process.
# ---------------------------------------------------------------------------
SSM_INVOCATIONS = {}
SSM_GET_CALLS = []
SSM_LIST_COMMANDS = []
SSM_SEND_CALLS = []

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
import build_dispatcher  # noqa: E402

# The handler above captured its (fake-wrapped) clients at import;
# restore the real factory so OTHER test modules collected in the same
# pytest process get untouched moto clients.
boto3.client = _REAL_BOTO3_CLIENT

NOW = 1_786_017_773_000
_MINUTE_MS = 60 * 1000


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
    del SSM_GET_CALLS[:]
    del SSM_LIST_COMMANDS[:]
    del SSM_SEND_CALLS[:]
    SSM_INVOCATIONS.clear()


def _get_job(job_id):
    return build_dispatcher.to_native(
        _JOBS.get_item(Key={"build_job_id": job_id}).get("Item"))


def _seed_running_job(job_id, command_id, instance_id, status="building",
                      server_id=None, **extra):
    attempt_id = str(uuid.uuid4())
    job = {
        "build_job_id": job_id,
        "build_target": build_domain.TARGET_JP5,
        "execution_mode": (build_domain.EXECUTION_MODE_DEDICATED
                           if server_id else
                           build_domain.EXECUTION_MODE_EPHEMERAL),
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
    if server_id:
        job["server_id"] = server_id
        _SERVERS.put_item(Item={
            "server_id": server_id,
            "instance_id": instance_id,
            "lifecycle_state": "running",
            "running_build_job_id": job_id,
        })
    else:
        job["runner"] = {"instance_id": instance_id}
    job.update(extra)
    _JOBS.put_item(Item=job)
    return job


def _script_invocation(command_id, instance_id, status="Failed",
                       response_code=127, stderr="", stdout=""):
    SSM_INVOCATIONS[command_id] = {
        "CommandId": command_id,
        "InstanceId": instance_id,
        "Status": status,
        "StatusDetails": status,
        "ResponseCode": response_code,
        "StandardOutputContent": stdout,
        "StandardErrorContent": stderr,
    }


def _tick(now):
    with mock.patch.object(build_dispatcher, "run_shell_sync",
                           return_value=None):
        build_dispatcher.run_tick(now=now)


# ---------------------------------------------------------------------------
# Missing event: tick reconciliation settles the same outcome (Req 2.5)
# ---------------------------------------------------------------------------

class TestMissingEventReconciliation:

    def setup_method(self):
        _clear_state()

    def test_terminal_failed_invocation_settles_on_the_tick(self):
        command_id = str(uuid.uuid4())
        _seed_running_job("job-1", command_id, "i-fake1",
                          server_id="srv-1")
        _script_invocation(command_id, "i-fake1", stderr="exit 127")

        _tick(NOW)

        job = _get_job("job-1")
        assert job["status"] == build_domain.STATUS_FAILED
        assert job["error"]["code"] == \
            build_reconciliation.CODE_COMMAND_EXECUTION_FAILED
        diag = job["execution_diagnostic"]
        assert diag["response_code"] == 127
        assert diag["source"] == ["scheduled_reconciliation"]
        # The dedicated allocation was released for promotion (Req 3.2).
        server = build_dispatcher.to_native(
            _SERVERS.get_item(Key={"server_id": "srv-1"}).get("Item"))
        assert "running_build_job_id" not in server
        assert len([a for a in AUDIT_EVENTS
                    if a["action"] == "build_failed"]) == 1

    def test_nonterminal_invocation_stays_nonterminal(self):
        command_id = str(uuid.uuid4())
        _seed_running_job("job-2", command_id, "i-fake2")
        _script_invocation(command_id, "i-fake2", status="InProgress",
                           response_code=0)

        _tick(NOW)
        _tick(NOW + _MINUTE_MS)

        job = _get_job("job-2")
        assert job["status"] == build_domain.STATUS_BUILDING
        assert "error" not in job
        assert AUDIT_EVENTS == []

    def test_repeated_ticks_converge_without_duplicate_effects(self):
        command_id = str(uuid.uuid4())
        _seed_running_job("job-3", command_id, "i-fake3")
        _script_invocation(command_id, "i-fake3", stderr="boom")

        _tick(NOW)
        first = _get_job("job-3")
        _tick(NOW + _MINUTE_MS)
        second = _get_job("job-3")

        assert second["status"] == build_domain.STATUS_FAILED
        assert second["ended_at"] == first["ended_at"]
        assert len([a for a in AUDIT_EVENTS
                    if a["action"] == "build_failed"]) == 1


# ---------------------------------------------------------------------------
# Success settlement: AGENT_RESULT_MISSING only after the bound (Req 2.4)
# ---------------------------------------------------------------------------

class TestSuccessSettlement:

    def setup_method(self):
        _clear_state()

    def test_success_without_callback_settles_after_the_window(self):
        command_id = str(uuid.uuid4())
        _seed_running_job("job-4", command_id, "i-fake4")
        _script_invocation(command_id, "i-fake4", status="Success",
                           response_code=0)

        # Tick 1: settlement wait recorded; the job stays nonterminal.
        _tick(NOW)
        job = _get_job("job-4")
        assert job["status"] == build_domain.STATUS_BUILDING
        deadline = job["reconciliation"]["settlement_deadline"]
        assert deadline == NOW + build_dispatcher.SETTLEMENT_WINDOW_MS

        # Tick 2, inside the window: still waiting for the in-flight
        # agent result (never classified early).
        _tick(NOW + _MINUTE_MS)
        assert _get_job("job-4")["status"] == build_domain.STATUS_BUILDING

        # Tick 3, past the settlement deadline: AGENT_RESULT_MISSING.
        _tick(deadline + _MINUTE_MS)
        job = _get_job("job-4")
        assert job["status"] == build_domain.STATUS_FAILED
        assert job["error"]["code"] == \
            build_reconciliation.CODE_AGENT_RESULT_MISSING

    def test_callback_inside_settlement_window_wins(self):
        """A valid agent result recorded during settlement keeps
        authority; the later settlement tick cannot overwrite it."""
        command_id = str(uuid.uuid4())
        _seed_running_job("job-5", command_id, "i-fake5")
        _script_invocation(command_id, "i-fake5", status="Success",
                           response_code=0)

        _tick(NOW)  # settlement wait recorded
        deadline = _get_job("job-5")["reconciliation"]["settlement_deadline"]
        # The agent's succeeded callback lands (recorded by the event
        # consumer through the existing phase path).
        _JOBS.update_item(
            Key={"build_job_id": "job-5"},
            UpdateExpression="SET #s = :succeeded, ended_at = :end",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":succeeded": "succeeded",
                                       ":end": NOW + 30_000})

        _tick(deadline + _MINUTE_MS)

        job = _get_job("job-5")
        assert job["status"] == build_domain.STATUS_SUCCEEDED
        assert job["ended_at"] == NOW + 30_000
        assert "error" not in job


# ---------------------------------------------------------------------------
# Ambiguous SendCommand recovery (Req 2.7)
# ---------------------------------------------------------------------------

def _seed_sending_job(job_id, instance_id, sending_at):
    attempt_id = str(uuid.uuid4())
    comment = build_reconciliation.command_comment(job_id, attempt_id)
    job = {
        "build_job_id": job_id,
        "build_target": build_domain.TARGET_JP5,
        "execution_mode": build_domain.EXECUTION_MODE_EPHEMERAL,
        "status": build_domain.STATUS_BUILDING,
        "requested_by": "operator-1",
        "created_at": NOW - 10 * _MINUTE_MS,
        "started_at": NOW - 9 * _MINUTE_MS,
        "config_snapshot": {"max_runtime_hours": 4},
        "runner": {"instance_id": instance_id},
        "execution_attempt": {
            "attempt_id": attempt_id,
            "dispatch_state": build_reconciliation.DISPATCH_SENDING,
            "instance_id": instance_id,
            "command_id": None,
            "command_comment": comment,
            "claimed_at": sending_at,
            "sending_at": sending_at,
            "sent_at": None,
        },
    }
    _JOBS.put_item(Item=job)
    return job, comment


class TestAmbiguousSendRecovery:

    def setup_method(self):
        _clear_state()

    def test_existing_command_is_attached_before_any_resend(self):
        _, comment = _seed_sending_job("job-6", "i-fake6",
                                       NOW - 10 * _MINUTE_MS)
        SSM_LIST_COMMANDS.append(
            {"CommandId": "cmd-recovered", "Comment": comment})

        _tick(NOW)

        job = _get_job("job-6")
        attempt = job["execution_attempt"]
        assert attempt["command_id"] == "cmd-recovered"
        assert attempt["dispatch_state"] == \
            build_reconciliation.DISPATCH_SENT
        assert job["ssm"]["command_id"] == "cmd-recovered"
        assert SSM_SEND_CALLS == [], \
            "recovery through the command comment must NOT resend"

    def test_no_resend_inside_the_visibility_bound(self):
        _seed_sending_job("job-7", "i-fake7", NOW - _MINUTE_MS)

        _tick(NOW)

        job = _get_job("job-7")
        assert job["execution_attempt"]["dispatch_state"] == \
            build_reconciliation.DISPATCH_SENDING
        assert SSM_SEND_CALLS == [], \
            "an ambiguous send inside the visibility bound is never resent"

    def test_conditional_resend_after_the_visibility_bound(self):
        instance_id = _launch_instance()
        _, comment = _seed_sending_job(
            "job-8", instance_id,
            NOW - build_dispatcher.AMBIGUOUS_SEND_VISIBILITY_MS
            - _MINUTE_MS)

        _tick(NOW)

        assert len(SSM_SEND_CALLS) == 1, \
            "exactly one conditional attempt after the visibility bound"
        assert SSM_SEND_CALLS[0]["Comment"] == comment
        job = _get_job("job-8")
        attempt = job["execution_attempt"]
        assert attempt["dispatch_state"] == \
            build_reconciliation.DISPATCH_SENT
        assert attempt["command_id"]
        assert job["ssm"]["command_id"] == attempt["command_id"]

        # A later tick does not send again: the attempt is settled.
        _tick(NOW + _MINUTE_MS)
        assert len(SSM_SEND_CALLS) == 1


# ---------------------------------------------------------------------------
# Dispatch records the execution attempt around SendCommand (Req 2.7)
# ---------------------------------------------------------------------------

class TestDispatchAttemptRecording:

    def setup_method(self):
        _clear_state()

    def test_dedicated_dispatch_records_attempt_and_comment(self):
        instance_id = _launch_instance()
        _SERVERS.put_item(Item={
            "server_id": "srv-d",
            "name": "arm-server-d",
            "instance_id": instance_id,
            "lifecycle_state": "running",
            "cpu_architecture": build_domain.ARCH_ARM64,
        })
        _JOBS.put_item(Item={
            "build_job_id": "job-9",
            "build_target": build_domain.TARGET_JP5,
            "execution_mode": build_domain.EXECUTION_MODE_DEDICATED,
            "status": build_domain.STATUS_QUEUED,
            "requested_by": "operator-1",
            "created_at": NOW - _MINUTE_MS,
            "server_id": "srv-d",
            "config_snapshot": {"max_runtime_hours": 4},
        })

        with mock.patch.object(build_dispatcher, "run_shell_sync",
                               return_value=""):
            build_dispatcher.run_tick(now=NOW)

        job = _get_job("job-9")
        assert job["status"] == build_domain.STATUS_BUILDING
        attempt = job["execution_attempt"]
        assert attempt["dispatch_state"] == \
            build_reconciliation.DISPATCH_SENT
        assert attempt["command_id"] == job["ssm"]["command_id"]
        assert attempt["instance_id"] == instance_id
        assert job["ssm"]["instance_id"] == instance_id
        expected_comment = build_reconciliation.command_comment(
            "job-9", attempt["attempt_id"])
        assert attempt["command_comment"] == expected_comment
        # The SendCommand itself carried the deterministic comment.
        agent_sends = [c for c in SSM_SEND_CALLS
                       if c.get("Comment") == expected_comment]
        assert len(agent_sends) == 1


# ---------------------------------------------------------------------------
# Terminal jobs gain diagnostic completeness only (Req 2.6)
# ---------------------------------------------------------------------------

class TestTerminalDiagnosticCompletion:

    def setup_method(self):
        _clear_state()

    def test_late_diagnostics_never_touch_the_absorbed_outcome(self):
        command_id = str(uuid.uuid4())
        _seed_running_job(
            "job-10", command_id, "i-fake10",
            status=build_domain.STATUS_FAILED,
            ended_at=NOW - _MINUTE_MS,
            error={"code": "AGENT_COMMAND_FAILED",
                   "message": "The build agent SSM command ended with "
                              "status 'Failed' before reporting a build "
                              "result."})
        _script_invocation(command_id, "i-fake10", stderr="exit 127")

        _tick(NOW)

        job = _get_job("job-10")
        # Absorbed outcome untouched (Req 2.6 terminal absorption).
        assert job["status"] == build_domain.STATUS_FAILED
        assert job["error"]["code"] == "AGENT_COMMAND_FAILED"
        assert job["ended_at"] == NOW - _MINUTE_MS
        # Diagnostic completeness increased.
        diag = job["execution_diagnostic"]
        assert diag["response_code"] == 127
        assert diag["stderr"]["available"] is True
        assert diag["complete"] is True
        assert AUDIT_EVENTS == []
