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
BUG CONDITION EXPLORATION for jp7-ephemeral-runner-provisioning (task 1).

**Property 1: Bug Condition** - Wrong-OS JP7 provisioning, missing
capability gate, noble-incompatible shared bootstrap

**Validates: Requirements 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 1.8**

**CRITICAL**: every test in this file MUST FAIL on the current (unfixed)
code. The failures ARE the result: they reproduce ``isBugCondition(X)``
from the design — a build job with ``build_target = JP7`` dispatched, in
either execution mode, onto a host that lacks the noble (Ubuntu 24.04)
JP7 host prerequisites:

  (a) the ephemeral RunnerPlan carries NO OS-release dimension, so the
      dispatcher cannot distinguish JP7's required 24.04 host from
      JP5's 22.04 host (defect clause 1.3);
  (b) AMI resolution for a JP7 plan reads the Ubuntu 22.04 (jammy)
      arm64 SSM parameter — the wrong OS release (1.1, 1.2);
  (c) the jammy BUILD_ARM64_AMI_ID env override is honored for a JP7
      plan, pinning the runner to a jammy image (1.1);
  (d) `validate_build_request` accepts JP7 + dedicated against a
      running arm64 server whose recorded ``ubuntu_version`` is
      '22.04' — and against a pre-ec1dc38 record with NO
      ``ubuntu_version`` field at all — with no capability gate and no
      missing-capability diagnostic (1.7, 1.8);
  (e) the shared bootstrap `setup-build-server.sh` is noble-
      INCOMPATIBLE: its three pip installs carry no PEP 668 flag and
      its docker-compose section knows only the snap install (no
      `docker compose` shim), while `build_fleet.render_user_data`
      renders the same release-blind plain script run for a 24.04
      launch as for 22.04 (1.4, 1.5, 1.6).

