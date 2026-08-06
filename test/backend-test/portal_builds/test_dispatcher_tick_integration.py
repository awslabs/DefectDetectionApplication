# Copyright 2025 Amazon Web Services, Inc.
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
Moto integration tests for the build dispatcher tick
(``edge-cv-portal/backend/functions/build_dispatcher.py``, task 8.2 of
portal-build-fleet-and-workflow-gates).

**Validates: Requirements 3.1, 3.7, 3.8, 7.2, 7.5, 7.8**

End-to-end ``run_tick`` executions over moto-mocked DynamoDB / EC2 / SSM:

- Dedicated dispatch with the allocation lock: the eligible queued job
  takes the server's single running slot via the conditional update
  (``attribute_not_exists(running_build_job_id)``), transitions
  queued -> building, and the build agent is started via a real (moto)
  SSM SendCommand (Req 7.1, 7.2, 7.5). A second job targeting the same
  server — or a job targeting an already occupied server — stays queued
  (Req 7.2), and the conditional lock itself rejects a double
  allocation.
- Pre-dispatch pgrep verification: a detected build process defers the
  job (status queued, original ``created_at`` retained, ``deferred_at``
  recorded) with re-verification only after the 5-minute retry interval
  (Req 7.5, 7.6).
- Ephemeral provisioning: exactly one RunInstances per dispatched job,
  instance type / volume from the job's OWN ``config_snapshot``
  (Req 3.1, 9.3), agent SendCommand once the runner is SSM-managed, and
  runner termination once the job is terminal.
- Provisioning failure (Req 3.7): the job is marked failed with the
  provisioning cause, partially provisioned compute is terminated, and
  the failure is audited.
- Runtime timeout watchdog (Req 3.8): a running job past its
  ``config_snapshot`` max runtime gets the SSM stop commands, is failed
  with a timeout error, its logs reference is retained, and a dedicated
  server's allocation is released.
- Serialization watchdog (Req 7.8): a build-process count >= 2 triggers
  the stop-all within the 60-second window, fails the associated job
  with SERIALIZATION_VIOLATION, audits the violation, and releases the
  server allocation.

