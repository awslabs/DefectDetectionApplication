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
Moto integration test for JP7 ephemeral dispatcher-tick provisioning
(``edge-cv-portal/backend/functions/build_dispatcher.py``, task 8.1 of
jp7-ephemeral-runner-provisioning).

**Validates: Requirements 2.1, 2.2, 2.4**

End-to-end ``run_tick`` executions over moto-mocked DynamoDB / EC2 / SSM,
following the ``test_dispatcher_tick_integration.py`` conventions:

- A queued JP7 ephemeral Build_Job is provisioned with the noble arm64
  AMI id returned by the stubbed SSM seam: the module seeds a parameter
  at ``ARM64_NOBLE_AMI_SSM_PARAMETER`` (env-pointed at a moto-writable
  path) whose value is a distinct moto AMI, sets NO
  ``BUILD_ARM64_NOBLE_AMI_ID`` pin, and keeps the jammy
  ``BUILD_ARM64_AMI_ID`` override pointing at a DIFFERENT AMI — so the
  only way the runner can launch from the noble id is target-aware
  resolution through the noble SSM parameter (Req 2.1, 2.2).
- The real (moto) RunInstances call carries that ImageId, and the runner
  record on the Build_Job carries ``os_release='24.04'`` alongside
  arch/instance_type (Req 2.2, 2.4).
- The readiness/Bootstrap_Marker/agent-command flow proceeds unchanged
  for the noble runner: SSM Online alone does not release the agent, the
  Bootstrap_Marker probe gates the SendCommand, the readiness evidence is
  recorded before the agent starts, the agent command targets the
  recorded repository directory, and the terminal-job watchdog terminates
  the runner (Req 2.4).
- A JP5 job dispatched by the same tick still launches from the jammy
  override AMI, pinning that the noble resolution is keyed by the
  target's required OS release, not applied fleet-wide.

DynamoDB, EC2, and SSM are real moto; the dispatcher's synchronous SSM
verification helpers (``run_shell_sync``, ``instance_ssm_online``) are
stubbed at drive time because moto's SSM mock does not emulate
get_command_invocation output for AWS-RunShellScript. The agent
SendCommand path itself runs against real moto SSM. shared_utils is
replaced by a minimal fake (the sibling standalone-suite pattern); this
suite asserts no audit content, so it never records through the shared
module seam other collected modules may rebind.
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

_JOBS_TABLE = "build-jobs-jp7t81"
_SERVERS_TABLE = "build-servers-jp7t81"
os.environ["BUILD_JOBS_TABLE"] = _JOBS_TABLE
os.environ["BUILD_SERVERS_TABLE"] = _SERVERS_TABLE
# No repo URL: runner user-data bootstrap is skipped (pre-baked-AMI mode).
os.environ.pop("BUILD_REPO_URL", None)
os.environ.pop("BUILD_ALERT_TOPIC_ARN", None)
os.environ.pop("BUILD_INSTANCE_PROFILE_ARN", None)
os.environ.pop("BUILD_INSTANCE_PROFILE_NAME", None)
os.environ.pop("BUILD_SECURITY_GROUP_ID", None)
os.environ.pop("BUILD_SUBNET_ID", None)
# The noble pin must be ABSENT so resolution can only reach the noble AMI
# through the SSM parameter seam (Req 2.1).
os.environ.pop("BUILD_ARM64_NOBLE_AMI_ID", None)

# Import boto3 (and thus botocore/urllib3) from the test environment BEFORE
# the Lambda function directory joins sys.path.
import boto3  # noqa: E402

# The flask-app verification container's python3.9 is built without the
# _bz2 C extension, and moto's request path imports moto.s3 -> bz2 on
# every call. bz2 is only used for S3-Select payload decompression, which
# this suite never exercises, so a minimal stdlib-shaped stub keeps the
# import chain intact where _bz2 is absent (sibling-suite shim).
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
# Fake shared_utils (build_dispatcher imports only log_audit_event from the
# layer). This suite asserts no audit content, so the fake is a sink.
# ---------------------------------------------------------------------------


