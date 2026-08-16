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
"""Threshold-gate property tests (task 4.2) for build-server-disk-pruning.

**Property 4: Fix Checking** — Post-Prune Threshold Gate.

**Validates: Requirements 2.3, 2.4**

Two legs, per design Property 4:

* **Script leg** — _for any_ generated post-prune free-space value (served
  by a stub ``df``) × any ``BUILD_MIN_FREE_DISK_GB`` setting (0, below,
  equal, above, unset → default 60, non-numeric junk), the REAL
  ``scripts/prune-build-server-disk.sh`` exits 3 IFF enforcement is enabled
  (threshold > 0) AND the measured free space is strictly below the
  threshold, and exits 0 otherwise; the
  ``PRUNE-DISK-INSUFFICIENT free=<X>GB required=<Y>GB`` line appears
  EXACTLY on the exit-3 branch. Unmeasurable free space (``df`` failing)
  and non-numeric thresholds fail OPEN (exit 0, no insufficiency line).

* **Agent leg** — _for any_ prune exit code, the fixed
  ``scripts/portal-build-agent.sh`` (byte-identical sandbox copy + planted
  stub prune script): exit 3 → ``failed`` detail with
  ``"error_kind":"disk"`` and NO ``portal-build.sh`` invocation; exit 0 →
  the build proceeds normally; any other nonzero (incl. the script's
  internal-failure exit 4) → warning logged, build proceeds (fail open).

HONESTY GUARD (absolute): nothing here runs real docker, aws, gdk, or
touches the build server. The script leg runs a byte-identical copy of the
REAL prune script inside a sandbox repo (so ``REPO_DIR`` — and hence the
``custom-build/`` removal — resolves inside the sandbox) with stub
``docker`` / ``aws`` / ``df`` binaries on PATH and ``PRUNE_TMP_DIR``
pointed at a sandboxed empty dir (never the real ``/tmp``). The agent leg
reuses the exploration suite's sandbox-agent technique: ``EVENT_BUS``
unset (``emit_event`` PRINTS each detail instead of calling ``aws
events``), ``ATTEMPT_ID`` unset (no preflight aws calls), and the REAL
``/var/lock/dda-build.lock`` checked first — a held lock SKIPS rather than
disturb a running build (builds.md).

Helpers are module-local (tasks 4.1/4.3 are written concurrently in this
directory — no shared conftest).
"""
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import uuid

import pytest
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# Module-level repo root resolution (this file lives at
# test/backend-test/build_server_disk_pruning/).
# ---------------------------------------------------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.normpath(os.path.join(_HERE, "..", "..", ".."))

AGENT_SCRIPT = os.path.join(REPO_ROOT, "scripts", "portal-build-agent.sh")
PRUNE_SCRIPT = os.path.join(REPO_ROOT, "scripts",
                            "prune-build-server-disk.sh")
PRUNE_SCRIPT_RELPATH = "scripts/prune-build-server-disk.sh"
AGENT_LOCK_FILE = "/var/lock/dda-build.lock"

#: The exit-3 marker line (design Decision 3 / File 1 step 6).
_INSUFFICIENT_RE = re.compile(
    r"^PRUNE-DISK-INSUFFICIENT free=(\d+)GB required=([0-9]+)GB$",
    re.MULTILINE)

#: The default threshold when BUILD_MIN_FREE_DISK_GB is unset.
_DEFAULT_THRESHOLD_GB = 60

_KB_PER_GB = 1048576  # the script's own conversion: kb / 1048576


def _sha256(path):
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def _install_stub(bin_dir, name, content):
    path = os.path.join(bin_dir, name)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(content)
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP
             | stat.S_IXOTH)


# ===========================================================================
# Leg 1 — the script threshold gate (Requirement 2.3).
# ===========================================================================

#: df stub: serves DF_AVAIL_KB as the available-KB column for every
#: measurement call, or fails outright when DF_FAIL=1 (the unmeasurable
#: fail-open path).
_DF_STUB = """\
#!/usr/bin/env bash
if [ "${DF_FAIL:-0}" = "1" ]; then
    exit 1
fi
echo "Avail"
echo "${DF_AVAIL_KB:-0}"
exit 0
"""