DynamoDB and EC2 are real moto; the dispatcher's synchronous SSM
verification helpers (``run_shell_sync``, ``instance_ssm_online``) and —
where a test only needs to observe the stop commands —
``send_shell_command`` are stubbed, because moto's SSM mock does not
emulate get_command_invocation output for AWS-RunShellScript. The agent
SendCommand path itself runs against real moto SSM. shared_utils is
replaced by a minimal fake capturing Audit_Log entries (the sibling
standalone-suite pattern).
"""
import os
import sys
import types
from unittest import mock

# ---------------------------------------------------------------------------
# Environment BEFORE any import: build_dispatcher binds its boto3
# resources/clients and env-derived settings at import time.
# ---------------------------------------------------------------------------
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ["AWS_ACCESS_KEY_ID"] = "testing"
os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
os.environ["AWS_SECURITY_TOKEN"] = "testing"
os.environ["AWS_SESSION_TOKEN"] = "testing"

_JOBS_TABLE = "build-jobs-t82"
_SERVERS_TABLE = "build-servers-t82"
os.environ["BUILD_JOBS_TABLE"] = _JOBS_TABLE
os.environ["BUILD_SERVERS_TABLE"] = _SERVERS_TABLE
# No repo URL: runner user-data bootstrap is skipped (pre-baked-AMI mode).
os.environ.pop("BUILD_REPO_URL", None)
os.environ.pop("BUILD_ALERT_TOPIC_ARN", None)
os.environ.pop("BUILD_INSTANCE_PROFILE_ARN", None)
os.environ.pop("BUILD_INSTANCE_PROFILE_NAME", None)
os.environ.pop("BUILD_SECURITY_GROUP_ID", None)
os.environ.pop("BUILD_SUBNET_ID", None)

# Import boto3 (and thus botocore/urllib3) from the test environment BEFORE
# the Lambda function directory joins sys.path.
import boto3  # noqa: E402
from botocore.exceptions import ClientError  # noqa: E402

# The flask-app verification container's python3.9 is built without the
# _bz2 C extension, and moto's request path imports moto.s3 -> bz2 on
# every call (moto.core.authorization -> moto.iam.access_control ->
# moto.s3.models). bz2 is only used for S3-Select payload decompression,
# which this suite never exercises, so a minimal stdlib-shaped stub keeps
# the import chain intact where _bz2 is absent (same shim as the sibling
# test_build_history_ordering.py).
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
# Fake shared_utils capturing Audit_Log entries (build_dispatcher imports
# only log_audit_event from the layer).
# ---------------------------------------------------------------------------
AUDIT_EVENTS = []


def _fake_shared_utils():
    module = types.ModuleType("shared_utils")

    def log_audit_event(**kwargs):
        AUDIT_EVENTS.append(kwargs)

    module.log_audit_event = log_audit_event
    return module


# Fresh modules so build_dispatcher's module-level boto3 handles are created
# under the moto mock started below (sibling pattern).
for _module in ("build_dispatcher", "build_planner", "build_domain",
                "shared_utils"):
    sys.modules.pop(_module, None)
sys.modules["shared_utils"] = _fake_shared_utils()

# Module-scope moto: active for every import below and for the whole run.
_MOCK = mock_aws()
_MOCK.start()

_DDB = boto3.resource("dynamodb", region_name="us-east-1")
for _name in (_JOBS_TABLE, _SERVERS_TABLE):
    _key = "build_job_id" if _name == _JOBS_TABLE else "server_id"
    _DDB.create_table(
        TableName=_name,
        KeySchema=[{"AttributeName": _key, "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": _key, "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
_JOBS = _DDB.Table(_JOBS_TABLE)
_SERVERS = _DDB.Table(_SERVERS_TABLE)

_EC2 = boto3.client("ec2", region_name="us-east-1")
_SSM = boto3.client("ssm", region_name="us-east-1")


def _default_ami_id():
    """Any moto-provided AMI id (fallback: register one)."""
    images = _EC2.describe_images(Owners=["amazon"]).get("Images", [])
    if images:
        return images[0]["ImageId"]
    return _EC2.register_image(  # pragma: no cover - moto version dependent
        Name="dda-test-ami", RootDeviceName="/dev/sda1",
        VirtualizationType="hvm")["ImageId"]


_AMI_ID = _default_ami_id()
# Explicit AMI ids so resolve_ami never consults the (absent) canonical
# public SSM parameters. Must be set BEFORE build_dispatcher is imported.
os.environ["BUILD_ARM64_AMI_ID"] = _AMI_ID
os.environ["BUILD_X86_64_AMI_ID"] = _AMI_ID

import build_domain  # noqa: E402
import build_planner  # noqa: E402
import build_dispatcher  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

NOW = 1_700_000_000_000  # ms epoch anchor for deterministic tick times
_MINUTE_MS = 60 * 1000
_HOUR_MS = 60 * _MINUTE_MS


def _clear_tables():
    for item in _JOBS.scan().get("Items", []):
        _JOBS.delete_item(Key={"build_job_id": item["build_job_id"]})
    for item in _SERVERS.scan().get("Items", []):
        _SERVERS.delete_item(Key={"server_id": item["server_id"]})


def _get_job(job_id):
    return build_dispatcher.to_native(
        _JOBS.get_item(Key={"build_job_id": job_id}).get("Item"))


def _get_server(server_id):
    return build_dispatcher.to_native(
        _SERVERS.get_item(Key={"server_id": server_id}).get("Item"))


def _launch_instance(instance_type="m6g.4xlarge"):
    """Real moto EC2 instance backing a Dedicated_Build_Server."""
    response = _EC2.run_instances(
        ImageId=_AMI_ID, InstanceType=instance_type, MinCount=1, MaxCount=1)
    return response["Instances"][0]["InstanceId"]


def _instance_state(instance_id):
    response = _EC2.describe_instances(InstanceIds=[instance_id])
    return response["Reservations"][0]["Instances"][0]["State"]["Name"]


def _seed_server(server_id, name="server-1", running_build_job_id=None,
                 lifecycle_state="running"):
    instance_id = _launch_instance()
    item = {
        "server_id": server_id,
        "name": name,
        "instance_id": instance_id,
        "lifecycle_state": lifecycle_state,
        "cpu_architecture": build_domain.ARCH_ARM64,
    }
    if running_build_job_id is not None:
        item["running_build_job_id"] = running_build_job_id
    _SERVERS.put_item(Item=item)
    return instance_id


def _seed_job(job_id, status, execution_mode, server_id=None,
              build_target=build_domain.TARGET_JP5, created_at=NOW,
              **extra):
    item = {
        "build_job_id": job_id,
        "build_target": build_target,
        "execution_mode": execution_mode,
        "status": status,
        "requested_by": "operator-1",
        "created_at": created_at,
    }
    if server_id is not None:
        item["server_id"] = server_id
    item.update(extra)
    _JOBS.put_item(Item=item)
    return item


def _runner_instances_for_job(job_id):
    """Instances tagged dda-build:job-id = job_id (any state)."""
    response = _EC2.describe_instances(Filters=[
        {"Name": f"tag:{build_dispatcher.TAG_JOB_ID}", "Values": [job_id]},
    ])
    return [
        instance
        for reservation in response.get("Reservations", [])
        for instance in reservation.get("Instances", [])
    ]


def _audits(action):
    return [entry for entry in AUDIT_EVENTS if entry["action"] == action]


def _setup():
    _clear_tables()
    del AUDIT_EVENTS[:]


_CLEAN_PGREP = ""  # no build process found (Req 7.5 clean verification)
_BUSY_PGREP = "12345 /bin/sh -c gdk component build\n"


# ---------------------------------------------------------------------------
# Dedicated dispatch with the allocation lock (Req 7.1, 7.2, 7.5)
# ---------------------------------------------------------------------------

class TestDedicatedDispatch:

    def setup_method(self):
        _setup()

    def test_dispatch_allocates_lock_and_starts_agent(self):
        """The eligible queued dedicated job takes the server's single
        slot, passes the clean pre-dispatch verification, transitions
        queued -> building, and the agent SendCommand goes to the
        server's instance (Req 7.1, 7.5). A later job targeting the same
        server stays queued (Req 7.2)."""
        instance_id = _seed_server("srv-1", name="arm-server-1")
        _seed_job("job-a", build_domain.STATUS_QUEUED,
                  build_domain.EXECUTION_MODE_DEDICATED, server_id="srv-1",
                  created_at=NOW - _MINUTE_MS)
        _seed_job("job-b", build_domain.STATUS_QUEUED,
                  build_domain.EXECUTION_MODE_DEDICATED, server_id="srv-1",
                  created_at=NOW)

        with mock.patch.object(build_dispatcher, "run_shell_sync",
                               return_value=_CLEAN_PGREP) as verify:
            build_dispatcher.run_tick(now=NOW)

        # The allocation lock is held by exactly the dispatched job.
        assert _get_server("srv-1")["running_build_job_id"] == "job-a"

        job_a = _get_job("job-a")
        assert job_a["status"] == build_domain.STATUS_BUILDING
        assert job_a["dispatched_at"] == NOW
        assert job_a["started_at"] == NOW

        # Pre-dispatch pgrep verification ran against the server's
        # instance before the start (Req 7.5).
        verify.assert_called_once_with(
            instance_id, build_dispatcher.VERIFY_BUILD_PROCESS_COMMANDS)

        # The agent SendCommand is a real (moto) SSM command against the
        # server's instance, recorded on the job with its log stream.
        command_id = job_a["ssm"]["command_id"]
        assert job_a["log"]["group"] == build_dispatcher.BUILD_LOG_GROUP
        assert job_a["log"]["stream"] == \
            f"{command_id}/{instance_id}/aws-runShellScript/stdout"
        commands = _SSM.list_commands(CommandId=command_id)["Commands"]
        assert len(commands) == 1
        assert commands[0]["DocumentName"] == "AWS-RunShellScript"
        agent_line = commands[0]["Parameters"]["commands"][0]
        assert "portal-build-agent.sh" in agent_line
        assert "BUILD_JOB_ID=job-a" in agent_line
        assert f"BUILD_TARGET={build_domain.TARGET_JP5}" in agent_line

        # The second job for the occupied server stays queued (Req 7.2).
        job_b = _get_job("job-b")
        assert job_b["status"] == build_domain.STATUS_QUEUED
        assert "ssm" not in job_b

    def test_occupied_server_keeps_new_job_queued(self):
        """A job dispatched to a server already running a Build_Job goes
        to that server's queue instead of starting (Req 7.2)."""
        _seed_server("srv-1", running_build_job_id="job-running")
        _seed_job("job-running", build_domain.STATUS_BUILDING,
                  build_domain.EXECUTION_MODE_DEDICATED, server_id="srv-1",
                  created_at=NOW - _MINUTE_MS, started_at=NOW - _MINUTE_MS)
        _seed_job("job-c", build_domain.STATUS_QUEUED,
                  build_domain.EXECUTION_MODE_DEDICATED, server_id="srv-1")

        with mock.patch.object(build_dispatcher, "run_shell_sync",
                               return_value=_CLEAN_PGREP):
            build_dispatcher.run_tick(now=NOW)

        assert _get_server("srv-1")["running_build_job_id"] == "job-running"
        job_c = _get_job("job-c")
        assert job_c["status"] == build_domain.STATUS_QUEUED
        assert "ssm" not in job_c

    def test_allocation_lock_is_a_conditional_update(self):
        """The DynamoDB conditional update on
        attribute_not_exists(running_build_job_id) is the authoritative
        serialization lock: a second allocation attempt is rejected until
        the holder releases (Req 7.1, 7.2)."""
        _seed_server("srv-1")
        assert build_dispatcher.allocate_server("srv-1", "job-1") is True
        assert build_dispatcher.allocate_server("srv-1", "job-2") is False
        assert _get_server("srv-1")["running_build_job_id"] == "job-1"
        # A stale release by a non-holder never frees the slot.
        assert build_dispatcher.release_server("srv-1", "job-2") is False
        assert build_dispatcher.release_server("srv-1", "job-1") is True
        assert build_dispatcher.allocate_server("srv-1", "job-2") is True


