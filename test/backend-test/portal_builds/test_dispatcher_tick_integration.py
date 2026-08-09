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
  (Req 3.1, 9.3), agent SendCommand once the runner is SSM-managed AND
  its Bootstrap_Marker has been observed (Req 6.1, 6.2, 6.4), the agent
  invoked from the repository directory the provisioning pass recorded
  (Req 5.1, 5.4) with the Source_Sync preamble and ``SOURCE_REF`` when a
  ref is selected (Req 4.1, 4.2), and runner termination once the job is
  terminal.
- Dedicated readiness sequence (build-source-selection task 6):
  allocation lock -> clean pgrep verification -> bootstrap readiness
  policy -> agent command against the directory the Build_Server record
  carries. Inside its bootstrap budget from launch a server with no
  marker defers the job; past that budget (and for every server that
  records no launch time) no probe is issued and an advisory note is
  recorded instead (Req 5.3, 6.2, 6.4, 7.1).
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
import build_reconciliation  # noqa: E402
import build_source  # noqa: E402
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
                 lifecycle_state="running", **extra):
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
    item.update(extra)
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

# Bootstrap_Marker probe outputs (Req 6.2). Built from the build_planner
# constants, the single definition of the probe keys and the log path, so
# the fixture can never drift from the probe the dispatcher actually sends.
_MARKER_ABSENT = (
    f"{build_planner.BOOTSTRAP_DONE_PROBE_KEY}=0\n"
    f"{build_planner.BOOTSTRAP_LOG_PROBE_KEY}="
    f"{build_planner.BOOTSTRAP_LOG_PATH}\n"
)
_MARKER_OBSERVED = (
    f"{build_planner.BOOTSTRAP_DONE_PROBE_KEY}=1\n"
    f"{build_planner.BOOTSTRAP_LOG_PROBE_KEY}="
    f"{build_planner.BOOTSTRAP_LOG_PATH}\n"
)


def _shell_sync_router(marker_output, pgrep_output=_CLEAN_PGREP):
    """``run_shell_sync`` side effect routing on the command list.

    The dispatcher runs the pgrep verification and the Bootstrap_Marker
    probe through the same helper, so a tick that exercises both needs the
    stub to answer per command set rather than per call order. Routing on
    ``build_dispatcher.BOOTSTRAP_PROBE_COMMANDS`` also asserts, implicitly,
    that the readiness probe is issued with exactly those commands.
    """
    def _run(instance_id, commands, **kwargs):
        if commands == build_dispatcher.BOOTSTRAP_PROBE_COMMANDS:
            return marker_output
        return pgrep_output
    return _run


def _agent_command_text(command_id):
    """The single AWS-RunShellScript command text of an SSM command."""
    commands = _SSM.list_commands(CommandId=command_id)["Commands"]
    assert len(commands) == 1
    return commands[0]["Parameters"]["commands"][0]


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
# Dedicated end-to-end sequence with the bootstrap readiness policy
# (Req 5.1, 5.2, 5.3, 6.2, 6.4, 7.5)
# ---------------------------------------------------------------------------

