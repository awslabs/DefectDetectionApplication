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
"""Sandbox end-to-end integration suite (Task 4.4) for
build-server-disk-pruning.

REAL agent (``scripts/portal-build-agent.sh``) + REAL prune script
(``scripts/prune-build-server-disk.sh``) + stub ``docker``/``aws``/``df``
+ recording stub ``portal-build.sh``, over the four scenario classes from
design "Integration Tests":

1. **stale+roomy** — stale ECR-confirmed generations pruned (confirmed
   ONLY), old build logs pruned, ``custom-build/`` removed, dangling prune
   runs, threshold passes, the build runs — the full
   event/exit/transcript contract asserted.
2. **stale+tight** — pruning frees what it can, post-prune free space is
   still below ``BUILD_MIN_FREE_DISK_GB`` → ``error_kind=disk`` failed
   event, ``portal-build.sh`` never invoked, the agent exits nonzero.
3. **clean+roomy** — the prune no-ops (zero deletions, measurements still
   logged); the emitted detail sequence, field sets, result round-trip,
   step ordering, and exit code are identical to the task 2 unfixed
   clean-run baseline.
4. **ECR-down** — zero image deletions of unverifiable candidates, loud
   ``ECR_UNVERIFIABLE`` retention logs, the always-safe steps (dangling /
   logs / ``custom-build/``) still run, the build proceeds (fail open).

# Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.5, 3.1, 3.2, 3.3

HONESTY GUARD (absolute): nothing here runs real docker, aws, gdk, or
touches the build server. The agent and the prune script are executed as
sha256-verified byte-identical sandbox copies (so ``REPO_DIR`` — and hence
the ``custom-build/`` removal — resolves inside the sandbox, never the
live clone); ``docker``/``aws``/``df`` are recording stubs on PATH;
``PRUNE_TMP_DIR`` points at a sandboxed dir so the log-pruning step never
scans the real ``/tmp``. ``EVENT_BUS`` is unset (``emit_event`` PRINTS
each detail — no AWS by construction) and ``ATTEMPT_ID`` is unset (no
preflight aws calls). The REAL ``/var/lock/dda-build.lock`` is checked
first — a held lock SKIPS rather than disturb a running build
(builds.md).

Helpers are module-local by design (the other suites in this directory
are final — no shared conftest, no cross-file imports).
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
import time
import uuid
from collections import namedtuple

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.normpath(os.path.join(_HERE, "..", "..", ".."))

AGENT_SCRIPT = os.path.join(REPO_ROOT, "scripts", "portal-build-agent.sh")
PRUNE_SCRIPT = os.path.join(REPO_ROOT, "scripts",
                            "prune-build-server-disk.sh")
AGENT_LOCK_FILE = "/var/lock/dda-build.lock"

_KB_PER_GB = 1048576
ROOMY_AVAIL_KB = 128 * _KB_PER_GB   # well above the default 60 GB floor
TIGHT_AVAIL_KB = 29 * _KB_PER_GB    # the e1d672ce incident's free space
DEFAULT_THRESHOLD_GB = 60

# ---------------------------------------------------------------------------
# The canned stale-server state (scenarios 1, 2, 4).
# ---------------------------------------------------------------------------

ECR_REGISTRY = "123456789012.dkr.ecr.us-east-1.amazonaws.com"
BACKEND_REPO = f"{ECR_REGISTRY}/dda/flask-app"
FRONTEND_REPO = f"{ECR_REGISTRY}/dda/react-webapp"

#: The two ECR-confirmed per-version generations (RepoDigest present,
#: digest in ECR) — THE safe set for the stale scenarios.
CONFIRMED_BACKEND_REF = f"{BACKEND_REPO}:1.0.7"
CONFIRMED_FRONTEND_REF = f"{FRONTEND_REPO}:1.0.7"
CONFIRMED_BACKEND_DIGEST = "sha256:" + "a" * 64
CONFIRMED_FRONTEND_DIGEST = "sha256:" + "b" * 64

#: An unpushed per-version generation (push failed → no RepoDigest):
#: structurally undeletable, retained with reason=NO_REPODIGEST.
UNPUSHED_BACKEND_REF = f"{BACKEND_REPO}:1.0.9"

#: docker `image ls` rows (Repository\tTag\tSize) for the stale scenarios:
#: the deletion candidates PLUS everything that must never be touched
#: (ECR :latest, the unqualified :latest lineage, a dangling row).
STALE_IMAGE_ROWS = [
    f"{BACKEND_REPO}\t1.0.7\t42GB",
    f"{FRONTEND_REPO}\t1.0.7\t29GB",
    f"{BACKEND_REPO}\t1.0.9\t42GB",
    f"{BACKEND_REPO}\tlatest\t42GB",
    "flask-app\tlatest\t42GB",
    "react-webapp\tlatest\t8GB",
    "edgemlsdk\tlatest\t20GB",
    "<none>\t<none>\t1GB",
]

#: Only the :latest sets — the clean-server scenario's inventory.
CLEAN_IMAGE_ROWS = [
    f"{BACKEND_REPO}\tlatest\t42GB",
    f"{FRONTEND_REPO}\tlatest\t8GB",
    "flask-app\tlatest\t42GB",
    "react-webapp\tlatest\t8GB",
    "edgemlsdk\tlatest\t20GB",
]

_NOISE_DIGEST_LINE = "docker.io/library/noise@sha256:" + "f" * 64

#: Per-ref `docker image inspect` state: (exit code, RepoDigest lines).
STALE_INSPECT_STATES = {
    CONFIRMED_BACKEND_REF: (
        0, [_NOISE_DIGEST_LINE,
            f"{BACKEND_REPO}@{CONFIRMED_BACKEND_DIGEST}"]),
    CONFIRMED_FRONTEND_REF: (
        0, [_NOISE_DIGEST_LINE,
            f"{FRONTEND_REPO}@{CONFIRMED_FRONTEND_DIGEST}"]),
    UNPUSHED_BACKEND_REF: (0, [_NOISE_DIGEST_LINE]),  # no usable RepoDigest
}

#: Per-digest ECR state (aws stub): digests present in ECR — full
#: confirmation. ECR-down (scenario 4) writes NO state files, so the aws
#: stub's default (exit 255, network error) answers every describe-images.
ECR_UP_AWS_STATES = {
    CONFIRMED_BACKEND_DIGEST.split(":", 1)[1]: "present",
    CONFIRMED_FRONTEND_DIGEST.split(":", 1)[1]: "present",
}

#: The sandboxed tmp population for the stale scenarios: over-age matches
#: of all four build-log patterns (pruned), an in-window match and a
#: non-matching file (survive). The current job's log is planted
#: separately (over-age — must survive anyway).
STALE_TMP_LOGS = {
    "gdk-build-old.log": 20,
    "gdk-publish-old.log": 20,
    "portal-build-agent-bj-finished.log": 20,
    "inference-uploader-build-old.log": 20,
    "gdk-build-recent.log": 1,
    "unrelated.dat": 20,
}
EXPECTED_PRUNED_LOGS = {
    "gdk-build-old.log", "gdk-publish-old.log",
    "portal-build-agent-bj-finished.log",
    "inference-uploader-build-old.log",
}
CURRENT_JOB_LOG_AGE_DAYS = 20  # over-age, immune regardless

#: The task 2 clean-run baseline (recorded on the UNFIXED tree) the
#: clean+roomy scenario must reproduce at the detail level (3.1).
CLEAN_RUN_PHASES = ["building", "succeeded"]
BUILDING_DETAIL_KEYS = {"build_job_id", "phase", "build_target",
                        "source_ref", "source_commit"}
SUCCEEDED_DETAIL_KEYS = {"build_job_id", "phase", "build_target", "result"}
STUB_RESULT = {"component_name": "aws.edgeml.dda.LocalServer.arm64JP7",
               "published_version": "1.0.0", "pushed_image_refs": []}

#: The exit-3 marker (design Decision 3 / File 1 step 6).
_INSUFFICIENT_RE = re.compile(
    r"^PRUNE-DISK-INSUFFICIENT free=(\d+)GB required=(\d+)GB$",
    re.MULTILINE)

#: Every aws call the prune script is allowed to make (3.2).
_ALLOWED_AWS_CALL = re.compile(r"^ecr describe-images\b")

# ---------------------------------------------------------------------------
# Stub binaries — the deploy_reliability / csi_nvargus_optional recording
# pattern, mirroring the (final) task 4.1/4.2 suites.
# ---------------------------------------------------------------------------

_DOCKER_STUB = """\
#!/usr/bin/env bash
echo "$@" >> "$DOCKER_ARGV_LOG"
if [ "$1" = "rmi" ]; then
    exit 0
