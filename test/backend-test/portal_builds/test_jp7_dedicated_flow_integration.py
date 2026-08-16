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
Request-to-rejection dedicated flow integration test
(jp7-ephemeral-runner-provisioning, task 8.4).

**Validates: Requirements 2.7, 3.6**

End-to-end over the REAL request flow (`build_jobs.submit_build` against
a moto DynamoDB BuildJobs/BuildServers state) and the REAL dispatcher
tick (`build_dispatcher.run_tick`):

- **Rejection at the API validation boundary (Req 2.7)**: a JP7 +
  dedicated submission selecting a running arm64 server whose recorded
  ``ubuntu_version`` is '22.04' — and again a pre-ec1dc38 server record
  with NO ``ubuntu_version`` field at all — is rejected with the
  BUILD_REQUEST_INVALID envelope carrying rule
  ``server_os_release_mismatch``, a diagnostic naming BOTH the missing
  Ubuntu 24.04 arm64 capability and the server's actual release, and NO
  Build_Job record is created (the BuildJobs table stays empty and a
  subsequent dispatcher tick allocates nothing).

- **Acceptance dispatches through the UNCHANGED dedicated machinery
  (Req 3.6)**: the same submission against a running 24.04 arm64 server
  is accepted (201, one queued Build_Job persisted) and a dispatcher
  tick routes it exactly as today: allocation of exactly the selected
  server (a second capable server in the fleet is untouched), the
  single running slot (a second accepted submission stays queued behind
  it), pre-dispatch pgrep verification gating the start (a detected
  build process defers the job with its original ``created_at`` and the
  5-minute re-verification interval), and the agent SendCommand going
  to the selected server's instance via real (moto) SSM.

DynamoDB / EC2 / SSM are real moto; the dispatcher's synchronous SSM
verification helper (``run_shell_sync``) is stubbed per tick because
moto's SSM mock does not emulate get_command_invocation output for
AWS-RunShellScript (the sibling test_dispatcher_tick_integration.py
convention). shared_utils / rbac_middleware are replaced by the minimal
fakes the sibling suites use.

Run ONLY this file, from the repository root:

    python3 -m pytest \\
        test/backend-test/portal_builds/test_jp7_dedicated_flow_integration.py \\
        --noconftest -q