class TestDedicatedReadinessSequence:

    def setup_method(self):
        _setup()

    def test_allocation_pgrep_readiness_then_agent_on_recorded_dir(self):
        """The dedicated sequence end to end: allocation lock -> clean
        pgrep verification -> bootstrap readiness policy -> agent command
        against the repository directory the SERVER RECORD carries.

        A server still inside its bootstrap budget from launch with no
        Bootstrap_Marker observed is DEFERRED, never failed (the dedicated
        policy is advisory, Req 5.3, 7.1); once the marker is observed the
        job starts and the readiness evidence is recorded on the job
        (Req 6.4). The agent path is rooted in the server's recorded
        directory, so it cannot drift from that server's bootstrap clone
        (Req 5.1, 5.2, 5.4)."""
        recorded_repo_dir = "/opt/dda-build/DefectDetectionApplication"
        assert recorded_repo_dir != build_source.DEFAULT_REPO_DIR, \
            "the fixture must differ from the default to be meaningful"
        instance_id = _seed_server(
            "srv-r", name="arm-server-ready", repo_dir=recorded_repo_dir,
            # Launched a minute ago: inside the bootstrap budget, which is
            # the only window where the marker is required.
            created_at=NOW - _MINUTE_MS)
        _seed_job("job-r", build_domain.STATUS_QUEUED,
                  build_domain.EXECUTION_MODE_DEDICATED, server_id="srv-r",
                  created_at=NOW - 2 * _MINUTE_MS,
                  config_snapshot={"max_runtime_hours": 4})

        # Tick 1: allocation taken, pgrep clean, marker NOT observed ->
        # the job is deferred at the head of its queue, still queued, and
        # the allocation is retained. No agent command is sent.
        with mock.patch.object(
                build_dispatcher, "run_shell_sync",
                side_effect=_shell_sync_router(_MARKER_ABSENT)) as shell:
            build_dispatcher.run_tick(now=NOW)

        assert _get_server("srv-r")["running_build_job_id"] == "job-r"
        # Both the pgrep verification and the marker probe ran, in that
        # order, against the server's instance (Req 6.2, 7.5).
        assert [call.args for call in shell.call_args_list] == [
            (instance_id, build_dispatcher.VERIFY_BUILD_PROCESS_COMMANDS),
            (instance_id, build_dispatcher.BOOTSTRAP_PROBE_COMMANDS),
        ]
        job = _get_job("job-r")
        assert job["status"] == build_domain.STATUS_QUEUED
        assert job["deferred_at"] == NOW
        assert "ssm" not in job
        assert "bootstrap" not in job

        # Tick 2, past the re-verification interval: marker observed ->
        # queued -> building, readiness evidence recorded, agent started.
        later = NOW + build_planner.PREDISPATCH_RETRY_INTERVAL_MS
        with mock.patch.object(
                build_dispatcher, "run_shell_sync",
                side_effect=_shell_sync_router(_MARKER_OBSERVED)):
            build_dispatcher.run_tick(now=later)

        job = _get_job("job-r")
        assert job["status"] == build_domain.STATUS_BUILDING
        assert job["dispatched_at"] == later
        assert job["started_at"] == later
        assert job["created_at"] == NOW - 2 * _MINUTE_MS, \
            "a deferral must retain the ORIGINAL submission time"
        assert job["bootstrap"]["marker_at"] == later
        assert job["bootstrap"]["log_path"] == \
            build_planner.BOOTSTRAP_LOG_PATH

        command_id = job["ssm"]["command_id"]
        assert job["log"]["group"] == build_dispatcher.BUILD_LOG_GROUP
        assert job["log"]["stream"] == \
            f"{command_id}/{instance_id}/aws-runShellScript/stdout"
        agent_text = _agent_command_text(command_id)
        # The agent runs from the directory THIS server recorded, not the
        # module default (Req 5.1, 5.2, 5.4).
        assert build_source.agent_script_path(recorded_repo_dir) in agent_text
        assert build_source.DEFAULT_REPO_DIR not in agent_text
        assert "BUILD_JOB_ID=job-r" in agent_text
        assert f"BUILD_TARGET={build_domain.TARGET_JP5}" in agent_text
        assert agent_text == build_dispatcher.agent_command(
            job, recorded_repo_dir)
        # DEDICATED dispatch runs the build as ubuntu (the servers are
        # groomed for the ubuntu user): the root-context ownership heal
        # precedes the sudo build-user execution, and the wrapped body's
        # exit status is propagated as the SSM command status.
        assert build_dispatcher.repo_ownership_heal_command(
            recorded_repo_dir) in agent_text
        assert (f"sudo -H -u {build_dispatcher.BUILD_USER} bash"
                in agent_text)
        assert build_dispatcher.agent_run_body(
            job, recorded_repo_dir) in agent_text
        assert agent_text.splitlines()[-1] == \
            f'exit "${build_dispatcher.RUN_STATUS_VAR}"'
        assert _get_server("srv-r")["running_build_job_id"] == "job-r"

    def test_server_past_bootstrap_budget_needs_no_marker(self):
        """A Dedicated_Build_Server past its bootstrap budget from launch
        (and every server that records no launch time, i.e. every server
        registered before this change) requires NO marker: the dispatch
        proceeds with an advisory note and no probe round trip at all
        (Req 5.3, 7.1)."""
        instance_id = _seed_server(
            "srv-old", name="arm-server-old",
            created_at=NOW - 2 * build_planner.DEFAULT_BOOTSTRAP_TIMEOUT_MINUTES
            * _MINUTE_MS)
        _seed_job("job-old", build_domain.STATUS_QUEUED,
                  build_domain.EXECUTION_MODE_DEDICATED, server_id="srv-old",
                  created_at=NOW - _MINUTE_MS)

        with mock.patch.object(
                build_dispatcher, "run_shell_sync",
                side_effect=_shell_sync_router(_MARKER_ABSENT)) as shell:
            build_dispatcher.run_tick(now=NOW)

        # Only the pgrep verification ran: no marker probe is issued
        # outside the bootstrap window.
        assert [call.args for call in shell.call_args_list] == [
            (instance_id, build_dispatcher.VERIFY_BUILD_PROCESS_COMMANDS),
        ]
        job = _get_job("job-old")
        assert job["status"] == build_domain.STATUS_BUILDING
        assert job["bootstrap"]["marker_observed"] is False
        assert job["bootstrap"]["advisory"]
        # With no recorded directory the resolution is the authoritative
        # default every pre-existing server already uses (Req 5.3).
        agent_text = _agent_command_text(job["ssm"]["command_id"])
        assert build_source.agent_script_path(
            build_source.DEFAULT_REPO_DIR) in agent_text