#: docker stub: empty image inventory (this leg exercises ONLY the
#: threshold gate — task 4.1 owns the prune-safety surface).
_DOCKER_STUB = """\
#!/usr/bin/env bash
case "$1 $2" in
    "image ls")    : ;;
    "image prune") echo "Total reclaimed space: 0B" ;;
esac
exit 0
"""

#: aws stub: never reached with an empty inventory, present for safety.
_AWS_STUB = """\
#!/usr/bin/env bash
exit 0
"""


def _run_prune_script(avail_kb=None, df_fail=False, threshold=None,
                      build_job_id="bj-threshold"):
    """Run a byte-identical sandbox copy of the REAL prune script with the
    stub df/docker/aws on PATH. ``threshold=None`` leaves
    BUILD_MIN_FREE_DISK_GB unset (default 60). Returns CompletedProcess."""
    with tempfile.TemporaryDirectory(prefix="dda-prune-threshold-") as tmp:
        # Sandbox repo: the script resolves REPO_DIR from its own location,
        # so its custom-build/ removal stays inside the sandbox.
        repo = os.path.join(tmp, "DefectDetectionApplication")
        os.makedirs(os.path.join(repo, "scripts"))
        prune = os.path.join(repo, PRUNE_SCRIPT_RELPATH)
        shutil.copyfile(PRUNE_SCRIPT, prune)
        os.chmod(prune, os.stat(prune).st_mode | stat.S_IXUSR)
        assert _sha256(prune) == _sha256(PRUNE_SCRIPT), \
            "the sandbox prune script is not byte-identical to the real one"

        bin_dir = os.path.join(tmp, "bin")
        os.makedirs(bin_dir)
        _install_stub(bin_dir, "df", _DF_STUB)
        _install_stub(bin_dir, "docker", _DOCKER_STUB)
        _install_stub(bin_dir, "aws", _AWS_STUB)

        fake_tmp = os.path.join(tmp, "fake-tmp")  # never the real /tmp
        os.makedirs(fake_tmp)

        env = {
            "PATH": bin_dir + os.pathsep
            + os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "HOME": tmp,
            "LC_ALL": "C.UTF-8",
            "DF_AVAIL_KB": "" if avail_kb is None else str(avail_kb),
            "DF_FAIL": "1" if df_fail else "0",
            "PRUNE_TMP_DIR": fake_tmp,
        }
        if threshold is not None:
            env["BUILD_MIN_FREE_DISK_GB"] = threshold
        return subprocess.run(
            ["bash", prune, f"BUILD_JOB_ID={build_job_id}"],
            cwd=tmp, env=env, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=120)


def _is_enforceable(threshold):
    """Mirror of the script's gate. ``threshold=None`` (unset) AND
    ``threshold=""`` (empty env) both take the ``${VAR:-60}`` default —
    bash `:-` treats empty the same as unset. A non-empty value containing
    any non-digit hits the ``*[!0-9]*`` fail-open branch."""
    if threshold is None or threshold == "":
        return True
    return re.fullmatch(r"[0-9]+", threshold) is not None


def _effective_threshold(threshold):
    if threshold is None or threshold == "":
        return _DEFAULT_THRESHOLD_GB
    return int(threshold)


#: Post-prune available KB — spans 0..300 GB non-aligned, so the script's
#: floor division (kb / 1048576) and the strict `<` boundary both get
#: exercised.
_AVAIL_KB = st.integers(min_value=0, max_value=300 * _KB_PER_GB)

#: BUILD_MIN_FREE_DISK_GB settings: unset (None → default 60), empty env
#: ("" → also default 60 via `${VAR:-60}`), numeric 0 (disabled) through
#: 300 (below/equal/above any measured value), and non-numeric junk (the
#: fail-open branch).
_THRESHOLDS = st.one_of(
    st.none(),
    st.just(""),
    st.integers(min_value=0, max_value=300).map(str),
    st.sampled_from(["sixty", "60GB", "-5", "6.5", " 60"]),
)


