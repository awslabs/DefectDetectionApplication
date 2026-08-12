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
FIX-CHECKING PROPERTY TESTS for jp7-ephemeral-runner-provisioning.

This file hosts the bug-condition (fix-checking) properties that run
against the FIXED code (tasks 5.1, 5.2 and 5.4 of the implementation
plan share this module; each property lives in its own test class).

Task 5.1 — **Property 1: Target-aware OS release on the ephemeral
plan**: for any supported build target planned as an ephemeral job,
`plan_runner` yields a plan whose ``(os_release, arch)`` matches a
frozen oracle deliberately re-spelled in this file — JP7 ->
('24.04', 'arm64'), every other target -> ('22.04', its required
arch) — and for a JP7 ephemeral job `resolve_ami(plan.arch,
plan.os_release)` resolves through the noble arm64 conventions (the
canonical noble SSM parameter with the ``ebs-gp3`` segment, or on SSM
failure the Canonical-owner DescribeImages fallback with the
``hvm-ssd-gp3`` noble name filter), never through any 22.04 parameter
or the jammy BUILD_ARM64_AMI_ID env override.

**Validates: Requirements 2.1, 2.2**

Task 5.2 — **Property 2: Fail-closed AMI resolution for unmapped
pairings**: for any generated ``(os_release, arch)`` pairing outside
{('22.04','arm64'), ('22.04','x86_64'), ('24.04','arm64')} (including
('24.04','x86_64'), arbitrary release strings, and arbitrary arch
strings), `resolve_ami` raises an error whose message names BOTH the
release and the architecture, the recording stubs show ZERO AWS calls
(no SSM GetParameter, no DescribeImages, no RunInstances), the raised
type is caught by `provision_ephemeral`'s (ClientError, ValueError)
handler, and no fallback AMI of a different OS release is ever
returned.

**Validates: Requirements 2.3**

--------------------------------------------------------------------------
Safety
--------------------------------------------------------------------------

No test launches EC2 compute, sends an SSM command, calls real AWS, or
starts a build. `build_dispatcher.ssm`/`build_dispatcher.ec2` are
replaced by in-process recording stubs before any resolution call, and
`plan_runner` is pure.

Run ONLY this file, from the repository root:

    python3 -m pytest \\
        test/backend-test/portal_builds/test_jp7_ephemeral_provisioning_properties.py \\
        --noconftest -q