# ---------------------------------------------------------------------------
# Pre-dispatch pgrep verification defers on a busy server (Req 7.5, 7.6)
# ---------------------------------------------------------------------------

class TestPredispatchVerification:

    def setup_method(self):
        _setup()

    def test_busy_server_defers_then_dispatches_after_interval(self):
        """A detected build process defers the job (queued, original
        created_at retained, deferred_at recorded, allocation kept); no
        re-verification happens before the 5-minute interval; a clean
        re-verification after the interval starts the build (Req 7.5,
        7.6)."""
        created_at = NOW - 2 * _MINUTE_MS
        _seed_server("srv-1")
        _seed_job("job-d", build_domain.STATUS_QUEUED,
                  build_domain.EXECUTION_MODE_DEDICATED, server_id="srv-1",
                  created_at=created_at)

        # Tick 1: a build process is running on the server -> defer.
        with mock.patch.object(build_dispatcher, "run_shell_sync",
                               return_value=_BUSY_PGREP) as verify:
            build_dispatcher.run_tick(now=NOW)
        assert verify.call_count == 1

        job = _get_job("job-d")
        assert job["status"] == build_domain.STATUS_QUEUED
        assert job["created_at"] == created_at, \
            "a deferral must retain the ORIGINAL submission time"
        assert job["deferred_at"] == NOW
        assert "ssm" not in job
        # The allocation is kept so no other job slips onto the server.
        assert _get_server("srv-1")["running_build_job_id"] == "job-d"

        # Tick 2, one minute later: within the 5-minute retry interval,
        # no re-verification is attempted (Req 7.6).
        with mock.patch.object(build_dispatcher, "run_shell_sync",
                               return_value=_CLEAN_PGREP) as verify:
            build_dispatcher.run_tick(now=NOW + _MINUTE_MS)
        assert verify.call_count == 0
        assert _get_job("job-d")["status"] == build_domain.STATUS_QUEUED

        # Tick 3, past the retry interval: re-verified clean -> building.
        later = NOW + build_planner.PREDISPATCH_RETRY_INTERVAL_MS
        with mock.patch.object(build_dispatcher, "run_shell_sync",
                               return_value=_CLEAN_PGREP) as verify:
            build_dispatcher.run_tick(now=later)
        assert verify.call_count == 1

        job = _get_job("job-d")
        assert job["status"] == build_domain.STATUS_BUILDING
        assert job["ssm"]["command_id"]
        assert _get_server("srv-1")["running_build_job_id"] == "job-d"