# ---------------------------------------------------------------------------
# Ephemeral provisioning, agent start, and runner termination (Req 3.1)
# ---------------------------------------------------------------------------

class TestEphemeralProvisionAndTerminate:

    def setup_method(self):
        _setup()

    def test_provision_from_snapshot_start_agent_then_terminate(self):
        """Tick 1 provisions exactly one runner sized from the job's own
        config_snapshot (Req 3.1, 9.3); tick 2 finds the runner
        SSM-managed but its bootstrap not yet signalled, so NO agent
        command is sent (Req 6.1, 6.2); tick 3 observes the
        Bootstrap_Marker and starts the agent against the repository
        directory the runner's own provisioning recorded (Req 5.1, 5.4,
        6.4); a tick after the job is terminal terminates the runner."""
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
        # The provisioning pass records the directory its bootstrap uses,
        # so the agent path can never drift from it (Req 5.1, 5.4).
        recorded_repo_dir = job["runner"]["repo_dir"]
        assert recorded_repo_dir == build_dispatcher.BUILD_REPO_DIR
        assert recorded_repo_dir == build_source.DEFAULT_REPO_DIR

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

        # Tick 2: the runner pings SSM Online, but its bootstrap has NOT
        # signalled completion yet -> the gate stays shut (Req 6.1, 6.2).
        # SSM Online is reached long before cloud-init finishes, which is
        # exactly the live race this gate closes.
        with mock.patch.object(build_dispatcher, "instance_ssm_online",
                               return_value=True), \
                mock.patch.object(
                    build_dispatcher, "run_shell_sync",
                    side_effect=_shell_sync_router(_MARKER_ABSENT)) as probe:
            build_dispatcher.run_tick(now=NOW + _MINUTE_MS)

        probe.assert_called_once_with(
            runner_instance_id, build_dispatcher.BOOTSTRAP_PROBE_COMMANDS)
        job = _get_job("job-eph")
        assert job["status"] == build_domain.STATUS_PROVISIONING
        assert "ssm" not in job, \
            "no agent command may precede an observed Bootstrap_Marker"
        assert "bootstrap" not in job

        # Tick 3: the Bootstrap_Marker is observed -> readiness evidence is
        # recorded and the agent SendCommand goes out (Req 6.2, 6.4).
        with mock.patch.object(build_dispatcher, "instance_ssm_online",
                               return_value=True), \
                mock.patch.object(
                    build_dispatcher, "run_shell_sync",
                    side_effect=_shell_sync_router(_MARKER_OBSERVED)):
            build_dispatcher.run_tick(now=NOW + 2 * _MINUTE_MS)

        job = _get_job("job-eph")
        assert job["bootstrap"]["marker_at"] == NOW + 2 * _MINUTE_MS
        assert job["bootstrap"]["log_path"] == \
            build_planner.BOOTSTRAP_LOG_PATH
        command_id = job["ssm"]["command_id"]
        assert job["log"]["stream"] == \
            f"{command_id}/{runner_instance_id}/aws-runShellScript/stdout"
        agent_text = _agent_command_text(command_id)
        assert "BUILD_JOB_ID=job-eph" in agent_text
        # The agent is invoked from the directory the runner's own
        # provisioning recorded, not an independent literal (Req 5.1, 5.4).
        assert build_source.agent_script_path(recorded_repo_dir) in agent_text
        # No ref selected -> no Source_Sync preamble; only the additive
        # environment exports (region + PATH) precede the invocation
        # (Req 7.1).
        assert agent_text == build_dispatcher.agent_command(
            job, recorded_repo_dir)
        assert "SOURCE_REF=" not in agent_text

        # Tick 4: agent already dispatched -> no second SendCommand.
        with mock.patch.object(build_dispatcher, "instance_ssm_online",
                               return_value=True), \
                mock.patch.object(build_dispatcher, "send_agent") as send:
            build_dispatcher.run_tick(now=NOW + 3 * _MINUTE_MS)
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
        build_dispatcher.run_tick(now=NOW + 4 * _MINUTE_MS)

        job = _get_job("job-eph")
        assert job["runner"]["terminated_at"] == NOW + 4 * _MINUTE_MS
        assert _instance_state(runner_instance_id) in \
            ("shutting-down", "terminated")

    def test_ephemeral_agent_command_carries_selected_repository_and_ref(self):
        """The ephemeral end-to-end sequence for a job WITH a selected
        source: queued -> provisioning -> Bootstrap_Marker observed ->
        agent SendCommand carrying the resolved repository directory, the
        Source_Sync preamble for the selected repository, and SOURCE_REF
        (Req 4.1, 4.2, 5.1, 6.2)."""
        source_ref = "feature/portal-build-fleet-and-workflow-gates"
        _seed_job("job-ref", build_domain.STATUS_QUEUED,
                  build_domain.EXECUTION_MODE_EPHEMERAL,
                  build_target=build_domain.TARGET_JP5,
                  config_snapshot={
                      "arm64_instance_type": "m6g.2xlarge",
                      "volume_size_gb": 120,
                      "max_runtime_hours": 4,
                      "source_ref": source_ref,
                  })

        build_dispatcher.run_tick(now=NOW)
        job = _get_job("job-ref")
        assert job["status"] == build_domain.STATUS_PROVISIONING
        runner_instance_id = job["runner"]["instance_id"]
        repo_dir = job["runner"]["repo_dir"]

        with mock.patch.object(build_dispatcher, "instance_ssm_online",
                               return_value=True), \
                mock.patch.object(
                    build_dispatcher, "run_shell_sync",
                    side_effect=_shell_sync_router(_MARKER_OBSERVED)):
            build_dispatcher.run_tick(now=NOW + _MINUTE_MS)

        job = _get_job("job-ref")
        agent_text = _agent_command_text(job["ssm"]["command_id"])

        # EPHEMERAL dispatch uses the same run-as-ubuntu model as the
        # dedicated fleet (ONE environment model): the root-context
        # ownership heal precedes the sudo build-user execution of the
        # exports + sync preamble + agent invocation body, and the
        # wrapped body's exit status is propagated as the SSM command
        # status.
        body = build_dispatcher.agent_run_body(job, repo_dir)
        assert body in agent_text
        assert build_dispatcher.repo_ownership_heal_command(
            repo_dir) in agent_text
        assert (f"sudo -H -u {build_dispatcher.BUILD_USER} bash"
                in agent_text)
        assert agent_text.index(
            build_dispatcher.repo_ownership_heal_command(repo_dir)) < \
            agent_text.index(body)
        assert agent_text.splitlines()[-1] == \
            f'exit "${build_dispatcher.RUN_STATUS_VAR}"'
        lines = body.split("\n")

        # The agent invocation is the LAST line, rooted in the resolved
        # repository directory and carrying the selected ref (Req 5.1,
        # 5.4).
        assert build_source.agent_script_path(repo_dir) in lines[-1]
        assert "BUILD_JOB_ID=job-ref" in lines[-1]
        assert f"SOURCE_REF={source_ref}" in lines[-1]

        # Everything before it is exactly the additive environment exports
        # (HOME: the SSM shell runs as root with HOME unset; region: a
        # fresh runner has no region of its own and AWS CLI v1 does not
        # infer one from instance metadata; PATH: pip3 --user installs
        # land in $HOME/.local/bin, which a non-login shell omits)
        # followed by the Source_Sync command text from its single
        # generator, so the runner obtains the selected source BEFORE the
        # agent runs (Req 4.1, 4.2, 4.3).
        preamble = lines[:-1]
        assert preamble, "a selected ref must produce a sync preamble"
        assert preamble == (
            build_dispatcher.runner_env_export_commands(job)
            + build_dispatcher.agent_preamble_commands(job, repo_dir)
            # The additive dispatch preflight guard (task 7.1) runs
            # AFTER the sync and BEFORE the agent, targeting the SAME
            # resolved repository directory (no drift).
            + build_dispatcher.preflight_guard_commands(repo_dir))
        checkout_index = next(
            index for index, line in enumerate(lines)
            if "git checkout --force" in line)
        assert checkout_index < len(lines) - 1, \
            "the source sync must precede the agent invocation"
        assert f"{build_source.VAR_SOURCE_REF}={source_ref}" in agent_text

        assert agent_text == build_dispatcher.agent_command(job, repo_dir)
        assert job["log"]["stream"].endswith(
            f"/{runner_instance_id}/aws-runShellScript/stdout")


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
        (Req 3.8) — under the task 6.2 verified-cleanup contract: the
        terminal write records cleanup PENDING plus the complete timing
        diagnostic, the stop is sent idempotently, and the release
        happens only once pgrep confirms no build process remains."""
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

        zero = "GDK_BUILD_COUNT=0\nCUSTOM_BUILD_COUNT=0\n"
        with mock.patch.object(build_dispatcher,
                               "send_shell_command") as send, \
                mock.patch.object(build_dispatcher, "run_shell_sync",
                                  return_value=zero) as confirm:
            build_dispatcher.run_tick(now=NOW)

        # The build processes were stopped on the job's instance, and
        # their absence was pgrep-confirmed before the release.
        send.assert_called_once_with(
            instance_id, build_dispatcher.STOP_BUILD_COMMANDS)
        confirm.assert_called_once_with(
            instance_id, build_dispatcher.COUNT_BUILD_PROCESS_COMMANDS)

        job = _get_job("job-slow")
        assert job["status"] == build_domain.STATUS_FAILED
        assert job["error"]["code"] == build_dispatcher.ERROR_TIMEOUT
        assert "1 hours" in job["error"]["message"]
        assert job["ended_at"]
        # Logs produced up to termination are retained (Req 3.8).
        assert job["log"] == log
        # The complete safe timing diagnostic was persisted with the
        # terminal write (task 8.2 dispatcher wiring, Req 2.18).
        assert job["timing"]["timeout_kind"] == \
            build_reconciliation.CODE_MAX_RUNTIME_EXCEEDED
        assert job["timing"]["timeout_decided_at"] == NOW
        assert job["timeout_evidence"]["timeout_kind"] == \
            build_reconciliation.CODE_MAX_RUNTIME_EXCEEDED
        # Verified stop-before-release on the ledger (Req 3.11).
        effects = job["terminal_effects"]
        assert effects["compute_cleanup"] == \
            build_reconciliation.EFFECT_DONE
        assert effects["allocation_release"] == \
            build_reconciliation.EFFECT_DONE

        # The dedicated server's slot is released for promotion.
        assert "running_build_job_id" not in (_get_server("srv-t") or {})

        audits = _audits("build_timeout")
        assert len(audits) == 1
        assert audits[0]["resource_id"] == "job-slow"
        assert audits[0]["result"] == "failure"

    def test_timeout_release_blocked_until_stop_verified(self):
        """Verified stop before release (task 6.2, Req 3.11): while the
        pgrep confirmation cannot positively complete, the terminal job
        keeps its allocation and no follower can take the slot; the next
        tick re-sends the stop idempotently, confirms, and only then
        releases — with exactly ONE logical audit across the retries."""
        instance_id = _seed_server("srv-t", running_build_job_id="job-slow")
        _seed_job("job-slow", build_domain.STATUS_BUILDING,
                  build_domain.EXECUTION_MODE_DEDICATED, server_id="srv-t",
                  started_at=NOW - 2 * _HOUR_MS,
                  config_snapshot={"max_runtime_hours": 1},
                  ssm={"command_id": "cmd-0",
                       "last_serialization_check_at": NOW})

        # Tick 1: process state UNKNOWN -> failed, stop sent, slot HELD.
        with mock.patch.object(build_dispatcher,
                               "send_shell_command") as send, \
                mock.patch.object(build_dispatcher, "run_shell_sync",
                                  return_value=None):
            build_dispatcher.run_tick(now=NOW)
        send.assert_called_once_with(
            instance_id, build_dispatcher.STOP_BUILD_COMMANDS)
        job = _get_job("job-slow")
        assert job["status"] == build_domain.STATUS_FAILED
        assert job["terminal_effects"]["compute_cleanup"] == \
            build_reconciliation.EFFECT_PENDING
        assert _get_server("srv-t")["running_build_job_id"] == "job-slow"

        # Tick 2: cleanup re-driven, pgrep-confirmed absent -> released.
        zero = "GDK_BUILD_COUNT=0\nCUSTOM_BUILD_COUNT=0\n"
        with mock.patch.object(build_dispatcher,
                               "send_shell_command") as send2, \
                mock.patch.object(build_dispatcher, "run_shell_sync",
                                  return_value=zero):
            build_dispatcher.run_tick(now=NOW + _MINUTE_MS)
        # The stop was re-sent idempotently before the confirmation.
        send2.assert_called_once_with(
            instance_id, build_dispatcher.STOP_BUILD_COMMANDS)
        job = _get_job("job-slow")
        assert job["terminal_effects"]["compute_cleanup"] == \
            build_reconciliation.EFFECT_DONE
        assert job["terminal_effects"]["allocation_release"] == \
            build_reconciliation.EFFECT_DONE
        assert "running_build_job_id" not in (_get_server("srv-t") or {})
        # ONE logical audit despite the retried effects (Req 2.7).
        assert len(_audits("build_timeout")) == 1

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
        zero_output = "GDK_BUILD_COUNT=0\nCUSTOM_BUILD_COUNT=0\n"
        with mock.patch.object(build_dispatcher, "run_shell_sync",
                               side_effect=[count_output,
                                            zero_output]) as count, \
                mock.patch.object(build_dispatcher,
                                  "send_shell_command") as send:
            build_dispatcher.run_tick(now=NOW)

        # The pgrep count ran against the server's instance; after the
        # stop, the cleanup was pgrep-CONFIRMED before the release
        # (task 6.2 verified stop-before-release, Req 3.11).
        assert count.call_args_list == [
            mock.call(instance_id,
                      build_dispatcher.COUNT_BUILD_PROCESS_COMMANDS),
            mock.call(instance_id,
                      build_dispatcher.COUNT_BUILD_PROCESS_COMMANDS),
        ]
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
        assert job["terminal_effects"]["compute_cleanup"] == \
            build_reconciliation.EFFECT_DONE
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


# ---------------------------------------------------------------------------
# ADDITIVE integrated mocked reconciliation flows
# (build-fleet-execution-failures task 10.8).
#
# **Validates: Requirements 2.5, 2.6, 2.7, 2.11, 3.2, 3.4**
#
# These flows exercise the scheduled command reconciliation step of the
# SAME ``run_tick`` used above, end to end over moto DynamoDB/EC2, with
# only ``retrieve_invocation`` scripted per command id (moto's SSM mock
# does not emulate GetCommandInvocation output for AWS-RunShellScript;
# the file-header stubbing rationale applies unchanged):
#
# - dedicated reconciliation of a SUPPRESSED EventBridge event: the tick
#   retrieves the final Failed invocation, settles the deterministic
#   outcome with full diagnostics, releases the server slot, and the
#   next tick promotes the OLDEST eligible follower (Req 2.5, 2.7, 3.2,
#   3.4);
# - ephemeral reconciliation: the same settlement plus the runner's
#   verified idempotent termination completing the ledger's
#   compute-cleanup effect (Req 2.7, 3.2);
# - transient InvocationDoesNotExist: bounded retry within the lookup
#   window, never a fabricated failure, then full-evidence settlement
#   once the invocation becomes visible (Req 2.5);
# - `Success` settlement: nonterminal inside the callback window,
#   AGENT_RESULT_MISSING only after it; a valid callback recorded inside
#   the window keeps authority (Req 2.4/2.5, 2.6).
#
# Stop-before-release is already covered additively above
# (TestRuntimeTimeoutWatchdog.test_timeout_release_blocked_until_stop_verified,
# task 6.2). Existing expectations in this file are unchanged.
# ---------------------------------------------------------------------------

import uuid  # noqa: E402  (additive section import)


def _terminal_invocation(command_id, instance_id, status="Failed",
                         response_code=127, stdout="", stderr=""):
    """A final GetCommandInvocation shape for the scripted lookup."""
    return {
        "CommandId": command_id,
        "InstanceId": instance_id,
        "Status": status,
        "StatusDetails": status,
        "ResponseCode": response_code,
        "StandardOutputContent": stdout,
        "StandardErrorContent": stderr,
    }


def _scripted_invocations(script):
    """``retrieve_invocation`` side effect keyed by command id, so a
    command dispatched DURING the tick (e.g. a promoted follower's brand
    new SendCommand) is never fed another job's terminal evidence."""
    def _lookup(command_id, instance_id):
        return script.get(command_id)
    return _lookup


