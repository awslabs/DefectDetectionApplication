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
Unit tests for the ``build_dispatcher.resolve_ami`` noble branch and
override scoping (jp7-ephemeral-runner-provisioning task 5.6).

Asserted here:

* noble (24.04/arm64) resolution order: the ``BUILD_ARM64_NOBLE_AMI_ID``
  pin wins over everything; otherwise the canonical noble SSM parameter
  (the ``ebs-gp3`` path) answers; on SSM failure the Canonical-owner
  DescribeImages fallback selects the NEWEST image by CreationDate; the
  release-qualified cache key serves a second call without a second AWS
  call; a resolution finding no image raises a diagnostic naming both
  the release and the architecture;
* override scoping: the jammy ``BUILD_ARM64_AMI_ID``/``BUILD_X86_64_AMI_ID``
  pins never apply to noble resolution, and the noble pin never applies
  to jammy resolution;
* fail-closed unmapped pairings: the ``ValueError`` names both release
  and arch and is raised BEFORE any AWS call;
* ``provision_ephemeral`` converts that unmapped-pairing ``ValueError``
  into a failed job with ``ERROR_PROVISIONING_FAILED`` and a cause
  message carrying the pairing (stubbed persistence), terminating
  partial compute and auditing — the dispatcher tick never crashes.

**Validates: Requirements 2.1, 2.3, 3.2**

--------------------------------------------------------------------------
Safety
--------------------------------------------------------------------------

No test launches EC2 compute, sends an SSM command, or calls real AWS:
``build_dispatcher.ssm``/``build_dispatcher.ec2`` are replaced by
in-process recording stubs before every resolution call, and the
``provision_ephemeral`` drive stubs every persistence seam
(``transition_job``, ``update_job_fields``, ``terminate_partial_compute``,
``complete_effect``) so nothing reaches DynamoDB.

Run ONLY this file, from the repository root::

    python3 -m pytest \\
        test/backend-test/portal_builds/test_jp7_resolve_ami_unit.py \\
        --noconftest -q