fi
case "$1 $2" in
    "image ls")
        printf '%s' "$DOCKER_IMAGE_LS"
        ;;
    "image inspect")
        ref="${!#}"
        key="$(printf '%s' "$ref" | tr -c 'A-Za-z0-9._-' '_')"
        state="$STUB_STATE_DIR/inspect_${key}"
        if [ -f "$state" ]; then
            rc="$(head -n 1 "$state")"
            tail -n +2 "$state"
            exit "$rc"
        fi
        exit 1
        ;;
    "image prune")
        echo "Total reclaimed space: 123.4MB"
        ;;
esac
exit 0
"""

_AWS_STUB = """\
#!/usr/bin/env bash
echo "$@" >> "$AWS_ARGV_LOG"
digest=""
for arg in "$@"; do
    case "$arg" in
        imageDigest=*) digest="${arg#imageDigest=}" ;;
    esac
done
hex="${digest#sha256:}"
state="$STUB_STATE_DIR/aws_${hex}"
mode="error"
if [ -f "$state" ]; then
    mode="$(cat "$state")"
fi
case "$mode" in
    present)
        printf '{"imageDetails":[{"imageDigest":"%s"}]}\\n' "$digest"
        exit 0
        ;;
    absent)
        echo "An error occurred (ImageNotFoundException) when calling the \
DescribeImages operation: the image requested does not exist" >&2
        exit 254
        ;;
    *)
        echo "Could not connect to the endpoint URL: ECR unreachable" >&2
        exit 255
        ;;