(This run contains property-based tests and may generate/shrink
counterexamples.)
"""
import os
import sys
import types
import uuid

import pytest
from botocore.exceptions import ClientError
from hypothesis import given, settings, strategies as st

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

_SUFFIX = "jp7-provisioning-properties"
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
# (same convention as the sibling suites in this directory).
# ---------------------------------------------------------------------------
AUDIT_EVENTS = []


def _fake_shared_utils():
    module = types.ModuleType("shared_utils")

    def log_audit_event(**kwargs):
        AUDIT_EVENTS.append(kwargs)

    def create_response(status_code, body):
        return {"statusCode": status_code, "body": body}

    def get_user_from_event(event):
        return {"user_id": "properties-user", "role": "PortalAdmin"}

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
# Recording AWS stubs and AMI fixtures (module-level harness shared by
# every property class in this file).
# ---------------------------------------------------------------------------

#: What any stubbed 22.04 parameter resolves to (a jammy image id).
JAMMY_STUB_AMI = "ami-0jammy22040000stub"
#: What any stubbed 24.04 parameter / noble DescribeImages resolves to.
NOBLE_STUB_AMI = "ami-0noble24040000stub"
#: A jammy pin for the BUILD_ARM64_AMI_ID env override.
JAMMY_PIN_AMI = "ami-0jammypinned0000001"

#: Frozen oracle values re-spelled here (NOT imported from production
#: code): the Canonical owner id and the noble arm64 name filter the
#: DescribeImages fallback must carry (design Property 1).
FROZEN_CANONICAL_OWNER_ID = "099720109477"
FROZEN_NOBLE_NAME_FILTER_SEGMENTS = ("hvm-ssd-gp3", "noble", "24.04",
                                     "arm64")


class _RecordingSsm:
    """`build_dispatcher.ssm` stand-in: records every GetParameter name
    and serves a jammy or noble stub id keyed on the release segment of
    the requested path. Never touches the network."""

    def __init__(self):
        self.get_parameter_names = []

    def get_parameter(self, **kwargs):
        name = kwargs.get("Name", "")
        self.get_parameter_names.append(name)
        value = NOBLE_STUB_AMI if "24.04" in name else JAMMY_STUB_AMI
        return {"Parameter": {"Value": value}}

    def __getattr__(self, item):  # pragma: no cover - guard rail
        raise AssertionError(
            f"unexpected SSM operation '{item}' during AMI resolution")


class _FailingSsm:
    """`build_dispatcher.ssm` stand-in whose GetParameter always fails
    with a ClientError — the condition under which noble resolution must
    take the Canonical-owner DescribeImages fallback."""

    def __init__(self):
        self.get_parameter_names = []

    def get_parameter(self, **kwargs):
        name = kwargs.get("Name", "")
        self.get_parameter_names.append(name)
        raise ClientError(
            {"Error": {"Code": "ParameterNotFound",
                       "Message": f"Parameter {name} not found."}},
            "GetParameter")

    def __getattr__(self, item):  # pragma: no cover - guard rail
        raise AssertionError(
            f"unexpected SSM operation '{item}' during AMI resolution")


class _RecordingEc2:
    """`build_dispatcher.ec2` stand-in for the DescribeImages fallback:
    records every call and serves one noble image."""

    def __init__(self):
        self.describe_images_calls = []

    def describe_images(self, **kwargs):
        self.describe_images_calls.append(dict(kwargs))
        return {"Images": [{"ImageId": NOBLE_STUB_AMI,
                            "CreationDate": "2026-01-01T00:00:00.000Z"}]}

    def __getattr__(self, item):  # pragma: no cover - guard rail
        raise AssertionError(
            f"unexpected EC2 operation '{item}' during AMI resolution")


def _fresh_ami_resolution(ssm_fails=False):
    """Reset every AMI-resolution seam: recording stubs installed, the
    per-container cache emptied, and every env-override module global
    cleared."""
    ssm_stub = _FailingSsm() if ssm_fails else _RecordingSsm()
    ec2_stub = _RecordingEc2()
    build_dispatcher.ssm = ssm_stub
    build_dispatcher.ec2 = ec2_stub
    build_dispatcher._AMI_CACHE.clear()
    build_dispatcher.BUILD_ARM64_AMI_ID = ""
    build_dispatcher.BUILD_X86_64_AMI_ID = ""
    build_dispatcher.BUILD_ARM64_NOBLE_AMI_ID = ""
    return ssm_stub, ec2_stub


# ---------------------------------------------------------------------------
# Generated inputs: whole config_snapshots (sizing, spot flags, region)
# are generated; the target set is the domain's supported five.
# ---------------------------------------------------------------------------

_REGIONS = st.sampled_from(["us-east-1", "us-west-2", "eu-central-1"])
_CONFIG_SNAPSHOTS = st.fixed_dictionaries({}, optional={
    "arm64_instance_type": st.sampled_from(
        ["m6g.4xlarge", "c7g.8xlarge", "r6g.2xlarge"]),
    "x86_64_instance_type": st.sampled_from(
        ["m6i.4xlarge", "c6i.8xlarge"]),
    "volume_size_gb": st.integers(min_value=50, max_value=500),
    "use_spot_for_ephemeral": st.booleans(),
    "max_runtime_hours": st.integers(min_value=1, max_value=12),
    "region": _REGIONS,
})


def _ephemeral_job(target, config_snapshot):
    return {
        "build_job_id": str(uuid.uuid4()),
        "build_target": target,
        "execution_mode": build_domain.EXECUTION_MODE_EPHEMERAL,
        "config_snapshot": dict(config_snapshot),
    }


# ===========================================================================
# Property 1: Target-aware OS release on the ephemeral plan
#
# **Feature: jp7-ephemeral-runner-provisioning, Property 1: Target-aware
# OS release on the ephemeral plan**
# **Validates: Requirements 2.1, 2.2**
# ===========================================================================

#: The frozen (os_release, arch) oracle, deliberately RE-SPELLED here
#: rather than derived from build_domain's table, so a regression in the
#: domain table cannot silently rewrite the expectation (design
#: Property 1).
FROZEN_PLAN_ORACLE = {
    "JP5": ("22.04", "arm64"),
    "JP6": ("22.04", "arm64"),
    "JP7": ("24.04", "arm64"),
    "AMD64": ("22.04", "x86_64"),
    "AMD64_NVIDIA": ("22.04", "x86_64"),
}


class TestProperty1PlanOsReleaseMatchesFrozenOracle:
    """`plan_runner` derives the (os_release, arch) pairing per target."""

    @settings(max_examples=100, deadline=None)
    @given(target=st.sampled_from(sorted(FROZEN_PLAN_ORACLE)),
           config_snapshot=_CONFIG_SNAPSHOTS)
    def test_plan_release_and_arch_match_frozen_oracle(
            self, target, config_snapshot):
        """For any supported build target planned as an ephemeral job
        (generated config_snapshots, sizing, spot flags), the plan's
        (os_release, arch) equals the frozen oracle: JP7 ->
        ('24.04', 'arm64'), every other target -> ('22.04', its
        required arch).

        **Validates: Requirements 2.1, 2.2**"""
        plan = build_planner.plan_runner(
            _ephemeral_job(target, config_snapshot))
        assert (plan.os_release, plan.arch) == FROZEN_PLAN_ORACLE[target], (
            f"plan for {target} carries "
            f"(os_release, arch)=({plan.os_release!r}, {plan.arch!r}), "
            f"expected {FROZEN_PLAN_ORACLE[target]!r} "
            f"(plan fields: {plan._asdict()})"
        )


class TestProperty1NobleResolutionForJp7:
    """`resolve_ami(plan.arch, plan.os_release)` for a JP7 ephemeral job
    resolves through the noble arm64 conventions only."""

    @settings(max_examples=100, deadline=None)
    @given(config_snapshot=_CONFIG_SNAPSHOTS, jammy_pin_set=st.booleans())
    def test_jp7_resolves_through_noble_ssm_parameter(
            self, config_snapshot, jammy_pin_set):
        """With SSM healthy, the recording stub sees a parameter path
        containing '24.04' and the 'ebs-gp3' segment; NO 22.04 parameter
        is read; and the jammy BUILD_ARM64_AMI_ID override is never
        honored even when set.

        **Validates: Requirements 2.1, 2.2**"""
        ssm_stub, ec2_stub = _fresh_ami_resolution()
        if jammy_pin_set:
            build_dispatcher.BUILD_ARM64_AMI_ID = JAMMY_PIN_AMI

        plan = build_planner.plan_runner(
            _ephemeral_job("JP7", config_snapshot))
        ami = build_dispatcher.resolve_ami(plan.arch, plan.os_release)

        requested = list(ssm_stub.get_parameter_names)
        noble_reads = [name for name in requested
                       if "24.04" in name and "ebs-gp3" in name]
        jammy_reads = [name for name in requested if "22.04" in name]
        assert noble_reads and not jammy_reads, (
            f"JP7 resolution read SSM parameters {requested}; expected "
            "exactly the noble path (containing '24.04' and 'ebs-gp3') "
            "and no 22.04 parameter"
        )
        assert not ec2_stub.describe_images_calls, (
            "DescribeImages fallback taken although the noble SSM "
            "parameter resolved"
        )
        assert ami == NOBLE_STUB_AMI, (
            f"JP7 resolution returned {ami!r}, expected the noble stub "
            f"{NOBLE_STUB_AMI!r}"
        )
        assert ami != JAMMY_PIN_AMI, (
            "the jammy BUILD_ARM64_AMI_ID override was honored for a "
            "JP7 (noble) plan"
        )

    @settings(max_examples=100, deadline=None)
    @given(config_snapshot=_CONFIG_SNAPSHOTS, jammy_pin_set=st.booleans())
    def test_jp7_falls_back_to_canonical_describe_images_on_ssm_failure(
            self, config_snapshot, jammy_pin_set):
        """On SSM failure, resolution takes a DescribeImages call
        carrying the Canonical owner id and the hvm-ssd-gp3 noble name
        filter; NO 22.04 parameter is read; and the jammy
        BUILD_ARM64_AMI_ID override is never honored even when set.

        **Validates: Requirements 2.1, 2.2**"""
        ssm_stub, ec2_stub = _fresh_ami_resolution(ssm_fails=True)
        if jammy_pin_set:
            build_dispatcher.BUILD_ARM64_AMI_ID = JAMMY_PIN_AMI

        plan = build_planner.plan_runner(
            _ephemeral_job("JP7", config_snapshot))
        ami = build_dispatcher.resolve_ami(plan.arch, plan.os_release)

        requested = list(ssm_stub.get_parameter_names)
        jammy_reads = [name for name in requested if "22.04" in name]
        assert not jammy_reads, (
            f"JP7 resolution read a 22.04 SSM parameter: {jammy_reads}"
        )
        assert len(ec2_stub.describe_images_calls) == 1, (
            "expected exactly one DescribeImages fallback call on SSM "
            f"failure, saw {len(ec2_stub.describe_images_calls)}"
        )
        call = ec2_stub.describe_images_calls[0]
        assert call.get("Owners") == [FROZEN_CANONICAL_OWNER_ID], (
            f"DescribeImages owners {call.get('Owners')!r} != "
            f"[{FROZEN_CANONICAL_OWNER_ID!r}] (Canonical)"
        )
        name_filter_values = [
            value
            for filter_entry in call.get("Filters", [])
            if filter_entry.get("Name") == "name"
            for value in filter_entry.get("Values", [])
        ]
        assert name_filter_values and all(
            segment in name_filter_values[0]
            for segment in FROZEN_NOBLE_NAME_FILTER_SEGMENTS), (
            f"DescribeImages name filter {name_filter_values!r} does not "
            "carry the noble arm64 conventions "
            f"{FROZEN_NOBLE_NAME_FILTER_SEGMENTS}"
        )
        assert ami == NOBLE_STUB_AMI, (
            f"JP7 fallback resolution returned {ami!r}, expected the "
            f"noble stub {NOBLE_STUB_AMI!r}"
        )
        assert ami != JAMMY_PIN_AMI, (
            "the jammy BUILD_ARM64_AMI_ID override was honored for a "
            "JP7 (noble) plan"
        )


# ===========================================================================
# Property 2: Fail-closed AMI resolution for unmapped pairings
#
# **Feature: jp7-ephemeral-runner-provisioning, Property 2: Fail-closed
# AMI resolution for unmapped pairings**
# **Validates: Requirements 2.3**
# ===========================================================================

#: The mapped (os_release, arch) pairings, deliberately RE-SPELLED here
#: (design Property 2): everything outside this set must fail closed.
MAPPED_PAIRINGS = frozenset({
    ("22.04", "arm64"),
    ("22.04", "x86_64"),
    ("24.04", "arm64"),
})

#: Printable, non-empty arbitrary strings so the "message names BOTH the
#: release and the architecture" assertion is never vacuous (an empty
#: string is a substring of any message).
_ARBITRARY_TOKEN = st.text(
    alphabet=st.characters(min_codepoint=33, max_codepoint=126),
    min_size=1, max_size=24)

_KNOWN_RELEASES = st.sampled_from(["22.04", "24.04"])
_KNOWN_ARCHES = st.sampled_from(["arm64", "x86_64"])

#: Unmapped (os_release, arch) pairings: the concrete noble/x86_64 hole,
#: arbitrary release strings against known arches, known releases
#: against arbitrary arch strings, and fully arbitrary pairings — all
#: filtered against the mapped set.
_UNMAPPED_PAIRINGS = st.one_of(
    st.just(("24.04", "x86_64")),
    st.tuples(_ARBITRARY_TOKEN, _KNOWN_ARCHES),
    st.tuples(_KNOWN_RELEASES, _ARBITRARY_TOKEN),
    st.tuples(_ARBITRARY_TOKEN, _ARBITRARY_TOKEN),
).filter(lambda pairing: pairing not in MAPPED_PAIRINGS)


class TestProperty2FailClosedAmiResolutionForUnmappedPairings:
    """`resolve_ami` fails closed on any unmapped (os_release, arch)
    pairing: a diagnostic error naming both dimensions, zero AWS calls,
    an exception type `provision_ephemeral`'s handler converts into a
    failed job, and never a fallback AMI of a different OS release."""

    @settings(max_examples=100, deadline=None)
    @given(pairing=_UNMAPPED_PAIRINGS, overrides_set=st.booleans())
    def test_unmapped_pairing_raises_before_any_aws_call(
            self, pairing, overrides_set):
        """For any (os_release, arch) pairing outside
        {('22.04','arm64'), ('22.04','x86_64'), ('24.04','arm64')},
        `resolve_ami` raises an error whose message names BOTH the
        release and the architecture; the recording stubs see ZERO AWS
        calls (no SSM GetParameter, no DescribeImages, no RunInstances —
        the stubs' __getattr__ guard rails turn any other AWS operation
        into an AssertionError that would escape the expected-exception
        check below); the raised type is caught by
        `provision_ephemeral`'s (ClientError, ValueError) handler; and
        no fallback AMI of a different OS release is ever returned,
        even with every AMI env pin set.

        **Validates: Requirements 2.3**"""
        os_release, arch = pairing
        ssm_stub, ec2_stub = _fresh_ami_resolution()
        if overrides_set:
            # Even with every pin set, an unmapped pairing must never
            # come back as a pinned AMI of a different OS release.
            build_dispatcher.BUILD_ARM64_AMI_ID = JAMMY_PIN_AMI
            build_dispatcher.BUILD_X86_64_AMI_ID = JAMMY_PIN_AMI
            build_dispatcher.BUILD_ARM64_NOBLE_AMI_ID = NOBLE_STUB_AMI

        # pytest.raises with exactly (ClientError, ValueError) asserts
        # both that resolution fails closed (no AMI returned) and that
        # the raised type is one provision_ephemeral's handler catches
        # and routes to fail_provisioning. Any other type — including
        # build_fleet's RuntimeError convention, which would crash the
        # dispatcher tick — fails this test.
        with pytest.raises((ClientError, ValueError)) as exc_info:
            build_dispatcher.resolve_ami(arch, os_release)

        message = str(exc_info.value)
        assert os_release in message, (
            f"error for unmapped pairing ({os_release!r}, {arch!r}) does "
            f"not name the OS release: {message!r}"
        )
        assert arch in message, (
            f"error for unmapped pairing ({os_release!r}, {arch!r}) does "
            f"not name the architecture: {message!r}"
        )

        assert ssm_stub.get_parameter_names == [], (
            "unmapped pairing reached SSM GetParameter before failing "
            f"closed: {ssm_stub.get_parameter_names}"
        )
        assert ec2_stub.describe_images_calls == [], (
            "unmapped pairing reached EC2 DescribeImages before failing "
            f"closed: {ec2_stub.describe_images_calls}"
        )


# ===========================================================================
# Property 6: Noble deltas live once in the shared bootstrap script and
# both seams reach them
#
# **Feature: jp7-ephemeral-runner-provisioning, Property 6: Noble deltas
# live once in the shared bootstrap script and both seams reach them**
# **Validates: Requirements 2.6, 2.8**
# ===========================================================================

import inspect  # noqa: E402
import re  # noqa: E402
from unittest import mock  # noqa: E402

import build_fleet  # noqa: E402

#: The one shared bootstrap seam both execution modes execute (Req 2.8).
SETUP_SCRIPT_PATH = os.path.join(_REPO_ROOT, "setup-build-server.sh")

#: The delta texts that must live ONLY in setup-build-server.sh: any of
#: these appearing in a rendered user-data body would be the second,
#: divergent copy Req 2.8 prohibits.
DELTA_MARKERS = (
    "--break-system-packages",
    "exec docker compose",
    "PIP_BREAK_FLAG",
    "/usr/local/bin/docker-compose",
)

_PEP668_FLAG = "--break-system-packages"
_RELEASE_KEY_PATTERN = re.compile(
    r'if\s+\[\s+"\$OS_RELEASE"\s+=\s+"24\.04"\s+\]')


def _script_text():
    with open(SETUP_SCRIPT_PATH, "r", encoding="utf-8") as handle:
        return handle.read()


def _non_comment_lines(script):
    return [line for line in script.splitlines()
            if not line.strip().startswith("#")]


def _pip_install_lines(script):
    """The three PEP 668-affected install command lines of
    setup-build-server.sh, keyed by package."""
    lines = {}
    for line in _non_comment_lines(script):
        if "pip3 install" not in line:
            continue
        if "awscli" in line:
            lines["awscli"] = line.strip()
        elif "botocore[crt]" in line:
            lines["botocore[crt]"] = line.strip()
        elif "aws-greengrass-gdk-cli" in line:
            lines["gdk-cli"] = line.strip()
    return lines


def _release_branch_split(script):
    """(then_branch, else_branch) of the docker-compose section's
    `if [ "$OS_RELEASE" = "24.04" ]` — the detected-24.04 branch and the
    22.04 (snap) branch. Nested `if`/`fi` pairs are tracked so inner
    run_cmd guards do not confuse the split."""
    lines = script.splitlines()
    start = next((index for index, line in enumerate(lines)
                  if _RELEASE_KEY_PATTERN.search(line)
                  and "docker-compose" in "\n".join(
                      lines[max(0, index - 10):index + 10])
                  and "PIP_BREAK_FLAG" not in "\n".join(
                      lines[index:index + 4])), None)
    assert start is not None, (
        "setup-build-server.sh has no docker-compose block keyed on the "
        'detected release (`if [ "$OS_RELEASE" = "24.04" ]`)'
    )
    depth = 1
    then_branch, else_branch = [], []
    current = then_branch
    for line in lines[start + 1:]:
        stripped = line.strip()
        if stripped.startswith("if "):
            depth += 1
        elif stripped == "fi" or stripped.startswith("fi "):
            depth -= 1
            if depth == 0:
                break
        elif stripped == "else" and depth == 1:
            current = else_branch
            continue
        current.append(line)
    assert depth == 0, (
        "unbalanced if/fi while parsing the docker-compose release branch"
    )
    return "\n".join(then_branch), "\n".join(else_branch)


class TestProperty6NobleDeltasLiveOnceInTheSharedScript:
    """The fixed setup-build-server.sh carries exactly ONE implementation
    of the noble deltas, keyed on the detected OS release of the host it
    runs on (static text assertions on the checked-in shared seam).

    **Feature: jp7-ephemeral-runner-provisioning, Property 6: Noble
    deltas live once in the shared bootstrap script and both seams reach
    them**
    **Validates: Requirements 2.6, 2.8**"""

    def test_detected_2404_branch_writes_the_executable_shim(self):
        """Under the detected-24.04 branch, /usr/local/bin/docker-compose
        is written with `exec docker compose "$@"` and made executable,
        and the snap docker-compose install is unreachable on that
        branch (it lives only on the other branch).

        **Validates: Requirements 2.6, 2.8**"""
        script = _script_text()
        then_branch, else_branch = _release_branch_split(script)

        assert "/usr/local/bin/docker-compose" in then_branch, (
            "the detected-24.04 branch does not write "
            "/usr/local/bin/docker-compose"
        )
        assert "exec docker compose" in then_branch, (
            "the detected-24.04 branch's shim does not delegate via "
            "`exec docker compose \"$@\"`"
        )
        assert re.search(r"chmod \+x\s+/usr/local/bin/docker-compose",
                         then_branch), (
            "the detected-24.04 branch does not make the shim executable "
            "(chmod +x /usr/local/bin/docker-compose)"
        )
        assert "snap install docker-compose" not in then_branch, (
            "the snap docker-compose install is reachable on the "
            "detected-24.04 branch"
        )
        assert "snap install docker-compose" in else_branch, (
            "the snap docker-compose install no longer exists on the "
            "non-24.04 branch (the 22.04 path must stay today's)"
        )

    def test_pep668_flag_is_release_keyed_and_defined_once(self):
        """The `--break-system-packages` delta has exactly ONE
        implementation: a single non-comment assignment guarded by the
        detected-24.04 release test, consumed as a variable by the
        installs (no second literal copy anywhere in the script).

        **Validates: Requirements 2.6, 2.8**"""
        script = _script_text()
        lines = script.splitlines()
        flag_lines = [
            index for index, line in enumerate(lines)
            if _PEP668_FLAG in line and not line.strip().startswith("#")
        ]
        assert len(flag_lines) == 1, (
            f"expected exactly one non-comment occurrence of "
            f"'{_PEP668_FLAG}' in setup-build-server.sh (the release-"
            f"keyed flag assignment); found {len(flag_lines)}: "
            + ", ".join(repr(lines[index].strip())
                        for index in flag_lines)
        )
        assignment_index = flag_lines[0]
        assignment = lines[assignment_index].strip()
        assert re.match(r"PIP_BREAK_FLAG=.*--break-system-packages",
                        assignment), (
            f"the single '{_PEP668_FLAG}' occurrence is not the "
            f"PIP_BREAK_FLAG assignment: {assignment!r}"
        )
        guard_window = "\n".join(
            lines[max(0, assignment_index - 3):assignment_index])
        assert _RELEASE_KEY_PATTERN.search(guard_window), (
            "the PIP_BREAK_FLAG='--break-system-packages' assignment is "
            "not guarded by the detected-24.04 release test"
        )

    def test_shim_body_appears_exactly_once(self):
        """The docker-compose shim body (`exec docker compose`) has
        exactly one implementation in the script — no divergent copy.

        **Validates: Requirements 2.8**"""
        script = _script_text()
        occurrences = [line.strip()
                       for line in _non_comment_lines(script)
                       if "exec docker compose" in line]
        assert len(occurrences) == 1, (
            "expected exactly one shim body ('exec docker compose') in "
            f"setup-build-server.sh, found {len(occurrences)}: "
            f"{occurrences}"
        )

    def test_exactly_the_three_pep668_installs_carry_the_keyed_flag(self):
        """The awscli, botocore[crt], and GDK CLI installs each carry
        the flag VIA the release-keyed $PIP_BREAK_FLAG variable, and no
        other pip3 install line does.

        **Validates: Requirements 2.6, 2.8**"""
        script = _script_text()
        pip_lines = _pip_install_lines(script)
        assert set(pip_lines) == {"awscli", "botocore[crt]", "gdk-cli"}, (
            "setup-build-server.sh no longer contains the three PEP 668-"
            f"affected install command lines; found {sorted(pip_lines)}"
        )
        unflagged = {package: line for package, line in pip_lines.items()
                     if not re.search(r"\$\{?PIP_BREAK_FLAG\}?", line)}
        assert not unflagged, (
            "PEP 668-affected installs not carrying the release-keyed "
            "$PIP_BREAK_FLAG:\n" + "\n".join(
                f"  - {line}" for line in unflagged.values())
        )
        other_flagged = [
            line.strip() for line in _non_comment_lines(script)
            if "pip3 install" in line
            and re.search(r"\$\{?PIP_BREAK_FLAG\}?", line)
            and line.strip() not in pip_lines.values()
        ]
        assert not other_flagged, (
            "the release-keyed flag reaches pip3 install lines beyond "
            f"the three PEP 668-affected ones: {other_flagged}"
        )


#: Generated seam inputs (mirroring the sibling suites' conventions).
_REPO_URLS = st.sampled_from(
    ["https://github.com/example-org/DefectDetectionApplication.git",
     "https://github.com/other-org/dda-fork.git"])
_REPO_DIRS = st.sampled_from(
    [None, "/opt/dda/DefectDetectionApplication",
     "/home/ubuntu/github/DefectDetectionApplication"])
_SOURCE_REFS = st.sampled_from(
    [None, "main", "release/2.4", "v1.6.2",
     "0123456789abcdef0123456789abcdef01234567"])
#: ANY ubuntu_version, including the 24.04 JP7 host release: the launch
#: user-data must be release-blind (the deltas travel in the script).
_UBUNTU_VERSIONS = st.sampled_from([None, "22.04", "24.04", "20.04"])
_SUPPORTED_TARGETS = st.sampled_from(sorted(FROZEN_PLAN_ORACLE))


def _assert_delta_free_and_executes_shared_script(body, seam):
    """The emitted body executes setup-build-server.sh and contains NO
    copy of any delta text."""
    assert "setup-build-server.sh" in body, (
        f"the {seam} body does not execute the shared "
        "setup-build-server.sh seam"
    )
    copied = [marker for marker in DELTA_MARKERS if marker in body]
    assert not copied, (
        f"the {seam} body carries a copy of noble delta text "
        f"{copied} — the deltas must live ONLY in setup-build-server.sh "
        "(Req 2.8: one shared implementation, no divergent copies)"
    )


class TestProperty6LaunchUserDataReachesDeltasThroughTheSharedSeam:
    """For any rendered launch user-data (`build_fleet.render_user_data`,
    generated repository/dir/ref, ANY ubuntu_version including 24.04),
    the emitted body executes the shared script and contains no copy of
    any delta text.

    **Feature: jp7-ephemeral-runner-provisioning, Property 6: Noble
    deltas live once in the shared bootstrap script and both seams reach
    them**
    **Validates: Requirements 2.6, 2.8**"""

    @settings(max_examples=100, deadline=None)
    @given(repo_url=_REPO_URLS, repo_dir=_REPO_DIRS,
           source_ref=_SOURCE_REFS, ubuntu_version=_UBUNTU_VERSIONS)
    def test_rendered_launch_user_data_is_delta_free_for_any_release(
            self, repo_url, repo_dir, source_ref, ubuntu_version):
        """A 24.04 launch reaches the noble deltas through the single
        shared implementation, not through user-data text: the rendered
        body executes setup-build-server.sh, carries no delta copy, and
        is identical whatever ubuntu_version the launch selected.

        **Validates: Requirements 2.6, 2.8**"""
        render_parameters = inspect.signature(
            build_fleet.render_user_data).parameters
        kwargs = {"repo_dir": repo_dir, "source_ref": source_ref}
        # Valid whether or not the seam ever grows a release parameter:
        # pass the generated release when the signature accepts one.
        if "ubuntu_version" in render_parameters and ubuntu_version:
            kwargs["ubuntu_version"] = ubuntu_version
        rendered = build_fleet.render_user_data(repo_url, **kwargs)

        _assert_delta_free_and_executes_shared_script(
            rendered, "rendered launch user-data")

        # Release-blind: the same repository/dir/ref renders the same
        # body a 22.04 launch gets — ubuntu_version selects the AMI,
        # never a second bootstrap variant.
        jammy_kwargs = {"repo_dir": repo_dir, "source_ref": source_ref}
        if "ubuntu_version" in render_parameters:
            jammy_kwargs["ubuntu_version"] = "22.04"
        assert rendered == build_fleet.render_user_data(
            repo_url, **jammy_kwargs), (
            f"render_user_data emits a different body for "
            f"ubuntu_version={ubuntu_version!r} than for 22.04 — a "
            "release-keyed user-data variant is a second delta "
            "implementation (Req 2.8)"
        )

    def test_module_level_user_data_template_is_delta_free(self):
        """The USER_DATA_TEMPLATE constant (the exact text a launch
        renders) carries no delta copy either.

        **Validates: Requirements 2.8**"""
        _assert_delta_free_and_executes_shared_script(
            build_fleet.USER_DATA_TEMPLATE, "USER_DATA_TEMPLATE")


class TestProperty6EphemeralBootstrapReachesDeltasThroughTheSharedSeam:
    """For any generated ephemeral bootstrap (`runner_bootstrap_user_data`,
    ANY supported target), the emitted body executes the shared script
    and contains no copy of any delta text.

    **Feature: jp7-ephemeral-runner-provisioning, Property 6: Noble
    deltas live once in the shared bootstrap script and both seams reach
    them**
    **Validates: Requirements 2.6, 2.8**"""

    @settings(max_examples=100, deadline=None)
    @given(target=_SUPPORTED_TARGETS, repo_url=_REPO_URLS,
           repo_dir=_REPO_DIRS, source_ref=_SOURCE_REFS,
           region=_REGIONS)
    def test_ephemeral_bootstrap_is_delta_free_for_any_target(
            self, target, repo_url, repo_dir, source_ref, region):
        """A JP7 (noble) runner reaches the deltas exactly as a jammy
        runner does — by executing setup-build-server.sh — with no
        release-keyed text and no delta copy in the emitted user-data.

        **Validates: Requirements 2.6, 2.8**"""
        job = {
            "build_job_id": str(uuid.uuid4()),
            "build_target": target,
            "execution_mode": build_domain.EXECUTION_MODE_EPHEMERAL,
            "config_snapshot": {"source_ref": source_ref,
                                "region": region},
        }
        with mock.patch.object(build_dispatcher, "BUILD_REPO_URL",
                               repo_url):
            body = build_dispatcher.runner_bootstrap_user_data(
                job, repo_dir=repo_dir)

        assert body, (
            "runner_bootstrap_user_data emitted no bootstrap although a "
            "repository URL is configured"
        )
        _assert_delta_free_and_executes_shared_script(
            body, f"ephemeral bootstrap ({target})")