class TestScriptThresholdGate:
    """The prune script exits 3 IFF enforcement is enabled (threshold > 0)
    AND post-prune free space is strictly below the threshold, else exits
    0; the PRUNE-DISK-INSUFFICIENT line appears exactly on the exit-3
    branch.

    # Validates: Requirements 2.3
    """

    @settings(deadline=None,
              suppress_health_check=[HealthCheck.too_slow])
    @given(avail_kb=_AVAIL_KB, threshold=_THRESHOLDS)
    # Boundary: free == threshold → NOT below → exit 0 (strictly below).
    @example(avail_kb=60 * _KB_PER_GB, threshold="60")
    # One KB under the boundary → floor 59 GB < 60 → exit 3.
    @example(avail_kb=60 * _KB_PER_GB - 1, threshold="60")
    # Unset → default 60 enforced (the e1d672ce 29 GB case fails fast).
    @example(avail_kb=29 * _KB_PER_GB, threshold=None)
    # 0 disables enforcement even on an empty disk.
    @example(avail_kb=0, threshold="0")
    def test_exit_3_iff_enforced_and_free_below_threshold(
            self, avail_kb, threshold):
        # Validates: Requirements 2.3
        result = _run_prune_script(avail_kb=avail_kb, threshold=threshold)
        stdout = result.stdout.decode("utf-8", "replace")

        free_gb = avail_kb // _KB_PER_GB
        enforceable = _is_enforceable(threshold)
        effective = _effective_threshold(threshold) if enforceable else None
        expect_exit_3 = (enforceable and effective > 0
                         and free_gb < effective)

        observed = json.dumps({
            "avail_kb": avail_kb, "free_gb": free_gb,
            "threshold": threshold, "enforceable": enforceable,
            "effective_threshold": effective,
            "expected_exit": 3 if expect_exit_3 else 0,
            "exit": result.returncode, "stdout": stdout}, indent=2)

        assert result.returncode == (3 if expect_exit_3 else 0), (
            "threshold-gate exit code disagrees with Property 4 "
            "(exit 3 IFF threshold > 0 AND free < threshold, else 0): "
            + observed)

        matches = _INSUFFICIENT_RE.findall(stdout)
        if expect_exit_3:
            # Exactly one insufficiency line, with the measured free space
            # and the effective threshold verbatim.
            expected_required = (str(_DEFAULT_THRESHOLD_GB)
                                 if threshold in (None, "") else threshold)
            assert matches == [(str(free_gb), expected_required)], (
                "the exit-3 branch must log exactly one "
                "PRUNE-DISK-INSUFFICIENT free=<X>GB required=<Y>GB line "
                "with the measured values: " + observed)
        else:
            assert matches == [], (
                "PRUNE-DISK-INSUFFICIENT must appear ONLY on the exit-3 "
                "branch: " + observed)

    @settings(deadline=None,
              suppress_health_check=[HealthCheck.too_slow])
    @given(threshold=st.one_of(
        st.none(), st.integers(min_value=1, max_value=300).map(str)))
    def test_unmeasurable_free_space_fails_open(self, threshold):
        """df failing → free space unmeasurable → enforcement is skipped
        (exit 0, no insufficiency line, a loud warning) even though the
        threshold is enabled — fail open (design Decision 3 / File 1).

        # Validates: Requirements 2.3
        """
        result = _run_prune_script(df_fail=True, threshold=threshold)
        stdout = result.stdout.decode("utf-8", "replace")
        observed = json.dumps({
            "threshold": threshold, "exit": result.returncode,
            "stdout": stdout}, indent=2)

        assert result.returncode == 0, (
            "unmeasurable free space must fail OPEN (exit 0), never block "
            "the build: " + observed)
        assert not _INSUFFICIENT_RE.search(stdout), (
            "no PRUNE-DISK-INSUFFICIENT line may appear when free space "
            "is unmeasurable: " + observed)
        assert "unmeasurable" in stdout, (
            "the unmeasurable fail-open path must log loudly: " + observed)

    @settings(deadline=None,
              suppress_health_check=[HealthCheck.too_slow])
    @given(avail_kb=_AVAIL_KB,
           threshold=st.sampled_from(["sixty", "60GB", "-5", "6.5", " 60"]))
    def test_non_numeric_threshold_fails_open_with_warning(
            self, avail_kb, threshold):
        """A non-numeric BUILD_MIN_FREE_DISK_GB skips enforcement with a
        loud warning — exit 0 whatever the measured free space.

        # Validates: Requirements 2.3
        """
        result = _run_prune_script(avail_kb=avail_kb, threshold=threshold)
        stdout = result.stdout.decode("utf-8", "replace")
        observed = json.dumps({
            "avail_kb": avail_kb, "threshold": threshold,
            "exit": result.returncode, "stdout": stdout}, indent=2)

        assert result.returncode == 0, (
            "a non-numeric threshold must fail OPEN (exit 0): " + observed)
        assert not _INSUFFICIENT_RE.search(stdout), observed
        assert "not a number" in stdout, (
            "the non-numeric fail-open path must log loudly: " + observed)