esac
"""

#: df stub: serves DF_AVAIL_KB as the available-KB column for every
#: measurement call (before AND after pruning — the stub cannot model the
#: reclaim, which is fine: the threshold gate reads only the AFTER value).
_DF_STUB = """\
#!/usr/bin/env bash
echo "Avail"
echo "${DF_AVAIL_KB:-0}"
exit 0
"""

_SANITIZE_RE = re.compile(r"[^A-Za-z0-9._-]")


def _sha256(path):
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def _install_stub(bin_dir, name, content):
    path = os.path.join(bin_dir, name)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(content)
    os.chmod(path, 0o755)


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


def _plant_file(directory, name, age_days):
    """Create directory/name (content = its own name) with an mtime
    age_days + one hour in the past — the one-hour margin keeps find(1)'s
    whole-day -mtime arithmetic unambiguous (over a retention of R days
    IFF age_days > R)."""
    path = os.path.join(directory, name)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(name)
    stamp = time.time() - (age_days * 86400 + 3600)
    os.utime(path, (stamp, stamp))
    return path


Outcome = namedtuple(
    "Outcome",
    ["job_id", "returncode", "stdout", "details", "record_lines",
     "docker_lines", "aws_lines", "tmp_survivors",
     "custom_build_survived"])


def _emitted_details(stdout):
    """Phase-event details parsed from the ``EVENT_BUS not set — skipping
    event: <detail>`` lines (info/prune lines are plain stdout, ignored
    by construction)."""
    details = []
    marker = "skipping event: "
    for line in stdout.splitlines():
        if marker in line:
            details.append(json.loads(line.split(marker, 1)[1]))
    return details


def _run_integration(avail_kb, image_rows, inspect_states, aws_states,
                     plant_stale_tmp_state=True):
    """One end-to-end sandbox pass: REAL agent + REAL prune script + stub
    docker/aws/df + recording stub portal-build.sh (JP7 — the incident's
    target). Returns an Outcome."""
    _require_free_build_lock()
    job_id = f"bj-integ-{uuid.uuid4()}"
    current_job_log = f"portal-build-agent-{job_id}.log"
    try:
        with tempfile.TemporaryDirectory(prefix="dda-prune-integ-") as tmp:
            # Sandbox repo: byte-identical agent + byte-identical prune
            # script, so REPO_DIR (and the custom-build/ removal) resolves
            # inside the sandbox — never the live clone.
            repo = os.path.join(tmp, "DefectDetectionApplication")
            os.makedirs(os.path.join(repo, "scripts"))
            agent = os.path.join(repo, "scripts", "portal-build-agent.sh")
            shutil.copyfile(AGENT_SCRIPT, agent)
            os.chmod(agent, os.stat(agent).st_mode | stat.S_IXUSR)
            assert _sha256(agent) == _sha256(AGENT_SCRIPT), \
                "the sandbox agent is not byte-identical to the real script"
            prune = os.path.join(repo, "scripts",
                                 "prune-build-server-disk.sh")
            shutil.copyfile(PRUNE_SCRIPT, prune)
            os.chmod(prune, os.stat(prune).st_mode | stat.S_IXUSR)
            assert _sha256(prune) == _sha256(PRUNE_SCRIPT), \
                "the sandbox prune script is not byte-identical to the " \
                "real one"

            # Recording stub portal-build.sh (the build itself is outside
            # this spec's surface — the contract is WHETHER/WHEN it runs).
            record = os.path.join(tmp, "invocations.log")
            build_stub = os.path.join(repo, "portal-build.sh")
            with open(build_stub, "w", encoding="utf-8") as handle:
                handle.write("\n".join([
                    "#!/bin/bash",
                    f'echo "BUILD $*" >> "{record}"',
                    'echo "STUB portal-build.sh $*"',
                    "echo 'PORTAL_BUILD_RESULT "
                    + json.dumps(STUB_RESULT, separators=(",", ":"))
                    + "'",
                    "exit 0",
                ]) + "\n")
            os.chmod(build_stub, 0o755)

            # Stub binaries + their state files.
            bin_dir = os.path.join(tmp, "bin")
            state_dir = os.path.join(tmp, "state")
            fake_tmp = os.path.join(tmp, "fake-tmp")  # never the real /tmp
            for path in (bin_dir, state_dir, fake_tmp):
                os.makedirs(path)
            _install_stub(bin_dir, "docker", _DOCKER_STUB)
            _install_stub(bin_dir, "aws", _AWS_STUB)
            _install_stub(bin_dir, "df", _DF_STUB)
            for ref, (exit_code, digest_lines) in inspect_states.items():
                key = _SANITIZE_RE.sub("_", ref)
                with open(os.path.join(state_dir, f"inspect_{key}"), "w",
                          encoding="utf-8") as handle:
                    handle.write(str(exit_code) + "\n")
                    for line in digest_lines:
                        handle.write(line + "\n")
            for digest_hex, mode in aws_states.items():
                with open(os.path.join(state_dir, f"aws_{digest_hex}"),
                          "w", encoding="utf-8") as handle:
                    handle.write(mode)

            # The stale disk state (scenarios 1/2/4): old build logs in
            # the sandboxed tmp + a current-job log (over-age, immune) +
            # custom-build/ leftovers.
            if plant_stale_tmp_state:
                for name, age in STALE_TMP_LOGS.items():
                    _plant_file(fake_tmp, name, age)
                _plant_file(fake_tmp, current_job_log,
                            CURRENT_JOB_LOG_AGE_DAYS)
                workspace = os.path.join(repo, "custom-build", "staging")
                os.makedirs(workspace)
                with open(os.path.join(workspace, "leftover.tar"),
                          "wb") as handle:
                    handle.write(b"\0" * (1 << 20))  # 1 MiB → du size real

            env = {
                "PATH": bin_dir + os.pathsep
                + os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
                "HOME": tmp,
                "LC_ALL": "C.UTF-8",
                "GIT_CONFIG_GLOBAL": os.path.join(tmp, "gitconfig-absent"),
                # Stub state — inherited by the prune script the agent
                # invokes. EVENT_BUS/ATTEMPT_ID deliberately absent.
                "DOCKER_ARGV_LOG": os.path.join(tmp, "docker-argv.log"),
                "AWS_ARGV_LOG": os.path.join(tmp, "aws-argv.log"),
                "STUB_STATE_DIR": state_dir,
                "DOCKER_IMAGE_LS": "\n".join(image_rows),
                "DF_AVAIL_KB": str(avail_kb),
                "PRUNE_TMP_DIR": fake_tmp,
            }
            result = subprocess.run(
                ["bash", agent, f"BUILD_JOB_ID={job_id}",
                 "BUILD_TARGET=JP7"],
                cwd=repo, env=env, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, timeout=120)

            def _lines(path):
                if not os.path.exists(path):
                    return []
                with open(path, encoding="utf-8") as handle:
                    return handle.read().splitlines()

            stdout = result.stdout.decode("utf-8", "replace")
            return Outcome(
                job_id=job_id,
                returncode=result.returncode,
                stdout=stdout,
                details=_emitted_details(stdout),
                record_lines=_lines(record),
                docker_lines=_lines(env["DOCKER_ARGV_LOG"]),
                aws_lines=_lines(env["AWS_ARGV_LOG"]),
                tmp_survivors=set(os.listdir(fake_tmp)),
                custom_build_survived=os.path.exists(
                    os.path.join(repo, "custom-build")))
    finally:
        # The agent tees the build output to the REAL /tmp — clean it up.
        leftover = f"/tmp/portal-build-agent-{job_id}.log"
        if os.path.exists(leftover):
            os.remove(leftover)


def _rmi_refs(docker_lines):
    """The deleted image set, from the stub docker's recorded rmi argv."""
    return {line.split(None, 1)[1] for line in docker_lines
            if line.startswith("rmi ")}


