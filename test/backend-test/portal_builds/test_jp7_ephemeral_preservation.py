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
JP7 ephemeral runner provisioning — PRESERVATION baseline
(jp7-ephemeral-runner-provisioning, task 2).

**Property 3: Preservation** — Non-JP7 planning/AMI resolution, both
bootstrap seams, and dedicated validation outcomes.

**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.8, 3.9**

Observation-first: every oracle in this file was RECORDED by running the
CURRENT (unfixed) working tree on inputs where the design's
`isBugCondition` is false, and re-spelled here as frozen constants and
frozen template functions (the suite's FROZEN_MATRIX style) — the
pre-fix code is never imported or re-derived. These tests MUST PASS now
(on unfixed code) and MUST keep passing verbatim after the JP7 fix
lands (task 3.9 re-runs this exact file). This oracle is IMMUTABLE: it
is never rebaselined after implementation.

The input domain of this file is exactly the complement of
`isBugCondition(input)`:

* **Req 3.4 — non-JP7 ephemeral planning.** `plan_runner` for generated
  JP5/JP6/AMD64/AMD64_NVIDIA ephemeral jobs with generated
  `config_snapshot`s: the recorded arch, instance type, volume size,
  spot flag, and status derivations (asserted BY NAME so the approved
  additive `os_release` field cannot break the baseline).
* **Req 3.1 / 3.2 — 22.04 AMI resolution.** The recorded `resolve_ami`
  resolution ORDER for both architectures across every
  `BUILD_ARM64_AMI_ID`/`BUILD_X86_64_AMI_ID` override configuration:
  env override first (zero SSM reads), else per-container cache, else
  exactly the recorded per-architecture canonical 22.04 SSM parameter
  path — and the cache-hit on a second call (no second SSM read).
* **Req 3.3 — the ephemeral bootstrap seam.** `runner_bootstrap_user_data`
  output text for generated jobs of ALL FIVE supported targets,
  generated refs, repo dirs, and regions: byte-identical to the frozen
  template recorded from unfixed code, still ending with the
  root-written Bootstrap_Marker statement.
* **Req 3.8 — the fleet-launch bootstrap seam.** `build_fleet
  .render_user_data` output text for generated repository/dir/ref
  inputs: byte-identical to the frozen template. Recorded observation:
  the launch handler renders this SAME text for `ubuntu_version` 22.04
  and absent alike (`render_user_data` takes no release input at all),
  so the frozen text covers both.
* **Req 3.5 / 3.9 — dedicated validation outcomes.** `validate_build_
  request` accept/reject outcomes and error RULE IDS over generated
  requests and fleets for every target/mode combination other than
  JP7+dedicated (the one combination the fix is allowed to flip),
  including JP5/JP6 dedicated against servers whose `ubuntu_version` is
  '22.04', '24.04', an arbitrary other string, and ABSENT — recorded:
  the release never influences any outcome.
* **Req 3.8 — the jammy command sequence of `setup-build-server.sh`.**
  The exact three pip install command lines (`sudo pip3 install awscli`,
  `sudo pip3 install --no-compile 'botocore[crt]'`, `pip3 install
  --user git+...aws-greengrass-gdk-cli.git@v1.6.2`) and the snap
  `docker-compose` branch, asserted on the script's EFFECTIVE jammy
  expansion (any release-keyed `$PIP_BREAK_FLAG` expanding to the empty
  string, exactly what a detected-22.04 host executes), so the frozen
  22.04 command sequence is pinned without freezing noble additions out.

SAFETY (absolute): no test in this file launches EC2 compute, sends an
SSM command, calls real AWS, or runs a build. `build_dispatcher.ssm` is
replaced by an in-process recording stub wherever resolution could read
a parameter; both user-data texts are only ever GENERATED and compared,
never executed; `setup-build-server.sh` is only ever read as text.
"""
import os
import re
import shlex
import sys
from unittest import mock

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# Environment BEFORE any import: the modules bind env at import time.
# ---------------------------------------------------------------------------
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
# Module defaults are the recorded baseline: no overrides, no repo pins.
for _var in ("BUILD_REPO_URL", "BUILD_REPO_DIR",
             "BUILD_ARM64_AMI_ID", "BUILD_X86_64_AMI_ID",
             "ARM64_AMI_SSM_PARAMETER", "X86_64_AMI_SSM_PARAMETER"):
    os.environ.pop(_var, None)

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.normpath(os.path.join(_HERE, "..", "..", ".."))
_BACKEND = os.path.join(_REPO_ROOT, "edge-cv-portal", "backend")
FUNCTIONS_DIR = os.path.join(_BACKEND, "functions")
LAYER_DIR = os.path.join(_BACKEND, "layers", "shared", "python")
for _p in (LAYER_DIR, FUNCTIONS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# Fresh modules bound to THIS file's environment.
for _module in ("build_dispatcher", "build_fleet", "build_domain",
                "build_planner", "build_source", "shared_utils"):
    sys.modules.pop(_module, None)

import build_domain  # noqa: E402
import build_planner  # noqa: E402
import build_dispatcher  # noqa: E402
import build_fleet  # noqa: E402

SETUP_SCRIPT_PATH = os.path.join(_REPO_ROOT, "setup-build-server.sh")


# ---------------------------------------------------------------------------
# THE FROZEN ORACLE — recorded from the CURRENT (unfixed) working tree.
# Every value below is a re-spelling of an OBSERVED output; nothing is
# read back from the modules under test.
# ---------------------------------------------------------------------------

#: Non-JP7 ephemeral planning matrix (Req 3.4): target -> (arch,
#: snapshot instance-type key, default instance type).
FROZEN_PLAN_MATRIX = {
    "JP5": ("arm64", "arm64_instance_type", "m6g.4xlarge"),
    "JP6": ("arm64", "arm64_instance_type", "m6g.4xlarge"),
    "AMD64": ("x86_64", "x86_64_instance_type", "m6i.4xlarge"),
    "AMD64_NVIDIA": ("x86_64", "x86_64_instance_type", "m6i.4xlarge"),
}
FROZEN_DEFAULT_VOLUME_GB = 100
FROZEN_PLAN_STATUS = "provisioning"

#: The exact per-architecture canonical 22.04 SSM parameter paths the
#: unfixed `resolve_ami` reads when no override/cache applies (Req 3.1).
FROZEN_2204_PARAMETER = {
    "arm64": ("/aws/service/canonical/ubuntu/server/22.04/stable/current/"
              "arm64/hvm/ebs-gp2/ami-id"),
    "x86_64": ("/aws/service/canonical/ubuntu/server/22.04/stable/current/"
               "amd64/hvm/ebs-gp2/ami-id"),
}

#: Every supported target (the ephemeral bootstrap text is recorded as
#: byte-identical for ALL of them — the noble deltas travel in
#: setup-build-server.sh, never in user-data).
ALL_TARGETS = ("JP5", "JP6", "JP7", "AMD64", "AMD64_NVIDIA")
NON_JP7_TARGETS = tuple(t for t in ALL_TARGETS if t != "JP7")

#: Validation rule ids (Req 3.5) — recorded strings, re-spelled.
RULE_TARGETS_EMPTY = "targets_empty"
RULE_UNSUPPORTED_TARGET = "unsupported_target"
RULE_EXECUTION_MODE_MISSING = "execution_mode_missing"
RULE_EXECUTION_MODE_INVALID = "execution_mode_invalid"
RULE_SERVER_ID_MISSING = "server_id_missing"
RULE_SERVER_NOT_FOUND = "server_not_found"
RULE_SERVER_NOT_RUNNING = "server_not_running"
RULE_SERVER_ARCH_MISMATCH = "server_arch_mismatch"

FROZEN_SUPPORTED_TARGETS = frozenset(ALL_TARGETS)
FROZEN_REQUIRED_ARCH = {
    "JP5": "arm64", "JP6": "arm64", "JP7": "arm64",
    "AMD64": "x86_64", "AMD64_NVIDIA": "x86_64",
}

#: The exact three PEP 668-affected pip install command lines of the
#: JAMMY effective command sequence of setup-build-server.sh (Req 3.8),
#: and the snap docker-compose branch markers.
FROZEN_PIP_AWSCLI = "sudo pip3 install awscli"
FROZEN_PIP_BOTOCORE = "sudo pip3 install --no-compile 'botocore[crt]'"
FROZEN_PIP_GDK = ("pip3 install --user git+https://github.com/"
                  "aws-greengrass/aws-greengrass-gdk-cli.git@v1.6.2")
FROZEN_COMPOSE_GUARD = "if ! command -v docker-compose"
FROZEN_COMPOSE_SNAP_INSTALL = "sudo snap install docker-compose"

#: The root-written Bootstrap_Marker statement the ephemeral bootstrap
#: text ends with (recorded verbatim, including the trailing newline).
FROZEN_MARKER_STATEMENT = \
    "touch /var/log/dda-build-server-bootstrap.done || true"


def _sync_guard(kind, exit_code):
    """The recorded Source_Sync failure guard text (no event emission —
    neither seam passes an event bus)."""
    return ('{ echo "PORTAL_SOURCE_SYNC_FAILED kind=%s '
            'repository=$REPO_URL ref=$SOURCE_REF"; exit %d; }'
            % (kind, exit_code))


_GUARD_UNREACHABLE = _sync_guard("repository_unreachable", 65)
_GUARD_REF = _sync_guard("ref_not_found", 66)


def _frozen_sync_lines(repo_url, repo_dir, source_ref):
    """The recorded Source_Sync block (shared by both seams): the three
    shlex-quoted assignments, clone-if-absent, cd, and — only when a ref
    was selected — fetch + checkout."""
    lines = [
        "REPO_DIR=" + shlex.quote(repo_dir),
        "REPO_URL=" + shlex.quote(repo_url),
        "SOURCE_REF=" + shlex.quote(source_ref),
        'git config --global --add safe.directory "$REPO_DIR" '
        "2>/dev/null || true",
        'mkdir -p "$(dirname "$REPO_DIR")"',
        'if [ ! -d "$REPO_DIR/.git" ]; then',
        '  git clone "$REPO_URL" "$REPO_DIR" || ' + _GUARD_UNREACHABLE,
        "fi",
        'cd "$REPO_DIR" || ' + _GUARD_UNREACHABLE,
    ]
    if source_ref:
        lines += [
            "git fetch --prune origin || " + _GUARD_UNREACHABLE,
            'if git rev-parse --verify --quiet '
            '"refs/remotes/origin/$SOURCE_REF" >/dev/null 2>&1; then',
            '  git checkout --force -B "$SOURCE_REF" "origin/$SOURCE_REF" '
            "|| " + _GUARD_REF,
            "else",
            '  git checkout --force "$SOURCE_REF" || ' + _GUARD_REF,
            "fi",
        ]
    return lines


def frozen_runner_bootstrap(repo_url, repo_dir, source_ref, region):
    """FROZEN byte-level oracle for `runner_bootstrap_user_data`,
    re-spelled from the output recorded on unfixed code (task 2
    observation run). Inputs mirror the real inputs: the configured
    repository URL, the resolved repo dir, the job's selected ref (''
    when none), and the dispatch region ('' when none resolves)."""
    qdir = shlex.quote(repo_dir)
    body = ['export HOME="${HOME:-/home/ubuntu}"']
    if region:
        qregion = shlex.quote(region)
        body += ["export AWS_DEFAULT_REGION=" + qregion,
                 "export AWS_REGION=" + qregion]
    body += ['export PATH="${HOME:-/home/ubuntu}/.local/bin:$PATH"']
    body += _frozen_sync_lines(repo_url, repo_dir, source_ref)
    body += ["bash ./setup-build-server.sh"]
    return "\n".join([
        "#!/bin/bash",
        "set -uo pipefail",
        "BOOTSTRAP_LOG=/var/log/dda-build-server-bootstrap.log",
        'if : > "$BOOTSTRAP_LOG" 2>/dev/null; then',
        '  exec >> "$BOOTSTRAP_LOG" 2>&1',
        "fi",
        'export HOME="${HOME:-/root}"',
        "export DEBIAN_FRONTEND=noninteractive",
        "apt-get update -y && apt-get install -y git",
        'mkdir -p "$(dirname %s)"' % qdir,
        'chown ubuntu:ubuntu "$(dirname %s)" 2>/dev/null || true' % qdir,
        "if [ -d %s ]; then chown -R ubuntu:ubuntu %s 2>/dev/null || true; fi"
        % (qdir, qdir),
        'PORTAL_RUN_SCRIPT="$(mktemp /tmp/portal-build-run.XXXXXX)" '
        "|| exit 1",
        "cat > \"$PORTAL_RUN_SCRIPT\" <<'PORTAL_RUN_EOF'",
        "\n".join(body),
        "PORTAL_RUN_EOF",
        'chmod 644 "$PORTAL_RUN_SCRIPT"',
        'if [ "$(id -u)" = "0" ] && id ubuntu >/dev/null 2>&1; then',
        '  sudo -H -u ubuntu bash "$PORTAL_RUN_SCRIPT"',
        "else",
        '  bash "$PORTAL_RUN_SCRIPT"',
        "fi",
        'PORTAL_RUN_STATUS="$?"',
        'rm -f "$PORTAL_RUN_SCRIPT"',
        'case "$PORTAL_RUN_STATUS" in',
        '  65|66) exit "$PORTAL_RUN_STATUS";;',
        "esac",
        FROZEN_MARKER_STATEMENT,
        "",
    ])


def frozen_fleet_user_data(repo_url, repo_dir, source_ref):
    """FROZEN byte-level oracle for `build_fleet.render_user_data`,
    re-spelled from the output recorded on unfixed code. Recorded
    observation: the launch handler renders this SAME text for
    `ubuntu_version` 22.04 and absent alike (the release selects only
    the AMI, never the bootstrap text), so this one oracle covers both
    launch shapes of Req 3.8."""
    return "\n".join([
        "#!/bin/bash",
        "set -x",
        "BOOTSTRAP_LOG=/var/log/dda-build-server-bootstrap.log",
        'if : > "$BOOTSTRAP_LOG" 2>/dev/null; then',
        '  exec >> "$BOOTSTRAP_LOG" 2>&1',
        "fi",
        "",
        "export DEBIAN_FRONTEND=noninteractive",
        "apt-get update",
        "apt-get install -y git",
        "",
        "# Clone the source repository for the build agent (design "
        "\u00a72/\u00a75).",
        "sudo -u ubuntu -H git clone %s %s" % (repo_url, repo_dir),
        "",
        "# Put that tree on the selected (repository, ref) through the "
        "shared",
        "# Sync_Generator (Req 4.1, 4.3): clone-if-absent, fetch, checkout. "
        "Run as",
        "# the build user, both because the clone above is owned by it and "
        "because",
        "# the build itself runs as that user. A sync failure echoes",
        "# PORTAL_SOURCE_SYNC_FAILED and exits 65/66 inside this block; the",
        "# bootstrap continues so the marker below is still written and the",
        "# dispatcher's readiness gate is not left hanging.",
        "sudo -u ubuntu -H bash -s <<'DDA_SOURCE_SYNC'",
        "\n".join(_frozen_sync_lines(repo_url, repo_dir, source_ref or "")),
        "DDA_SOURCE_SYNC",
        "",
        "# Run the repository's build-environment bootstrap as the build "
        "user",
        "# (setup-build-server.sh equivalent: docker via snap, "
        "docker-compose,",
        "# Python 3.11, AWS CLI, botocore[crt], GDK CLI).",
        "cd %s" % repo_dir,
        "sudo -u ubuntu -H bash -c 'cd %s && ./setup-build-server.sh' "
        "|| true" % repo_dir,
        "",
        FROZEN_MARKER_STATEMENT,
        "",
    ])


def frozen_validation_rules(body, servers):
    """FROZEN decision oracle for `validate_build_request` outside the
    JP7+dedicated bug condition: the recorded accept/reject outcome and
    the ORDERED error rule ids, re-spelled from unfixed behavior. Bodies
    generated by this file never carry repository/source_ref, so those
    rules never fire here."""
    rules = []
    targets = body.get("targets")
    if not isinstance(targets, list) or len(targets) == 0:
        rules.append(RULE_TARGETS_EMPTY)
        targets = []
    for target in targets:
        if target not in FROZEN_SUPPORTED_TARGETS:
            rules.append(RULE_UNSUPPORTED_TARGET)
    mode = body.get("execution_mode")
    if mode is None or mode == "":
        rules.append(RULE_EXECUTION_MODE_MISSING)
    elif mode not in ("ephemeral", "dedicated"):
        rules.append(RULE_EXECUTION_MODE_INVALID)
    if mode == "dedicated":
        server_id = body.get("server_id")
        if server_id is None or server_id == "":
            rules.append(RULE_SERVER_ID_MISSING)
        else:
            server = next((s for s in servers
                           if s.get("server_id") == server_id), None)
            if server is None:
                rules.append(RULE_SERVER_NOT_FOUND)
            else:
                if server.get("lifecycle_state") != "running":
                    rules.append(RULE_SERVER_NOT_RUNNING)
                for target in targets:
                    if target not in FROZEN_SUPPORTED_TARGETS:
                        continue
                    if server.get("arch") != FROZEN_REQUIRED_ARCH[target]:
                        rules.append(RULE_SERVER_ARCH_MISMATCH)
    return (len(rules) == 0, tuple(rules))


# ---------------------------------------------------------------------------
# Helpers and generators
# ---------------------------------------------------------------------------

def _observation(**fields):
    return "\n".join("%s=%r" % item for item in fields.items())


class RecordingSSM:
    """In-process SSM stand-in: records every requested parameter path
    and answers with a deterministic value. Nothing leaves the process."""

    def __init__(self, value="ami-0feedfacecafe0001"):
        self.value = value
        self.calls = []

    def get_parameter(self, Name, **kwargs):
        self.calls.append(Name)
        return {"Parameter": {"Value": self.value}}


_REF_ALPHABET = "abcdefghijklmnopqrstuvwxyzABCDEFXYZ0123456789._-"
_SEGMENT = st.text(alphabet=_REF_ALPHABET, min_size=1, max_size=12).filter(
    lambda s: not s.startswith((".", "-")) and not s.endswith("."))

#: Branch/tag names, 40-hex SHAs, and "no selection" ('' / None).
source_refs = st.one_of(
    st.none(),
    st.just(""),
    _SEGMENT,
    st.builds("{}/{}".format, _SEGMENT, _SEGMENT),
    st.text(alphabet="0123456789abcdef", min_size=40, max_size=40),
)

#: Absolute repo directories, including one shape needing shell quoting.
repo_dirs = st.one_of(
    st.builds("/home/ubuntu/{}".format, _SEGMENT),
    st.builds("/srv/{}/{}".format, _SEGMENT, _SEGMENT),
    st.builds("/opt/build tree/{}".format, _SEGMENT),
)

repo_urls = st.builds(
    "https://github.com/{}/{}".format, _SEGMENT, _SEGMENT)

regions = st.sampled_from(
    ["us-east-1", "us-west-2", "eu-west-1", "ap-northeast-1"])

instance_types = st.sampled_from(
    ["m6g.4xlarge", "c7g.8xlarge", "m6i.4xlarge", "c6i.2xlarge",
     "r6g.large"])

#: Generated config_snapshots: every planning-relevant key optional,
#: plus keys the planner must ignore.
config_snapshots = st.one_of(
    st.none(),
    st.fixed_dictionaries(
        {},
        optional={
            "arm64_instance_type": instance_types,
            "x86_64_instance_type": instance_types,
            "volume_size_gb": st.integers(min_value=20, max_value=900),
            "use_spot_for_ephemeral": st.booleans(),
            "source_ref": st.one_of(st.none(), _SEGMENT),
            "max_runtime_hours": st.integers(min_value=1, max_value=12),
            "region": regions,
        }),
)

ami_ids = st.builds("ami-0{}".format, st.text(
    alphabet="0123456789abcdef", min_size=16, max_size=16))

ubuntu_versions = st.sampled_from(
    ["22.04", "24.04", "20.99-custom", None])  # None => field ABSENT

server_states = st.sampled_from(
    ["running", "stopped", "pending", "terminated"])
server_arches = st.sampled_from(["arm64", "x86_64"])


def _server(server_id, state, arch, ubuntu_version):
    record = {"server_id": server_id, "lifecycle_state": state,
              "arch": arch, "name": "srv " + server_id}
    if ubuntu_version is not None:
        record["ubuntu_version"] = ubuntu_version
    return record


servers_lists = st.lists(
    st.builds(_server,
              st.sampled_from(["srv-1", "srv-2", "srv-3"]),
              server_states, server_arches, ubuntu_versions),
    max_size=3,
    unique_by=lambda s: s["server_id"])

target_lists = st.lists(
    st.sampled_from(list(ALL_TARGETS) + ["JP99", "amd64", "BAD"]),
    max_size=3)

execution_modes = st.one_of(
    st.none(), st.just(""), st.sampled_from(
        ["ephemeral", "dedicated", "weird-mode"]))

server_id_choices = st.sampled_from(
    [None, "", "srv-1", "srv-2", "srv-3", "srv-missing"])


# ===========================================================================
# Req 3.4 — non-JP7 ephemeral planning (frozen plan_runner outputs)
# ===========================================================================

class TestPlanRunnerBaseline:
    """**Property 3: Preservation** — `plan_runner` field outputs for
    generated non-JP7 ephemeral jobs, frozen from unfixed code. Fields
    are asserted BY NAME so the approved additive `os_release` field
    (task 3.3) cannot break this baseline.

    **Validates: Requirements 3.4**
    """

    @given(target=st.sampled_from(sorted(FROZEN_PLAN_MATRIX)),
           snapshot=config_snapshots)
    @settings(max_examples=150, deadline=None)
    def test_plan_fields_match_frozen_matrix(self, target, snapshot):
        arch, type_key, default_type = FROZEN_PLAN_MATRIX[target]
        job = {"build_job_id": "job-preserve-1", "build_target": target,
               "execution_mode": "ephemeral"}
        if snapshot is not None:
            job["config_snapshot"] = snapshot
        snap = snapshot or {}

        plan = build_planner.plan_runner(job)

        observed = _observation(target=target, snapshot=snapshot,
                                plan=str(plan))
        assert plan.build_job_id == "job-preserve-1", observed
        assert plan.arch == arch, observed
        assert plan.instance_type == (snap.get(type_key) or default_type), \
            observed
        expected_volume = snap.get("volume_size_gb")
        if expected_volume is None:
            expected_volume = FROZEN_DEFAULT_VOLUME_GB
        assert plan.volume_size_gb == expected_volume, observed
        assert plan.spot is bool(snap.get("use_spot_for_ephemeral", False)), \
            observed
        assert plan.status == FROZEN_PLAN_STATUS, observed

    @given(target=st.sampled_from(sorted(FROZEN_PLAN_MATRIX)))
    @settings(max_examples=20, deadline=None)
    def test_non_ephemeral_job_still_raises(self, target):
        """Recorded guard: planning a runner for a dedicated job is a
        ValueError, unchanged."""
        with pytest.raises(ValueError):
            build_planner.plan_runner(
                {"build_job_id": "job-x", "build_target": target,
                 "execution_mode": "dedicated"})


# ===========================================================================
# Req 3.1 / 3.2 — 22.04 AMI resolution order (frozen resolve_ami)
# ===========================================================================

class TestResolveAmiBaseline:
    """**Property 3: Preservation** — the recorded `resolve_ami`
    resolution ORDER across every env-override configuration: override
    -> per-container cache -> the exact 22.04 canonical parameter, with
    the cache-hit on a second call.

    **Validates: Requirements 3.1, 3.2**
    """

    def _resolve(self, arch, arm_override, x86_override, ssm_stub):
        with mock.patch.object(build_dispatcher, "ssm", ssm_stub), \
                mock.patch.object(build_dispatcher, "BUILD_ARM64_AMI_ID",
                                  arm_override), \
                mock.patch.object(build_dispatcher, "BUILD_X86_64_AMI_ID",
                                  x86_override), \
                mock.patch.dict(build_dispatcher._AMI_CACHE, clear=True):
            first = build_dispatcher.resolve_ami(arch)
            cache_after_first = dict(build_dispatcher._AMI_CACHE)
            second = build_dispatcher.resolve_ami(arch)
        return first, second, cache_after_first

    @given(arch=st.sampled_from(["arm64", "x86_64"]),
           arm_override=st.one_of(st.just(""), ami_ids),
           x86_override=st.one_of(st.just(""), ami_ids),
           ssm_value=ami_ids)
    @settings(max_examples=120, deadline=None)
    def test_resolution_order_and_cache_hit(self, arch, arm_override,
                                            x86_override, ssm_value):
        stub = RecordingSSM(value=ssm_value)
        first, second, cache = self._resolve(
            arch, arm_override, x86_override, stub)
        override = arm_override if arch == "arm64" else x86_override
        observed = _observation(arch=arch, arm_override=arm_override,
                                x86_override=x86_override, first=first,
                                second=second, ssm_calls=stub.calls,
                                cache=cache)
        if override:
            # Env override wins; NOTHING is read and nothing cached.
            assert first == override, observed
            assert second == override, observed
            assert stub.calls == [], observed
            assert cache == {}, observed
        else:
            # SSM read of exactly the frozen 22.04 parameter, once;
            # the second call is served from the per-container cache.
            assert first == ssm_value, observed
            assert second == ssm_value, observed
            assert stub.calls == [FROZEN_2204_PARAMETER[arch]], observed
            assert cache.get(arch) == ssm_value, observed

    @given(ssm_value=ami_ids)
    @settings(max_examples=30, deadline=None)
    def test_overrides_are_scoped_per_architecture(self, ssm_value):
        """An arm64 pin never answers an x86_64 resolution and vice
        versa (recorded cross-architecture behavior)."""
        stub = RecordingSSM(value=ssm_value)
        with mock.patch.object(build_dispatcher, "ssm", stub), \
                mock.patch.object(build_dispatcher, "BUILD_ARM64_AMI_ID",
                                  "ami-0armpin000000001"), \
                mock.patch.object(build_dispatcher, "BUILD_X86_64_AMI_ID",
                                  ""), \
                mock.patch.dict(build_dispatcher._AMI_CACHE, clear=True):
            arm = build_dispatcher.resolve_ami("arm64")
            x86 = build_dispatcher.resolve_ami("x86_64")
        observed = _observation(arm=arm, x86=x86, ssm_calls=stub.calls)
        assert arm == "ami-0armpin000000001", observed
        assert x86 == ssm_value, observed
        assert stub.calls == [FROZEN_2204_PARAMETER["x86_64"]], observed


# ===========================================================================
# Req 3.3 — ephemeral bootstrap seam (frozen byte-level user-data)
# ===========================================================================

class TestRunnerBootstrapBaseline:
    """**Property 3: Preservation** — `runner_bootstrap_user_data`
    output text, byte-identical to the frozen template recorded from
    unfixed code, for generated jobs of ALL supported targets, refs,
    repo dirs, and regions; still ending with the root-written
    Bootstrap_Marker statement.

    **Validates: Requirements 3.3**
    """

    @given(target=st.sampled_from(ALL_TARGETS),
           source_ref=source_refs,
           repo_dir=repo_dirs,
           repo_url=repo_urls,
           region=regions)
    @settings(max_examples=150, deadline=None)
    def test_bootstrap_text_is_byte_identical_to_frozen_oracle(
            self, target, source_ref, repo_dir, repo_url, region):
        job = {"build_job_id": "job-preserve-2", "build_target": target,
               "execution_mode": "ephemeral",
               "config_snapshot": {"source_ref": source_ref}}
        with mock.patch.object(build_dispatcher, "BUILD_REPO_URL",
                               repo_url), \
                mock.patch.dict(os.environ, {"AWS_REGION": region}):
            text = build_dispatcher.runner_bootstrap_user_data(
                job, repo_dir=repo_dir)
        expected = frozen_runner_bootstrap(
            repo_url, repo_dir,
            source_ref if isinstance(source_ref, str) else "", region)
        observed = _observation(target=target, source_ref=source_ref,
                                repo_dir=repo_dir, repo_url=repo_url,
                                region=region, text=text,
                                expected=expected)
        assert text == expected, observed
        # The trailing root-written Bootstrap_Marker statement.
        assert text.rstrip("\n").endswith(FROZEN_MARKER_STATEMENT), observed

    @given(target=st.sampled_from(ALL_TARGETS), source_ref=source_refs,
           repo_dir=repo_dirs, repo_url=repo_urls, region=regions)
    @settings(max_examples=100, deadline=None)
    def test_bootstrap_text_is_target_independent(
            self, target, source_ref, repo_dir, repo_url, region):
        """Recorded: the target NEVER changes the bootstrap text — for
        the same (url, dir, ref, region) every target emits the same
        bytes (this is what lets the fix keep the seam untouched)."""
        texts = set()
        with mock.patch.object(build_dispatcher, "BUILD_REPO_URL",
                               repo_url), \
                mock.patch.dict(os.environ, {"AWS_REGION": region}):
            for candidate in (target, "JP5", "JP7"):
                job = {"build_job_id": "job-preserve-2",
                       "build_target": candidate,
                       "execution_mode": "ephemeral",
                       "config_snapshot": {"source_ref": source_ref}}
                texts.add(build_dispatcher.runner_bootstrap_user_data(
                    job, repo_dir=repo_dir))
        assert len(texts) == 1, _observation(
            target=target, source_ref=source_ref, repo_dir=repo_dir,
            repo_url=repo_url, region=region, distinct_texts=len(texts))

    def test_no_repo_url_still_means_no_user_data(self):
        """Recorded pre-baked-AMI mode: no configured repository, no
        bootstrap text."""
        with mock.patch.object(build_dispatcher, "BUILD_REPO_URL", ""):
            assert build_dispatcher.runner_bootstrap_user_data(
                {"build_job_id": "j", "build_target": "JP5",
                 "execution_mode": "ephemeral"}) == ""

    def test_snapshot_region_fallback_when_env_absent(self):
        """Recorded: with no dispatcher-env region the job snapshot's
        region is exported; with neither, no region export lines."""
        job = {"build_job_id": "job-preserve-2", "build_target": "JP6",
               "execution_mode": "ephemeral",
               "config_snapshot": {"source_ref": "main",
                                   "region": "eu-central-1"}}
        environ = {key: value for key, value in os.environ.items()
                   if key != "AWS_REGION"}
        with mock.patch.object(build_dispatcher, "BUILD_REPO_URL",
                               "https://github.com/example/dda"), \
                mock.patch.dict(os.environ, environ, clear=True):
            with_snapshot = build_dispatcher.runner_bootstrap_user_data(
                job, repo_dir="/home/ubuntu/dda")
            job_no_region = dict(job,
                                 config_snapshot={"source_ref": "main"})
            without_any = build_dispatcher.runner_bootstrap_user_data(
                job_no_region, repo_dir="/home/ubuntu/dda")
        assert with_snapshot == frozen_runner_bootstrap(
            "https://github.com/example/dda", "/home/ubuntu/dda", "main",
            "eu-central-1"), _observation(text=with_snapshot)
        assert without_any == frozen_runner_bootstrap(
            "https://github.com/example/dda", "/home/ubuntu/dda", "main",
            ""), _observation(text=without_any)


# ===========================================================================
# Req 3.8 — fleet-launch bootstrap seam (frozen byte-level user-data)
# ===========================================================================

class TestFleetUserDataBaseline:
    """**Property 3: Preservation** — `build_fleet.render_user_data`
    output text, byte-identical to the frozen template recorded from
    unfixed code, for generated repository/dir/ref inputs. The launch
    handler renders this same text for `ubuntu_version` 22.04 and
    ABSENT alike (recorded: the release is not an input to the
    rendering at all), so this baseline covers both launch shapes.

    **Validates: Requirements 3.8**
    """

    @given(repo_url=repo_urls, repo_dir=repo_dirs, source_ref=source_refs)
    @settings(max_examples=150, deadline=None)
    def test_rendered_text_is_byte_identical_to_frozen_oracle(
            self, repo_url, repo_dir, source_ref):
        rendered = build_fleet.render_user_data(
            repo_url, repo_dir=repo_dir, source_ref=source_ref)
        expected = frozen_fleet_user_data(repo_url, repo_dir, source_ref)
        assert rendered == expected, _observation(
            repo_url=repo_url, repo_dir=repo_dir, source_ref=source_ref,
            rendered=rendered, expected=expected)

    def test_default_directory_render_matches_frozen_oracle(self):
        """The no-dir, no-ref render (the legacy launch shape) against
        the frozen oracle at the recorded default clone location."""
        url = "https://github.com/awslabs/DefectDetectionApplication"
        rendered = build_fleet.render_user_data(url)
        expected = frozen_fleet_user_data(
            url, "/home/ubuntu/DefectDetectionApplication", None)
        assert rendered == expected, _observation(
            rendered=rendered, expected=expected)

    def test_rendering_takes_no_release_input(self):
        """Recorded observation pinning the 22.04/absent equivalence:
        the render signature carries repository, directory and ref ONLY
        — there is no release parameter whose value could fork the
        bootstrap text at launch time."""
        import inspect
        parameters = list(
            inspect.signature(build_fleet.render_user_data).parameters)
        assert parameters == ["repo_url", "repo_dir", "source_ref"], \
            _observation(parameters=parameters)


# ===========================================================================
# Req 3.5 / 3.9 — dedicated validation outcomes (frozen rule oracle)
# ===========================================================================

class TestValidationBaseline:
    """**Property 3: Preservation** — `validate_build_request`
    accept/reject outcomes and ORDERED error rule ids over generated
    requests and fleets for every target/mode combination other than
    JP7+dedicated, frozen from unfixed code; and the recorded
    `ubuntu_version` invariance for JP5/JP6 dedicated requests.

    **Validates: Requirements 3.5, 3.9**
    """

    @given(targets=target_lists, mode=execution_modes,
           server_id=server_id_choices, servers=servers_lists)
    @settings(max_examples=200, deadline=None)
    def test_outcomes_match_frozen_rule_oracle(self, targets, mode,
                                               server_id, servers):
        # The ONE combination the fix may flip is excluded: JP7 in a
        # dedicated-mode request. Everything else is frozen.
        assume(not (mode == "dedicated" and "JP7" in targets))
        body = {"targets": targets}
        if mode is not None:
            body["execution_mode"] = mode
        if server_id is not None:
            body["server_id"] = server_id

        result = build_domain.validate_build_request(body, servers)

        expected_valid, expected_rules = frozen_validation_rules(
            body, servers)
        observed = _observation(
            body=body, servers=servers, valid=result.valid,
            rules=[e["rule"] for e in result.errors],
            expected_valid=expected_valid, expected_rules=expected_rules)
        assert result.valid == expected_valid, observed
        assert tuple(e["rule"] for e in result.errors) == expected_rules, \
            observed

    @given(target=st.sampled_from(["JP5", "JP6"]),
           state=server_states, arch=server_arches)
    @settings(max_examples=100, deadline=None)
    def test_jp5_jp6_dedicated_outcome_invariant_to_ubuntu_version(
            self, target, state, arch):
        """For the SAME JP5/JP6 dedicated request, fleets differing only
        in the selected server's recorded release ('22.04', '24.04', an
        arbitrary other string, ABSENT) produce the SAME outcome and the
        SAME rules — recorded: the release is not a validation input."""
        body = {"targets": [target], "execution_mode": "dedicated",
                "server_id": "srv-1"}
        outcomes = []
        for release in ("22.04", "24.04", "20.99-custom", None):
            servers = [_server("srv-1", state, arch, release)]
            result = build_domain.validate_build_request(body, servers)
            outcomes.append(
                (result.valid, tuple(e["rule"] for e in result.errors)))
        observed = _observation(target=target, state=state, arch=arch,
                                outcomes=outcomes)
        assert len(set(outcomes)) == 1, observed
        # And the outcome itself is the frozen one: accepted iff the
        # server is running arm64 (JP5/JP6 require arm64), any release.
        expected_valid = (state == "running" and arch == "arm64")
        assert outcomes[0][0] is expected_valid, observed

    def test_jp5_dedicated_accepted_on_2204_2404_and_fieldless(self):
        """The concrete recorded acceptances of Req 3.9."""
        body = {"targets": ["JP5"], "execution_mode": "dedicated",
                "server_id": "srv-1"}
        for release in ("22.04", "24.04", None):
            servers = [_server("srv-1", "running", "arm64", release)]
            result = build_domain.validate_build_request(body, servers)
            assert result.valid, _observation(release=release,
                                              errors=result.errors)


# ===========================================================================
# Req 3.8 — the jammy command sequence of setup-build-server.sh
# ===========================================================================

def _jammy_effective_text():
    """The script text under its 22.04 (jammy) expansion: any
    release-keyed $PIP_BREAK_FLAG expands to the empty string (on a
    detected-22.04 host the flag is empty by design), runs of blanks
    collapsed. On the unfixed script this normalization is an identity
    apart from whitespace."""
    with open(SETUP_SCRIPT_PATH, "r", encoding="utf-8") as handle:
        text = handle.read()
    text = re.sub(r'"?\$\{?PIP_BREAK_FLAG\}?"?', "", text)
    return re.sub(r"[ \t]+", " ", text)


class TestSetupScriptJammySequenceBaseline:
    """**Property 3: Preservation** — the frozen jammy effective command
    sequence of `setup-build-server.sh`: the exact three PEP
    668-affected pip install command lines and the snap docker-compose
    branch, recorded from the checked-in script. The script is only
    ever READ here, never executed.

    **Validates: Requirements 3.8**
    """

    def test_the_three_pip_install_command_lines(self):
        text = _jammy_effective_text()
        for frozen in (FROZEN_PIP_AWSCLI, FROZEN_PIP_BOTOCORE,
                       FROZEN_PIP_GDK):
            assert frozen in text, _observation(
                missing=frozen,
                note="the jammy effective pip command line changed")
        # Recorded relative order: awscli, then botocore[crt], then GDK.
        assert text.index(FROZEN_PIP_AWSCLI) \
            < text.index(FROZEN_PIP_BOTOCORE) \
            < text.index(FROZEN_PIP_GDK), _observation(
                note="the recorded pip install order changed")

    def test_the_snap_docker_compose_branch(self):
        text = _jammy_effective_text()
        guard = text.find(FROZEN_COMPOSE_GUARD)
        snap = text.find(FROZEN_COMPOSE_SNAP_INSTALL)
        observed = _observation(guard_index=guard, snap_index=snap)
        # The jammy branch still exists: the docker-compose presence
        # guard, and the snap install reachable behind it.
        assert guard != -1, observed
        assert snap != -1, observed
        assert guard < snap, observed


# ===========================================================================
# Task 6.1 — Property 3 re-checked on the FIXED code (additive section).
#
# Everything above this line is the task-2 frozen oracle and is
# IMMUTABLE. The classes below EXTEND the file for task 6.1: they
# assert the FIXED `plan_runner` and `resolve_ami` against those same
# frozen constants, plus the two behaviors that only exist post-fix —
# the plan's `os_release` field equals the '22.04' default for every
# non-JP7 target, and the widened `resolve_ami(arch, os_release)`
# signature resolves 22.04 through the identical order whether the
# release is defaulted or spelled explicitly, indifferent to the new
# noble-only override.
# ===========================================================================

#: The recorded default OS release (re-spelled, not read back from the
#: modules under test): the release every pre-fix runner was implicitly
#: provisioned on, now the `RunnerPlan.os_release` default.
FROZEN_DEFAULT_OS_RELEASE = "22.04"


class RecordingEC2:
    """In-process EC2 stand-in: records DescribeImages calls so the
    tests can assert the 22.04 path NEVER reaches the noble fallback.
    Nothing leaves the process."""

    def __init__(self):
        self.describe_calls = []

    def describe_images(self, **kwargs):
        self.describe_calls.append(kwargs)
        return {"Images": []}


class TestPlanRunnerPreservationOnFixedCode:
    """**Property 3: Preservation** — Non-JP7 planning unchanged on the
    FIXED `plan_runner`: for any non-JP7 ephemeral Build_Job (generated
    config_snapshots, sizing, spot flags) the plan carries the same
    arch, instance type, volume size, spot flag, and status as the
    task-2 frozen matrix, and the additive `os_release` field equals
    the '22.04' default.

    **Validates: Requirements 3.4**
    """

    @given(target=st.sampled_from(sorted(FROZEN_PLAN_MATRIX)),
           snapshot=config_snapshots)
    @settings(max_examples=120, deadline=None)
    def test_non_jp7_plan_matches_frozen_matrix_and_default_release(
            self, target, snapshot):
        arch, type_key, default_type = FROZEN_PLAN_MATRIX[target]
        job = {"build_job_id": "job-preserve-61", "build_target": target,
               "execution_mode": "ephemeral"}
        if snapshot is not None:
            job["config_snapshot"] = snapshot
        snap = snapshot or {}

        plan = build_planner.plan_runner(job)

        observed = _observation(target=target, snapshot=snapshot,
                                plan=str(plan))
        assert plan.build_job_id == "job-preserve-61", observed
        assert plan.arch == arch, observed
        assert plan.instance_type == (snap.get(type_key) or default_type), \
            observed
        expected_volume = snap.get("volume_size_gb")
        if expected_volume is None:
            expected_volume = FROZEN_DEFAULT_VOLUME_GB
        assert plan.volume_size_gb == expected_volume, observed
        assert plan.spot is bool(snap.get("use_spot_for_ephemeral", False)), \
            observed
        assert plan.status == FROZEN_PLAN_STATUS, observed
        # The one post-fix addition: the derived release for every
        # non-JP7 target IS the frozen default the pre-fix code
        # implicitly provisioned on.
        assert plan.os_release == FROZEN_DEFAULT_OS_RELEASE, observed

    def test_runner_plan_defaults_os_release_to_2204(self):
        """The additive field carries the frozen default, so a plan
        constructed without it (any pre-existing construction shape)
        is a 22.04 plan."""
        plan = build_planner.RunnerPlan(
            build_job_id="job-preserve-61", arch="arm64",
            instance_type="m6g.4xlarge", volume_size_gb=100,
            spot=False, status="provisioning")
        assert plan.os_release == FROZEN_DEFAULT_OS_RELEASE, \
            _observation(plan=str(plan))


class TestResolveAmiPreservationOnFixedCode:
    """**Property 3: Preservation** — 22.04 AMI resolution unchanged on
    the FIXED `resolve_ami`: for any env-override configuration
    (including the new noble-only `BUILD_ARM64_NOBLE_AMI_ID`, which
    must be invisible to jammy resolution) and both architectures, the
    same AMI id is produced through the same frozen order — env
    override (zero AWS calls, nothing cached), else per-container
    cache, else exactly the frozen per-architecture 22.04 SSM parameter
    — whether the release argument is defaulted or spelled '22.04'
    explicitly, with the cache-hit on a second call and the noble
    DescribeImages fallback never reached.

    **Validates: Requirements 3.1, 3.2**
    """

    @staticmethod
    def _patched(arm_override, x86_override, noble_override, ssm_stub,
                 ec2_stub):
        return (
            mock.patch.object(build_dispatcher, "ssm", ssm_stub),
            mock.patch.object(build_dispatcher, "ec2", ec2_stub),
            mock.patch.object(build_dispatcher, "BUILD_ARM64_AMI_ID",
                              arm_override),
            mock.patch.object(build_dispatcher, "BUILD_X86_64_AMI_ID",
                              x86_override),
            mock.patch.object(build_dispatcher, "BUILD_ARM64_NOBLE_AMI_ID",
                              noble_override),
            mock.patch.dict(build_dispatcher._AMI_CACHE, clear=True),
        )

    @given(arch=st.sampled_from(["arm64", "x86_64"]),
           arm_override=st.one_of(st.just(""), ami_ids),
           x86_override=st.one_of(st.just(""), ami_ids),
           noble_override=st.one_of(st.just(""), ami_ids),
           ssm_value=ami_ids,
           explicit_release=st.booleans())
    @settings(max_examples=120, deadline=None)
    def test_2204_resolution_order_unchanged(
            self, arch, arm_override, x86_override, noble_override,
            ssm_value, explicit_release):
        ssm_stub = RecordingSSM(value=ssm_value)
        ec2_stub = RecordingEC2()
        patches = self._patched(arm_override, x86_override,
                                noble_override, ssm_stub, ec2_stub)
        with patches[0], patches[1], patches[2], patches[3], \
                patches[4], patches[5]:
            if explicit_release:
                first = build_dispatcher.resolve_ami(arch, "22.04")
            else:
                first = build_dispatcher.resolve_ami(arch)
            cache_after_first = dict(build_dispatcher._AMI_CACHE)
            second = build_dispatcher.resolve_ami(arch)

        override = arm_override if arch == "arm64" else x86_override
        observed = _observation(
            arch=arch, arm_override=arm_override,
            x86_override=x86_override, noble_override=noble_override,
            explicit_release=explicit_release, first=first,
            second=second, ssm_calls=ssm_stub.calls,
            describe_calls=ec2_stub.describe_calls,
            cache=cache_after_first)
        if override:
            # The frozen head of the order: the per-architecture jammy
            # env override wins, zero AWS calls, nothing cached.
            assert first == override, observed
            assert second == override, observed
            assert ssm_stub.calls == [], observed
            assert cache_after_first == {}, observed
        else:
            # Else exactly ONE read of the frozen 22.04 parameter, the
            # value cached under the frozen bare-architecture key, and
            # the second call served from that cache (no second read).
            assert first == ssm_value, observed
            assert second == ssm_value, observed
            assert ssm_stub.calls == [FROZEN_2204_PARAMETER[arch]], observed
            assert cache_after_first.get(arch) == ssm_value, observed
        # The noble DescribeImages fallback is never reached by 22.04
        # resolution, and the exact-value assertions above already pin
        # the result to the jammy override/parameter — so the noble-only
        # BUILD_ARM64_NOBLE_AMI_ID is invisible in every configuration.
        assert ec2_stub.describe_calls == [], observed

    @given(arch=st.sampled_from(["arm64", "x86_64"]),
           arm_override=st.one_of(st.just(""), ami_ids),
           x86_override=st.one_of(st.just(""), ami_ids),
           noble_override=st.one_of(st.just(""), ami_ids),
           ssm_value=ami_ids)
    @settings(max_examples=100, deadline=None)
    def test_default_release_call_equals_explicit_2204_call(
            self, arch, arm_override, x86_override, noble_override,
            ssm_value):
        """The widened signature preserves the pre-fix call shape: for
        the same configuration, `resolve_ami(arch)` and
        `resolve_ami(arch, '22.04')` produce the same AMI id through
        the same recorded resolution order."""
        outcomes = []
        for call in (lambda: build_dispatcher.resolve_ami(arch),
                     lambda: build_dispatcher.resolve_ami(arch, "22.04")):
            ssm_stub = RecordingSSM(value=ssm_value)
            ec2_stub = RecordingEC2()
            patches = self._patched(arm_override, x86_override,
                                    noble_override, ssm_stub, ec2_stub)
            with patches[0], patches[1], patches[2], patches[3], \
                    patches[4], patches[5]:
                ami = call()
                cache = dict(build_dispatcher._AMI_CACHE)
            outcomes.append((ami, tuple(ssm_stub.calls),
                             tuple(map(str, ec2_stub.describe_calls)),
                             tuple(sorted(cache.items()))))
        observed = _observation(
            arch=arch, arm_override=arm_override,
            x86_override=x86_override, noble_override=noble_override,
            default_call=outcomes[0], explicit_call=outcomes[1])
        assert outcomes[0] == outcomes[1], observed


# ===========================================================================
# Task 6.2 — Property 4 re-checked on the FIXED code (additive section).
#
# Everything above the task 6.1 banner is the task-2 frozen oracle and
# is IMMUTABLE. The class below EXTENDS the file for task 6.2: it
# asserts the FIXED `runner_bootstrap_user_data` against the same
# frozen byte-level oracle (`frozen_runner_bootstrap`), for ANY
# supported target INCLUDING JP7 — the noble host deltas must reach a
# noble runner NOT through user-data text but because the emitted
# build-user body executes `setup-build-server.sh`, the one shared
# seam. So the emitted text must (a) be byte-identical to the task-2
# oracle, (b) still end with the root-written Bootstrap_Marker
# statement, (c) still invoke the shared seam, and (d) carry NO copy
# of any noble delta text.
# ===========================================================================

#: The shared-seam invocation recorded inside the frozen build-user
#: body (re-spelled): both execution modes reach the noble deltas ONLY
#: by executing this script.
FROZEN_SHARED_SEAM_INVOCATION = "bash ./setup-build-server.sh"

#: Noble delta text that must NEVER appear in the emitted user-data —
#: the deltas live once, release-keyed, inside setup-build-server.sh
#: (design Property 6); any copy here would be the second divergent
#: implementation Req 2.8 prohibits.
NOBLE_DELTA_FRAGMENTS = (
    "--break-system-packages",
    'exec docker compose "$@"',
    "/usr/local/bin/docker-compose",
)


class TestRunnerBootstrapByteIdentityOnFixedCode:
    """**Property 4: Preservation** — Ephemeral bootstrap text
    byte-identical, deltas via the shared seam: for any ephemeral
    Build_Job of ANY supported target (generated source refs, repo
    dirs, regions), the FIXED `runner_bootstrap_user_data` emits text
    byte-identical to the task-2 frozen oracle, still ending with the
    root-written Bootstrap_Marker statement; the noble runner reaches
    the deltas only because the emitted build-user body executes
    `setup-build-server.sh`.

    **Validates: Requirements 2.4, 3.3**
    """

    @staticmethod
    def _emit(target, source_ref, repo_dir, repo_url, region):
        job = {"build_job_id": "job-preserve-62", "build_target": target,
               "execution_mode": "ephemeral",
               "config_snapshot": {"source_ref": source_ref}}
        with mock.patch.object(build_dispatcher, "BUILD_REPO_URL",
                               repo_url), \
                mock.patch.dict(os.environ, {"AWS_REGION": region}):
            return build_dispatcher.runner_bootstrap_user_data(
                job, repo_dir=repo_dir)

    @given(target=st.sampled_from(ALL_TARGETS),
           source_ref=source_refs,
           repo_dir=repo_dirs,
           repo_url=repo_urls,
           region=regions)
    @settings(max_examples=120, deadline=None)
    def test_fixed_bootstrap_byte_identical_to_frozen_oracle(
            self, target, source_ref, repo_dir, repo_url, region):
        """The fixed emitter, for EVERY supported target (JP7
        included), produces bytes identical to the task-2 frozen
        oracle, ending with the frozen Bootstrap_Marker statement."""
        text = self._emit(target, source_ref, repo_dir, repo_url, region)
        expected = frozen_runner_bootstrap(
            repo_url, repo_dir,
            source_ref if isinstance(source_ref, str) else "", region)
        observed = _observation(target=target, source_ref=source_ref,
                                repo_dir=repo_dir, repo_url=repo_url,
                                region=region, text=text,
                                expected=expected)
        assert text == expected, observed
        assert text.rstrip("\n").endswith(FROZEN_MARKER_STATEMENT), observed

    @given(target=st.sampled_from(ALL_TARGETS),
           source_ref=source_refs,
           repo_dir=repo_dirs,
           repo_url=repo_urls,
           region=regions)
    @settings(max_examples=120, deadline=None)
    def test_deltas_reachable_only_through_the_shared_seam(
            self, target, source_ref, repo_dir, repo_url, region):
        """The emitted build-user body still executes the one shared
        seam (`setup-build-server.sh`) — that execution, not any
        user-data text, is how a noble runner receives the noble
        deltas — and the emitted text carries NO copy of any delta."""
        text = self._emit(target, source_ref, repo_dir, repo_url, region)
        observed = _observation(target=target, source_ref=source_ref,
                                repo_dir=repo_dir, repo_url=repo_url,
                                region=region, text=text)
        assert FROZEN_SHARED_SEAM_INVOCATION in text, observed
        for fragment in NOBLE_DELTA_FRAGMENTS:
            assert fragment not in text, _observation(
                target=target, source_ref=source_ref, repo_dir=repo_dir,
                repo_url=repo_url, region=region, leaked_delta=fragment,
                text=text)


# ===========================================================================
# Task 6.3 — Property 7 re-checked on the FIXED code (additive section).
#
# **Feature: jp7-ephemeral-runner-provisioning, Property 7: 22.04
# bootstrap byte-identical in both seams**
#
# Everything above the task 6.1 banner is the task-2 frozen oracle and
# is IMMUTABLE. The classes below EXTEND the file for task 6.3: they
# assert the FIXED fleet-launch seam (`build_fleet.render_user_data` /
# `USER_DATA_TEMPLATE`) and the FIXED `setup-build-server.sh` against
# those same frozen oracles — for any fleet launch with
# `ubuntu_version` 22.04 or ABSENT the rendered bootstrap text is
# byte-identical to the task-2 `frozen_fleet_user_data` oracle, and on
# a detected 22.04 host the fixed script executes the same effective
# command sequence as the frozen jammy oracle: the release-keyed
# $PIP_BREAK_FLAG expanding to nothing so the three install command
# lines are today's exactly, and the snap `docker-compose` branch
# reached exactly as today. The script is only ever READ as text here,
# never executed.
# ===========================================================================

#: The recorded default clone location the fleet-launch template's
#: pre-bound directory points at (re-spelled from the task-2 recorded
#: default-directory render, not read back from the module under test).
FROZEN_DEFAULT_FLEET_REPO_DIR = "/home/ubuntu/DefectDetectionApplication"

#: The two launch shapes of Req 3.8 whose rendering must stay
#: byte-identical to today: `ubuntu_version` '22.04' and ABSENT (None).
jammy_launch_releases = st.sampled_from(["22.04", None])

#: Detected host releases a jammy-preservation host may report: 22.04
#: itself, a failed detection (empty), and any other non-24.04 string —
#: on ALL of them the release-keyed flag must expand to nothing.
non_noble_detected_releases = st.one_of(
    st.just("22.04"),
    st.just(""),
    st.sampled_from(["20.04", "23.10", "18.04"]),
    st.text(alphabet="0123456789.", min_size=1, max_size=8).filter(
        lambda s: s != "24.04"),
)


def _setup_script_text():
    with open(SETUP_SCRIPT_PATH, "r", encoding="utf-8") as handle:
        return handle.read()


def _derived_pip_break_flag(script_text, detected_release):
    """The $PIP_BREAK_FLAG a host reporting `detected_release` derives,
    read off the script TEXT (never executed). First PIN the frozen
    derivation shape — the flag starts empty and is reassigned exactly
    once, behind the detected-24.04 guard — then evaluate it. Any other
    assignment shape fails here rather than silently mis-simulating."""
    derivation = re.search(
        r"PIP_BREAK_FLAG=''\s*\n"
        r"if \[ \"\$OS_RELEASE\" = \"24\.04\" \]; then\s*\n"
        r"\s*PIP_BREAK_FLAG='--break-system-packages'\s*\n"
        r"fi",
        script_text)
    assert derivation is not None, _observation(
        note="the release-keyed PIP_BREAK_FLAG derivation shape changed")
    assert len(re.findall(r"PIP_BREAK_FLAG=", script_text)) == 2, \
        _observation(
            note="an additional PIP_BREAK_FLAG assignment appeared")
    if detected_release == "24.04":
        return "--break-system-packages"
    return ""


def _effective_text_for_flag(script_text, flag_value):
    """The script text under a concrete flag expansion (the task-2
    `_jammy_effective_text` normalization, generalized): every
    `$PIP_BREAK_FLAG` spelling replaced by `flag_value`, runs of blanks
    collapsed — exactly what a host that derived `flag_value` runs."""
    text = re.sub(r'"?\$\{?PIP_BREAK_FLAG\}?"?', flag_value, script_text)
    return re.sub(r"[ \t]+", " ", text)


class TestFleetLaunchByteIdentityOnFixedCode:
    """**Property 7: Preservation** — 22.04 bootstrap byte-identical in
    both seams (the fleet-launch seam): for any fleet launch with
    `ubuntu_version` 22.04 or ABSENT (generated repository/dir/ref),
    the FIXED `render_user_data`/`USER_DATA_TEMPLATE` renders text
    byte-identical to the task-2 frozen oracle.

    **Validates: Requirements 3.3, 3.8**
    """

    @given(repo_url=repo_urls, repo_dir=repo_dirs, source_ref=source_refs,
           launch_release=jammy_launch_releases)
    @settings(max_examples=120, deadline=None)
    def test_fixed_render_byte_identical_for_2204_and_absent_launches(
            self, repo_url, repo_dir, source_ref, launch_release):
        """The generated launch release ('22.04' or ABSENT) cannot
        reach the fixed rendering at all — the signature still carries
        repository, directory and ref ONLY — so both launch shapes of
        Req 3.8 render the ONE frozen byte sequence."""
        import inspect
        parameters = list(
            inspect.signature(build_fleet.render_user_data).parameters)
        assert parameters == ["repo_url", "repo_dir", "source_ref"], \
            _observation(launch_release=launch_release,
                         parameters=parameters)
        rendered = build_fleet.render_user_data(
            repo_url, repo_dir=repo_dir, source_ref=source_ref)
        expected = frozen_fleet_user_data(repo_url, repo_dir, source_ref)
        assert rendered == expected, _observation(
            launch_release=launch_release, repo_url=repo_url,
            repo_dir=repo_dir, source_ref=source_ref, rendered=rendered,
            expected=expected)

    @given(repo_url=repo_urls, launch_release=jammy_launch_releases)
    @settings(max_examples=100, deadline=None)
    def test_fixed_template_renders_byte_identical_at_default_shape(
            self, repo_url, launch_release):
        """The module-level USER_DATA_TEMPLATE — the constant every
        legacy `.format(repo_url=...)` caller renders — is still the
        frozen bytes at the recorded default clone location with no
        ref, for a 22.04 and an ABSENT-release launch alike."""
        rendered = build_fleet.USER_DATA_TEMPLATE.format(repo_url=repo_url)
        expected = frozen_fleet_user_data(
            repo_url, FROZEN_DEFAULT_FLEET_REPO_DIR, None)
        assert rendered == expected, _observation(
            launch_release=launch_release, repo_url=repo_url,
            rendered=rendered, expected=expected)

    @given(repo_url=repo_urls, repo_dir=repo_dirs, source_ref=source_refs)
    @settings(max_examples=100, deadline=None)
    def test_rendered_text_carries_no_noble_delta_copy(
            self, repo_url, repo_dir, source_ref):
        """The jammy rendering stays byte-identical BECAUSE no delta
        text was added to the launch seam: the rendered body still
        executes `setup-build-server.sh` and contains no copy of any
        noble delta fragment."""
        rendered = build_fleet.render_user_data(
            repo_url, repo_dir=repo_dir, source_ref=source_ref)
        observed = _observation(repo_url=repo_url, repo_dir=repo_dir,
                                source_ref=source_ref, rendered=rendered)
        assert "./setup-build-server.sh" in rendered, observed
        for fragment in NOBLE_DELTA_FRAGMENTS:
            assert fragment not in rendered, _observation(
                repo_url=repo_url, repo_dir=repo_dir,
                source_ref=source_ref, leaked_delta=fragment,
                rendered=rendered)


class TestSetupScriptJammySequenceOnFixedCode:
    """**Property 7: Preservation** — 22.04 bootstrap byte-identical in
    both seams (the shared-script seam): on a detected 22.04 host the
    FIXED `setup-build-server.sh` executes the same effective command
    sequence as the frozen jammy oracle — the release-keyed flag
    expanding to nothing so the three install command lines are today's
    exactly, and the snap `docker-compose` branch reached exactly as
    today. The script is only ever READ here, never executed.

    **Validates: Requirements 3.3, 3.8**
    """

    @given(detected_release=non_noble_detected_releases)
    @settings(max_examples=100, deadline=None)
    def test_flag_expands_to_nothing_and_pip_lines_are_todays_exactly(
            self, detected_release):
        """For ANY detected non-24.04 release (22.04 first among them,
        plus failed detection and arbitrary other strings) the
        release-keyed flag derives to the empty string, and the
        resulting effective text carries the three frozen pip install
        command lines exactly, in the frozen order."""
        script_text = _setup_script_text()
        flag = _derived_pip_break_flag(script_text, detected_release)
        assert flag == "", _observation(
            detected_release=detected_release, flag=flag)
        effective = _effective_text_for_flag(script_text, flag)
        observed = _observation(detected_release=detected_release)
        for frozen in (FROZEN_PIP_AWSCLI, FROZEN_PIP_BOTOCORE,
                       FROZEN_PIP_GDK):
            assert frozen in effective, _observation(
                detected_release=detected_release, missing=frozen)
        assert effective.index(FROZEN_PIP_AWSCLI) \
            < effective.index(FROZEN_PIP_BOTOCORE) \
            < effective.index(FROZEN_PIP_GDK), observed

    def test_jammy_effective_expansion_matches_the_frozen_oracle_helper(self):
        """The detected-22.04 expansion IS the task-2 frozen jammy
        normalization: deriving the flag for '22.04' and substituting
        it reproduces `_jammy_effective_text()` byte-for-byte."""
        script_text = _setup_script_text()
        flag = _derived_pip_break_flag(script_text, "22.04")
        assert _effective_text_for_flag(script_text, flag) \
            == _jammy_effective_text(), _observation(
                note="the jammy effective expansion diverged from the "
                     "task-2 frozen normalization")

    def test_snap_docker_compose_branch_reached_exactly_as_today(self):
        """The jammy effective text still reaches the snap
        docker-compose install exactly as today: the presence guard
        first, and the snap install behind the ELSE of the detected-
        24.04 test — the branch every non-24.04 host takes."""
        text = _jammy_effective_text()
        guard = text.find(FROZEN_COMPOSE_GUARD)
        snap = text.find(FROZEN_COMPOSE_SNAP_INSTALL)
        release_test = text.find('if [ "$OS_RELEASE" = "24.04" ]', guard)
        observed = _observation(guard_index=guard, snap_index=snap,
                                release_test_index=release_test)
        assert guard != -1, observed
        assert snap != -1, observed
        assert release_test != -1, observed
        # Recorded reachability order: presence guard, then the release
        # test, then — on its non-24.04 arm — the snap install.
        assert guard < release_test < snap, observed
        else_index = text.rfind("else", release_test, snap)
        assert else_index != -1, _observation(
            note="the snap install is no longer behind the release "
                 "test's else arm", **{"release_test": release_test,
                                       "snap": snap})