# ===========================================================================
# Leg 2 — the agent's prune-exit-code handling (Requirement 2.4).
# The exploration suite's sandbox-agent technique: byte-identical agent
# copy, recording stub portal-build.sh, planted stub prune script exiting
# the generated code; EVENT_BUS unset, ATTEMPT_ID unset.
# ===========================================================================

def _require_free_build_lock():
    """Refuse to run a sandbox agent pass while the on-server build lock
    is held: the byte-identical copy takes the same REAL lock, and a held
    lock means a build may be in progress (builds.md)."""
    fd = os.open(AGENT_LOCK_FILE, os.O_WRONLY | os.O_CREAT, 0o666)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        pytest.skip(f"{AGENT_LOCK_FILE} is held (a build may be running)")
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)


def _sandbox_repo(prune_exit):
    """A temporary repository: byte-identical agent copy, recording stub
    portal-build.sh, and a recording stub prune script planted at
    scripts/prune-build-server-disk.sh exiting ``prune_exit``. Returns
    (repo_dir, agent_script, record_file)."""
    root = tempfile.mkdtemp(prefix="dda-prune-threshold-agent-")
    repo = os.path.join(root, "DefectDetectionApplication")
    os.makedirs(os.path.join(repo, "scripts"))

    agent = os.path.join(repo, "scripts", "portal-build-agent.sh")
    shutil.copyfile(AGENT_SCRIPT, agent)
    os.chmod(agent, os.stat(agent).st_mode | stat.S_IXUSR)
    assert _sha256(agent) == _sha256(AGENT_SCRIPT), \
        "the sandbox agent is not byte-identical to the real script"

    record = os.path.join(root, "invocations.log")

    build_stub = os.path.join(repo, "portal-build.sh")
    with open(build_stub, "w", encoding="utf-8") as handle:
        handle.write("\n".join([
            "#!/bin/bash",
            f'echo "BUILD $*" >> "{record}"',
            'echo "STUB portal-build.sh $*"',
            "echo 'PORTAL_BUILD_RESULT {\"component_name\":"
            "\"aws.edgeml.dda.LocalServer.arm64JP7\",\"published_version\":"
            "\"1.0.0\",\"pushed_image_refs\":[]}'",
            "exit 0",
        ]) + "\n")
    os.chmod(build_stub, 0o755)

    prune_stub = os.path.join(repo, PRUNE_SCRIPT_RELPATH)
    with open(prune_stub, "w", encoding="utf-8") as handle:
        handle.write("\n".join([
            "#!/bin/bash",
            f'echo "PRUNE $*" >> "{record}"',
            'echo "STUB prune-build-server-disk.sh $*"',
            f"exit {prune_exit}",
        ]) + "\n")
    os.chmod(prune_stub, 0o755)

    return repo, agent, record


def _agent_env(home):
    """Minimal environment: EVENT_BUS deliberately absent (emit_event
    PRINTS each detail — no AWS by construction); ATTEMPT_ID absent (the
    preflight and its aws calls never run)."""
    return {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": home,
        "LC_ALL": "C.UTF-8",
        "GIT_CONFIG_GLOBAL": os.path.join(home, "gitconfig-absent"),
    }


def _emitted_details(stdout):
    """Phase-event details parsed from the ``EVENT_BUS not set — skipping
    event: <detail>`` lines."""
    details = []
    marker = "skipping event: "
    for line in stdout.splitlines():
        if marker in line:
            details.append(json.loads(line.split(marker, 1)[1]))
    return details