def _observed(outcome, **extra):
    return json.dumps(dict(
        job_id=outcome.job_id,
        exit=outcome.returncode,
        details=outcome.details,
        record_lines=outcome.record_lines,
        docker_argv=outcome.docker_lines,
        aws_argv=outcome.aws_lines,
        tmp_survivors=sorted(outcome.tmp_survivors),
        custom_build_survived=outcome.custom_build_survived,
        stdout=outcome.stdout,
        **extra), indent=2, default=str)


def _assert_prune_ran_before_the_build(outcome, observed):
    prune_pos = outcome.stdout.find("Running pre-build disk prune")
    marker_pos = outcome.stdout.find("=== DDA pre-build disk prune")
    build_pos = outcome.stdout.find("STUB portal-build.sh")
    assert prune_pos >= 0 and marker_pos >= 0, (
        "the agent's Step 2.5 hook did not run the prune script: "
        + observed)
    assert build_pos >= 0, (
        "portal-build.sh never ran in a proceed scenario: " + observed)
    assert marker_pos < build_pos, (
        "the prune must run BEFORE the build: " + observed)


def _assert_measurements_logged(outcome, observed):
    for token in ("Free disk before pruning:", "Free disk after pruning:",
                  "Total freed:"):
        assert token in outcome.stdout, (
            f"missing the free-space measurement token {token!r} (2.5): "
            + observed)


