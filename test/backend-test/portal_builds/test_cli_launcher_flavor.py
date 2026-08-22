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
"""CLI launcher --flavor tests (ubuntu-pro-build-servers, task 6.2).

Drives the REAL ``edge-cv-portal/launch-arm64-build-server.sh`` with a
PATH-shimmed fake ``aws`` executable that records every invocation to a
log file and returns scripted output — no real AWS API is ever touched.

Asserted behavior (design section "5. launch-arm64-build-server.sh"):

* ``--flavor pro`` issues exactly one ``ec2 describe-images`` with the
  ``ubuntu-pro-server`` name pattern and never queries the standard
  pattern; ``--flavor standard`` and a flavorless invocation mirror that
  with the standard pattern and no Pro query (Req 5.2, 5.3).
* An unresolvable AMI exits nonzero naming the selected flavor and
  release, with no cross-flavor query and no ``run-instances`` (Req 5.4).
* An invalid flavor value exits nonzero naming ``pro`` / ``standard``
  before ANY aws invocation — the shim is never called (Req 5.5).
* ``--ami-id`` skips flavor-based resolution entirely (Req 5.7).
* The ``--dry-run`` configuration summary contains the selected flavor
  and the resolved AMI id (Req 5.6).
* The Pro name pattern is correct for every supported release —
  18.04/20.04/22.04 (``hvm-ssd``) and 24.04 (``hvm-ssd-gp3``) (Req 5.1,
  5.8).

**Validates: Requirements 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 5.7, 5.8**

HONESTY GUARD: the shim ``aws`` is first on PATH for every invocation, so
no test can reach a real AWS endpoint; the invalid-flavor tests assert the
shim log stayed EMPTY (zero aws calls of any kind).
"""
import os
import stat
import subprocess
import tempfile

import pytest

# ---------------------------------------------------------------------------
# Repo root resolution (this file lives at test/backend-test/portal_builds/).
# ---------------------------------------------------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.normpath(os.path.join(_HERE, "..", "..", ".."))
LAUNCHER = os.path.join(REPO_ROOT, "edge-cv-portal",
                        "launch-arm64-build-server.sh")

DEFAULT_AMI = "ami-0123456789abcdef0"

# Every release the launcher supports, with its codename and volume-path
# segment (hvm-ssd for 18.04/20.04/22.04, hvm-ssd-gp3 for 24.04 — Req 5.8).
RELEASES = [
    ("18.04", "bionic", "hvm-ssd"),
    ("20.04", "focal", "hvm-ssd"),
    ("22.04", "jammy", "hvm-ssd"),
    ("24.04", "noble", "hvm-ssd-gp3"),
]


def _pro_pattern(version, codename, ssd_path):
    return ("ubuntu-pro-server/images/{p}/ubuntu-{c}-{v}-arm64-pro-server-*"
            .format(p=ssd_path, c=codename, v=version))


def _standard_pattern(version, codename, ssd_path):
    return ("ubuntu/images/{p}/ubuntu-{c}-{v}-arm64-server-*"
            .format(p=ssd_path, c=codename, v=version))


# The shim records each invocation as one line of space-joined arguments,
# then returns scripted output per subcommand. describe-images output is
# scripted via AWS_SHIM_DESCRIBE_IMAGES_OUTPUT ("None" simulates the CLI's
# zero-match text output).
_AWS_SHIM = """#!/bin/sh
printf '%s\\n' "$*" >> "$AWS_SHIM_LOG"
case "$1 $2" in
  "ec2 describe-images")
    printf '%s\\n' "${AWS_SHIM_DESCRIBE_IMAGES_OUTPUT:-@DEFAULT_AMI@}"
    ;;
  "ec2 describe-vpcs")
    echo "vpc-shim0000000000000"
    ;;
  "ec2 create-security-group")
    echo "sg-shim00000000000000"
    ;;
  "ec2 run-instances")
    echo "i-shim000000000000000"
    ;;
  "ec2 describe-instances")
    echo "192.0.2.1"
    ;;
  *)
    # iam get-role / get-instance-profile etc: succeed silently (the
    # role and profile "already exist", so the launcher creates nothing).
    exit 0
    ;;
esac
""".replace("@DEFAULT_AMI@", DEFAULT_AMI)


