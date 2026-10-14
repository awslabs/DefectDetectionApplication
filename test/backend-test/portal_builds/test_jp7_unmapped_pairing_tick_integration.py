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
Unmapped-pairing dispatcher-tick integration test
(jp7-ephemeral-runner-provisioning task 8.3).

**Validates: Requirements 2.3**

A synthetic RunnerPlan carrying an OS-release/architecture pairing with
no AMI mapping (noble on x86_64 — bugfix.md 2.3's own example) is
injected into a multi-job ephemeral batch. One full ``run_tick`` over
moto-mocked DynamoDB / EC2 / SSM must:

- fail EXACTLY the synthetic job with ``ERROR_PROVISIONING_FAILED`` and
  a cause message naming BOTH the release and the architecture of the
  unmapped pairing;
- NOT raise (``resolve_ami``'s fail-closed ValueError is caught by
  ``provision_ephemeral``'s ``(ClientError, ValueError)`` handler and
  routed to ``fail_provisioning``, never crashing the tick);
- provision every OTHER job in the batch normally (status provisioning,
  runner record written, exactly one RunInstances each);
- launch NO instance for the failed job (fail-closed BEFORE any AWS
  call — in particular the jammy env override pins must NOT be honored
  as a fallback AMI of a different OS release).

Module scaffolding follows the suite's
``test_dispatcher_tick_integration.py`` conventions: module-scope moto,
real (moto) DynamoDB/EC2, shared_utils replaced by a minimal fake
capturing Audit_Log entries, explicit jammy AMI env pins so healthy
resolution never consults the absent canonical public SSM parameters.
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

_JOBS_TABLE = "build-jobs-t83"
_SERVERS_TABLE = "build-servers-t83"
os.environ["BUILD_JOBS_TABLE"] = _JOBS_TABLE
os.environ["BUILD_SERVERS_TABLE"] = _SERVERS_TABLE
# No repo URL: runner user-data bootstrap is skipped (pre-baked-AMI mode).
os.environ.pop("BUILD_REPO_URL", None)
os.environ.pop("BUILD_ALERT_TOPIC_ARN", None)
os.environ.pop("BUILD_INSTANCE_PROFILE_ARN", None)
os.environ.pop("BUILD_INSTANCE_PROFILE_NAME", None)
os.environ.pop("BUILD_SECURITY_GROUP_ID", None)
os.environ.pop("BUILD_SUBNET_ID", None)
os.environ.pop("BUILD_ARM64_NOBLE_AMI_ID", None)

# Import boto3 (and thus botocore/urllib3) from the test environment BEFORE
# the Lambda function directory joins sys.path.
import boto3  # noqa: E402

# _bz2-less interpreter shim (same as the sibling tick suite): moto's
# request path imports moto.s3 -> bz2 on every call.
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
# Fake shared_utils capturing Audit_Log entries.
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

_EC2 = boto3.client("ec2", region_name="us-east-1")


def _default_ami_id():
    """Any moto-provided AMI id (fallback: register one)."""
    images = _EC2.describe_images(Owners=["amazon"]).get("Images", [])
    if images:
        return images[0]["ImageId"]
    return _EC2.register_image(  # pragma: no cover - moto version dependent
        Name="dda-test-ami", RootDeviceName="/dev/sda1",
        VirtualizationType="hvm")["ImageId"]


_AMI_ID = _default_ami_id()
# Explicit jammy AMI pins so the healthy jobs' resolve_ami never consults
# the (absent) canonical public SSM parameters. Deliberately ALSO present
# while the unmapped pairing resolves: Req 2.3 forbids honoring a fallback
# AMI of a different OS release, so these pins must NOT rescue it.
os.environ["BUILD_ARM64_AMI_ID"] = _AMI_ID
os.environ["BUILD_X86_64_AMI_ID"] = _AMI_ID

import build_domain  # noqa: E402
import build_planner  # noqa: E402
import build_dispatcher  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

NOW = 1_700_000_000_000  # ms epoch anchor for deterministic tick times

#: The unmapped pairing under test: noble on x86_64, the exact example
#: bugfix.md 2.3 and design Property 2 name. resolve_ami must fail this
#: closed BEFORE any AWS call.
_UNMAPPED_RELEASE = build_domain.OS_RELEASE_NOBLE  # '24.04'
_UNMAPPED_ARCH = build_domain.ARCH_X86_64          # 'x86_64'

_SYNTHETIC_JOB_ID = "job-unmapped"


def _clear_state():
    for item in _JOBS.scan().get("Items", []):
        _JOBS.delete_item(Key={"build_job_id": item["build_job_id"]})
    del AUDIT_EVENTS[:]
    build_dispatcher._AMI_CACHE.clear()
    # Order-independence within one pytest process (the sibling
    # test_no_live_validation_contract.py convention): a suite collected
    # LATER may rebind this module instance's ``log_audit_event`` to ITS
    # OWN stub at import time. Rebind it to THIS file's recorder so
    # AUDIT_EVENTS is authoritative regardless of collection order.
    build_dispatcher.log_audit_event = \
        lambda **kwargs: AUDIT_EVENTS.append(kwargs)


def _get_job(job_id):
    return build_dispatcher.to_native(
        _JOBS.get_item(Key={"build_job_id": job_id}).get("Item"))


def _seed_ephemeral_job(job_id, build_target, created_at=NOW, **extra):
    item = {
        "build_job_id": job_id,
        "build_target": build_target,
        "execution_mode": build_domain.EXECUTION_MODE_EPHEMERAL,
        "status": build_domain.STATUS_QUEUED,
        "requested_by": "operator-1",
        "created_at": created_at,
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


# Bound BEFORE the patch below so the wrapper reaches the real planner,
# not the mock replacing the module attribute.
_REAL_PLAN_EPHEMERAL_PROVISIONING = build_planner.plan_ephemeral_provisioning


def _synthetic_plan_batch(jobs):
    """The real per-tick planning, with EXACTLY the synthetic job's plan
    rewritten to the unmapped release/arch pairing.

    ``RunnerPlan`` is a NamedTuple, so ``_replace`` yields the identical
    plan apart from the injected pairing — sizing, spot flag, and status
    stay exactly what ``plan_runner`` derived. Every other job's plan is
    passed through untouched.
    """
    plans = _REAL_PLAN_EPHEMERAL_PROVISIONING(jobs)
    return [
        plan._replace(os_release=_UNMAPPED_RELEASE, arch=_UNMAPPED_ARCH)
        if plan.build_job_id == _SYNTHETIC_JOB_ID else plan
        for plan in plans
    ]


class TestUnmappedPairingTick:
    """One tick over a multi-job ephemeral batch containing a synthetic
    unmapped-pairing plan (Req 2.3)."""

    def setup_method(self):
        _clear_state()

    def test_unmapped_pairing_fails_only_its_job_and_launches_nothing(self):
        """The synthetic plan's job fails with ERROR_PROVISIONING_FAILED
        naming the pairing; the tick does not raise; the two healthy jobs
        in the same batch are provisioned normally; no instance is
        launched for the failed job."""
        _seed_ephemeral_job(
            "job-jp5", build_domain.TARGET_JP5,
            created_at=NOW - 2000,
            config_snapshot={"arm64_instance_type": "m6g.2xlarge",
                             "volume_size_gb": 120,
                             "max_runtime_hours": 4})
        # Planned normally as ('22.04', 'x86_64'); the synthetic rewrite
        # turns exactly this job's plan into the unmapped noble/x86_64.
        _seed_ephemeral_job(
            _SYNTHETIC_JOB_ID, build_domain.TARGET_AMD64,
            created_at=NOW - 1000,
            config_snapshot={"max_runtime_hours": 4})
        _seed_ephemeral_job(
            "job-amd", build_domain.TARGET_AMD64_NVIDIA,
            created_at=NOW,
            config_snapshot={"x86_64_instance_type": "m6i.2xlarge",
                             "max_runtime_hours": 4})

        with mock.patch.object(
                build_planner, "plan_ephemeral_provisioning",
                side_effect=_synthetic_plan_batch):
            # The tick must NOT raise: resolve_ami's fail-closed
            # ValueError is converted into the one job's failure.
            build_dispatcher.run_tick(now=NOW)

        # --- exactly the synthetic job failed, with the pairing named ---
        failed = _get_job(_SYNTHETIC_JOB_ID)
        assert failed["status"] == build_domain.STATUS_FAILED
        assert failed["error"]["code"] == \
            build_dispatcher.ERROR_PROVISIONING_FAILED
        message = failed["error"]["message"]
        assert message.startswith(
            "Provisioning the Ephemeral_Build_Runner failed:")
        # The cause names BOTH halves of the unmapped pairing (Req 2.3).
        assert _UNMAPPED_RELEASE in message
        assert _UNMAPPED_ARCH in message
        assert "runner" not in failed, \
            "no runner record may be written for the failed job"

        # No instance was launched for the failed job: the resolution
        # failed closed BEFORE any AWS call, and in particular the jammy
        # env pins (set module-wide) were NOT honored as a wrong-release
        # fallback AMI (Req 2.3).
        assert _runner_instances_for_job(_SYNTHETIC_JOB_ID) == []

        # The provisioning failure was audited for exactly this job.
        failure_audits = [entry for entry in AUDIT_EVENTS
                          if entry["action"] == "build_provisioning_failed"]
        assert [entry["resource_id"] for entry in failure_audits] == \
            [_SYNTHETIC_JOB_ID]
        assert failure_audits[0]["result"] == "failure"
        assert failure_audits[0]["details"][
            "terminated_partial_compute"] == [], \
            "there was no partial compute to terminate"

        # --- the other jobs in the batch were provisioned normally ---
        for job_id, arch, instance_type in (
                ("job-jp5", build_domain.ARCH_ARM64, "m6g.2xlarge"),
                ("job-amd", build_domain.ARCH_X86_64, "m6i.2xlarge")):
            job = _get_job(job_id)
            assert job["status"] == build_domain.STATUS_PROVISIONING, \
                f"{job_id} must be provisioned despite the batch-mate's " \
                f"unmapped pairing"
            assert job["dispatched_at"] == NOW
            assert "error" not in job
            assert job["runner"]["arch"] == arch
            assert job["runner"]["instance_type"] == instance_type
            assert job["runner"]["os_release"] == \
                build_domain.OS_RELEASE_JAMMY

            instances = _runner_instances_for_job(job_id)
            assert len(instances) == 1, \
                f"exactly one RunInstances for {job_id}"
            instance = instances[0]
            assert instance["InstanceId"] == job["runner"]["instance_id"]
            assert instance["ImageId"] == _AMI_ID, \
                "healthy jobs resolve through the jammy env pin"
            assert instance["InstanceType"] == instance_type