def _assert_aws_transcript_read_only(outcome, observed):
    for call in outcome.aws_lines:
        assert _ALLOWED_AWS_CALL.match(call), (
            "the prune issued an aws call other than `ecr "
            "describe-images` — describe-images is the only ECR call "
            "allowed (3.2): " + observed)


def _assert_stale_tmp_and_workspace_pruned(outcome, observed):
    current_job_log = f"portal-build-agent-{outcome.job_id}.log"
    expected_survivors = ((set(STALE_TMP_LOGS) - EXPECTED_PRUNED_LOGS)
                          | {current_job_log})
    assert outcome.tmp_survivors == expected_survivors, (
        "log pruning deleted the wrong set — exactly the over-age "
        "matches of the four patterns, current job's log immune (2.1, "
        "2.5, 3.3): " + observed)
    assert re.search(
        r"RETAINED log .*" + re.escape(current_job_log)
        + r" \(current job's log", outcome.stdout), (
        "the over-age current-job log was not logged as retained (3.3): "
        + observed)
    for name in EXPECTED_PRUNED_LOGS:
        # Content = its own name, so the byte count is exact (2.5).
        assert re.search(
            r"PRUNED log \S*/" + re.escape(name)
            + r" \(" + str(len(name.encode("utf-8"))) + r" bytes\)",
            outcome.stdout), (
            f"missing the size-logged PRUNED line for {name!r} (2.5): "
            + observed)
    assert not outcome.custom_build_survived, (
        "custom-build/ leftovers were not removed (2.1): " + observed)
    assert "PRUNED workspace" in outcome.stdout, (
        "the workspace prune was not logged (2.5): " + observed)
    assert any(line.split() == ["image", "prune", "-f"]
               for line in outcome.docker_lines), (
        "the dangling-image prune (dangling-only -f) did not run (2.1): "
        + observed)