"""
import json
import os
import sys
import time
import types
from unittest import mock

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

_JOBS_TABLE = "build-jobs-jp7-dedicated-flow"
_SERVERS_TABLE = "build-servers-jp7-dedicated-flow"
os.environ["BUILD_JOBS_TABLE"] = _JOBS_TABLE
os.environ["BUILD_SERVERS_TABLE"] = _SERVERS_TABLE
# No settings table: effective_build_config falls back to the documented
# defaults; no dispatcher function name: the on-submit async invoke is a
# logged no-op and the tick is driven explicitly by the test.
os.environ.pop("SETTINGS_TABLE", None)
os.environ.pop("BUILD_DISPATCHER_FUNCTION_NAME", None)
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

# The flask-app verification container's python3.9 is built without the
# _bz2 C extension, and moto's request path imports moto.s3 -> bz2 on
# every call. bz2 is only used for S3-Select payload decompression, which
# this suite never exercises (same shim as the sibling suites).
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
# Minimal stand-ins for the Lambda layer modules the handlers import
# (same convention as the sibling suites). One fake shared_utils serves
# both build_jobs (create_response / get_user_from_event / audit) and
# build_dispatcher (audit only).
# ---------------------------------------------------------------------------
AUDIT_EVENTS = []


def _fake_shared_utils():
    module = types.ModuleType("shared_utils")

    def log_audit_event(**kwargs):
        AUDIT_EVENTS.append(kwargs)

    def create_response(status_code, body):
        return {"statusCode": status_code, "body": body}

    def get_user_from_event(event):
        return {"user_id": "jp7-flow-user", "role": "PortalAdmin"}

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

    module.require_builds_read = _identity_decorator_factory
    module.require_builds_submit = _identity_decorator_factory
    module.require_builds_cancel = _identity_decorator_factory
    module.super_user_only = lambda func: func
    return module


# Fresh modules so every module-level boto3 handle is created under the
# moto mock started below (sibling pattern).
for _module in ("build_dispatcher", "build_planner", "build_domain",
                "build_fleet", "build_jobs", "build_source",
                "build_reconciliation", "shared_utils", "rbac_middleware"):
    sys.modules.pop(_module, None)
sys.modules["shared_utils"] = _fake_shared_utils()
sys.modules["rbac_middleware"] = _fake_rbac_middleware()

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
# Explicit jammy AMI ids so no code path can consult the (absent)
# canonical public SSM parameters. This suite dispatches DEDICATED jobs
# only, so no ephemeral resolution runs; set for robustness anyway.
os.environ["BUILD_ARM64_AMI_ID"] = _AMI_ID
os.environ["BUILD_X86_64_AMI_ID"] = _AMI_ID

import build_domain  # noqa: E402
import build_planner  # noqa: E402
import build_jobs  # noqa: E402
import build_dispatcher  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_MINUTE_MS = 60 * 1000

_CLEAN_PGREP = ""  # no build process found (Req 7.5 clean verification)
_BUSY_PGREP = "12345 /bin/sh -c gdk component build\n"


def _clear_tables():
    for item in _JOBS.scan().get("Items", []):
        _JOBS.delete_item(Key={"build_job_id": item["build_job_id"]})
    for item in _SERVERS.scan().get("Items", []):
        _SERVERS.delete_item(Key={"server_id": item["server_id"]})


def _setup():
    _clear_tables()
    del AUDIT_EVENTS[:]


def _all_jobs():
    return [build_dispatcher.to_native(item)
            for item in _JOBS.scan().get("Items", [])]


def _get_job(job_id):
    return build_dispatcher.to_native(
        _JOBS.get_item(Key={"build_job_id": job_id}).get("Item"))


def _get_server(server_id):
    return build_dispatcher.to_native(
        _SERVERS.get_item(Key={"server_id": server_id}).get("Item"))


def _launch_instance():
    """Real moto EC2 instance backing a Dedicated_Build_Server."""
    response = _EC2.run_instances(
        ImageId=_AMI_ID, InstanceType="m6g.4xlarge", MinCount=1, MaxCount=1)
    return response["Instances"][0]["InstanceId"]


def _seed_server(server_id, name, ubuntu_version=None):
    """A running arm64 Dedicated_Build_Server record. ``ubuntu_version``
    None means the field is ABSENT (a pre-ec1dc38 record). No
    ``created_at`` is recorded, so the dedicated bootstrap-readiness
    policy takes its advisory no-probe branch and the tick under test
    exercises exactly the pgrep verification path."""
    instance_id = _launch_instance()
    item = {
        "server_id": server_id,
        "name": name,
        "instance_id": instance_id,
        "lifecycle_state": build_domain.SERVER_STATE_RUNNING,
        "cpu_architecture": build_domain.ARCH_ARM64,
    }
    if ubuntu_version is not None:
        item["ubuntu_version"] = ubuntu_version
    _SERVERS.put_item(Item=item)
    return instance_id


def _submit_jp7_dedicated(server_id):
    """Drive the REAL request flow: POST /builds selecting JP7 +
    dedicated on the given server."""
    event = {
        "httpMethod": "POST",
        "resource": "/builds",
        "path": "/builds",
        "body": json.dumps({
            "targets": [build_domain.TARGET_JP7],
            "execution_mode": build_domain.EXECUTION_MODE_DEDICATED,
            "server_id": server_id,
        }),
    }
    return build_jobs.submit_build(event, None)


def _agent_command_text(command_id):
    """The single AWS-RunShellScript command text of an SSM command."""
    commands = _SSM.list_commands(CommandId=command_id)["Commands"]
    assert len(commands) == 1
    return commands[0]["Parameters"]["commands"][0]


def _assert_capability_rejection(response, server_id, actual_release):
    """The Req 2.7 rejection contract: 400 BUILD_REQUEST_INVALID whose
    single error carries rule server_os_release_mismatch and a message
    naming the missing Ubuntu 24.04 arm64 capability, the server, and
    its actual release."""
    assert response["statusCode"] == 400, (
        f"JP7 + dedicated against a {actual_release} host must be "
        f"rejected at the API validation boundary, got "
        f"{response['statusCode']}: {response['body']!r}")
    error = response["body"]["error"]
    assert error["code"] == "BUILD_REQUEST_INVALID"
    errors = error["details"]["errors"]
    rules = [e["rule"] for e in errors]
    assert rules == [build_domain.RULE_SERVER_OS_RELEASE_MISMATCH], (
        f"a running arm64 server failing only the release must be "
        f"rejected by exactly the capability gate, got rules {rules}")
    message = errors[0]["message"]
    assert "24.04" in message and "arm64" in message, (
        f"the diagnostic must name the missing Ubuntu 24.04 arm64 "
        f"capability: {message!r}")
    assert actual_release in message, (
        f"the diagnostic must name the server's actual release "
        f"({actual_release!r}): {message!r}")
    assert server_id in message


# ---------------------------------------------------------------------------
# Rejection at the API validation boundary: capability diagnostic, and
# NO Build_Job record is created (Req 2.7)
# ---------------------------------------------------------------------------

class TestRequestToRejection:

    def setup_method(self):
        _setup()

    def test_jammy_server_rejected_with_capability_diagnostic_and_no_job(self):
        """A JP7 + dedicated submission selecting a running arm64 server
        recorded at ubuntu_version '22.04' is rejected at validation with
        the capability diagnostic; the BuildJobs table stays EMPTY and a
        subsequent dispatcher tick allocates nothing (Req 2.7)."""
        _seed_server("srv-jammy", "arm-server-jammy", ubuntu_version="22.04")

        response = _submit_jp7_dedicated("srv-jammy")

        _assert_capability_rejection(response, "srv-jammy", "22.04")
        # THE fail-closed assertion: no Build_Job record was created.
        assert _all_jobs() == [], (
            "a rejected JP7 + dedicated request must create NO Build_Job "
            "record")
        # No build_requested audit entry either: nothing was submitted.
        assert [e for e in AUDIT_EVENTS
                if e.get("action") == "build_requested"] == []

        # A dispatcher tick over this state has nothing to dispatch: the
        # jammy server's slot stays free and no verification runs.
        with mock.patch.object(build_dispatcher, "run_shell_sync",
                               return_value=_CLEAN_PGREP) as verify:
            build_dispatcher.run_tick(now=build_dispatcher.now_ms())
        assert verify.call_count == 0
        assert "running_build_job_id" not in (_get_server("srv-jammy") or {})

    def test_pre_ec1dc38_server_without_field_rejected_and_no_job(self):
        """The same submission against a pre-ec1dc38 server record with
        NO ubuntu_version field is rejected identically — the absent
        field is the 22.04 host the record predates — and again no
        Build_Job record is created (Req 2.7)."""
        _seed_server("srv-old", "arm-server-pre-ec1dc38", ubuntu_version=None)

        response = _submit_jp7_dedicated("srv-old")

        # The diagnostic names the EFFECTIVE release of the field-less
        # record: 22.04.
        _assert_capability_rejection(response, "srv-old", "22.04")
        assert _all_jobs() == [], (
            "a rejected JP7 + dedicated request must create NO Build_Job "
            "record (pre-ec1dc38 server)")

        with mock.patch.object(build_dispatcher, "run_shell_sync",
                               return_value=_CLEAN_PGREP) as verify:
            build_dispatcher.run_tick(now=build_dispatcher.now_ms())
        assert verify.call_count == 0
        assert "running_build_job_id" not in (_get_server("srv-old") or {})


# ---------------------------------------------------------------------------
# Acceptance dispatches through the UNCHANGED dedicated machinery
# (Req 3.6): exact-selected-server allocation, single running slot,
# queueing, pre-dispatch pgrep verification
# ---------------------------------------------------------------------------

class TestAcceptedDispatchThroughUnchangedMachinery:

    def setup_method(self):
        _setup()

    def test_accepted_jp7_allocates_exactly_the_selected_server(self):
        """The same JP7 + dedicated submission against a running 24.04
        arm64 server is accepted (201, one queued Build_Job persisted),
        and the tick dispatches it as today: the clean pre-dispatch pgrep
        verification runs against the SELECTED server's instance, the
        allocation lock is taken on exactly that server (a second capable
        server in the fleet is untouched), queued -> building, and the
        agent SendCommand goes to that instance (Req 2.2, 3.6, 7.1,
        7.5)."""
        instance_id = _seed_server("srv-noble", "arm-server-noble",
                                   ubuntu_version="24.04")
        # A second, equally capable server: the machinery must target
        # exactly the server SELECTED in the request, never a substitute.
        _seed_server("srv-noble-2", "arm-server-noble-2",
                     ubuntu_version="24.04")

        response = _submit_jp7_dedicated("srv-noble")

        assert response["statusCode"] == 201, (
            f"a running 24.04 arm64 server must be accepted, got "
            f"{response['statusCode']}: {response['body']!r}")
        submitted = response["body"]["jobs"]
        assert len(submitted) == 1
        job_id = submitted[0]["build_job_id"]

        # Exactly one Build_Job record was persisted, queued, targeting
        # exactly the selected server.
        stored = _all_jobs()
        assert [j["build_job_id"] for j in stored] == [job_id]
        assert stored[0]["status"] == build_domain.STATUS_QUEUED
        assert stored[0]["build_target"] == build_domain.TARGET_JP7
        assert stored[0]["server_id"] == "srv-noble"

        with mock.patch.object(build_dispatcher, "run_shell_sync",
                               return_value=_CLEAN_PGREP) as verify:
            now = build_dispatcher.now_ms()
            build_dispatcher.run_tick(now=now)

        # Pre-dispatch pgrep verification ran against the SELECTED
        # server's instance before the start (Req 7.5).
        verify.assert_called_once_with(
            instance_id, build_dispatcher.VERIFY_BUILD_PROCESS_COMMANDS)

        # Exact-selected-server allocation (Req 2.2, 7.1): the selected
        # server holds the slot; the equally capable bystander does not.
        assert _get_server("srv-noble")["running_build_job_id"] == job_id
        assert "running_build_job_id" not in _get_server("srv-noble-2")

        job = _get_job(job_id)
        assert job["status"] == build_domain.STATUS_BUILDING
        assert job["dispatched_at"] == now
        assert job["started_at"] == now

        # The agent SendCommand is a real (moto) SSM command against the
        # selected server's instance, recorded on the job.
        command_id = job["ssm"]["command_id"]
        assert job["ssm"]["instance_id"] == instance_id
        assert job["log"]["stream"] == \
            f"{command_id}/{instance_id}/aws-runShellScript/stdout"
        agent_text = _agent_command_text(command_id)
        assert f"BUILD_JOB_ID={job_id}" in agent_text
        assert f"BUILD_TARGET={build_domain.TARGET_JP7}" in agent_text

    def test_single_running_slot_queues_the_second_accepted_job(self):
        """Two accepted JP7 + dedicated submissions for the same 24.04
        server, one tick: the earlier submission takes the server's
        single running slot, the later one stays QUEUED with no agent
        command — today's single-slot and queueing semantics, unchanged
        (Req 3.6, 7.1, 7.2)."""
        _seed_server("srv-noble", "arm-server-noble", ubuntu_version="24.04")

        first = _submit_jp7_dedicated("srv-noble")
        time.sleep(0.002)  # strictly later created_at for the second job
        second = _submit_jp7_dedicated("srv-noble")
        assert first["statusCode"] == 201
        assert second["statusCode"] == 201
        first_id = first["body"]["jobs"][0]["build_job_id"]
        second_id = second["body"]["jobs"][0]["build_job_id"]

        with mock.patch.object(build_dispatcher, "run_shell_sync",
                               return_value=_CLEAN_PGREP):
            build_dispatcher.run_tick(now=build_dispatcher.now_ms())

        # The earlier submission holds the single running slot.
        assert _get_server("srv-noble")["running_build_job_id"] == first_id
        assert _get_job(first_id)["status"] == build_domain.STATUS_BUILDING

        # The later submission is queued behind it: no start, no command.
        second_job = _get_job(second_id)
        assert second_job["status"] == build_domain.STATUS_QUEUED
        assert "ssm" not in second_job

    def test_busy_pgrep_defers_then_clean_verification_dispatches(self):
        """Pre-dispatch pgrep verification behaves as today for the
        accepted JP7 job (Req 3.6, 7.5, 7.6): a detected build process
        defers it (status queued, ORIGINAL created_at retained,
        deferred_at recorded, allocation kept, no agent command); no
        re-verification runs inside the 5-minute retry interval; a clean
        re-verification after the interval starts the build."""
        instance_id = _seed_server("srv-noble", "arm-server-noble",
                                   ubuntu_version="24.04")

        response = _submit_jp7_dedicated("srv-noble")
        assert response["statusCode"] == 201
        job_id = response["body"]["jobs"][0]["build_job_id"]
        created_at = _get_job(job_id)["created_at"]

        # Tick 1: a build process is running on the server -> defer.
        t0 = build_dispatcher.now_ms()
        with mock.patch.object(build_dispatcher, "run_shell_sync",
                               return_value=_BUSY_PGREP) as verify:
            build_dispatcher.run_tick(now=t0)
        verify.assert_called_once_with(
            instance_id, build_dispatcher.VERIFY_BUILD_PROCESS_COMMANDS)

        job = _get_job(job_id)
        assert job["status"] == build_domain.STATUS_QUEUED
        assert job["created_at"] == created_at, \
            "a deferral must retain the ORIGINAL submission time"
        assert job["deferred_at"] == t0
        assert "ssm" not in job
        # The allocation is kept so no other job slips onto the server.
        assert _get_server("srv-noble")["running_build_job_id"] == job_id

        # Tick 2, one minute later: inside the 5-minute retry interval,
        # no re-verification is attempted (Req 7.6).
        with mock.patch.object(build_dispatcher, "run_shell_sync",
                               return_value=_CLEAN_PGREP) as verify:
            build_dispatcher.run_tick(now=t0 + _MINUTE_MS)
        assert verify.call_count == 0
        assert _get_job(job_id)["status"] == build_domain.STATUS_QUEUED

        # Tick 3, past the retry interval: re-verified clean -> building
        # with the agent command sent to the selected server's instance.
        later = t0 + build_planner.PREDISPATCH_RETRY_INTERVAL_MS
        with mock.patch.object(build_dispatcher, "run_shell_sync",
                               return_value=_CLEAN_PGREP) as verify:
            build_dispatcher.run_tick(now=later)
        assert verify.call_count == 1

        job = _get_job(job_id)
        assert job["status"] == build_domain.STATUS_BUILDING
        assert job["created_at"] == created_at
        assert job["ssm"]["command_id"]
        assert job["ssm"]["instance_id"] == instance_id
        assert _get_server("srv-noble")["running_build_job_id"] == job_id