# ---------------------------------------------------------------------------
# Ephemeral provisioning, agent start, and runner termination (Req 3.1)
# ---------------------------------------------------------------------------

class TestEphemeralProvisionAndTerminate:

    def setup_method(self):
        _setup()

    def test_provision_from_snapshot_start_agent_then_terminate(self):
        """Tick 1 provisions exactly one runner sized from the job's own
        config_snapshot (Req 3.1, 9.3); tick 2 starts the agent once the
        runner is SSM-managed; a tick after the job is terminal
        terminates the runner."""
        _seed_job("job-eph", build_domain.STATUS_QUEUED,
                  build_domain.EXECUTION_MODE_EPHEMERAL,
                  build_target=build_domain.TARGET_JP5,
                  config_snapshot={
                      "arm64_instance_type": "m6g.2xlarge",
                      "volume_size_gb": 120,
                      "max_runtime_hours": 4,
                  })

        # Tick 1: queued -> provisioning + RunInstances (Req 3.1).
        build_dispatcher.run_tick(now=NOW)

        job = _get_job("job-eph")
        assert job["status"] == build_domain.STATUS_PROVISIONING
        assert job["dispatched_at"] == NOW
        runner_instance_id = job["runner"]["instance_id"]
        assert job["runner"]["instance_type"] == "m6g.2xlarge"
        assert job["runner"]["arch"] == build_domain.ARCH_ARM64

        instances = _runner_instances_for_job("job-eph")
        assert len(instances) == 1, \
            "exactly one Ephemeral_Build_Runner per dispatched job"
        instance = instances[0]
        assert instance["InstanceId"] == runner_instance_id
        # Sizing comes from the job's OWN config_snapshot (Req 3.1, 9.3).
        assert instance["InstanceType"] == "m6g.2xlarge"
        tags = {tag["Key"]: tag["Value"] for tag in instance["Tags"]}
        assert tags[build_dispatcher.TAG_EPHEMERAL] == "true"
        assert tags[build_dispatcher.TAG_JOB_ID] == "job-eph"

        # Tick 2: the runner pings SSM Online -> agent SendCommand.
        with mock.patch.object(build_dispatcher, "instance_ssm_online",
                               return_value=True):
            build_dispatcher.run_tick(now=NOW + _MINUTE_MS)

        job = _get_job("job-eph")
        command_id = job["ssm"]["command_id"]
        assert job["log"]["stream"] == \
            f"{command_id}/{runner_instance_id}/aws-runShellScript/stdout"
        commands = _SSM.list_commands(CommandId=command_id)["Commands"]
        assert "BUILD_JOB_ID=job-eph" in \
            commands[0]["Parameters"]["commands"][0]

        # Tick 3: agent already dispatched -> no second SendCommand.
        with mock.patch.object(build_dispatcher, "instance_ssm_online",
                               return_value=True), \
                mock.patch.object(build_dispatcher, "send_agent") as send:
            build_dispatcher.run_tick(now=NOW + 2 * _MINUTE_MS)
        send.assert_not_called()

        # The job reaches a terminal status (event consumer's job) ->
        # the termination watchdog terminates the runner.
        _JOBS.update_item(
            Key={"build_job_id": "job-eph"},
            UpdateExpression="SET #s = :s",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":s": build_domain.STATUS_SUCCEEDED},
        )
        build_dispatcher.run_tick(now=NOW + 3 * _MINUTE_MS)

        job = _get_job("job-eph")
        assert job["runner"]["terminated_at"] == NOW + 3 * _MINUTE_MS
        assert _instance_state(runner_instance_id) in \
            ("shutting-down", "terminated")