# ===========================================================================
# Scenario 1 — stale+roomy: generations pruned (ECR-confirmed only), logs
# pruned, threshold passes, build runs — full event/exit/transcript
# contract.
# ===========================================================================

class TestStaleRoomy:
    """Stale disk state + roomy volume: prune everything provably safe,
    then build normally.

    # Validates: Requirements 2.1, 2.2, 2.5, 3.2, 3.3
    """

    def test_stale_state_pruned_then_build_runs_normally(self):
        # Validates: Requirements 2.1, 2.2, 2.5, 3.2, 3.3
        outcome = _run_integration(
            avail_kb=ROOMY_AVAIL_KB, image_rows=STALE_IMAGE_ROWS,
            inspect_states=STALE_INSPECT_STATES,
            aws_states=ECR_UP_AWS_STATES)
        observed = _observed(outcome)

        # The full proceed contract: exit 0, phases building → succeeded,
        # portal-build.sh invoked exactly once, AFTER the prune.
        assert outcome.returncode == 0, observed
        assert [d["phase"] for d in outcome.details] == CLEAN_RUN_PHASES, (
            "the stale+roomy build must proceed with the normal phase "
            "sequence: " + observed)
        assert outcome.record_lines == ["BUILD aarch64 7"], (
            "portal-build.sh must run exactly once with the JP7 mapping: "
            + observed)
        _assert_prune_ran_before_the_build(outcome, observed)

        # Deleted images == EXACTLY the ECR-confirmed safe set (2.1, 2.2):
        # never :latest, never the unqualified lineage, never the
        # unpushed generation.
        assert _rmi_refs(outcome.docker_lines) == {
            CONFIRMED_BACKEND_REF, CONFIRMED_FRONTEND_REF}, (
            "the deleted set is not exactly the ECR-confirmed safe set "
            "(2.1, 2.2): " + observed)
        for ref, digest in ((CONFIRMED_BACKEND_REF,
                             CONFIRMED_BACKEND_DIGEST),
                            (CONFIRMED_FRONTEND_REF,
                             CONFIRMED_FRONTEND_DIGEST)):
            assert re.search(
                r"PRUNED image " + re.escape(ref)
                + r" \(\S+, digest " + re.escape(digest) + r"\)",
                outcome.stdout), (
                f"missing the PRUNED line (with size and digest) for "
                f"{ref!r} (2.5): " + observed)
        assert (f"RETAINED image {UNPUSHED_BACKEND_REF} "
                "reason=NO_REPODIGEST") in outcome.stdout, (
            "the unpushed generation was not retained-and-logged (2.1): "
            + observed)

        # Logs / workspace / dangling actions + the measurement contract.
        _assert_stale_tmp_and_workspace_pruned(outcome, observed)
        _assert_measurements_logged(outcome, observed)
        assert not _INSUFFICIENT_RE.search(outcome.stdout), (
            "no insufficiency line may appear on a roomy volume (2.3): "
            + observed)

        # ECR read-only: describe-images is the ONLY aws call, exactly one
        # per confirmable candidate (3.2).
        _assert_aws_transcript_read_only(outcome, observed)
        assert len(outcome.aws_lines) == 2, (
            "expected exactly one describe-images per RepoDigest-bearing "
            "candidate: " + observed)


# ===========================================================================
# Scenario 2 — stale+tight: pruning frees what it can, post-prune free
# still < threshold → error_kind=disk failed event, portal-build.sh never
# invoked, agent exits nonzero.
# ===========================================================================