def _fake_shared_utils():
    module = types.ModuleType("shared_utils")

    def log_audit_event(**kwargs):
        pass

    module.log_audit_event = log_audit_event
    return module


# Fresh modules so build_dispatcher's module-level boto3 handles and env
# bindings are created under the moto mock started below (sibling pattern).
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

_EC2 = boto3.client("ec2", region_name="us-east-1")
_SSM = boto3.client("ssm", region_name="us-east-1")


def _two_distinct_ami_ids():
    """Two DISTINCT moto AMI ids: one plays the jammy override pin, the
    other the noble AMI the stubbed SSM parameter returns. Their
    distinctness is what lets the test prove which resolution path
    produced the runner's ImageId."""
    images = _EC2.describe_images(Owners=["amazon"]).get("Images", [])
    ids = sorted({image["ImageId"] for image in images})
    while len(ids) < 2:  # pragma: no cover - moto version dependent
        ids.append(_EC2.register_image(
            Name=f"dda-test-ami-{len(ids)}", RootDeviceName="/dev/sda1",
            VirtualizationType="hvm")["ImageId"])
    return ids[0], ids[1]


_JAMMY_AMI_ID, _NOBLE_AMI_ID = _two_distinct_ami_ids()
assert _JAMMY_AMI_ID != _NOBLE_AMI_ID

# Jammy resolution: explicit env AMI ids so the (absent) canonical 22.04
# parameters are never consulted. These are jammy pins and MUST NOT be
# honored for a JP7 (noble) job.
os.environ["BUILD_ARM64_AMI_ID"] = _JAMMY_AMI_ID
os.environ["BUILD_X86_64_AMI_ID"] = _JAMMY_AMI_ID

# Noble resolution: the canonical noble path lives under /aws/..., which
# moto's put_parameter rejects, so the env-overridable parameter NAME is
# pointed at a moto-writable path and the parameter is seeded with the
# noble AMI id. resolve_ami must reach this value through the stubbed SSM
# GetParameter seam (Req 2.1) — there is no noble pin and the moto account
# holds no Canonical-owner images for the DescribeImages fallback.
_NOBLE_AMI_PARAMETER = "/dda-test/canonical/ubuntu/noble/arm64/ami-id"
os.environ["ARM64_NOBLE_AMI_SSM_PARAMETER"] = _NOBLE_AMI_PARAMETER
_SSM.put_parameter(Name=_NOBLE_AMI_PARAMETER, Type="String",
                   Value=_NOBLE_AMI_ID)

import build_domain  # noqa: E402
import build_planner  # noqa: E402
import build_source  # noqa: E402
import build_dispatcher  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers (sibling conventions)
# ---------------------------------------------------------------------------

NOW = 1_700_000_000_000  # ms epoch anchor for deterministic tick times
_MINUTE_MS = 60 * 1000


def _clear_tables():
    for item in _JOBS.scan().get("Items", []):
        _JOBS.delete_item(Key={"build_job_id": item["build_job_id"]})


def _get_job(job_id):
    return build_dispatcher.to_native(
        _JOBS.get_item(Key={"build_job_id": job_id}).get("Item"))


def _seed_job(job_id, build_target, created_at=NOW, **extra):
    item = {
        "build_job_id": job_id,
        "build_target": build_target,
        "execution_mode": build_domain.EXECUTION_MODE_EPHEMERAL,
        "status": build_domain.STATUS_QUEUED,
        "requested_by": "operator-1",
        "created_at": created_at,
        "config_snapshot": {
            "arm64_instance_type": "m6g.2xlarge",
            "volume_size_gb": 120,
            "max_runtime_hours": 4,
        },
    }
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


def _instance_state(instance_id):
    response = _EC2.describe_instances(InstanceIds=[instance_id])
    return response["Reservations"][0]["Instances"][0]["State"]["Name"]


def _agent_command_text(command_id):
    """The single AWS-RunShellScript command text of an SSM command."""
    commands = _SSM.list_commands(CommandId=command_id)["Commands"]
    assert len(commands) == 1
    assert commands[0]["DocumentName"] == "AWS-RunShellScript"
    return commands[0]["Parameters"]["commands"][0]