# ---------------------------------------------------------------------------
# Provisioning failure path (Req 3.7)
# ---------------------------------------------------------------------------

class TestProvisioningFailure:

    def setup_method(self):
        _setup()

    def test_runinstances_failure_fails_job_and_cleans_partial_compute(self):
        """A RunInstances failure marks the job failed with the
        provisioning cause, terminates partially provisioned compute,
        and records an Audit_Log entry (Req 3.7)."""
        _seed_job("job-fail", build_domain.STATUS_QUEUED,
                  build_domain.EXECUTION_MODE_EPHEMERAL,
                  build_target=build_domain.TARGET_AMD64)

        # Partially provisioned compute already tagged for the job.
        partial = _EC2.run_instances(
            ImageId=_AMI_ID, InstanceType="m6i.4xlarge",
            MinCount=1, MaxCount=1,
            TagSpecifications=[{
                "ResourceType": "instance",
                "Tags": [{"Key": build_dispatcher.TAG_JOB_ID,
                          "Value": "job-fail"}],
            }])["Instances"][0]["InstanceId"]

        error = ClientError(
            {"Error": {"Code": "InsufficientInstanceCapacity",
                       "Message": "no capacity for m6i.4xlarge"}},
            "RunInstances")
        with mock.patch.object(build_dispatcher.ec2, "run_instances",
                               create=True, side_effect=error):
            build_dispatcher.run_tick(now=NOW)

        job = _get_job("job-fail")
        assert job["status"] == build_domain.STATUS_FAILED
        assert job["error"]["code"] == build_dispatcher.ERROR_PROVISIONING_FAILED
        assert "InsufficientInstanceCapacity" in job["error"]["message"]
        assert job["ended_at"]

        # Partially provisioned compute is terminated (Req 3.7).
        assert _instance_state(partial) in ("shutting-down", "terminated")

        audits = _audits("build_provisioning_failed")
        assert len(audits) == 1
        entry = audits[0]
        assert entry["resource_id"] == "job-fail"
        assert entry["result"] == "failure"
        assert entry["details"]["error_code"] == \
            build_dispatcher.ERROR_PROVISIONING_FAILED
        assert partial in entry["details"]["terminated_partial_compute"]