"""
import os
import sys
import types
import uuid
from unittest import mock

import pytest
from botocore.exceptions import ClientError

# ---------------------------------------------------------------------------
# Environment BEFORE any import: the handlers bind boto3 clients and
# env-derived settings at import time. No AWS call is ever made; the
# dummy credentials only satisfy client construction.
# ---------------------------------------------------------------------------
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ["AWS_ACCESS_KEY_ID"] = "testing"
os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
os.environ["AWS_SECURITY_TOKEN"] = "testing"
os.environ["AWS_SESSION_TOKEN"] = "testing"

_SUFFIX = "jp7-resolve-ami-unit"
os.environ["BUILD_JOBS_TABLE"] = f"dda-portal-build-jobs-{_SUFFIX}"
os.environ["BUILD_SERVERS_TABLE"] = f"dda-portal-build-servers-{_SUFFIX}"
os.environ["SETTINGS_TABLE"] = f"dda-portal-settings-{_SUFFIX}"

# The AMI env overrides must NOT leak in from the environment: every
# test controls them explicitly through the module globals.
for _var in ("BUILD_ARM64_AMI_ID", "BUILD_X86_64_AMI_ID",
             "BUILD_ARM64_NOBLE_AMI_ID", "ARM64_AMI_SSM_PARAMETER",
             "X86_64_AMI_SSM_PARAMETER", "ARM64_NOBLE_AMI_SSM_PARAMETER",
             "BUILD_REPO_DIR"):
    os.environ.pop(_var, None)

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.normpath(os.path.join(_HERE, "..", "..", ".."))
_FUNCTIONS_DIR = os.path.join(
    _REPO_ROOT, "edge-cv-portal", "backend", "functions")
if _FUNCTIONS_DIR not in sys.path:
    sys.path.insert(0, _FUNCTIONS_DIR)

# ---------------------------------------------------------------------------
# Minimal stand-ins for the Lambda layer modules the handlers import
# (same convention as the sibling portal_builds suites).
# ---------------------------------------------------------------------------
AUDIT_EVENTS = []


def _fake_shared_utils():
    module = types.ModuleType("shared_utils")

    def log_audit_event(**kwargs):
        AUDIT_EVENTS.append(kwargs)

    def create_response(status_code, body):
        return {"statusCode": status_code, "body": body}

    def get_user_from_event(event):
        return {"user_id": "unit-user", "role": "PortalAdmin"}

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


for _module in ("build_domain", "build_planner", "build_dispatcher",
                "build_fleet", "build_source", "build_reconciliation",
                "shared_utils", "rbac_middleware"):
    sys.modules.pop(_module, None)
sys.modules["shared_utils"] = _fake_shared_utils()
sys.modules["rbac_middleware"] = _fake_rbac_middleware()

import build_domain  # noqa: E402
import build_planner  # noqa: E402
import build_dispatcher  # noqa: E402

# ---------------------------------------------------------------------------
# AMI fixtures and recording stubs
# ---------------------------------------------------------------------------

#: What the stubbed 22.04 parameters resolve to (a jammy image id).
JAMMY_SSM_AMI = "ami-0jammy22040000ssm00"
#: What the stubbed noble parameter resolves to (a noble image id).
NOBLE_SSM_AMI = "ami-0noble24040000ssm00"
#: Explicit jammy pins for the 22.04-scoped env overrides.
JAMMY_ARM64_PIN = "ami-0jammyarm64pin00001"
JAMMY_X86_64_PIN = "ami-0jammyx8664pin00001"
#: Explicit noble pin for BUILD_ARM64_NOBLE_AMI_ID.
NOBLE_PIN = "ami-0noblepinned0000001"

#: DescribeImages fallback images, deliberately UNSORTED by
#: CreationDate; the newest is in the middle.
FALLBACK_NEWEST_AMI = "ami-0noblenewest0000001"
FALLBACK_IMAGES = [
    {"ImageId": "ami-0nobleolder00000001",
     "CreationDate": "2025-06-01T00:00:00.000Z"},
    {"ImageId": FALLBACK_NEWEST_AMI,
     "CreationDate": "2026-03-05T12:00:00.000Z"},
    {"ImageId": "ami-0nobleoldest0000001",
     "CreationDate": "2024-01-01T00:00:00.000Z"},
]


class _RecordingSsm:
    """`build_dispatcher.ssm` stand-in: records every GetParameter name
    and serves a jammy or noble stub id keyed on the release segment of
    the requested path. Never touches the network."""

    def __init__(self):
        self.get_parameter_names = []

    def get_parameter(self, **kwargs):
        name = kwargs.get("Name", "")
        self.get_parameter_names.append(name)
        value = NOBLE_SSM_AMI if "24.04" in name else JAMMY_SSM_AMI
        return {"Parameter": {"Value": value}}

    def __getattr__(self, item):  # pragma: no cover - guard rail
        raise AssertionError(
            f"unexpected SSM operation '{item}' during AMI resolution")


class _FailingSsm(_RecordingSsm):
    """SSM stub whose GetParameter fails with a ClientError, forcing the
    noble DescribeImages fallback (still recording the attempt)."""

    def get_parameter(self, **kwargs):
        self.get_parameter_names.append(kwargs.get("Name", ""))
        raise ClientError(
            {"Error": {"Code": "ParameterNotFound",
                       "Message": "no such parameter"}},
            "GetParameter")


class _RecordingEc2:
    """`build_dispatcher.ec2` stand-in: records DescribeImages calls and
    serves a configurable image list. Any other operation (RunInstances
    in particular) trips the guard rail."""

    def __init__(self, images=None):
        self.describe_images_calls = []
        self._images = FALLBACK_IMAGES if images is None else images

    def describe_images(self, **kwargs):
        self.describe_images_calls.append(dict(kwargs))
        return {"Images": [dict(image) for image in self._images]}

    def __getattr__(self, item):  # pragma: no cover - guard rail
        raise AssertionError(
            f"unexpected EC2 operation '{item}' — no instance may be "
            f"launched by these tests")


def _fresh_resolution(ssm_stub=None, ec2_stub=None):
    """Reset every AMI-resolution seam: recording stubs installed, the
    per-container cache emptied, and every env-override module global
    cleared."""
    ssm_stub = ssm_stub if ssm_stub is not None else _RecordingSsm()
    ec2_stub = ec2_stub if ec2_stub is not None else _RecordingEc2()
    build_dispatcher.ssm = ssm_stub
    build_dispatcher.ec2 = ec2_stub
    build_dispatcher._AMI_CACHE.clear()
    build_dispatcher.BUILD_ARM64_AMI_ID = ""
    build_dispatcher.BUILD_X86_64_AMI_ID = ""
    build_dispatcher.BUILD_ARM64_NOBLE_AMI_ID = ""
    return ssm_stub, ec2_stub


NOBLE = "24.04"
JAMMY = "22.04"
ARM64 = build_domain.ARCH_ARM64
X86_64 = build_domain.ARCH_X86_64


# ===========================================================================
# Noble branch resolution order (Req 2.1, 3.2)
# ===========================================================================

class TestNobleBranchResolution:

    def test_ssm_parameter_success_returns_parameter_ami(self):
        """The canonical noble parameter's value is returned, read from
        the 24.04 ebs-gp3 path, with no DescribeImages fallback."""
        ssm_stub, ec2_stub = _fresh_resolution()
        ami = build_dispatcher.resolve_ami(ARM64, NOBLE)
        assert ami == NOBLE_SSM_AMI
        assert ssm_stub.get_parameter_names == \
            [build_dispatcher.ARM64_NOBLE_AMI_SSM_PARAMETER]
        requested = ssm_stub.get_parameter_names[0]
        assert "24.04" in requested and "ebs-gp3" in requested
        assert ec2_stub.describe_images_calls == []

    def test_describe_images_fallback_selects_newest_by_creation_date(self):
        """On SSM failure the Canonical-owner DescribeImages fallback
        answers, selecting the NEWEST image by CreationDate from an
        unsorted response."""
        ssm_stub, ec2_stub = _fresh_resolution(ssm_stub=_FailingSsm())
        ami = build_dispatcher.resolve_ami(ARM64, NOBLE)
        assert ami == FALLBACK_NEWEST_AMI
        assert len(ec2_stub.describe_images_calls) == 1
        call = ec2_stub.describe_images_calls[0]
        assert call["Owners"] == [build_dispatcher.CANONICAL_OWNER_ID]
        name_values = [f["Values"] for f in call["Filters"]
                       if f["Name"] == "name"][0]
        assert name_values == [build_dispatcher.ARM64_NOBLE_AMI_NAME_FILTER]
        assert "hvm-ssd-gp3" in name_values[0] and "noble" in name_values[0]

    def test_noble_pin_takes_precedence_over_ssm_and_fallback(self):
        """BUILD_ARM64_NOBLE_AMI_ID wins over the parameter AND the
        fallback: zero AWS calls are made when it is set."""
        ssm_stub, ec2_stub = _fresh_resolution()
        build_dispatcher.BUILD_ARM64_NOBLE_AMI_ID = NOBLE_PIN
        ami = build_dispatcher.resolve_ami(ARM64, NOBLE)
        assert ami == NOBLE_PIN
        assert ssm_stub.get_parameter_names == []
        assert ec2_stub.describe_images_calls == []

    def test_noble_cache_hit_serves_second_call_without_aws(self):
        """The release-qualified cache key serves a second resolution
        without a second AWS call."""
        ssm_stub, ec2_stub = _fresh_resolution()
        first = build_dispatcher.resolve_ami(ARM64, NOBLE)
        second = build_dispatcher.resolve_ami(ARM64, NOBLE)
        assert (first, second) == (NOBLE_SSM_AMI, NOBLE_SSM_AMI)
        assert len(ssm_stub.get_parameter_names) == 1
        assert ec2_stub.describe_images_calls == []

    def test_fallback_result_is_cached_too(self):
        """A fallback-resolved id is cached: the second call makes no
        further SSM or EC2 call."""
        ssm_stub, ec2_stub = _fresh_resolution(ssm_stub=_FailingSsm())
        first = build_dispatcher.resolve_ami(ARM64, NOBLE)
        second = build_dispatcher.resolve_ami(ARM64, NOBLE)
        assert first == second == FALLBACK_NEWEST_AMI
        assert len(ssm_stub.get_parameter_names) == 1
        assert len(ec2_stub.describe_images_calls) == 1

    def test_no_image_resolution_raises_naming_release_and_arch(self):
        """SSM failing and DescribeImages returning zero images raises a
        diagnostic naming BOTH the release and the architecture."""
        _fresh_resolution(ssm_stub=_FailingSsm(),
                          ec2_stub=_RecordingEc2(images=[]))
        with pytest.raises(ValueError) as excinfo:
            build_dispatcher.resolve_ami(ARM64, NOBLE)
        message = str(excinfo.value)
        assert NOBLE in message and ARM64 in message


# ===========================================================================
# Override scoping (Req 3.2)
# ===========================================================================

class TestOverrideScoping:

    def test_jammy_overrides_do_not_apply_to_noble_resolution(self):
        """With BOTH jammy pins set, noble resolution still resolves
        through the noble parameter — never a jammy pin."""
        ssm_stub, _ = _fresh_resolution()
        build_dispatcher.BUILD_ARM64_AMI_ID = JAMMY_ARM64_PIN
        build_dispatcher.BUILD_X86_64_AMI_ID = JAMMY_X86_64_PIN
        ami = build_dispatcher.resolve_ami(ARM64, NOBLE)
        assert ami == NOBLE_SSM_AMI
        assert ami not in (JAMMY_ARM64_PIN, JAMMY_X86_64_PIN)
        assert ssm_stub.get_parameter_names == \
            [build_dispatcher.ARM64_NOBLE_AMI_SSM_PARAMETER]

    @pytest.mark.parametrize("arch,jammy_parameter_attr", [
        (ARM64, "ARM64_AMI_SSM_PARAMETER"),
        (X86_64, "X86_64_AMI_SSM_PARAMETER"),
    ])
    def test_noble_override_does_not_apply_to_jammy_resolution(
            self, arch, jammy_parameter_attr):
        """With the noble pin set, 22.04 resolution for BOTH
        architectures still reads its own 22.04 parameter."""
        ssm_stub, _ = _fresh_resolution()
        build_dispatcher.BUILD_ARM64_NOBLE_AMI_ID = NOBLE_PIN
        ami = build_dispatcher.resolve_ami(arch, JAMMY)
        assert ami == JAMMY_SSM_AMI
        assert ami != NOBLE_PIN
        assert ssm_stub.get_parameter_names == \
            [getattr(build_dispatcher, jammy_parameter_attr)]

    def test_jammy_pins_still_scoped_to_jammy(self):
        """Sanity: the jammy pins DO keep answering 22.04 resolution
        (their existing scope, Req 3.2) with zero AWS calls."""
        ssm_stub, ec2_stub = _fresh_resolution()
        build_dispatcher.BUILD_ARM64_AMI_ID = JAMMY_ARM64_PIN
        build_dispatcher.BUILD_X86_64_AMI_ID = JAMMY_X86_64_PIN
        assert build_dispatcher.resolve_ami(ARM64, JAMMY) == JAMMY_ARM64_PIN
        assert build_dispatcher.resolve_ami(X86_64, JAMMY) == JAMMY_X86_64_PIN
        assert ssm_stub.get_parameter_names == []
        assert ec2_stub.describe_images_calls == []


# ===========================================================================
# Fail-closed unmapped pairings (Req 2.3)
# ===========================================================================

class TestUnmappedPairings:

    @pytest.mark.parametrize("arch,os_release", [
        (X86_64, "24.04"),   # noble is arm64-only on the ephemeral path
        (ARM64, "20.04"),    # unmapped release
        (ARM64, "26.04"),    # unmapped release
        ("riscv64", "22.04"),  # unmapped architecture
        ("", ""),            # degenerate pairing
    ], ids=["noble-x86_64", "arm64-20.04", "arm64-26.04",
            "riscv64-jammy", "empty-empty"])
    def test_message_names_release_and_arch_before_any_aws_call(
            self, arch, os_release):
        """The ValueError names BOTH the release and the arch, and the
        recording stubs saw ZERO AWS calls."""
        ssm_stub, ec2_stub = _fresh_resolution()
        with pytest.raises(ValueError) as excinfo:
            build_dispatcher.resolve_ami(arch, os_release)
        message = str(excinfo.value)
        assert os_release in message and arch in message
        assert ssm_stub.get_parameter_names == []
        assert ec2_stub.describe_images_calls == []

    def test_overrides_never_leak_into_unmapped_pairings(self):
        """Even with every pin set, an unmapped pairing still fails
        closed — no pin of a different OS release is returned."""
        ssm_stub, ec2_stub = _fresh_resolution()
        build_dispatcher.BUILD_ARM64_AMI_ID = JAMMY_ARM64_PIN
        build_dispatcher.BUILD_X86_64_AMI_ID = JAMMY_X86_64_PIN
        build_dispatcher.BUILD_ARM64_NOBLE_AMI_ID = NOBLE_PIN
        with pytest.raises(ValueError):
            build_dispatcher.resolve_ami(X86_64, "24.04")
        assert ssm_stub.get_parameter_names == []
        assert ec2_stub.describe_images_calls == []


# ===========================================================================
# provision_ephemeral converts the unmapped-pairing ValueError into a
# failed job (Req 2.3): ERROR_PROVISIONING_FAILED, cause carrying the
# pairing, partial compute terminated, audited, tick never crashes.
# ===========================================================================

NOW = 1_760_000_000_000


class TestProvisionEphemeralFailsClosed:

    def _drive_unmapped_provisioning(self):
        """Drive the REAL provision_ephemeral over one queued ephemeral
        job whose plan carries an unmapped (24.04, x86_64) pairing, with
        every persistence seam stubbed. Returns the recorders."""
        ssm_stub, ec2_stub = _fresh_resolution()
        del AUDIT_EVENTS[:]
        job_id = str(uuid.uuid4())
        job = {
            "build_job_id": job_id,
            "build_target": "JP7",
            "execution_mode": build_domain.EXECUTION_MODE_EPHEMERAL,
            "status": build_domain.STATUS_QUEUED,
            "config_snapshot": {},
        }
        # An unmapped pairing on the plan: resolve_ami must fail closed
        # inside run_runner_instance BEFORE any AWS call.
        plan = build_planner.RunnerPlan(
            build_job_id=job_id,
            arch=X86_64,
            instance_type="m6i.4xlarge",
            volume_size_gb=100,
            spot=False,
            status=build_domain.STATUS_PROVISIONING,
            os_release="24.04",
        )
        transitions = []
        field_updates = []
        terminations = []

        def fake_transition(build_job_id, expected, new, extra=None):
            transitions.append((build_job_id, expected, new, extra))
            return True

        def fake_update(build_job_id, fields):
            field_updates.append((build_job_id, fields))

        def fake_terminate(build_job_id):
            terminations.append(build_job_id)
            return ["i-0partialcompute0001"]

        # Rebind the audit recorder AT DRIVE TIME: pytest imports every
        # test module before running any test, and a later-collected
        # module (test_no_live_validation_contract.py) rebinds
        # ``build_dispatcher.log_audit_event`` on the shared module
        # instance, redirecting audits away from this file's
        # AUDIT_EVENTS. Patching inside the drive makes this recorder
        # authoritative regardless of collection order.
        def recording_log_audit_event(**kwargs):
            AUDIT_EVENTS.append(kwargs)

        with mock.patch.object(build_planner, "plan_ephemeral_provisioning",
                               return_value=[plan]), \
                mock.patch.object(build_dispatcher, "log_audit_event",
                                  side_effect=recording_log_audit_event), \
                mock.patch.object(build_dispatcher, "transition_job",
                                  side_effect=fake_transition), \
                mock.patch.object(build_dispatcher, "update_job_fields",
                                  side_effect=fake_update), \
                mock.patch.object(build_dispatcher,
                                  "terminate_partial_compute",
                                  side_effect=fake_terminate), \
                mock.patch.object(build_dispatcher, "complete_effect",
                                  return_value=True):
            # Never crashes the dispatcher tick: any escape of the
            # ValueError would propagate and fail this call.
            build_dispatcher.provision_ephemeral([job], NOW)
        return (job_id, transitions, field_updates, terminations,
                ssm_stub, ec2_stub)

    def test_unmapped_pairing_fails_job_with_provisioning_error(self):
        job_id, transitions, field_updates, terminations, ssm_stub, \
            ec2_stub = self._drive_unmapped_provisioning()

        # queued -> provisioning, then provisioning -> failed; the
        # terminal write carries ERROR_PROVISIONING_FAILED and a cause
        # naming the unmapped pairing.
        assert [(t[1], t[2]) for t in transitions] == [
            (build_domain.STATUS_QUEUED, build_domain.STATUS_PROVISIONING),
            (build_domain.STATUS_PROVISIONING, build_domain.STATUS_FAILED),
        ]
        terminal_extra = transitions[1][3]
        error = terminal_extra["error"]
        assert error["code"] == build_dispatcher.ERROR_PROVISIONING_FAILED
        assert "24.04" in error["message"] and X86_64 in error["message"]
        assert "ended_at" in terminal_extra

        # Partial compute terminated for exactly this job.
        assert terminations == [job_id]

        # No runner record was written for the failed job (the loop
        # `continue`s before update_job_fields).
        assert field_updates == []

        # Zero AWS calls: no SSM read, no DescribeImages — and the EC2
        # guard rail proves no RunInstances was attempted.
        assert ssm_stub.get_parameter_names == []
        assert ec2_stub.describe_images_calls == []

    def test_unmapped_pairing_failure_is_audited(self):
        job_id, _, _, _, _, _ = self._drive_unmapped_provisioning()
        provisioning_audits = [
            event for event in AUDIT_EVENTS
            if event.get("action") == "build_provisioning_failed"
            and event.get("resource_id") == job_id]
        assert len(provisioning_audits) == 1
        audit_event = provisioning_audits[0]
        assert audit_event.get("result") == "failure"
        details = audit_event.get("details") or {}
        assert details.get("error_code") == \
            build_dispatcher.ERROR_PROVISIONING_FAILED
        assert "24.04" in details.get("message", "")
        assert X86_64 in details.get("message", "")
        assert details.get("terminated_partial_compute") == \
            ["i-0partialcompute0001"]