class _Launch:
    """Result of one shimmed launcher invocation."""

    def __init__(self, proc, log_path):
        self.proc = proc
        self.out = proc.stdout + proc.stderr
        if os.path.isfile(log_path):
            with open(log_path, "r", encoding="utf-8") as fh:
                self.aws_calls = [ln for ln in fh.read().splitlines() if ln]
        else:
            self.aws_calls = []

    def calls(self, *prefix):
        joined = " ".join(prefix)
        return [c for c in self.aws_calls if c.startswith(joined)]


def _run_launcher(args, describe_images_output=None, timeout=60):
    """Run the real launcher with the fake ``aws`` first on PATH."""
    with tempfile.TemporaryDirectory() as tmp:
        shim_dir = os.path.join(tmp, "bin")
        os.mkdir(shim_dir)
        shim = os.path.join(shim_dir, "aws")
        with open(shim, "w", encoding="utf-8") as fh:
            fh.write(_AWS_SHIM)
        os.chmod(shim, os.stat(shim).st_mode | stat.S_IXUSR
                 | stat.S_IXGRP | stat.S_IXOTH)
        log_path = os.path.join(tmp, "aws-calls.log")
        env = dict(os.environ)
        env.pop("PYTHONHOME", None)
        env["PATH"] = shim_dir + os.pathsep + env.get("PATH", "")
        env["AWS_SHIM_LOG"] = log_path
        if describe_images_output is not None:
            env["AWS_SHIM_DESCRIBE_IMAGES_OUTPUT"] = describe_images_output
        proc = subprocess.run(  # nosec B603 — shimmed aws, no real API
            ["bash", LAUNCHER] + list(args),
            env=env, capture_output=True, text=True, timeout=timeout)
        return _Launch(proc, log_path)


# Baseline arguments: a key name (required), an explicit security group
# (skips SG creation) and --dry-run (no instance is ever launched).
BASE_ARGS = ["--key-name", "test-key", "--security-group-id", "sg-caller",
             "--dry-run"]


# ---------------------------------------------------------------------------
# Flavor-selected resolution: exactly one describe-images, right pattern,
# never the other flavor's pattern (Req 5.2, 5.3).
# ---------------------------------------------------------------------------

def test_flavor_pro_queries_exactly_one_pro_pattern_and_no_standard():
    """--flavor pro resolves via a single Pro describe-images query.

    **Validates: Requirements 5.2**
    """
    r = _run_launcher(BASE_ARGS + ["--flavor", "pro",
                                   "--ubuntu-version", "22.04"])
    assert r.proc.returncode == 0, r.out
    describe = r.calls("ec2", "describe-images")
    assert len(describe) == 1, describe
    assert _pro_pattern("22.04", "jammy", "hvm-ssd") in describe[0]
    assert all("ubuntu-pro-server" in c or "describe-images" not in c
               for c in r.aws_calls)
    # No standard-pattern query anywhere in the invocation log.
    assert not any(_standard_pattern("22.04", "jammy", "hvm-ssd") in c
                   for c in r.aws_calls)


def test_flavor_standard_queries_exactly_one_standard_pattern_and_no_pro():
    """--flavor standard resolves via a single standard query, no Pro.

    **Validates: Requirements 5.3**
    """
    r = _run_launcher(BASE_ARGS + ["--flavor", "standard",
                                   "--ubuntu-version", "22.04"])
    assert r.proc.returncode == 0, r.out
    describe = r.calls("ec2", "describe-images")
    assert len(describe) == 1, describe
    assert _standard_pattern("22.04", "jammy", "hvm-ssd") in describe[0]
    assert not any("ubuntu-pro-server" in c for c in r.aws_calls)


def test_no_flavor_defaults_to_standard_resolution():
    """A flavorless invocation resolves standard Ubuntu, no Pro query.

    **Validates: Requirements 5.3**
    """
    r = _run_launcher(BASE_ARGS)  # default --ubuntu-version 18.04
    assert r.proc.returncode == 0, r.out
    describe = r.calls("ec2", "describe-images")
    assert len(describe) == 1, describe
    assert _standard_pattern("18.04", "bionic", "hvm-ssd") in describe[0]
    assert not any("ubuntu-pro-server" in c for c in r.aws_calls)