def _reconcile_tick(now, script=None, pgrep=None):
    """One full ``run_tick`` with the scripted invocation lookup and the
    synchronous-SSM stub (same seams the rest of this file uses)."""
    with mock.patch.object(
            build_dispatcher, "retrieve_invocation",
            side_effect=_scripted_invocations(script or {})), \
            mock.patch.object(build_dispatcher, "run_shell_sync",
                              return_value=pgrep):
        build_dispatcher.run_tick(now=now)


def _seed_command_job(job_id, command_id, instance_id, execution_mode,
                      server_id=None, **extra):
    """A building job carrying its correlated attempt/command identity
    (the state a real dispatch leaves behind)."""
    attempt_id = str(uuid.uuid4())
    item = {
        "build_job_id": job_id,
        "build_target": build_domain.TARGET_JP5,
        "execution_mode": execution_mode,
        "status": build_domain.STATUS_BUILDING,
        "requested_by": "operator-1",
        "created_at": NOW - 10 * _MINUTE_MS,
        "started_at": NOW - 9 * _MINUTE_MS,
        "config_snapshot": {"max_runtime_hours": 4},
        # Serialization check recently done: only reconciliation acts.
        "ssm": {"command_id": command_id, "instance_id": instance_id,
                "last_serialization_check_at": NOW},
        "execution_attempt": {
            "attempt_id": attempt_id,
            "command_id": command_id,
            "instance_id": instance_id,
        },
    }
    if server_id is not None:
        item["server_id"] = server_id
    item.update(extra)
    _JOBS.put_item(Item=item)
    return item


