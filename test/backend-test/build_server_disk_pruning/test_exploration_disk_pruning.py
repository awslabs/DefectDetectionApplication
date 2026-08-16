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
Bug condition EXPLORATION suite (build-server-disk-pruning, task 1).

**Property 1: Bug Condition** — New Builds Prune Stale Disk State and
Fail Fast on Insufficient Disk.

**Validates: Requirements 1.1, 1.2, 1.3, 1.4, 1.5**

Incident context (bugfix.md): fleet JP7 build `e1d672ce` on dedicated
server `i-092e45480d30c89c4` (192 GB disk) died mid-run with
`RUNNER_DISK_FULL` (ENOSPC extracting `libonnxruntime_providers_cuda.so`)
~1 h in, AFTER the expensive GPU compile — the measurement-only disk
preflight had "passed" at 29 GB free. Root cause: unbounded accumulation
across builds (every ECR-path publish leaves a per-version locally-tagged
image generation behind; nothing prunes generations, dangling images, or
old `/tmp` build logs; no minimum-free threshold is enforced).

CRITICAL EXPECTATION ON THE UNFIXED TREE:
  * Case 1 (publish-path untag, defect 1.1)         — MUST FAIL
  * Case 2 (prune script + safety anchors, 1.2)     — MUST FAIL (absent)
  * Case 3 (agent runs prune BEFORE build, 1.2/1.4) — MUST FAIL
  * Case 4 (below-threshold fail-fast, 1.3/1.4/1.5) — MUST FAIL
  * Case 5 (F(X) pins — preserved behavior)          — MUST PASS
Those failures are the SUCCESS criterion of task 1: they are the
counterexamples proving the bug condition exists. The same suite,
unmodified, validates the fix when it passes after task 3 (task 3.4).

HONESTY GUARD (absolute): nothing here runs real docker, aws, gdk, or
touches the build server. Cases 1, 2 and 5 are comment-aware text
fingerprints of the repo scripts. Cases 3 and 4 run a BYTE-IDENTICAL
copy of the real `scripts/portal-build-agent.sh` inside a temporary
sandbox repository (the `test_source_selection_preservation.py`
`_sandbox_repo` technique) whose `portal-build.sh` is a recording stub
and whose `scripts/prune-build-server-disk.sh` is a planted recording
stub; `EVENT_BUS` is unset so `emit_event` PRINTS each detail instead of
calling `aws events` (no AWS by construction), and `ATTEMPT_ID` is unset
so the preflight (and its aws calls) never runs. The sandbox agent takes
the REAL `/var/lock/dda-build.lock`, so each behavioral case first
verifies the lock is free and SKIPS rather than disturb a running build.
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

# ---------------------------------------------------------------------------
# Module-level repo root resolution (this file lives at
# test/backend-test/build_server_disk_pruning/).
# ---------------------------------------------------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.normpath(os.path.join(_HERE, "..", "..", ".."))

PORTAL_BUILD_SH = os.path.join(REPO_ROOT, "portal-build.sh")
BUILD_CUSTOM_SH = os.path.join(REPO_ROOT, "build-custom.sh")
AGENT_SCRIPT = os.path.join(REPO_ROOT, "scripts", "portal-build-agent.sh")
PRUNE_SCRIPT = os.path.join(REPO_ROOT, "scripts",
                            "prune-build-server-disk.sh")
PRUNE_SCRIPT_RELPATH = "scripts/prune-build-server-disk.sh"

AGENT_LOCK_FILE = "/var/lock/dda-build.lock"

#: The four timestamped/job-suffixed `/tmp` build-log glob patterns that
#: accumulate across builds and are never reused (bugfix.md, defect 1.2).
TMP_LOG_PATTERNS = (
    "gdk-build-*.log",
    "gdk-publish-*.log",
    "portal-build-agent-*.log",
    "inference-uploader-build-*.log",
)