def _run_sandbox_agent(prune_exit):
    """One sandbox agent pass (JP7 — the incident's target). Returns
    (job_id, CompletedProcess, emitted_details, record_lines)."""
    _require_free_build_lock()
    repo, agent, record = _sandbox_repo(prune_exit=prune_exit)
    job_id = f"bj-threshold-{uuid.uuid4()}"
    try:
        result = subprocess.run(
            ["bash", agent, f"BUILD_JOB_ID={job_id}", "BUILD_TARGET=JP7"],
            cwd=repo, env=_agent_env(repo), stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=120)
        record_lines = []
        if os.path.exists(record):
            with open(record, encoding="utf-8") as handle:
                record_lines = handle.read().splitlines()
    finally:
        shutil.rmtree(os.path.dirname(repo), ignore_errors=True)
        leftover = f"/tmp/portal-build-agent-{job_id}.log"
        if os.path.exists(leftover):
            os.remove(leftover)
    return job_id, result, _emitted_details(
        result.stdout.decode("utf-8", "replace")), record_lines


def _observation(job_id, result, details, record_lines, **extra):
    return json.dumps(dict(
        job_id=job_id,
        exit_code=result.returncode,
        record_lines=record_lines,
        details=details,
        stdout_tail=result.stdout.decode("utf-8", "replace")[-2000:],
        **extra), indent=2, default=str)


class TestAgentPruneExitHandling:
    """For any stub prune exit code: 3 → failed detail with
    error_kind=disk and NO portal-build.sh invocation; 0 → build proceeds
    normally; any other nonzero (incl. 4) → warning logged, build proceeds
    (fail open).

    # Validates: Requirements 2.4
    """

    @settings(deadline=None,
              suppress_health_check=[HealthCheck.too_slow])
    @given(prune_exit=st.integers(min_value=0, max_value=255))
    # The three contract branches, pinned as explicit examples.
    @example(prune_exit=0)    # proceed
    @example(prune_exit=3)    # below threshold → fail fast
    @example(prune_exit=4)    # internal prune failure → fail open
    def test_agent_branches_on_the_prune_exit_code(self, prune_exit):
        # Validates: Requirements 2.4
        job_id, result, details, record_lines = _run_sandbox_agent(
            prune_exit=prune_exit)
        stdout = result.stdout.decode("utf-8", "replace")

        prune_runs = [line for line in record_lines
                      if line.startswith("PRUNE")]
        build_runs = [line for line in record_lines
                      if line.startswith("BUILD")]
        disk_failures = [detail for detail in details
                         if detail.get("phase") == "failed"
                         and detail.get("error_kind") == "disk"]
        phases = [detail.get("phase") for detail in details]

        observed = _observation(job_id, result, details, record_lines,
                                prune_exit=prune_exit)

        # The prune hook must always have run (before any branch).
        assert prune_runs, (
            "the agent never invoked the planted prune script: " + observed)

        if prune_exit == 3:
            # Below threshold: fail the job with error_kind=disk BEFORE
            # any build work.
            assert disk_failures, (
                "prune exit 3 must emit a failed detail with "
                '"error_kind":"disk": ' + observed)
            assert "Insufficient disk space before build" in \
                disk_failures[0].get("error_message", ""), (
                    "the disk failure must identify the pre-build disk "
                    "insufficiency: " + observed)
            assert not build_runs, (
                "portal-build.sh must NOT run after prune exit 3: "
                + observed)
            assert "succeeded" not in phases, observed
            assert result.returncode != 0, (
                "the agent must exit nonzero on the pre-build disk "
                "failure: " + observed)
        else:
            # Exit 0 → proceed; any other nonzero → fail open (warning +
            # proceed). Either way the build runs normally to success.
            assert build_runs, (
                f"prune exit {prune_exit} must not block the build "
                "(fail open): " + observed)
            assert record_lines.index(prune_runs[0]) < \
                record_lines.index(build_runs[0]), (
                    "the prune must run BEFORE the build: " + observed)
            assert phases == ["building", "succeeded"], (
                "the build must proceed normally (clean phase sequence): "
                + observed)
            assert not disk_failures, observed
            assert result.returncode == 0, observed
            if prune_exit != 0:
                assert f"disk prune exited {prune_exit}" in stdout, (
                    "a prune malfunction must be logged loudly as a "
                    "warning (fail open): " + observed)
                assert "fail-open" in stdout, observed
            else:
                assert "disk prune exited" not in stdout, (
                    "no prune-malfunction warning may appear on a clean "
                    "prune exit 0: " + observed)