class TestScheduledCommandReconciliation:

    def setup_method(self):
        _setup()

    def test_dedicated_suppressed_event_settles_and_promotes_oldest(self):
        """A dedicated job whose command failed with NO EventBridge
        event delivered: the tick retrieves the final invocation,
        settles COMMAND_EXECUTION_FAILED with the full diagnostic,
        releases the server slot the same tick, and the NEXT tick
        promotes exactly the OLDEST eligible follower (Req 2.5, 2.7,
        3.2, 3.4)."""
        instance_id = _seed_server("srv-rec", running_build_job_id="job-lead")
        command_id = "cmd-lead-1"
        _seed_command_job("job-lead", command_id, instance_id,
                          build_domain.EXECUTION_MODE_DEDICATED,
                          server_id="srv-rec")
        _seed_job("job-follow-old", build_domain.STATUS_QUEUED,
                  build_domain.EXECUTION_MODE_DEDICATED,
                  server_id="srv-rec", created_at=NOW - 5 * _MINUTE_MS)
        _seed_job("job-follow-young", build_domain.STATUS_QUEUED,
                  build_domain.EXECUTION_MODE_DEDICATED,
                  server_id="srv-rec", created_at=NOW - _MINUTE_MS)

        script = {command_id: _terminal_invocation(
            command_id, instance_id, stderr="agent exited 127")}

        # Tick 1: the suppressed event costs only latency — the
        # scheduled reconciliation settles the same outcome.
        _reconcile_tick(NOW, script=script)

        job = _get_job("job-lead")
        assert job["status"] == build_domain.STATUS_FAILED
        assert job["error"]["code"] == \
            build_reconciliation.CODE_COMMAND_EXECUTION_FAILED
        assert job["ended_at"]
        diag = job["execution_diagnostic"]
        assert diag["response_code"] == 127
        assert diag["source"] == ["scheduled_reconciliation"]
        assert diag["stderr"]["available"] is True
        # The slot was released the SAME tick (release_and_promote).
        assert "running_build_job_id" not in (_get_server("srv-rec") or {})
        # Followers were still queued at this tick's dispatch pass.
        assert _get_job("job-follow-old")["status"] == \
            build_domain.STATUS_QUEUED
        assert _get_job("job-follow-young")["status"] == \
            build_domain.STATUS_QUEUED

        # Tick 2: the scheduled dispatch pass IS the promotion path —
        # exactly the oldest eligible follower takes the slot.
        _reconcile_tick(NOW + _MINUTE_MS, script=script,
                        pgrep=_CLEAN_PGREP)

        promoted = _get_job("job-follow-old")
        assert promoted["status"] == build_domain.STATUS_BUILDING
        assert promoted["ssm"]["command_id"]
        assert promoted["created_at"] == NOW - 5 * _MINUTE_MS, \
            "promotion must preserve the ORIGINAL submission time"
        assert _get_job("job-follow-young")["status"] == \
            build_domain.STATUS_QUEUED
        assert _get_server("srv-rec")["running_build_job_id"] == \
            "job-follow-old"
        # ONE logical failure audit for the settled job across both
        # ticks (Req 2.7).
        assert len(_audits("build_failed")) == 1
        assert _audits("build_failed")[0]["resource_id"] == "job-lead"

    def test_ephemeral_suppressed_event_settles_then_terminates_runner(self):
        """An ephemeral job with a terminal Failed command and no event:
        the tick settles the outcome (compute cleanup PENDING on the
        ledger), and the following tick's termination watchdog completes
        the idempotent runner termination as the cleanup effect
        (Req 2.5, 2.7, 3.2)."""
        runner_instance = _launch_instance()
        command_id = "cmd-eph-rec-1"
        _seed_command_job("job-eph-rec", command_id, runner_instance,
                          build_domain.EXECUTION_MODE_EPHEMERAL,
                          runner={"instance_id": runner_instance})

        script = {command_id: _terminal_invocation(
            command_id, runner_instance, stderr="build step failed")}

        _reconcile_tick(NOW, script=script)

        job = _get_job("job-eph-rec")
        assert job["status"] == build_domain.STATUS_FAILED
        assert job["error"]["code"] == \
            build_reconciliation.CODE_COMMAND_EXECUTION_FAILED
        assert job["execution_diagnostic"]["source"] == \
            ["scheduled_reconciliation"]
        # Ephemeral cleanup is a retryable pending effect at settlement.
        assert job["terminal_effects"]["compute_cleanup"] == \
            build_reconciliation.EFFECT_PENDING
        assert "terminated_at" not in (job.get("runner") or {})

        # Next tick: verified idempotent termination completes cleanup.
        _reconcile_tick(NOW + _MINUTE_MS, script=script)

        job = _get_job("job-eph-rec")
        assert job["runner"]["terminated_at"] == NOW + _MINUTE_MS
        assert job["terminal_effects"]["compute_cleanup"] == \
            build_reconciliation.EFFECT_DONE
        assert _instance_state(runner_instance) in \
            ("shutting-down", "terminated")
        assert len(_audits("build_failed")) == 1

    def test_transient_invocation_does_not_exist_retries_then_settles(self):
        """A terminal observation whose GetCommandInvocation is
        transiently unavailable (InvocationDoesNotExist / eventual
        consistency): the bounded lookup retries on the tick without
        fabricating a failure, then settles with the FULL evidence once
        the invocation becomes visible inside the window (Req 2.5)."""
        instance_id = _seed_server("srv-tr", running_build_job_id="job-tr")
        command_id = "cmd-tr-1"
        _seed_command_job(
            "job-tr", command_id, instance_id,
            build_domain.EXECUTION_MODE_DEDICATED, server_id="srv-tr",
            # The event path observed 'Failed' but could not retrieve
            # the invocation yet — the recorded reconciliation state.
            reconciliation={
                "command_id": command_id,
                "command_status": "Failed",
                "first_observed_at": NOW - _MINUTE_MS,
                "lookup_state": build_reconciliation.LOOKUP_PENDING,
                "updated_at": NOW - _MINUTE_MS,
            })

        # Tick 1: still InvocationDoesNotExist -> bounded retry, the job
        # stays nonterminal, nothing is fabricated (Req 2.2, 2.5).
        _reconcile_tick(NOW, script={})
        job = _get_job("job-tr")
        assert job["status"] == build_domain.STATUS_BUILDING
        assert "error" not in job
        assert _get_server("srv-tr")["running_build_job_id"] == "job-tr"

        # Tick 2, inside the lookup window: the invocation is now
        # visible -> full-evidence settlement.
        script = {command_id: _terminal_invocation(
            command_id, instance_id, response_code=1,
            stderr="gdk component build failed")}
        _reconcile_tick(NOW + _MINUTE_MS, script=script)

        job = _get_job("job-tr")
        assert job["status"] == build_domain.STATUS_FAILED
        assert job["error"]["code"] == \
            build_reconciliation.CODE_COMMAND_EXECUTION_FAILED
        assert job["reconciliation"]["lookup_state"] == \
            build_reconciliation.LOOKUP_RETRIEVED
        diag = job["execution_diagnostic"]
        assert diag["response_code"] == 1
        assert diag["stderr"]["available"] is True

    def test_success_without_callback_settles_only_after_the_window(self):
        """`Success` with no agent callback: the settlement window keeps
        the job nonterminal (a valid in-flight result may still arrive);
        AGENT_RESULT_MISSING is classified only AFTER the window
        (Req 2.4, 2.5)."""
        instance_id = _seed_server("srv-sw", running_build_job_id="job-sw")
        command_id = "cmd-sw-1"
        _seed_command_job("job-sw", command_id, instance_id,
                          build_domain.EXECUTION_MODE_DEDICATED,
                          server_id="srv-sw")
        script = {command_id: _terminal_invocation(
            command_id, instance_id, status="Success", response_code=0)}

        # Tick 1: the settlement wait is recorded; still nonterminal.
        _reconcile_tick(NOW, script=script)
        job = _get_job("job-sw")
        assert job["status"] == build_domain.STATUS_BUILDING
        deadline = job["reconciliation"]["settlement_deadline"]
        assert deadline == NOW + build_dispatcher.SETTLEMENT_WINDOW_MS

        # Tick 2, INSIDE the window: never classified early.
        _reconcile_tick(NOW + _MINUTE_MS, script=script)
        assert _get_job("job-sw")["status"] == build_domain.STATUS_BUILDING

        # Tick 3, past the deadline: AGENT_RESULT_MISSING settles.
        _reconcile_tick(deadline + _MINUTE_MS, script=script)
        job = _get_job("job-sw")
        assert job["status"] == build_domain.STATUS_FAILED
        assert job["error"]["code"] == \
            build_reconciliation.CODE_AGENT_RESULT_MISSING
        assert "running_build_job_id" not in (_get_server("srv-sw") or {})

    def test_callback_inside_settlement_window_keeps_authority(self):
        """A valid agent result recorded INSIDE the settlement window
        keeps authority: the later settlement tick can only add
        diagnostic completeness, never resurrect or overwrite the
        absorbed terminal outcome (Req 2.4, 2.6)."""
        instance_id = _seed_server("srv-cb", running_build_job_id="job-cb")
        command_id = "cmd-cb-1"
        _seed_command_job("job-cb", command_id, instance_id,
                          build_domain.EXECUTION_MODE_DEDICATED,
                          server_id="srv-cb")
        script = {command_id: _terminal_invocation(
            command_id, instance_id, status="Success", response_code=0)}

        _reconcile_tick(NOW, script=script)  # settlement wait recorded
        deadline = _get_job("job-cb")["reconciliation"][
            "settlement_deadline"]

        # The agent's succeeded callback lands (recorded by the event
        # consumer through the existing phase path).
        _JOBS.update_item(
            Key={"build_job_id": "job-cb"},
            UpdateExpression="SET #s = :succeeded, ended_at = :end",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":succeeded":
                                       build_domain.STATUS_SUCCEEDED,
                                       ":end": NOW + 30_000})

        _reconcile_tick(deadline + _MINUTE_MS, script=script)

        job = _get_job("job-cb")
        assert job["status"] == build_domain.STATUS_SUCCEEDED
        assert job["ended_at"] == NOW + 30_000
        assert "error" not in job