#: Retention-reason log tokens the prune script must emit when it KEEPS a
#: candidate instead of deleting it (design Decision 2 — fail open).
RETENTION_REASON_TOKENS = ("NOT_IN_ECR", "NO_REPODIGEST", "ECR_UNVERIFIABLE")


def _read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def _code_lines(text):
    """(line_number, line) pairs with full-line comments removed — the
    comment-aware half of the text scans (none of the scanned scripts put
    load-bearing docker commands on a line behind a string literal, so
    string-awareness reduces to skipping comment lines)."""
    lines = []
    for number, line in enumerate(text.splitlines(), start=1):
        if line.lstrip().startswith("#"):
            continue
        lines.append((number, line))
    return lines


def _observation(**fields):
    return json.dumps(fields, indent=2, default=str)


# ---------------------------------------------------------------------------
# Case 1 — Publish-path untag exists (defect 1.1).
#
# EXPECTED ON UNFIXED CODE: FAIL. The ECR publish path runs
# `docker tag flask-app:latest ${ECR_REPO_BACKEND}:${COMPONENT_VERSION}` +
# `docker push` (and the react-webapp equivalent) with ZERO `docker rmi`
# anywhere in the script — one new ~29–42 GB locally-tagged generation
# per image per publish, which nothing ever removes.
# Validates: Requirements 1.1
# ---------------------------------------------------------------------------

class TestCase1PublishPathUntag:
    """`portal-build.sh` must untag each local per-version ECR ref after
    its successful push (design Decision 4)."""

    def _assert_untag_after_push(self, repo_var):
        text = _read(PORTAL_BUILD_SH)
        code = _code_lines(text)

        push_lines = [
            (number, line) for number, line in code
            if "docker push" in line and repo_var in line
            and "COMPONENT_VERSION" in line
        ]
        rmi_lines = [
            (number, line) for number, line in code
            if re.search(r"\bdocker\s+rmi\b", line) and repo_var in line
            and "COMPONENT_VERSION" in line
        ]
        observed = _observation(
            script=PORTAL_BUILD_SH,
            repo_var=repo_var,
            push_lines=push_lines,
            rmi_lines=rmi_lines,
            counterexample=(
                f"the ECR publish path tags and pushes "
                f"${{{repo_var}}}:${{COMPONENT_VERSION}} but never removes "
                f"the local per-version tag — one new ~29-42 GB "
                f"locally-tagged generation per publish (defect 1.1)"),
        )
        assert push_lines, observed  # the publish path itself must exist
        last_push = max(number for number, _ in push_lines)
        assert rmi_lines, observed
        assert any(number > last_push for number, _ in rmi_lines), observed

    def test_backend_per_version_ref_untagged_after_push(self):
        self._assert_untag_after_push("ECR_REPO_BACKEND")

    def test_frontend_per_version_ref_untagged_after_push(self):
        self._assert_untag_after_push("ECR_REPO_FRONTEND")


# ---------------------------------------------------------------------------
# Case 2 — Prune script exists with the safety anchors (defect 1.2).
#
# EXPECTED ON UNFIXED CODE: FAIL — scripts/prune-build-server-disk.sh
# does not exist. The ONLY between-build cleanup on the unfixed tree is
# portal-build.sh step [3/7] (`rm -rf greengrass-build/ .gdk/`).
# Validates: Requirements 1.2
# ---------------------------------------------------------------------------

