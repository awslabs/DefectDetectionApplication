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
"""Preservation property tests (Task 2) for build-server-disk-pruning.

**Property 2: Preservation** — Everything Outside the Stale-Disk Surface Is
Unchanged.

Observation-first: every baseline in this file was RECORDED on the UNFIXED
tree (2026-08-16, before task 3 lands any fix). These tests MUST PASS on the
unfixed tree and MUST keep passing verbatim after the fix (re-run in task
3.5), with exactly ONE intended golden update: the ``build_save_pkgs``
neighbor-script sha256 for ``scripts/portal-build-agent.sh`` (recorded
verbatim below, consciously rebaselined in task 3.2).

What is pinned here:

* **Clean-server identity (3.1)**: the sandbox agent (byte-identical copy of
  the real ``scripts/portal-build-agent.sh`` + stub ``portal-build.sh``,
  ``EVENT_BUS`` unset) on a clean-run scenario. RECORDED unfixed baseline:
  exit 0; emitted detail phases exactly ``["building", "succeeded"]`` in that
  order; ``building`` detail field set ``{build_job_id, phase, build_target,
  source_ref, source_commit}``; ``succeeded`` detail field set
  ``{build_job_id, phase, build_target, result}`` with the result parsed
  verbatim from the stub's ``PORTAL_BUILD_RESULT`` line. The fixed tree must
  reproduce these exactly — the future Step 2.5 prune section's
  zero-deletion info line goes to stdout, which the detail parser ignores.
* **ECR read-only (3.2), skip-as-absent PBT**: _for any_ generated scenario,
  the recorded stub ``aws`` argv transcript of a
  ``scripts/prune-build-server-disk.sh`` run contains ONLY
  ``ecr describe-images`` calls — no delete/put/batch ECR mutation ever.
  SKIP-AS-ABSENT (the ``csi_nvargus_optional`` pattern): the prune script
  does not exist on the unfixed tree; the property binds automatically when
  task 3.1 creates it and is re-run bound in task 3.5.
* **In-progress-build immunity (3.3), skip-as-absent**: the prune script
  never deletes the current job's log
  (``portal-build-agent-${BUILD_JOB_ID}.log``), even when over-age. The
  lock-placement leg (hook inside ``/var/lock/dda-build.lock``, after
  acquisition) is asserted structurally by task 3.2's text scan, not here.
* **ENOSPC classification identity (3.4)**: the existing
  ``portal_builds/test_enospc_classification_properties.py`` suite is green
  on the unfixed tree (RECORDED: 6 passed) — the ``error_kind=disk`` /
  ENOSPC-evidence → ``RUNNER_DISK_FULL`` path the fix reuses and must not
  disturb. Pinned here by collection-count + delegation (the suite itself is
  re-run in tasks 3.5/5).
* **Local dev builds untouched (3.5)**: text pin — ``run_jp_builds.sh`` and
  ``build-custom.sh`` contain no reference to ``prune-build-server-disk.sh``.
* **Pinned script contracts (3.6)**: the RECORDED-verbatim current agent
  sha256 golden value (the ONE intended rebaseline, applied in task 3.2);
  baseline green counts on the unfixed tree: ``build_save_pkgs`` = 21
  passed; ``portal_builds`` agent-contract suites = 175 passed
  (test_source_selection_preservation 35, test_agent_tail_truncation 6,
  test_preflight_target_matrix 11, test_preflight_agent_contract 50,
  test_execution_failure_preservation 73).

SAFETY / honesty guard: no test in this file runs real docker, aws, gdk, or
touches the build server. The sandbox agent runs with ``EVENT_BUS`` unset
(``emit_event`` prints instead of calling ``aws events``); the prune-script
legs drive the REAL script (once it exists) with stub ``docker`` / ``aws`` /
``df`` binaries on PATH. A held ``/var/lock/dda-build.lock`` skips the
sandbox-agent legs rather than disturbing a live build.
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
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

_HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.normpath(os.path.join(_HERE, "..", "..", ".."))

AGENT_SCRIPT = os.path.join(REPO_ROOT, "scripts", "portal-build-agent.sh")
PRUNE_SCRIPT = os.path.join(REPO_ROOT, "scripts",
                            "prune-build-server-disk.sh")
AGENT_LOCK_FILE = "/var/lock/dda-build.lock"

# ---------------------------------------------------------------------------
# THE ORACLE — recorded on the UNFIXED tree (2026-08-16).
# ---------------------------------------------------------------------------

#: Requirement 3.6 — the agent neighbor-script sha256 golden value RECORDED
#: VERBATIM from
#: test/backend-test/build_save_pkgs/baselines/scripts_portal-build-agent.sh.sha256.txt
#: on the unfixed tree. This is the ONE intended rebaseline of this spec:
#: task 3.2 updates the baseline file to the post-hook hash. Recording the
#: pre-fix value here makes that diff auditable — the baseline file must
#: either still hold this value (unfixed tree) or hold the sha256 of the
#: CURRENT agent script (fixed tree, consciously rebaselined). Any other
#: state is a golden/script mismatch, i.e. an unintended agent edit.
UNFIXED_AGENT_SHA256_GOLDEN = (
    "09ac1caa438840eb724e1009fdc1b3a73b6d71d0370d1190792a8d6ac0635b46")

AGENT_SHA256_BASELINE_FILE = os.path.join(
    REPO_ROOT, "test", "backend-test", "build_save_pkgs", "baselines",
    "scripts_portal-build-agent.sh.sha256.txt")

#: Requirement 3.1 — the clean-run sandbox baseline, RECORDED by running the
#: unfixed agent (byte-identical copy, stub portal-build.sh exiting 0 with a
#: PORTAL_BUILD_RESULT line, EVENT_BUS unset, no SOURCE_REF):
#:   exit code 0; phases ["building", "succeeded"] in order.
CLEAN_RUN_EXIT_CODE = 0
CLEAN_RUN_PHASES = ["building", "succeeded"]
#: Detail field sets (exact): the building detail (source_commit is the
#: already-landed additive field of build-source-selection Req 4.5 — part of
#: the unfixed baseline here) and the succeeded detail.
BUILDING_DETAIL_KEYS = {"build_job_id", "phase", "build_target",
                        "source_ref", "source_commit"}
SUCCEEDED_DETAIL_KEYS = {"build_job_id", "phase", "build_target", "result"}
#: The stub's PORTAL_BUILD_RESULT payload, echoed back verbatim in the
#: succeeded detail's result field.
STUB_RESULT = {"component_name": "aws.edgeml.dda.LocalServer.arm64JP5",
               "published_version": "1.0.0", "pushed_image_refs": []}

#: Requirement 3.4 — test_enospc_classification_properties.py green on the
#: unfixed tree: RECORDED 6 passed. Pinned as a collection count so a test
#: silently vanishing from the ENOSPC → RUNNER_DISK_FULL contract suite is
#: caught; the suite's own green-ness is delegated to its re-run (task 3.5/5).
ENOSPC_SUITE = os.path.join(REPO_ROOT, "test", "backend-test",
                            "portal_builds",
                            "test_enospc_classification_properties.py")
ENOSPC_SUITE_TEST_COUNT = 6

#: Requirement 3.5 — local dev build entry points that must never grow a
#: reference to the prune script.
LOCAL_DEV_SCRIPTS = ("run_jp_builds.sh", "build-custom.sh")

#: The four /tmp build-log glob patterns the prune script (once it exists)
#: is allowed to touch, from design Decision 5.
LOG_PATTERNS = ("gdk-build-*.log", "gdk-publish-*.log",
                "portal-build-agent-*.log", "inference-uploader-build-*.log")


# ---------------------------------------------------------------------------
# Helpers — the test_source_selection_preservation.py sandbox technique.
# ---------------------------------------------------------------------------

def _require_free_build_lock():
    """Refuse to run a sandbox agent pass while the on-server build lock is
    held: the byte-identical copy takes the same real lock, and a held lock
    means a build may be in progress (builds.md: never disturb it)."""
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


def _sandbox_repo():
    """A temporary repository whose scripts/portal-build-agent.sh is a
    BYTE-IDENTICAL copy of the real script and whose portal-build.sh is the
    clean-run stub (exit 0 + PORTAL_BUILD_RESULT). Returns (repo, agent)."""
    root = tempfile.mkdtemp(prefix="dda-prune-preserve-")
    repo = os.path.join(root, "DefectDetectionApplication")
    os.makedirs(os.path.join(repo, "scripts"))
    agent = os.path.join(repo, "scripts", "portal-build-agent.sh")
    shutil.copyfile(AGENT_SCRIPT, agent)
    os.chmod(agent, os.stat(agent).st_mode | stat.S_IXUSR)
    with open(AGENT_SCRIPT, "rb") as real, open(agent, "rb") as copy:
        assert (hashlib.sha256(real.read()).hexdigest()
                == hashlib.sha256(copy.read()).hexdigest()), \
            "the sandbox agent is not byte-identical to the real script"
    stub = os.path.join(repo, "portal-build.sh")
    with open(stub, "w", encoding="utf-8") as handle:
        handle.write(
            "#!/bin/bash\n"
            'echo "STUB portal-build.sh $*"\n'
            "echo 'PORTAL_BUILD_RESULT " + json.dumps(STUB_RESULT,
                                                      separators=(",", ":"))
            + "'\n"
            "exit 0\n")
    os.chmod(stub, 0o755)
    return repo, agent


def _agent_env(home):
    """Minimal environment: EVENT_BUS deliberately absent so emit_event
    PRINTS the detail instead of calling aws events (no AWS, by
    construction)."""
    return {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": home,
        "LC_ALL": "C.UTF-8",
        "GIT_CONFIG_GLOBAL": os.path.join(home, "gitconfig-absent"),
    }


def _emitted_details(stdout):
    """The phase-event details the agent emitted, parsed from the
    ``EVENT_BUS not set — skipping event: <detail>`` lines. Info lines (the
    future prune section's zero-deletion report included) are ignored by
    construction — only the marker lines carry details."""
    details = []
    marker = "skipping event: "
    for line in stdout.splitlines():
        if marker in line:
            details.append(json.loads(line.split(marker, 1)[1]))
    return details


def _run_clean_sandbox_agent(target="JP5"):
    """Run the sandbox agent on the clean-run scenario; returns
    (job_id, CompletedProcess, details)."""
    _require_free_build_lock()
    repo, agent = _sandbox_repo()
    job_id = f"bj-preserve-{uuid.uuid4()}"
    try:
        result = subprocess.run(
            ["bash", agent, f"BUILD_JOB_ID={job_id}",
             f"BUILD_TARGET={target}"],
            cwd=repo, env=_agent_env(repo), stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=120)
    finally:
        shutil.rmtree(os.path.dirname(repo), ignore_errors=True)
        leftover = f"/tmp/portal-build-agent-{job_id}.log"
        if os.path.exists(leftover):
            os.remove(leftover)
    return job_id, result, _emitted_details(
        result.stdout.decode("utf-8", "replace"))


def _install_stub(bin_dir, name, content):
    path = os.path.join(bin_dir, name)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(content)
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP
             | stat.S_IXOTH)


_RECORDING_AWS_STUB = """\
#!/usr/bin/env bash
echo "$@" >> "$AWS_ARGV_LOG"
case "$*" in
    *describe-images*) exit ${AWS_DESCRIBE_EXIT:-0} ;;
    *) exit 0 ;;