# ---------------------------------------------------------------------------
# Unresolvable AMI fails closed: nonzero, names flavor and release, no
# cross-flavor query, no run-instances (Req 5.4).
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("flavor,other_marker", [
    ("pro", "ubuntu/images/"),
    ("standard", "ubuntu-pro-server"),
])
def test_unresolvable_ami_exits_nonzero_without_cross_flavor_or_launch(
        flavor, other_marker):
    """No AMI match: nonzero exit naming flavor+release, no fallback.

    **Validates: Requirements 5.4**
    """
    r = _run_launcher(BASE_ARGS + ["--flavor", flavor,
                                   "--ubuntu-version", "22.04"],
                      describe_images_output="None")
    assert r.proc.returncode != 0
    assert flavor in r.out
    assert "22.04" in r.out
    # Exactly the one flavor-selected query — never the other flavor's.
    assert len(r.calls("ec2", "describe-images")) == 1
    assert not any(other_marker in c for c in r.aws_calls)
    assert not r.calls("ec2", "run-instances")


# ---------------------------------------------------------------------------
# Invalid flavor: nonzero before ANY aws call, names pro/standard (Req 5.5).
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad_flavor", ["Pro", "PRO", "Standard", "",
                                        "ubuntu-pro", "enterprise"])
def test_invalid_flavor_exits_nonzero_before_any_aws_call(bad_flavor):
    """Any non-pro/standard value fails fast with zero aws invocations.

    **Validates: Requirements 5.5**
    """
    r = _run_launcher(["--key-name", "test-key", "--flavor", bad_flavor,
                       "--dry-run"])
    assert r.proc.returncode != 0
    assert "pro" in r.out and "standard" in r.out
    assert r.aws_calls == [], (
        "the launcher called aws before rejecting the invalid flavor: "
        + repr(r.aws_calls))


# ---------------------------------------------------------------------------
# --ami-id skips flavor-based resolution entirely (Req 5.7).
# ---------------------------------------------------------------------------

def test_explicit_ami_id_skips_resolution():
    """--ami-id launches from the supplied id with no describe-images.

    **Validates: Requirements 5.7**
    """
    r = _run_launcher(BASE_ARGS + ["--flavor", "pro",
                                   "--ami-id", "ami-caller0000000001"])
    assert r.proc.returncode == 0, r.out
    assert not r.calls("ec2", "describe-images")
    assert "ami-caller0000000001" in r.out


# ---------------------------------------------------------------------------
# Dry-run summary shows the flavor and the resolved AMI id (Req 5.6).
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("flavor", ["pro", "standard"])
def test_dry_run_summary_contains_flavor_and_resolved_ami(flavor):
    """The configuration summary prints flavor + AMI id under --dry-run.

    **Validates: Requirements 5.6**
    """
    r = _run_launcher(BASE_ARGS + ["--flavor", flavor,
                                   "--ubuntu-version", "22.04"])
    assert r.proc.returncode == 0, r.out
    summary_lines = [ln for ln in r.proc.stdout.splitlines()
                     if "Ubuntu Flavor:" in ln]
    assert summary_lines and flavor in summary_lines[0], r.proc.stdout
    ami_lines = [ln for ln in r.proc.stdout.splitlines()
                 if "AMI ID:" in ln]
    assert ami_lines and DEFAULT_AMI in ami_lines[0], r.proc.stdout
    assert "[DRY-RUN]" in r.proc.stdout


# ---------------------------------------------------------------------------
# Pro pattern per release, including hvm-ssd-gp3 for 24.04 (Req 5.1, 5.8).
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("version,codename,ssd_path", RELEASES)
def test_pro_pattern_correct_for_every_supported_release(
        version, codename, ssd_path):
    """Pro resolution uses the release's exact name pattern and segment.

    **Validates: Requirements 5.1, 5.8**
    """
    r = _run_launcher(BASE_ARGS + ["--flavor", "pro",
                                   "--ubuntu-version", version])
    assert r.proc.returncode == 0, r.out
    describe = r.calls("ec2", "describe-images")
    assert len(describe) == 1, describe
    assert _pro_pattern(version, codename, ssd_path) in describe[0]