class TestCase2PruneScriptSafetyAnchors:
    """`scripts/prune-build-server-disk.sh` with every design Decision 2/3/5
    safety anchor, and NONE of the cache-destroying blanket prunes."""

    def _prune_text(self):
        assert os.path.isfile(PRUNE_SCRIPT), _observation(
            expected_script=PRUNE_SCRIPT,
            counterexample=(
                "no prune script exists — the only between-build cleanup "
                "on the unfixed tree is portal-build.sh step [3/7] "
                "rm -rf greengrass-build/ .gdk/ (defect 1.2)"),
        )
        return _read(PRUNE_SCRIPT)

    def test_ecr_digest_confirmation_anchor(self):
        """Deletion is gated on `aws ecr describe-images` with the local
        RepoDigest (`imageDigest=`), and every retention logs its reason
        (NOT_IN_ECR / NO_REPODIGEST / ECR_UNVERIFIABLE) — fail open."""
        text = self._prune_text()
        observed = _observation(script=PRUNE_SCRIPT)
        assert "describe-images" in text, observed
        assert "imageDigest" in text, observed
        for token in RETENTION_REASON_TOKENS:
            assert token in text, _observation(
                script=PRUNE_SCRIPT, missing_retention_token=token)

    def test_tmp_log_patterns_and_threshold_gate(self):
        """All four accumulating `/tmp` build-log patterns are targeted,
        and the post-prune BUILD_MIN_FREE_DISK_GB gate exists."""
        text = self._prune_text()
        for pattern in TMP_LOG_PATTERNS:
            assert pattern in text, _observation(
                script=PRUNE_SCRIPT, missing_tmp_log_pattern=pattern)
        assert "BUILD_MIN_FREE_DISK_GB" in text, _observation(
            script=PRUNE_SCRIPT, missing="BUILD_MIN_FREE_DISK_GB gate")

    def test_no_cache_destroying_blanket_prunes(self):
        """The manual remediation's deliberate KEEP: never `system prune`,
        `builder prune`, `image prune -a` (long or short form), or
        `rmi -f` — the BuildKit cache and the `:latest` lineage carry the
        ~1-2 h onnxruntime GPU compile layers."""
        text = self._prune_text()
        code = "\n".join(line for _, line in _code_lines(text))
        forbidden = {
            "docker system prune": re.compile(r"\bsystem\s+prune\b"),
            "docker builder prune": re.compile(r"\bbuilder\s+prune\b"),
            "docker image prune -a": re.compile(
                r"\bimage\s+prune\b[^\n]*(\s-[a-zA-Z]*a\b|--all\b)"),
            "docker rmi -f": re.compile(r"\brmi\b[^\n]*(\s-[a-zA-Z]*f\b|--force\b)"),
        }
        for name, pattern in forbidden.items():
            match = pattern.search(code)
            assert match is None, _observation(
                script=PRUNE_SCRIPT, forbidden_command=name,
                matched=match.group(0) if match else None)