The assertions encode the POST-FIX expected behavior (design "Fix
Checking"), so this exact file is re-run unchanged in task 3.8 where it
must PASS. Do NOT weaken these assertions and do NOT change production
code to make them pass now.

Scoped PBT approach: the defect is deterministic, so each property is
scoped to the concrete failing cases (build_target JP7; server
``ubuntu_version`` '22.04' and field-absent; the checked-in
`setup-build-server.sh` text) while the surrounding inputs
(config_snapshot, source ref, repo dir, region, server identity) are
generated with Hypothesis.

--------------------------------------------------------------------------
Safety
--------------------------------------------------------------------------

No test launches EC2 compute, sends an SSM command, calls real AWS, or
starts a build. `build_dispatcher.ssm` is replaced by an in-process
recording stub before any resolution call; `plan_runner`,
`validate_build_request`, and `render_user_data` are pure; the script
checks are static text assertions on the checked-in file.

Run ONLY this file, from the repository root:

    python3 -m pytest \\
        test/backend-test/portal_builds/test_jp7_ephemeral_provisioning_exploration.py \\
        --noconftest -q

(This run contains property-based tests and may generate/shrink
counterexamples.)
"""
import inspect
import os
import re
import sys
import types
import uuid

import pytest
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

_SUFFIX = "jp7-provisioning-explore"
os.environ["BUILD_JOBS_TABLE"] = f"dda-portal-build-jobs-{_SUFFIX}"
os.environ["BUILD_SERVERS_TABLE"] = f"dda-portal-build-servers-{_SUFFIX}"
os.environ["SETTINGS_TABLE"] = f"dda-portal-settings-{_SUFFIX}"

# The AMI env overrides must NOT leak in from the environment: cases (b)
# and (c) control them explicitly through the module globals.
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

#: The one shared bootstrap seam both execution modes execute (Req 2.8).
_SETUP_SCRIPT_PATH = os.path.join(_REPO_ROOT, "setup-build-server.sh")

# ---------------------------------------------------------------------------
# Minimal stand-ins for the Lambda layer modules the handlers import
# (same convention as the sibling exploration suites).
# ---------------------------------------------------------------------------
AUDIT_EVENTS = []


def _fake_shared_utils():
    module = types.ModuleType("shared_utils")

    def log_audit_event(**kwargs):
        AUDIT_EVENTS.append(kwargs)

    def create_response(status_code, body):
        return {"statusCode": status_code, "body": body}

    def get_user_from_event(event):
        return {"user_id": "explore-user", "role": "PortalAdmin"}

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
import build_fleet  # noqa: E402

# ---------------------------------------------------------------------------
# Recording SSM stub and AMI fixtures
# ---------------------------------------------------------------------------

#: What the stubbed 22.04 parameters resolve to (a jammy image id).
JAMMY_STUB_AMI = "ami-0jammy22040000stub"
#: What any stubbed 24.04 parameter resolves to (a noble image id).
NOBLE_STUB_AMI = "ami-0noble24040000stub"
#: A jammy pin for the BUILD_ARM64_AMI_ID env override (case c).
JAMMY_PIN_AMI = "ami-0jammypinned0000001"


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


class _RecordingEc2:
    """`build_dispatcher.ec2` stand-in for the DescribeImages fallback a
    fixed noble resolution may take on SSM failure. Records the call and
    serves one noble image."""

    def __init__(self):
        self.describe_images_calls = []

    def describe_images(self, **kwargs):
        self.describe_images_calls.append(dict(kwargs))
        return {"Images": [{"ImageId": NOBLE_STUB_AMI,
                            "CreationDate": "2026-01-01T00:00:00.000Z"}]}

    def __getattr__(self, item):  # pragma: no cover - guard rail
        raise AssertionError(
            f"unexpected EC2 operation '{item}' during AMI resolution")


def _fresh_ami_resolution():
    """Reset every AMI-resolution seam: recording stubs installed, the
    per-container cache emptied, and every env-override module global
    cleared (including the post-fix noble override, when it exists)."""
    ssm_stub = _RecordingSsm()
    ec2_stub = _RecordingEc2()
    build_dispatcher.ssm = ssm_stub
    build_dispatcher.ec2 = ec2_stub
    build_dispatcher._AMI_CACHE.clear()
    build_dispatcher.BUILD_ARM64_AMI_ID = ""
    build_dispatcher.BUILD_X86_64_AMI_ID = ""
    if hasattr(build_dispatcher, "BUILD_ARM64_NOBLE_AMI_ID"):
        build_dispatcher.BUILD_ARM64_NOBLE_AMI_ID = ""
    return ssm_stub, ec2_stub


def _resolve_ami_for_plan(plan):
    """Resolve the AMI exactly as the dispatcher would for this plan.

    Written to be valid both before and after the fix: when the plan
    carries an ``os_release`` AND `resolve_ami` accepts one, both are
    passed (the fixed call shape); otherwise the unfixed arch-only call
    is made. On unfixed code this is `resolve_ami(plan.arch)`."""
    os_release = getattr(plan, "os_release", None)
    parameters = inspect.signature(build_dispatcher.resolve_ami).parameters
    if os_release is not None and "os_release" in parameters:
        return build_dispatcher.resolve_ami(plan.arch, os_release)
    return build_dispatcher.resolve_ami(plan.arch)


# ---------------------------------------------------------------------------
# Generated surrounding inputs (the deterministic defect is scoped to
# JP7 / the concrete server releases / the checked-in script text).
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
_SOURCE_REFS = st.sampled_from(
    [None, "main", "release/2.4", "v1.6.2",
     "0123456789abcdef0123456789abcdef01234567"])
_REPO_DIRS = st.sampled_from(
    [None, "/opt/dda/DefectDetectionApplication",
     "/home/ubuntu/github/DefectDetectionApplication"])
_REPO_URLS = st.sampled_from(
    ["https://github.com/example-org/DefectDetectionApplication.git",
     "https://github.com/other-org/dda-fork.git"])
_SERVER_IDS = st.uuids().map(lambda u: f"srv-{u}")


def _ephemeral_job(target, config_snapshot):
    return {
        "build_job_id": str(uuid.uuid4()),
        "build_target": target,
        "execution_mode": build_domain.EXECUTION_MODE_EPHEMERAL,
        "config_snapshot": dict(config_snapshot),
    }


def _script_text():
    with open(_SETUP_SCRIPT_PATH, "r", encoding="utf-8") as handle:
        return handle.read()


def _pip_command_lines(script):
    """The three PEP 668-affected install command lines of
    setup-build-server.sh (bugfix clause 1.6)."""
    lines = {}
    for line in script.splitlines():
        if "pip3 install" not in line:
            continue
        if "awscli" in line:
            lines["awscli"] = line.strip()
        elif "botocore[crt]" in line:
            lines["botocore[crt]"] = line.strip()
        elif "aws-greengrass-gdk-cli" in line:
            lines["gdk-cli"] = line.strip()
    return lines


def _carries_pep668_flag(line, script):
    """True when the command line carries --break-system-packages, either
    literally or through a shell variable whose assignment in the script
    contains the flag (the release-keyed flag the fix introduces)."""
    if "--break-system-packages" in line:
        return True
    for var in re.findall(r"\$\{?(\w+)\}?", line):
        if re.search(r"^\s*%s=.*--break-system-packages" % re.escape(var),
                     script, re.MULTILINE):
            return True
    return False


# ===========================================================================
# (a) Plan carries no OS release (defect clause 1.3)
# ===========================================================================

class TestPlanCarriesRequiredOsRelease:

    @settings(max_examples=25, deadline=None)
    @given(config_snapshot=_CONFIG_SNAPSHOTS)
    def test_plan_distinguishes_jp7_host_os_from_jp5(self, config_snapshot):
        """EXPECTED BEHAVIOR (post-fix): `plan_runner` derives the OS
        release the target requires — '24.04' for JP7, '22.04' for JP5 —
        so the dispatcher can select a noble AMI.

        EXPECTED COUNTEREXAMPLE on unfixed code: RunnerPlan has no
        ``os_release`` field at all; a JP7 job is planned identically to
        a JP5 job of the same snapshot (arch arm64 only)."""
        jp7_plan = build_planner.plan_runner(
            _ephemeral_job(build_domain.TARGET_JP7, config_snapshot))
        jp5_plan = build_planner.plan_runner(
            _ephemeral_job(build_domain.TARGET_JP5, config_snapshot))
        jp7_release = getattr(jp7_plan, "os_release", None)
        jp5_release = getattr(jp5_plan, "os_release", None)
        assert (jp7_release, jp5_release) == ("24.04", "22.04"), (
            "WRONG-OS PROVISIONING PLAN (isBugCondition, clause 1.3)\n"
            f"  JP7 plan os_release : {jp7_release!r}\n"
            f"  JP5 plan os_release : {jp5_release!r}\n"
            f"  JP7 plan fields     : {jp7_plan._asdict()}\n"
            f"  JP5 plan fields     : {jp5_plan._asdict()}\n"
            "  RunnerPlan carries only the CPU architecture — the plan "
            "cannot distinguish JP7's required Ubuntu 24.04 host from "
            "JP5's 22.04 host, so the dispatcher has nothing to select "
            "a noble AMI with."
        )


# ===========================================================================
# (b) Jammy AMI selected for JP7 (defect clauses 1.1, 1.2)
# ===========================================================================

class TestJammyAmiSelectedForJp7:

    @settings(max_examples=25, deadline=None)
    @given(config_snapshot=_CONFIG_SNAPSHOTS)
    def test_noble_parameter_requested_for_jp7_plan(self, config_snapshot):
        """EXPECTED BEHAVIOR (post-fix): resolving the AMI for a JP7 plan
        (no env overrides) reads an Ubuntu 24.04 parameter path and never
        a 22.04 one.

        EXPECTED COUNTEREXAMPLE on unfixed code: the 22.04 arm64
        canonical parameter is read and a jammy image id is returned —
        the JP7 runner would be provisioned from the wrong OS release
        and the build fails mid-run (clauses 1.1, 1.2)."""
        ssm_stub, ec2_stub = _fresh_ami_resolution()
        plan = build_planner.plan_runner(
            _ephemeral_job(build_domain.TARGET_JP7, config_snapshot))
        ami = _resolve_ami_for_plan(plan)
        requested = list(ssm_stub.get_parameter_names)
        described = list(ec2_stub.describe_images_calls)
        noble_reads = [n for n in requested if "24.04" in n]
        jammy_reads = [n for n in requested if "22.04" in n]
        noble_resolved = bool(noble_reads) or bool(described)
        assert noble_resolved and not jammy_reads, (
            "JAMMY AMI SELECTED FOR JP7 (isBugCondition, clauses 1.1/1.2)\n"
            f"  requested SSM parameter paths : {requested}\n"
            f"  DescribeImages fallback calls : {len(described)}\n"
            f"  returned AMI id               : {ami}\n"
            "  AMI resolution is keyed only on CPU architecture: the JP7 "
            "plan resolves through the Ubuntu 22.04 arm64 canonical "
            "parameter instead of the 24.04 (noble) arm64 conventions."
        )


# ===========================================================================
# (c) Jammy env override applied to JP7 (defect clause 1.1)
# ===========================================================================

class TestJammyOverrideAppliedToJp7:

    @settings(max_examples=25, deadline=None)
    @given(config_snapshot=_CONFIG_SNAPSHOTS)
    def test_jammy_pin_not_returned_for_jp7_plan(self, config_snapshot):
        """EXPECTED BEHAVIOR (post-fix): BUILD_ARM64_AMI_ID is a jammy
        (22.04) pin and stays scoped to 22.04 resolution; it must never
        be returned for a JP7 (noble) plan.

        EXPECTED COUNTEREXAMPLE on unfixed code: the jammy pin is
        returned verbatim for the JP7 plan (clause 1.1)."""
        ssm_stub, _ = _fresh_ami_resolution()
        build_dispatcher.BUILD_ARM64_AMI_ID = JAMMY_PIN_AMI
        plan = build_planner.plan_runner(
            _ephemeral_job(build_domain.TARGET_JP7, config_snapshot))
        ami = _resolve_ami_for_plan(plan)
        assert ami != JAMMY_PIN_AMI, (
            "JAMMY ENV OVERRIDE APPLIED TO JP7 (isBugCondition, "
            "clause 1.1)\n"
            f"  BUILD_ARM64_AMI_ID (jammy pin) : {JAMMY_PIN_AMI}\n"
            f"  returned AMI id                : {ami}\n"
            f"  SSM parameters consulted       : "
            f"{ssm_stub.get_parameter_names}\n"
            "  The arch-keyed jammy override silently pins a JP7 runner "
            "to an Ubuntu 22.04 image."
        )


# ===========================================================================
# (d) No dedicated capability gate (defect clauses 1.7, 1.8)
# ===========================================================================

class TestNoDedicatedCapabilityGate:

    @pytest.mark.parametrize(
        "recorded_release",
        ["22.04",  # explicit jammy record (post-ec1dc38)
         None],    # field ABSENT: pre-ec1dc38 record, a 22.04 host
        ids=["ubuntu_version=22.04", "ubuntu_version-absent"])
    @settings(max_examples=25, deadline=None)
    @given(server_id=_SERVER_IDS, source_ref=_SOURCE_REFS)
    def test_jp7_dedicated_rejected_on_incapable_host(
            self, recorded_release, server_id, source_ref):
        """EXPECTED BEHAVIOR (post-fix): a JP7 + dedicated request
        selecting a running arm64 server whose recorded release is not
        24.04 is REJECTED at validation time, with a diagnostic naming
        the missing Ubuntu 24.04 arm64 capability and the server's
        actual release — before any Build_Job record is created
        (jetpack7-support Req 6.4/6.5 fail-closed semantics).

        EXPECTED COUNTEREXAMPLE on unfixed code: the request is ACCEPTED
        (validation checks only existence, running state, and CPU
        architecture — never the recorded ``ubuntu_version``), so the
        job is created and fails mid-build on the jammy host
        (clauses 1.7, 1.8)."""
        server = {
            "server_id": server_id,
            "name": "arm64-dedicated-1",
            "lifecycle_state": build_domain.SERVER_STATE_RUNNING,
            "arch": build_domain.ARCH_ARM64,
        }
        if recorded_release is not None:
            server["ubuntu_version"] = recorded_release
        body = {
            "targets": [build_domain.TARGET_JP7],
            "execution_mode": build_domain.EXECUTION_MODE_DEDICATED,
            "server_id": server_id,
        }
        if source_ref is not None:
            body["source_ref"] = source_ref

        result = build_domain.validate_build_request(body, [server])

        # Rejection must happen AT VALIDATION: `validate_build_request`
        # is the pure gate the request flow consults BEFORE creating any
        # Build_Job record (persistence-iff-accept), so `valid=False`
        # here is exactly "no job record created".
        actual_release = recorded_release or "22.04"
        messages = " | ".join(
            error.get("message", "") for error in result.errors)
        capability_named = any(
            "24.04" in error.get("message", "")
            and actual_release in error.get("message", "")
            for error in result.errors)
        assert (not result.valid) and capability_named, (
            "NO DEDICATED CAPABILITY GATE (isBugCondition, "
            "clauses 1.7/1.8)\n"
            f"  request                  : JP7 + dedicated on "
            f"'{server_id}'\n"
            f"  server record            : lifecycle running, arch "
            f"arm64, ubuntu_version="
            f"{server.get('ubuntu_version', '<field absent>')}\n"
            f"  observed validation      : valid={result.valid}\n"
            f"  observed errors          : {messages or '<none>'}\n"
            "  Expected a validation-time rejection naming the missing "
            "Ubuntu 24.04 arm64 capability and the server's actual "
            f"release ({actual_release}); instead the incapable host is "
            "accepted and a Build_Job record would be created."
        )


# ===========================================================================
# (e) Noble-incompatible shared bootstrap (defect clauses 1.4, 1.5, 1.6)
# ===========================================================================

class TestNobleIncompatibleSharedBootstrap:

    def test_pip_installs_carry_pep668_flag(self):
        """EXPECTED BEHAVIOR (post-fix): the three PEP 668-affected
        installs in setup-build-server.sh — `sudo pip3 install awscli`,
        `sudo pip3 install --no-compile 'botocore[crt]'`, and
        `pip3 install --user git+...aws-greengrass-gdk-cli` — each carry
        --break-system-packages (directly or via a release-keyed flag
        variable).

        EXPECTED COUNTEREXAMPLE on unfixed code: all three command lines
        are unflagged and are rejected by noble's externally-managed
        Python (clause 1.6)."""
        script = _script_text()
        pip_lines = _pip_command_lines(script)
        assert set(pip_lines) == {"awscli", "botocore[crt]", "gdk-cli"}, (
            f"expected the three pip install command lines, found: "
            f"{pip_lines}")
        unflagged = {name: line for name, line in pip_lines.items()
                     if not _carries_pep668_flag(line, script)}
        assert not unflagged, (
            "NOBLE-INCOMPATIBLE PIP INSTALLS (isBugCondition, "
            "clause 1.6)\n"
            "  unflagged PEP 668-affected command lines in "
            "setup-build-server.sh:\n"
            + "\n".join(f"    - {line}" for line in unflagged.values())
            + "\n  Each fails under Ubuntu 24.04's externally-managed "
            "Python (PEP 668) without --break-system-packages."
        )

    def test_docker_compose_section_provides_noble_shim(self):
        """EXPECTED BEHAVIOR (post-fix): the docker-compose section of
        setup-build-server.sh provides the noble shim — a
        `docker-compose` command delegating to the `docker compose`
        plugin (`exec docker compose "$@"`) — rather than knowing only
        the snap install.

        EXPECTED COUNTEREXAMPLE on unfixed code: the only docker-compose
        provisioning is `sudo snap install docker-compose`; noble ships
        only the `docker compose` plugin while build-custom.sh invokes
        `docker-compose` (clauses 1.4, 1.6)."""
        script = _script_text()
        snap_install = "snap install docker-compose" in script
        shim_present = "exec docker compose" in script
        assert shim_present, (
            "DOCKER-COMPOSE SHIM MISSING FROM SHARED BOOTSTRAP "
            "(isBugCondition, clauses 1.4/1.6)\n"
            f"  snap install branch present : {snap_install}\n"
            f"  noble shim present          : {shim_present}\n"
            "  setup-build-server.sh knows only the snap docker-compose "
            "install; it never writes the README 4.2 shim "
            "(`exec docker compose \"$@\"`), so build-custom.sh's "
            "`docker-compose` invocations cannot resolve on a noble "
            "host."
        )

    @settings(max_examples=25, deadline=None)
    @given(repo_url=_REPO_URLS, repo_dir=_REPO_DIRS,
           source_ref=_SOURCE_REFS)
    def test_rendered_launch_user_data_reaches_noble_deltas(
            self, repo_url, repo_dir, source_ref):
        """EXPECTED BEHAVIOR (post-fix): a portal fleet launch with
        ubuntu_version 24.04 produces a JP7-capable server with no
        manual steps — the rendered user-data executes the shared
        `setup-build-server.sh`, and that script carries the
        release-keyed noble deltas (the shim and the PEP 668 flags), so
        the launch bootstrap reaches them (Req 2.6/2.8: one shared
        implementation, no second copy).

        EXPECTED COUNTEREXAMPLE on unfixed code: `render_user_data` is
        release-blind (a 24.04 launch renders the identical plain script
        run as 22.04) and the script it runs contains NO noble delta at
        all, so the launched 24.04 server is NOT JP7-build-capable
        (clauses 1.5, 1.6)."""
        render_parameters = inspect.signature(
            build_fleet.render_user_data).parameters
        kwargs = {"repo_dir": repo_dir, "source_ref": source_ref}
        # Valid pre- and post-fix: pass the release when the (possibly
        # fixed) signature accepts one.
        if "ubuntu_version" in render_parameters:
            kwargs["ubuntu_version"] = "24.04"
        rendered = build_fleet.render_user_data(repo_url, **kwargs)

        script = _script_text()
        pip_lines = _pip_command_lines(script)
        script_has_deltas = (
            "exec docker compose" in script
            and all(_carries_pep668_flag(line, script)
                    for line in pip_lines.values()))
        executes_shared_script = "setup-build-server.sh" in rendered

        assert executes_shared_script and script_has_deltas, (
            "RELEASE-BLIND 24.04 LAUNCH BOOTSTRAP (isBugCondition, "
            "clauses 1.5/1.6)\n"
            f"  render_user_data signature accepts ubuntu_version : "
            f"{'ubuntu_version' in render_parameters}\n"
            f"  rendered body executes setup-build-server.sh      : "
            f"{executes_shared_script}\n"
            f"  shared script carries the noble deltas            : "
            f"{script_has_deltas}\n"
            "  A 24.04 fleet launch renders the same plain "
            "setup-build-server.sh run as a 22.04 launch, and the "
            "script has no release-keyed noble delta (no docker-compose "
            "shim, no PEP 668 flags) — the launched server is not "
            "JP7-build-capable without manual operator steps."
        )