# Bootstrap_Marker probe outputs (from the build_planner constants, the
# single definition of the probe keys, so the fixture cannot drift from
# the probe the dispatcher actually sends).
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


def _shell_sync_router(marker_output, pgrep_output=""):
    """``run_shell_sync`` side effect routing on the command list, so the
    Bootstrap_Marker probe is implicitly asserted to be issued with
    exactly ``build_dispatcher.BOOTSTRAP_PROBE_COMMANDS``."""
    def _run(instance_id, commands, **kwargs):
        if commands == build_dispatcher.BOOTSTRAP_PROBE_COMMANDS:
            return marker_output
        return pgrep_output
    return _run


# ---------------------------------------------------------------------------
# JP7 ephemeral provisioning through the dispatcher tick
# (Req 2.1, 2.2, 2.4)
# ---------------------------------------------------------------------------

class TestJp7EphemeralDispatcherTick:

    def setup_method(self):
        _clear_tables()

    def test_jp7_provisioned_from_noble_ami_and_flow_unchanged(self):
        """Tick 1 provisions the queued JP7 ephemeral job from the noble
        arm64 AMI the stubbed SSM parameter returned — the RunInstances
        call carries that ImageId, never the jammy override — and the
        runner record carries os_release='24.04' (Req 2.1, 2.2, 2.4).
        Ticks 2-4 pin the readiness/Bootstrap_Marker/agent-command flow
        unchanged on the noble runner, and the terminal watchdog
        terminates it."""
        _seed_job("job-jp7", build_domain.TARGET_JP7)

        # Tick 1: queued -> provisioning + RunInstances from the noble AMI.
        build_dispatcher.run_tick(now=NOW)

        job = _get_job("job-jp7")
        assert job["status"] == build_domain.STATUS_PROVISIONING
        assert job["dispatched_at"] == NOW
        runner = job["runner"]
        runner_instance_id = runner["instance_id"]
        assert runner["arch"] == build_domain.ARCH_ARM64
        assert runner["instance_type"] == "m6g.2xlarge"
        # The runner record carries the OS release the AMI was resolved
        # for (Req 2.2, 2.4).
        assert runner["os_release"] == build_domain.OS_RELEASE_NOBLE
        recorded_repo_dir = runner["repo_dir"]

        instances = _runner_instances_for_job("job-jp7")
        assert len(instances) == 1, \
            "exactly one Ephemeral_Build_Runner per dispatched job"
        instance = instances[0]
        assert instance["InstanceId"] == runner_instance_id
        # The RunInstances call carried the noble AMI id returned by the
        # stubbed SSM parameter (Req 2.1) ...
        assert instance["ImageId"] == _NOBLE_AMI_ID
        # ... and NOT the jammy env override, which stays scoped to 22.04.
        # (Asserted against THIS module instance's import-time binding, the
        # value resolve_ami actually consults, so later-collected sibling
        # modules mutating the process env cannot skew the check.)
        assert instance["ImageId"] != _JAMMY_AMI_ID
        assert build_dispatcher.BUILD_ARM64_AMI_ID == _JAMMY_AMI_ID
        tags = {tag["Key"]: tag["Value"] for tag in instance["Tags"]}
        assert tags[build_dispatcher.TAG_EPHEMERAL] == "true"
        assert tags[build_dispatcher.TAG_JOB_ID] == "job-jp7"

        # Tick 2: SSM Online but the Bootstrap_Marker not yet observed ->
        # the gate stays shut, exactly as for every other target.
        with mock.patch.object(build_dispatcher, "instance_ssm_online",
                               return_value=True), \
                mock.patch.object(
                    build_dispatcher, "run_shell_sync",
                    side_effect=_shell_sync_router(_MARKER_ABSENT)) as probe:
            build_dispatcher.run_tick(now=NOW + _MINUTE_MS)

        probe.assert_called_once_with(
            runner_instance_id, build_dispatcher.BOOTSTRAP_PROBE_COMMANDS)
        job = _get_job("job-jp7")
        assert job["status"] == build_domain.STATUS_PROVISIONING
        assert "ssm" not in job, \
            "no agent command may precede an observed Bootstrap_Marker"
        assert "bootstrap" not in job

        # Tick 3: marker observed -> readiness evidence recorded and the
        # agent SendCommand (real moto SSM) goes to the noble runner.
        with mock.patch.object(build_dispatcher, "instance_ssm_online",
                               return_value=True), \
                mock.patch.object(
                    build_dispatcher, "run_shell_sync",
                    side_effect=_shell_sync_router(_MARKER_OBSERVED)):
            build_dispatcher.run_tick(now=NOW + 2 * _MINUTE_MS)

        job = _get_job("job-jp7")
        assert job["bootstrap"]["marker_at"] == NOW + 2 * _MINUTE_MS
        assert job["bootstrap"]["log_path"] == \
            build_planner.BOOTSTRAP_LOG_PATH
        command_id = job["ssm"]["command_id"]
        assert job["log"]["group"] == build_dispatcher.BUILD_LOG_GROUP
        assert job["log"]["stream"] == \
            f"{command_id}/{runner_instance_id}/aws-runShellScript/stdout"
        agent_text = _agent_command_text(command_id)
        assert "BUILD_JOB_ID=job-jp7" in agent_text
        assert f"BUILD_TARGET={build_domain.TARGET_JP7}" in agent_text
        # The agent is invoked from the directory the runner's own
        # provisioning recorded, through the unchanged command builder.
        assert build_source.agent_script_path(recorded_repo_dir) \
            in agent_text
        assert agent_text == build_dispatcher.agent_command(
            job, recorded_repo_dir)

        # Tick 4: agent already dispatched -> no second SendCommand.
        with mock.patch.object(build_dispatcher, "instance_ssm_online",
                               return_value=True), \
                mock.patch.object(build_dispatcher, "send_agent") as send:
            build_dispatcher.run_tick(now=NOW + 3 * _MINUTE_MS)
        send.assert_not_called()

        # Terminal job -> the termination watchdog terminates the noble
        # runner, unchanged.
        _JOBS.update_item(
            Key={"build_job_id": "job-jp7"},
            UpdateExpression="SET #s = :s",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":s": build_domain.STATUS_SUCCEEDED},
        )
        build_dispatcher.run_tick(now=NOW + 4 * _MINUTE_MS)

        job = _get_job("job-jp7")
        assert job["runner"]["terminated_at"] == NOW + 4 * _MINUTE_MS
        assert _instance_state(runner_instance_id) in \
            ("shutting-down", "terminated")

    def test_same_tick_jp5_job_still_launches_from_jammy_override(self):
        """The noble resolution is keyed by the TARGET's required OS
        release, not applied tick-wide: a JP5 job dispatched by the same
        tick as a JP7 job launches from the jammy override AMI while the
        JP7 job launches from the noble parameter's AMI (Req 2.1, 2.2)."""
        _seed_job("job-jp7-pair", build_domain.TARGET_JP7,
                  created_at=NOW - _MINUTE_MS)
        _seed_job("job-jp5-pair", build_domain.TARGET_JP5)

        build_dispatcher.run_tick(now=NOW)

        jp7_instances = _runner_instances_for_job("job-jp7-pair")
        jp5_instances = _runner_instances_for_job("job-jp5-pair")
        assert len(jp7_instances) == 1
        assert len(jp5_instances) == 1
        assert jp7_instances[0]["ImageId"] == _NOBLE_AMI_ID
        assert jp5_instances[0]["ImageId"] == _JAMMY_AMI_ID

        jp7_job = _get_job("job-jp7-pair")
        jp5_job = _get_job("job-jp5-pair")
        assert jp7_job["runner"]["os_release"] == \
            build_domain.OS_RELEASE_NOBLE
        assert jp5_job["runner"]["os_release"] == \
            build_domain.OS_RELEASE_JAMMY