# ---------------------------------------------------------------------------
# Behavioral sandbox (cases 3 and 4) — the `_sandbox_repo` technique from
# test/backend-test/portal_builds/test_source_selection_preservation.py:
# a temp repo whose `scripts/portal-build-agent.sh` is a byte-identical
# copy (sha256 asserted) of the real agent, whose `portal-build.sh` is a
# recording stub, and with a recording stub prune script PLANTED at
# scripts/prune-build-server-disk.sh so a fixed agent would find and run
# it. EVENT_BUS unset (details printed, no aws); ATTEMPT_ID unset (no
# preflight aws calls).
# ---------------------------------------------------------------------------

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
    `portal-build.sh`, and a recording stub prune script planted at
    scripts/prune-build-server-disk.sh exiting `prune_exit`.

    Returns (repo_dir, agent_script, record_file). The record file
    receives one `PRUNE ...` line per prune-stub invocation and one
    `BUILD ...` line per portal-build.sh-stub invocation, in execution
    order — the step-sequence oracle for cases 3 and 4."""
    root = tempfile.mkdtemp(prefix="dda-prune-explore-")
    repo = os.path.join(root, "DefectDetectionApplication")
    os.makedirs(os.path.join(repo, "scripts"))

    agent = os.path.join(repo, "scripts", "portal-build-agent.sh")
    shutil.copyfile(AGENT_SCRIPT, agent)
    os.chmod(agent, os.stat(agent).st_mode | stat.S_IXUSR)
    with open(AGENT_SCRIPT, "rb") as real, open(agent, "rb") as copy:
        assert hashlib.sha256(real.read()).hexdigest() == \
            hashlib.sha256(copy.read()).hexdigest(), \
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
    """A minimal environment with none of the agent's parameters set.
    EVENT_BUS is deliberately absent: `emit_event` then PRINTS the detail
    instead of calling `aws events put-events` (no AWS, by construction).
    ATTEMPT_ID is absent: the preflight (and its aws calls) never runs."""
    return {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": home,
        "LC_ALL": "C.UTF-8",
        "GIT_CONFIG_GLOBAL": os.path.join(home, "gitconfig-absent"),
    }


def _run_agent(script, args, home):
    return subprocess.run(
        ["bash", script] + list(args), cwd=home, env=_agent_env(home),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)


def _emitted_details(stdout):
    """The phase-event details the agent emitted, parsed from the
    `EVENT_BUS not set — skipping event: <detail>` lines."""
    details = []
    for line in stdout.splitlines():
        marker = "skipping event: "
        if marker in line:
            details.append(json.loads(line.split(marker, 1)[1]))
    return details


def _run_sandbox_agent(prune_exit):
    """One sandbox agent pass (JP7 — the incident's target). Returns
    (job_id, completed_process, emitted_details, record_lines)."""
    _require_free_build_lock()
    repo, agent, record = _sandbox_repo(prune_exit=prune_exit)
    job_id = f"bj-explore-{uuid.uuid4()}"
    try:
        result = _run_agent(
            agent, [f"BUILD_JOB_ID={job_id}", "BUILD_TARGET=JP7"], repo)
        record_lines = []
        if os.path.exists(record):
            record_lines = _read(record).splitlines()
    finally:
        shutil.rmtree(os.path.dirname(repo), ignore_errors=True)
        leftover = f"/tmp/portal-build-agent-{job_id}.log"
        if os.path.exists(leftover):
            os.remove(leftover)
    return job_id, result, _emitted_details(
        result.stdout.decode("utf-8", "replace")), record_lines


# ---------------------------------------------------------------------------
# Case 3 — Agent invokes the prune BEFORE the build (defects 1.2/1.4).
#
# EXPECTED ON UNFIXED CODE: FAIL. The unfixed agent's step sequence is
# lock → (preflight, evidence-only, ATTEMPT_ID-gated) → sync → building →
# portal-build.sh — it never invokes any prune, even when a prune script
# is present at scripts/prune-build-server-disk.sh.
# Validates: Requirements 1.2, 1.4
# ---------------------------------------------------------------------------

class TestCase3AgentRunsPruneBeforeBuild:

    def test_prune_stub_runs_before_portal_build_stub(self):
        job_id, result, details, record_lines = _run_sandbox_agent(
            prune_exit=0)
        observed = _observation(
            job_id=job_id,
            exit_code=result.returncode,
            record_lines=record_lines,
            details=details,
            stdout_tail=result.stdout.decode("utf-8", "replace")[-2000:],
            counterexample=(
                "the agent ran the build without ever invoking the planted "
                "prune script — its step sequence has no prune step "
                "(defects 1.2/1.4): stale generations, dangling images and "
                "old /tmp logs survive into every new build"),
        )
        prune_indices = [index for index, line in enumerate(record_lines)
                         if line.startswith("PRUNE")]
        build_indices = [index for index, line in enumerate(record_lines)
                         if line.startswith("BUILD")]
        assert prune_indices, observed
        assert build_indices, observed  # prune exit 0 → the build proceeds
        assert min(prune_indices) < min(build_indices), observed


# ---------------------------------------------------------------------------
# Case 4 — Below-threshold fail-fast (defects 1.3/1.4/1.5).
#
# EXPECTED ON UNFIXED CODE: FAIL. A prune exit 3 means "free space is
# still below BUILD_MIN_FREE_DISK_GB after pruning" (design Decision 1);
# the fixed agent must fail the job with error_kind=disk BEFORE any build
# work. The unfixed agent runs the build regardless — the e1d672ce path:
# a 29 GB-free preflight "passed" (measurement-only), ~1 h of GPU compile
# was wasted, and recovery took manual operator cleanup plus a requeue.
# Validates: Requirements 1.3, 1.4, 1.5
# ---------------------------------------------------------------------------

class TestCase4BelowThresholdFailFast:

    def test_prune_exit_3_fails_job_with_disk_error_before_build(self):
        job_id, result, details, record_lines = _run_sandbox_agent(
            prune_exit=3)
        observed = _observation(
            job_id=job_id,
            exit_code=result.returncode,
            record_lines=record_lines,
            details=details,
            stdout_tail=result.stdout.decode("utf-8", "replace")[-2000:],
            counterexample=(
                "with the prune script reporting insufficient disk "
                "(exit 3), the agent proceeded into the build anyway and "
                "reported success — no fail-fast, no error_kind=disk "
                "(defects 1.3/1.4/1.5): on a real server this is the "
                "~1 h-wasted mid-build ENOSPC death requiring manual "
                "recovery"),
        )
        disk_failures = [detail for detail in details
                         if detail.get("phase") == "failed"
                         and detail.get("error_kind") == "disk"]
        assert disk_failures, observed
        # The expensive build must never have started.
        assert not any(line.startswith("BUILD") for line in record_lines), \
            observed
        assert result.returncode != 0, observed


# ---------------------------------------------------------------------------
# Case 5 — F(X) pins: preserved behavior that must PASS on the unfixed
# tree and must NOT be inverted by the fix.
#
# EXPECTED ON UNFIXED CODE: PASS (and still PASS after the fix).
# Validates: Requirements 1.2, 1.3 (the preserved baselines design
# Decisions 3 and 5 build on)
# ---------------------------------------------------------------------------

class TestCase5PreservedBaselinePins:

    def test_agent_preflight_stays_measurement_only(self):
        """`PREFLIGHT_MIN_DISK_GB` keeps its unset/measurement-only
        production semantics — `${PREFLIGHT_MIN_DISK_GB:-}` with NO
        hardcoded numeric default (design Decision 3 keeps the preflight
        byte-untouched; the portal_builds preflight suites pin it)."""
        text = _read(AGENT_SCRIPT)
        observed = _observation(script=AGENT_SCRIPT)
        assert 'PREFLIGHT_MIN_DISK_GB="${PREFLIGHT_MIN_DISK_GB:-}"' in text, \
            observed
        hardcoded = re.search(r"PREFLIGHT_MIN_DISK_GB:-[0-9]", text)
        assert hardcoded is None, _observation(
            script=AGENT_SCRIPT,
            hardcoded_default=hardcoded.group(0) if hardcoded else None)

    def test_agent_record_disk_capacity_still_exists(self):
        """The measurement-only preflight evidence recorder survives."""
        text = _read(AGENT_SCRIPT)
        assert "record_disk_capacity()" in text, _observation(
            script=AGENT_SCRIPT, missing="record_disk_capacity()")
        assert "record_disk_capacity" in [
            token
            for _, line in _code_lines(text)
            for token in line.replace("(", " ").replace(")", " ").split()
        ], _observation(script=AGENT_SCRIPT,
                        missing="record_disk_capacity invocation")

    def test_portal_build_step_3_cleanup_still_exists(self):
        """portal-build.sh step [3/7] `rm -rf greengrass-build/` — the
        existing between-build cleanup design Decision 5 leaves in place
        (no duplication into the prune script)."""
        text = _read(PORTAL_BUILD_SH)
        code = [line for _, line in _code_lines(text)]
        assert any("rm -rf greengrass-build/" in line for line in code), \
            _observation(script=PORTAL_BUILD_SH,
                         missing="rm -rf greengrass-build/")

    def test_build_custom_workspace_cleanup_still_exists(self):
        """build-custom.sh `rm -rf ./custom-build` at build start — the
        provably-safe deletion design Decision 5 mirrors."""
        text = _read(BUILD_CUSTOM_SH)
        code = [line for _, line in _code_lines(text)]
        assert any("rm -rf ./custom-build" in line for line in code), \
            _observation(script=BUILD_CUSTOM_SH,
                         missing="rm -rf ./custom-build")