# ---------------------------------------------------------------------------
# Runtime timeout watchdog (Req 3.8)
# ---------------------------------------------------------------------------

class TestRuntimeTimeoutWatchdog:

    def setup_method(self):
        _setup()

    def test_timeout_stops_build_fails_job_and_releases_server(self):
        """A building job past its config_snapshot max runtime gets the
        SSM stop commands, is failed with a timeout error, keeps its logs
        reference, and its dedicated server allocation is released
        (Req 3.8)."""
        instance_id = _seed_server("srv-t", running_build_job_id="job-slow")
        log = {"group": "/dda/portal-builds", "stream": "cmd/inst/stdout"}
        _seed_job("job-slow", build_domain.STATUS_BUILDING,
                  build_domain.EXECUTION_MODE_DEDICATED, server_id="srv-t",
                  started_at=NOW - 2 * _HOUR_MS,
                  config_snapshot={"max_runtime_hours": 1},
                  log=log,
                  # Serialization check recently done: only the timeout
                  # watchdog acts on this tick.
                  ssm={"command_id": "cmd-0",
                       "last_serialization_check_at": NOW})

        with mock.patch.object(build_dispatcher,
                               "send_shell_command") as send:
            build_dispatcher.run_tick(now=NOW)

        # The build processes were stopped on the job's instance.
        send.assert_called_once_with(
            instance_id, build_dispatcher.STOP_BUILD_COMMANDS)

        job = _get_job("job-slow")
        assert job["status"] == build_domain.STATUS_FAILED
        assert job["error"]["code"] == build_dispatcher.ERROR_TIMEOUT
        assert "1 hours" in job["error"]["message"]
        assert job["ended_at"]
        # Logs produced up to termination are retained (Req 3.8).
        assert job["log"] == log

        # The dedicated server's slot is released for promotion.
        assert "running_build_job_id" not in (_get_server("srv-t") or {})

        audits = _audits("build_timeout")
        assert len(audits) == 1
        assert audits[0]["resource_id"] == "job-slow"
        assert audits[0]["result"] == "failure"

    def test_job_within_runtime_limit_is_untouched(self):
        """A running job inside its max runtime is not failed."""
        _seed_server("srv-t", running_build_job_id="job-ok")
        _seed_job("job-ok", build_domain.STATUS_BUILDING,
                  build_domain.EXECUTION_MODE_DEDICATED, server_id="srv-t",
                  started_at=NOW - 30 * _MINUTE_MS,
                  config_snapshot={"max_runtime_hours": 1},
                  ssm={"command_id": "cmd-0",
                       "last_serialization_check_at": NOW})

        with mock.patch.object(build_dispatcher, "send_shell_command") as send:
            build_dispatcher.run_tick(now=NOW)

        send.assert_not_called()
        assert _get_job("job-ok")["status"] == build_domain.STATUS_BUILDING