class TestStaleTight:
    """Below the threshold even after pruning: fail fast BEFORE any build
    work, with the existing error_kind=disk classification.

    # Validates: Requirements 2.3, 2.4
    """

    def test_below_threshold_fails_fast_before_any_build_work(self):
        # Validates: Requirements 2.3, 2.4
        outcome = _run_integration(
            avail_kb=TIGHT_AVAIL_KB, image_rows=STALE_IMAGE_ROWS,
            inspect_states=STALE_INSPECT_STATES,
            aws_states=ECR_UP_AWS_STATES)
        observed = _observed(outcome)

        # Pruning still freed what it could (the safe set, the logs, the
        # workspace) before the gate fired.
        assert _rmi_refs(outcome.docker_lines) == {
            CONFIRMED_BACKEND_REF, CONFIRMED_FRONTEND_REF}, (
            "pruning must free what it can even when the threshold will "
            "still fail (2.3): " + observed)
        _assert_stale_tmp_and_workspace_pruned(outcome, observed)

        # The gate: 29 GB free (the e1d672ce value) < default 60 → the
        # marker line, then the agent's fail-fast.
        assert _INSUFFICIENT_RE.findall(outcome.stdout) == [
            (str(TIGHT_AVAIL_KB // _KB_PER_GB),
             str(DEFAULT_THRESHOLD_GB))], (
            "expected exactly one PRUNE-DISK-INSUFFICIENT line with the "
            "measured values (2.3): " + observed)

        # error_kind=disk failed event, no build work, nonzero exit —
        # never the e1d672ce path (~1 h wasted, manual recovery).
        assert [d["phase"] for d in outcome.details] == ["failed"], (
            "a below-threshold run must emit exactly one failed detail "
            "(no building/succeeded phase): " + observed)
        failed = outcome.details[0]
        assert failed.get("error_kind") == "disk", (
            'the failed detail must carry "error_kind":"disk" (the '
            "existing RUNNER_DISK_FULL classification, 2.4): " + observed)
        assert ("Insufficient disk space before build"
                in failed.get("error_message", "")), (
            "the failure message must identify the pre-build disk "
            "insufficiency (2.4): " + observed)
        assert outcome.record_lines == [], (
            "portal-build.sh must NEVER be invoked on a below-threshold "
            "run (2.4): " + observed)
        assert "STUB portal-build.sh" not in outcome.stdout, observed
        assert outcome.returncode == 1, (
            "the agent must exit 1 (plain build failure — the pinned "
            "64/75/78 codes keep their meanings) on the pre-build disk "
            "failure: " + observed)


# ===========================================================================
# Scenario 3 — clean+roomy: the prune no-ops; the event sequence, field
# sets, result round-trip, step ordering, and exit code are identical to
# the task 2 unfixed clean-run baseline.
# ===========================================================================

class TestCleanRoomy:
    """Nothing stale, plentiful disk: zero deletions, measurements still
    logged, and the build is byte-equivalent to the recorded unfixed
    baseline at the detail level.

    # Validates: Requirements 2.5, 3.1
    """

    def test_clean_run_reproduces_the_unfixed_baseline(self):
        # Validates: Requirements 2.5, 3.1
        outcome = _run_integration(
            avail_kb=ROOMY_AVAIL_KB, image_rows=CLEAN_IMAGE_ROWS,
            inspect_states={}, aws_states={},
            plant_stale_tmp_state=False)
        observed = _observed(outcome)

        # RECORDED task 2 baseline: exit 0; phases exactly
        # ["building", "succeeded"]; the exact detail field sets; the
        # stub result round-tripped verbatim.
        assert outcome.returncode == 0, observed
        assert [d["phase"] for d in outcome.details] == CLEAN_RUN_PHASES, (
            "clean-run phase sequence changed vs the recorded unfixed "
            "baseline (3.1): " + observed)
        building, succeeded = outcome.details
        assert set(building) == BUILDING_DETAIL_KEYS, (
            "the building detail field set changed vs the baseline "
            "(3.1): " + observed)
        assert building["build_job_id"] == outcome.job_id, observed
        assert building["build_target"] == "JP7", observed
        assert building["source_ref"] == "", observed
        assert set(succeeded) == SUCCEEDED_DETAIL_KEYS, (
            "the succeeded detail field set changed vs the baseline "
            "(3.1): " + observed)
        assert succeeded["build_job_id"] == outcome.job_id, observed
        assert succeeded["build_target"] == "JP7", observed
        assert succeeded["result"] == STUB_RESULT, (
            "the PORTAL_BUILD_RESULT round-trip changed vs the baseline "
            "(3.1): " + observed)

        # The prune no-oped: ZERO deletions of any kind, no aws call
        # (nothing confirmable — only :latest tags), no docker mutation
        # beyond the dangling-only prune.
        assert _rmi_refs(outcome.docker_lines) == set(), (
            "a clean server had images deleted (3.1): " + observed)
        assert "PRUNED " not in outcome.stdout, (
            "a clean run reported a prune action — nothing stale "
            "existed (3.1): " + observed)
        assert outcome.aws_lines == [], (
            "ECR was consulted on a clean server (only :latest tags — "
            "no candidates): " + observed)
        # Measurements are still logged even on the no-op run (2.5).
        _assert_measurements_logged(outcome, observed)

        # RECORDED baseline step ordering, with the prune section between
        # the lock and the building event (its info lines are plain
        # stdout the detail parser ignores).
        positions = [outcome.stdout.find(token) for token in (
            "Acquired build lock",
            "=== DDA pre-build disk prune",
            '"phase":"building"',
            "Starting portal-build.sh aarch64 7",
            "STUB portal-build.sh aarch64 7",
            "PORTAL_BUILD_RESULT ",
            '"phase":"succeeded"')]
        assert all(p >= 0 for p in positions), observed
        assert positions == sorted(positions), (
            "clean-run step ordering changed vs the recorded unfixed "
            "baseline (3.1): " + observed)


# ===========================================================================
# Scenario 4 — ECR-down: zero image deletions of unverifiable candidates,
# loud retention logs, the always-safe steps still run, the build proceeds
# (fail open).
# ===========================================================================

class TestEcrDown:
    """Total ECR outage: retain every candidate loudly, still run the
    always-safe steps, and never block the build.

    # Validates: Requirements 2.1, 2.2, 3.2, 3.3
    """

    def test_ecr_outage_retains_candidates_and_build_proceeds(self):
        # Validates: Requirements 2.1, 2.2, 3.2, 3.3
        outcome = _run_integration(
            avail_kb=ROOMY_AVAIL_KB, image_rows=STALE_IMAGE_ROWS,
            inspect_states=STALE_INSPECT_STATES,
            aws_states={})  # no state files → every describe-images errs
        observed = _observed(outcome)

        # ZERO image deletions: every candidate is unverifiable (3.2).
        assert _rmi_refs(outcome.docker_lines) == set(), (
            "images were deleted while ECR was unreachable — zero "
            "deletions of unverifiable candidates (fail open, 2.1/3.2): "
            + observed)
        for ref in (CONFIRMED_BACKEND_REF, CONFIRMED_FRONTEND_REF):
            assert (f"RETAINED image {ref} reason=ECR_UNVERIFIABLE "
                    "(aws exit 255)") in outcome.stdout, (
                f"missing the loud ECR_UNVERIFIABLE retention for {ref!r}"
                ": " + observed)
        assert (f"RETAINED image {UNPUSHED_BACKEND_REF} "
                "reason=NO_REPODIGEST") in outcome.stdout, observed
        _assert_aws_transcript_read_only(outcome, observed)

        # The always-safe steps (dangling / logs / custom-build) still
        # ran (2.1, 2.5), and the build proceeded normally (fail open).
        _assert_stale_tmp_and_workspace_pruned(outcome, observed)
        _assert_measurements_logged(outcome, observed)
        assert outcome.returncode == 0, (
            "an ECR outage blocked the build — fail open violated: "
            + observed)
        assert [d["phase"] for d in outcome.details] == CLEAN_RUN_PHASES, (
            "the build must proceed normally during an ECR outage: "
            + observed)
        assert outcome.record_lines == ["BUILD aarch64 7"], observed
        _assert_prune_ran_before_the_build(outcome, observed)