# ---------------------------------------------------------------------------
# Unmapped-pairing tick: fail-closed AMI resolution inside a multi-job
# batch (task 8.3 of jp7-ephemeral-runner-provisioning).
#
# **Validates: Requirements 2.3**
# ---------------------------------------------------------------------------

class TestUnmappedPairingTick:
    """A synthetic plan carrying an unmapped OS-release/architecture
    pairing (noble on x86_64) inside a multi-job batch fails EXACTLY that
    one job with ``ERROR_PROVISIONING_FAILED`` and a cause naming the
    pairing; the tick does not raise; the other jobs in the batch are
    provisioned normally from their own releases' AMIs; and no instance
    is ever launched for the failed job (Req 2.3).

    The synthetic plan is injected by wrapping the REAL
    ``build_planner.plan_ephemeral_provisioning`` and rewriting only the
    target job's ``os_release`` to '24.04' on its x86_64 plan — the
    pairing ``resolve_ami`` has no mapping for — leaving every other
    plan exactly as the real planner produced it. Everything downstream
    of the planner (transition, resolve_ami, RunInstances, failure
    routing) is the real dispatcher code over real moto AWS.
    """

    def setup_method(self):
        _clear_tables()

    def test_unmapped_pairing_fails_only_that_job(self):
        # Batch of three queued ephemeral jobs; the unmapped one is the
        # OLDEST so the tick provably continues past its failure to the
        # jobs dispatched after it.
        _seed_job("job-unmapped", build_domain.TARGET_AMD64,
                  created_at=NOW - 2 * _MINUTE_MS)
        _seed_job("job-jp5-ok", build_domain.TARGET_JP5,
                  created_at=NOW - _MINUTE_MS)
        _seed_job("job-jp7-ok", build_domain.TARGET_JP7)

        real_plan_provisioning = build_planner.plan_ephemeral_provisioning

        def synthetic_plans(jobs):
            plans = real_plan_provisioning(jobs)
            return [
                plan._replace(os_release=build_domain.OS_RELEASE_NOBLE)
                if plan.build_job_id == "job-unmapped" else plan
                for plan in plans
            ]

        # The tick MUST NOT raise: the unmapped pairing's ValueError is
        # confined to its one job by provision_ephemeral's
        # (ClientError, ValueError) -> fail_provisioning routing.
        with mock.patch.object(build_planner, "plan_ephemeral_provisioning",
                               side_effect=synthetic_plans):
            build_dispatcher.run_tick(now=NOW)

        # --- Exactly the synthetic-plan job failed, with the naming
        # --- diagnostic (Req 2.3).
        failed = _get_job("job-unmapped")
        assert failed["status"] == build_domain.STATUS_FAILED
        assert failed["error"]["code"] == \
            build_dispatcher.ERROR_PROVISIONING_FAILED
        # The cause names BOTH halves of the unmapped pairing.
        assert build_domain.OS_RELEASE_NOBLE in failed["error"]["message"]
        assert build_domain.ARCH_X86_64 in failed["error"]["message"]
        assert "Provisioning the Ephemeral_Build_Runner failed" in \
            failed["error"]["message"]
        assert failed["ended_at"]

        # --- No instance was ever launched for the failed job: the
        # --- ValueError is raised BEFORE RunInstances, and the partial-
        # --- compute sweep found nothing to terminate.
        assert _runner_instances_for_job("job-unmapped") == []
        assert "runner" not in failed, \
            "no runner record may exist for a job whose resolution " \
            "failed before RunInstances"

        # --- The OTHER jobs in the batch were provisioned normally, each
        # --- from its own release's AMI.
        jp5_job = _get_job("job-jp5-ok")
        assert jp5_job["status"] == build_domain.STATUS_PROVISIONING
        assert jp5_job["runner"]["os_release"] == \
            build_domain.OS_RELEASE_JAMMY
        jp5_instances = _runner_instances_for_job("job-jp5-ok")
        assert len(jp5_instances) == 1
        assert jp5_instances[0]["ImageId"] == _JAMMY_AMI_ID

        jp7_job = _get_job("job-jp7-ok")
        assert jp7_job["status"] == build_domain.STATUS_PROVISIONING
        assert jp7_job["runner"]["os_release"] == \
            build_domain.OS_RELEASE_NOBLE
        jp7_instances = _runner_instances_for_job("job-jp7-ok")
        assert len(jp7_instances) == 1
        assert jp7_instances[0]["ImageId"] == _NOBLE_AMI_ID