# ---------------------------------------------------------------------------
# Serialization watchdog (Req 7.8)
# ---------------------------------------------------------------------------

class TestSerializationWatchdog:

    def setup_method(self):
        _setup()

    def test_concurrent_builds_stop_all_and_fail_jobs(self):
        """A detected build-process count >= 2 stops every build process
        within the 60-second window, fails the associated Build_Job with
        SERIALIZATION_VIOLATION, audits the violation, and releases the
        server allocation (Req 7.8)."""
        instance_id = _seed_server("srv-s", running_build_job_id="job-v")
        _seed_job("job-v", build_domain.STATUS_BUILDING,
                  build_domain.EXECUTION_MODE_DEDICATED, server_id="srv-s",
                  started_at=NOW - 10 * _MINUTE_MS,
                  config_snapshot={"max_runtime_hours": 4})

        count_output = "GDK_BUILD_COUNT=2\nCUSTOM_BUILD_COUNT=2\n"
        with mock.patch.object(build_dispatcher, "run_shell_sync",
                               return_value=count_output) as count, \
                mock.patch.object(build_dispatcher,
                                  "send_shell_command") as send:
            build_dispatcher.run_tick(now=NOW)

        # The pgrep count ran against the server's instance.
        count.assert_called_once_with(
            instance_id, build_dispatcher.COUNT_BUILD_PROCESS_COMMANDS)
        # Every build process is stopped within the 60 s window.
        send.assert_called_once_with(
            instance_id, build_dispatcher.STOP_BUILD_COMMANDS,
            execution_timeout=(
                build_planner.SERIALIZATION_STOP_WINDOW_SECONDS))

        job = _get_job("job-v")
        assert job["status"] == build_domain.STATUS_FAILED
        assert job["error"]["code"] == \
            build_planner.SERIALIZATION_VIOLATION_ERROR
        assert job["ssm"]["last_serialization_check_at"] == NOW
        assert "running_build_job_id" not in (_get_server("srv-s") or {})

        audits = _audits("build_serialization_violation")
        assert len(audits) == 1
        entry = audits[0]
        assert entry["resource_id"] == "job-v"
        assert entry["result"] == "failure"
        assert entry["details"]["process_count"] == 2
        assert entry["details"]["instance_id"] == instance_id

    def test_single_build_process_is_no_violation(self):
        """A count of one build process (gdk + its build-custom.sh child)
        is healthy: no stop, no failure (Req 7.8 boundary)."""
        instance_id = _seed_server("srv-s", running_build_job_id="job-h")
        _seed_job("job-h", build_domain.STATUS_BUILDING,
                  build_domain.EXECUTION_MODE_DEDICATED, server_id="srv-s",
                  started_at=NOW - 10 * _MINUTE_MS,
                  config_snapshot={"max_runtime_hours": 4})

        count_output = "GDK_BUILD_COUNT=1\nCUSTOM_BUILD_COUNT=1\n"
        with mock.patch.object(build_dispatcher, "run_shell_sync",
                               return_value=count_output), \
                mock.patch.object(build_dispatcher,
                                  "send_shell_command") as send:
            build_dispatcher.run_tick(now=NOW)

        send.assert_not_called()
        job = _get_job("job-h")
        assert job["status"] == build_domain.STATUS_BUILDING
        assert job["ssm"]["last_serialization_check_at"] == NOW
        assert _get_server("srv-s")["running_build_job_id"] == "job-h"