esac
"""

#: docker stub: serves a canned `image ls` inventory, a canned RepoDigest on
#: `inspect`, records everything else (rmi, prune, ...) and succeeds.
_RECORDING_DOCKER_STUB = """\
#!/usr/bin/env bash
echo "$@" >> "$DOCKER_ARGV_LOG"
case "$1 $2" in
    "image ls")      printf '%b' "$DOCKER_IMAGE_LS" ;;
    "image inspect") printf '%s\\n' "$DOCKER_REPO_DIGEST" ;;
    "image prune")   echo "Total reclaimed space: 0B" ;;
esac
exit 0
"""

#: df stub: a roomy volume regardless of argv (the prune script's own
#: measurement calls) so the threshold gate never fires in these legs.
_ROOMY_DF_STUB = """\
#!/usr/bin/env bash
echo "1K-blocks Used Available"
echo "201326592 67108864 134217728"
exit 0
"""


def _run_prune_script(tmp, image_ls="", repo_digest="",
                      describe_exit=0, build_job_id="bj-current",
                      extra_env=None):
    """Run the REAL scripts/prune-build-server-disk.sh with recording stub
    docker/aws and a roomy stub df on PATH. Returns (CompletedProcess,
    aws_argv_lines, docker_argv_lines)."""
    bin_dir = os.path.join(tmp, "bin")
    os.makedirs(bin_dir, exist_ok=True)
    aws_log = os.path.join(tmp, "aws-argv.log")
    docker_log = os.path.join(tmp, "docker-argv.log")
    _install_stub(bin_dir, "aws", _RECORDING_AWS_STUB)
    _install_stub(bin_dir, "docker", _RECORDING_DOCKER_STUB)
    _install_stub(bin_dir, "df", _ROOMY_DF_STUB)
    env = {
        "PATH": bin_dir + os.pathsep
        + os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": tmp,
        "LC_ALL": "C.UTF-8",
        "AWS_ARGV_LOG": aws_log,
        "DOCKER_ARGV_LOG": docker_log,
        "AWS_DESCRIBE_EXIT": str(describe_exit),
        "DOCKER_IMAGE_LS": image_ls,
        "DOCKER_REPO_DIGEST": repo_digest,
    }
    if extra_env:
        env.update(extra_env)
    result = subprocess.run(
        ["bash", PRUNE_SCRIPT, f"BUILD_JOB_ID={build_job_id}"],
        cwd=tmp, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=120)

    def _lines(path):
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as handle:
            return handle.read().splitlines()

    return result, _lines(aws_log), _lines(docker_log)


_PRUNE_ABSENT = not os.path.isfile(PRUNE_SCRIPT)
_PRUNE_ABSENT_REASON = (
    "scripts/prune-build-server-disk.sh does not exist yet (created by task "
    "3.1) — this preservation property binds automatically once the prune "
    "script lands (re-run bound in task 3.5)")


# ---------------------------------------------------------------------------
# Leg 1 — Clean-server identity (Requirement 3.1). RECORDED unfixed
# baseline: exit 0, phases ["building", "succeeded"], the exact detail field
# sets and values below. The fixed tree must reproduce them byte-for-byte at
# the detail level: the Step 2.5 prune section's zero-deletion info line is
# plain stdout, which the detail parser ignores by construction.
# ---------------------------------------------------------------------------

class TestCleanServerIdentity:
    """The clean-run sandbox agent: same steps, same phase-event field sets,
    same PORTAL_BUILD_RESULT round-trip, same exit code as the recorded
    unfixed baseline.

    # Validates: Requirements 3.1
    """

    def test_clean_run_reproduces_the_unfixed_baseline(self):
        job_id, result, details = _run_clean_sandbox_agent(target="JP5")
        stdout = result.stdout.decode("utf-8", "replace")
        observed = json.dumps({"exit": result.returncode,
                               "details": details, "stdout": stdout},
                              indent=2)

        # RECORDED: exit 0.
        assert result.returncode == CLEAN_RUN_EXIT_CODE, observed
        # RECORDED: exactly ["building", "succeeded"], in order — no extra,
        # reordered, or missing phase event on a clean run.
        assert [d["phase"] for d in details] == CLEAN_RUN_PHASES, observed

        building, succeeded = details
        # RECORDED building field set and values.
        assert set(building) == BUILDING_DETAIL_KEYS, observed
        assert building["build_job_id"] == job_id, observed
        assert building["build_target"] == "JP5", observed
        assert building["source_ref"] == "", observed

        # RECORDED succeeded field set and the verbatim result round-trip.
        assert set(succeeded) == SUCCEEDED_DETAIL_KEYS, observed
        assert succeeded["build_job_id"] == job_id, observed
        assert succeeded["build_target"] == "JP5", observed
        assert succeeded["result"] == STUB_RESULT, observed

        # RECORDED step ordering on stdout: lock → building event →
        # portal-build.sh start → stub run → succeeded event.
        lock_pos = stdout.find("Acquired build lock")
        building_pos = stdout.find('"phase":"building"')
        start_pos = stdout.find("Starting portal-build.sh aarch64 5")
        stub_pos = stdout.find("STUB portal-build.sh aarch64 5")
        result_pos = stdout.find("PORTAL_BUILD_RESULT ")
        succeeded_pos = stdout.find('"phase":"succeeded"')
        positions = [lock_pos, building_pos, start_pos, stub_pos,
                     result_pos, succeeded_pos]
        assert all(p >= 0 for p in positions), observed
        assert positions == sorted(positions), (
            "clean-run step ordering changed vs the recorded unfixed "
            "baseline: " + observed)


# ---------------------------------------------------------------------------
# Leg 2 — ECR read-only (Requirement 3.2), SKIP-AS-ABSENT PBT: for any
# generated image inventory / ECR reachability / knob combination, the prune
# script's recorded aws argv transcript contains ONLY `ecr describe-images`
# calls. Binds when task 3.1 lands; re-run bound in task 3.5.
# ---------------------------------------------------------------------------

_ECR_REGISTRY = "123456789012.dkr.ecr.us-east-1.amazonaws.com"

_IMAGE_ROWS = st.lists(
    st.one_of(
        # Per-version ECR-repo generations (prune candidates).
        st.builds(
            lambda name, ver, size:
            f"{_ECR_REGISTRY}/dda/{name}\t{ver}\t{size}GB",
            st.sampled_from(["flask-app", "react-webapp"]),
            st.builds(lambda a, b, c: f"1.{a}.{b}{'' if c else '-rc'}",
                      st.integers(0, 3), st.integers(0, 9), st.booleans()),
            st.integers(1, 42)),
        # ECR-repo :latest (must never be deleted; still no ECR mutation).
        st.builds(lambda name: f"{_ECR_REGISTRY}/dda/{name}\tlatest\t30GB",
                  st.sampled_from(["flask-app", "react-webapp"])),
        # Unqualified local lineage.
        st.builds(lambda name: f"{name}\tlatest\t29GB",
                  st.sampled_from(["flask-app", "react-webapp",
                                   "edgemlsdk"])),
        # Dangling.
        st.just("<none>\t<none>\t1GB"),
        # Unrelated junk repos.
        st.builds(lambda name: f"{name}\t1.0\t100MB",
                  st.sampled_from(["ubuntu", "python", "moby/buildkit"])),
    ),
    max_size=8)

#: Digest served by the docker-inspect stub: present, or absent
#: (NO_REPODIGEST path) — the transcript must be mutation-free either way.
_REPO_DIGESTS = st.one_of(
    st.just(""),
    st.builds(lambda h: f"{_ECR_REGISTRY}/dda/flask-app@sha256:{h}",
              st.text(alphabet="0123456789abcdef", min_size=64,
                      max_size=64)))

#: aws describe-images exit code: 0 (confirmed), 254/255 (ECR unreachable /
#: not found — the fail-open paths).
_DESCRIBE_EXITS = st.sampled_from([0, 1, 254, 255])

#: A token that must appear in every allowed aws call.
_ALLOWED_AWS_CALL = re.compile(r"\becr\b.*\bdescribe-images\b")
#: Any mutating verb anywhere in an aws argv line is a violation.
_FORBIDDEN_AWS_VERB = re.compile(
    r"\b(batch-delete-image|delete-repository|put-image|batch-get-image"
    r"|delete-|put-|create-|start-image-scan|untag-resource|tag-resource)")


@pytest.mark.skipif(_PRUNE_ABSENT, reason=_PRUNE_ABSENT_REASON)
class TestEcrReadOnly:
    """Pruning acts ONLY on local tags: describe-images is the sole ECR call
    the prune script ever makes, for any inventory and any ECR state.

    # Validates: Requirements 3.2
    """

    @settings(deadline=None,
              suppress_health_check=[HealthCheck.too_slow])
    @given(rows=_IMAGE_ROWS, repo_digest=_REPO_DIGESTS,
           describe_exit=_DESCRIBE_EXITS)
    def test_aws_transcript_contains_only_ecr_describe_images(
            self, rows, repo_digest, describe_exit):
        # Validates: Requirements 3.2
        with tempfile.TemporaryDirectory(
                prefix="dda-prune-ecr-ro-") as tmp:
            result, aws_calls, _docker_calls = _run_prune_script(
                tmp, image_ls="".join(row + "\\n" for row in rows),
                repo_digest=repo_digest, describe_exit=describe_exit)
        observed = json.dumps({
            "rows": rows, "repo_digest": repo_digest,
            "describe_exit": describe_exit, "exit": result.returncode,
            "aws_calls": aws_calls,
            "stdout": result.stdout.decode("utf-8", "replace")}, indent=2)
        for call in aws_calls:
            assert not _FORBIDDEN_AWS_VERB.search(call), (
                "the prune script issued a MUTATING aws call — pruning "
                "must never touch ECR contents (3.2): " + observed)
            assert _ALLOWED_AWS_CALL.search(call), (
                "the prune script issued an aws call other than "
                "`ecr describe-images` — describe-images is the only ECR "
                "call allowed (3.2): " + observed)


# ---------------------------------------------------------------------------
# Leg 3 — In-progress-build immunity (Requirement 3.3), SKIP-AS-ABSENT:
# the prune script never deletes the current job's log
# (portal-build-agent-${BUILD_JOB_ID}.log), even when it is over-age.
# ---------------------------------------------------------------------------

_JOB_IDS = st.builds(
    lambda a, b: f"bj-{a}-{b}",
    st.text(alphabet="0123456789abcdef", min_size=4, max_size=12),
    st.integers(0, 999))

#: Ages in days for sibling logs, straddling the 7-day default retention.
_AGE_DAYS = st.integers(min_value=0, max_value=30)


@pytest.mark.skipif(_PRUNE_ABSENT, reason=_PRUNE_ABSENT_REASON)
class TestInProgressBuildImmunity:
    """The current job's log is excluded from log pruning no matter its age.

    Binding contract for task 3.1 (anticipated by task 4.3's "sandboxed tmp
    dir"): the prune script honors a ``PRUNE_TMP_DIR`` env override for its
    /tmp scan root so tests never touch the real /tmp. The structural
    guard below fails loudly if the script lands without the override —
    a silent scan of the real /tmp would make this property vacuous.

    # Validates: Requirements 3.3
    """

    def test_prune_script_supports_the_sandboxed_tmp_override(self):
        # Validates: Requirements 3.3
        with open(PRUNE_SCRIPT, encoding="utf-8") as handle:
            text = handle.read()
        assert "PRUNE_TMP_DIR" in text, (
            "scripts/prune-build-server-disk.sh does not honor "
            "PRUNE_TMP_DIR — the log-pruning tests (this file and task "
            "4.3's sandboxed tmp populations) cannot bind honestly "
            "without the test override for the /tmp scan root")

    @settings(deadline=None,
              suppress_health_check=[HealthCheck.too_slow])
    @given(job_id=_JOB_IDS, current_age_days=_AGE_DAYS,
           sibling_ages=st.lists(_AGE_DAYS, max_size=4))
    def test_prune_never_deletes_the_current_jobs_log(
            self, job_id, current_age_days, sibling_ages):
        # Validates: Requirements 3.3
        import time
        with tempfile.TemporaryDirectory(
                prefix="dda-prune-immune-") as tmp:
            log_dir = os.path.join(tmp, "fake-tmp")
            os.makedirs(log_dir)
            now = time.time()

            current_log = os.path.join(
                log_dir, f"portal-build-agent-{job_id}.log")
            with open(current_log, "w", encoding="utf-8") as handle:
                handle.write("current job log\n")
            aged = now - current_age_days * 86400
            os.utime(current_log, (aged, aged))

            for index, age in enumerate(sibling_ages):
                sibling = os.path.join(
                    log_dir, f"portal-build-agent-bj-sibling-{index}.log")
                with open(sibling, "w", encoding="utf-8") as handle:
                    handle.write("finished job log\n")
                stamp = now - age * 86400
                os.utime(sibling, (stamp, stamp))

            result, _aws, _docker = _run_prune_script(
                tmp, build_job_id=job_id,
                extra_env={"PRUNE_TMP_DIR": log_dir})
            survived = os.path.exists(current_log)

        observed = json.dumps({
            "job_id": job_id, "current_age_days": current_age_days,
            "sibling_ages": sibling_ages, "exit": result.returncode,
            "survived": survived,
            "stdout": result.stdout.decode("utf-8", "replace")}, indent=2)
        assert survived, (
            "the prune script DELETED the current job's log "
            f"(portal-build-agent-{job_id}.log) — in-progress builds must "
            "be untouched (3.3), even when the log is over-age: "
            + observed)


# ---------------------------------------------------------------------------
# Leg 4 — ENOSPC classification identity (Requirement 3.4): the existing
# suite that pins the error_kind=disk / ENOSPC-evidence → RUNNER_DISK_FULL
# path is present with its recorded shape. RECORDED on the unfixed tree:
# 6 passed. Its green-ness is re-verified by running it (tasks 2/3.5/5 run
# the file directly); here the recorded count is pinned so a silently
# vanishing test cannot shrink the contract.
# ---------------------------------------------------------------------------

class TestEnospcClassificationIdentity:
    """The ENOSPC → RUNNER_DISK_FULL contract suite the fix reuses is intact.

    # Validates: Requirements 3.4
    """

    def test_enospc_suite_exists_with_the_recorded_test_count(self):
        assert os.path.isfile(ENOSPC_SUITE), (
            "test_enospc_classification_properties.py is gone — the "
            "ENOSPC → RUNNER_DISK_FULL classification contract (3.4) has "
            "lost its pin")
        with open(ENOSPC_SUITE, encoding="utf-8") as handle:
            text = handle.read()
        count = len(re.findall(r"^\s*def test_", text, re.MULTILINE))
        assert count == ENOSPC_SUITE_TEST_COUNT, (
            f"test_enospc_classification_properties.py now defines {count} "
            f"tests (recorded unfixed baseline: "
            f"{ENOSPC_SUITE_TEST_COUNT}) — the ENOSPC classification "
            "contract changed shape; verify 3.4 is still fully pinned")

    def test_agent_still_classifies_enospc_evidence_as_disk(self):
        """The agent's own ENOSPC-evidence branch (BUILD_FAILURE_KIND=disk
        on `no space left on device|enospc` in the log tail) is part of the
        recorded 3.4 surface — pinned textually here; behaviorally by the
        delegated suite."""
        with open(AGENT_SCRIPT, encoding="utf-8") as handle:
            text = handle.read()
        assert "no space left on device|enospc" in text, (
            "the agent's ENOSPC log-evidence detection changed — the "
            "error_kind=disk classification path (3.4) must stay intact")
        assert 'BUILD_FAILURE_KIND="disk"' in text, (
            "the agent no longer reports error_kind=disk on ENOSPC "
            "evidence — RUNNER_DISK_FULL classification (3.4) broken")


# ---------------------------------------------------------------------------
# Leg 5 — Local dev builds untouched (Requirement 3.5): text pin.
# ---------------------------------------------------------------------------

class TestLocalDevBuildsUntouched:
    """run_jp_builds.sh and build-custom.sh never reference the prune
    script: local dev builds are structurally outside the pruning surface.

    # Validates: Requirements 3.5
    """

    @pytest.mark.parametrize("script", LOCAL_DEV_SCRIPTS)
    def test_local_dev_script_has_no_prune_reference(self, script):
        path = os.path.join(REPO_ROOT, script)
        assert os.path.isfile(path), f"{script} is missing from the repo root"
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
        assert "prune-build-server-disk" not in text, (
            f"{script} references prune-build-server-disk.sh — local dev "
            "builds must stay unaffected by the pruning fix (3.5)")


# ---------------------------------------------------------------------------
# Leg 6 — Pinned script contracts (Requirement 3.6): the ONE intended golden
# rebaseline, recorded verbatim; every other pinned contract must hold with
# the golden file CONSISTENT with the shipped agent script.
#
# RECORDED unfixed-tree baselines (2026-08-16), re-run green in task 3.5/5:
#   build_save_pkgs ................................. 21 passed
#   test_source_selection_preservation.py ........... 35 passed
#   test_agent_tail_truncation_properties.py ........  6 passed
#   test_preflight_target_matrix_properties.py ...... 11 passed
#   test_preflight_agent_contract.py ................ 50 passed
#   test_execution_failure_preservation.py .......... 73 passed
# ---------------------------------------------------------------------------

class TestPinnedScriptContracts:
    """The agent sha256 golden: on the unfixed tree it is the recorded
    value; after task 3.2 it must equal the sha256 of the CURRENT agent
    (the conscious rebaseline). In BOTH states the golden matches the
    shipped script — a mismatch is an unintended agent edit.

    # Validates: Requirements 3.6
    """

    RECORDED_UNFIXED_GOLDEN = UNFIXED_AGENT_SHA256_GOLDEN

    def _current_agent_sha256(self):
        with open(AGENT_SCRIPT, "rb") as handle:
            return hashlib.sha256(handle.read()).hexdigest()

    def _golden_value(self):
        assert os.path.isfile(AGENT_SHA256_BASELINE_FILE), (
            "the build_save_pkgs agent sha256 baseline file is gone — "
            "never delete the neighbor-script golden (3.6)")
        with open(AGENT_SHA256_BASELINE_FILE, encoding="utf-8") as handle:
            return handle.read().strip()

    def test_golden_file_holds_exactly_one_sha256(self):
        value = self._golden_value()
        assert re.fullmatch(r"[0-9a-f]{64}", value), (
            f"the agent sha256 golden is malformed: {value!r} — the "
            "neighbor-script pin (3.6) was weakened")

    def test_golden_matches_the_shipped_agent_script(self):
        """The load-bearing consistency pin: golden == sha256(agent), on
        the unfixed tree (recorded value) and on the fixed tree (the task
        3.2 conscious rebaseline) alike. Catches any agent edit that
        forgot its golden update, and any golden edit without an agent
        change."""
        golden = self._golden_value()
        current = self._current_agent_sha256()
        assert golden == current, (
            "scripts/portal-build-agent.sh (sha256 "
            f"{current}) does not match the build_save_pkgs golden "
            f"({golden}) — an agent edit landed without its conscious "
            "golden rebaseline, or vice versa (3.6). The recorded UNFIXED "
            f"golden was {self.RECORDED_UNFIXED_GOLDEN}; the ONE intended "
            "rebaseline is task 3.2's hook change.")

    def test_unfixed_golden_value_is_recorded_for_the_audit_trail(self):
        """The pre-fix golden value is recorded VERBATIM in this file (the
        task 2 audit record that makes the task 3.2 rebaseline diff
        auditable). The golden file must hold either the recorded unfixed
        value (before 3.2) or a DIFFERENT value that matches the current
        agent (after 3.2) — never a third state."""
        golden = self._golden_value()
        if golden != self.RECORDED_UNFIXED_GOLDEN:
            # Post-rebaseline: the new value must match the shipped agent
            # (the conscious 3.2 update), which the consistency pin above
            # asserts too — restated here so THIS test names the audit.
            assert golden == self._current_agent_sha256(), (
                "the agent sha256 golden changed from the recorded unfixed "
                f"value ({self.RECORDED_UNFIXED_GOLDEN}) to {golden}, but "
                "that is NOT the sha256 of the shipped agent — an "
                "unexplained golden edit (3.6)")

    def test_agent_pinned_contract_anchors_are_intact(self):
        """The portal_builds suites' pinned agent contracts, spot-pinned
        textually: argument contract, exit codes 64/75/78, preflight
        markers, ERROR_TAIL derivation. Their full behavioral pinning is
        the delegated suites' job (re-run in tasks 3.5/5); these anchors
        make THIS file fail fast on a contract-surface edit."""
        with open(AGENT_SCRIPT, encoding="utf-8") as handle:
            text = handle.read()
        observed = "scripts/portal-build-agent.sh contract anchors"
        for anchor in (
                # Argument contract (exit 64 paths).
                "BUILD_JOB_ID=*)", "BUILD_TARGET=*)", "EVENT_BUS=*)",
                "SOURCE_REF=*)", "exit 64",
                # Lock contract (exit 75).
                'LOCK_FILE="/var/lock/dda-build.lock"', "exit 75",
                # Preflight contract (exit 78, measurement-only knob).
                "exit 78",
                'PREFLIGHT_MIN_DISK_GB="${PREFLIGHT_MIN_DISK_GB:-}"',
                "record_disk_capacity",
                "DDA_PREFLIGHT_FAILED",
                # ERROR_TAIL derivation (tail-preserving truncation).
                "ERROR_TAIL=$(tail -n 5 \"$BUILD_LOG\" 2>/dev/null | tail -c 512)",
        ):
            assert anchor in text, (
                f"pinned agent contract anchor {anchor!r} vanished — the "
                "portal_builds suites' pinned contract surface (3.6) "
                f"changed [{observed}]")
