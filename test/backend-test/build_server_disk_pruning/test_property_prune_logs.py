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
"""Log/workspace-pruning property tests (Task 4.3) for
build-server-disk-pruning.

**Property 5: Fix Checking** — Log/Workspace Pruning Is Pattern-Scoped,
Age-Gated, and Fully Logged.

_For any_ generated ``/tmp`` population (sandboxed via the prune script's
``PRUNE_TMP_DIR`` override; files of arbitrary names — matches of the four
build-log globs, near-misses, unrelated files — and arbitrary mtimes
straddling the retention window), the REAL ``scripts/prune-build-server-
disk.sh`` SHALL:

* delete EXACTLY the files matching the four build-log glob patterns
  (``gdk-build-*.log``, ``gdk-publish-*.log``, ``portal-build-agent-*.log``,
  ``inference-uploader-build-*.log``) whose age exceeds
  ``PRUNE_LOG_RETENTION_DAYS``;
* NEVER delete the current job's log
  (``portal-build-agent-${BUILD_JOB_ID}.log``), even when over-age, and
  NEVER delete any non-matching file;
* emit a log line for every prune action (images, logs, workspaces,
  dangling) including the sizes freed, plus the BEFORE/AFTER free-space
  measurements and the freed total — present even on no-op runs;
* remove ``custom-build/`` leftovers with the size logged FIRST, while
  ``greengrass-build/`` / ``.gdk/`` remain step [3/7]'s job (no reference,
  never touched).

# Validates: Requirements 2.1, 2.5

SAFETY / honesty guard: no test in this file runs real docker, aws, gdk, or
touches the build server or the real /tmp state destructively. Every run
executes a BYTE-IDENTICAL copy of the real prune script planted inside a
throwaway sandbox repo (so its ``REPO_DIR`` resolution — and therefore the
``custom-build/`` removal — targets the sandbox, never the live clone),
with recording stub ``docker``/``aws`` and a roomy stub ``df`` on PATH and
``PRUNE_TMP_DIR`` pointed at a generated sandbox tmp dir.
"""
import fnmatch
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

_HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.normpath(os.path.join(_HERE, "..", "..", ".."))

PRUNE_SCRIPT = os.path.join(REPO_ROOT, "scripts",
                            "prune-build-server-disk.sh")

#: The four /tmp build-log glob patterns from design Decision 5 — the ONLY
#: names the prune script may delete from the tmp scan root.
LOG_PATTERNS = ("gdk-build-*.log", "gdk-publish-*.log",
                "portal-build-agent-*.log", "inference-uploader-build-*.log")

#: The default retention window (days) the script applies when
#: PRUNE_LOG_RETENTION_DAYS is unset.
DEFAULT_RETENTION_DAYS = 7


def _matches_build_log_patterns(name):
    """The oracle: does this basename match any of the four build-log
    globs? (fnmatchcase mirrors find(1)'s -name glob semantics for the
    safe alphabets generated below.)"""
    return any(fnmatch.fnmatchcase(name, pattern)
               for pattern in LOG_PATTERNS)


# ---------------------------------------------------------------------------
# Helpers — module-local by design (tasks 4.1/4.2 are written concurrently
# in this directory; no conftest, no cross-file imports).
# ---------------------------------------------------------------------------

def _install_stub(bin_dir, name, content):
    path = os.path.join(bin_dir, name)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(content)
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP
             | stat.S_IXOTH)


#: aws stub: records argv; describe-images succeeds (never reached with the
#: empty image inventory these legs serve, but present for completeness).
_RECORDING_AWS_STUB = """\
#!/usr/bin/env bash
echo "$@" >> "$AWS_ARGV_LOG"
exit 0
"""

#: docker stub: empty `image ls` inventory (the docker legs belong to task
#: 4.1), canned dangling-prune report, records everything.
_RECORDING_DOCKER_STUB = """\
#!/usr/bin/env bash
echo "$@" >> "$DOCKER_ARGV_LOG"
case "$1 $2" in
    "image ls")      : ;;
    "image inspect") : ;;
    "image prune")   echo "Total reclaimed space: 0B" ;;
esac
exit 0
"""

#: df stub: a roomy volume (128 GB free) so the threshold gate (task 4.2's
#: surface) never fires in these legs — exit 0 is the expected outcome.
_ROOMY_DF_STUB = """\
#!/usr/bin/env bash
echo "1K-blocks Used Available"
echo "201326592 67108864 134217728"
exit 0
"""


def _make_sandbox_repo(tmp):
    """Plant a BYTE-IDENTICAL copy of the real prune script inside a
    throwaway repo dir so the script's own REPO_DIR resolution (and hence
    its custom-build/ removal) targets the sandbox, never the live clone.
    Returns (repo_dir, script_path)."""
    repo = os.path.join(tmp, "DefectDetectionApplication")
    scripts_dir = os.path.join(repo, "scripts")
    os.makedirs(scripts_dir, exist_ok=True)
    script = os.path.join(scripts_dir, "prune-build-server-disk.sh")
    shutil.copyfile(PRUNE_SCRIPT, script)
    os.chmod(script, os.stat(script).st_mode | stat.S_IXUSR)
    with open(PRUNE_SCRIPT, "rb") as real, open(script, "rb") as copy:
        assert (hashlib.sha256(real.read()).hexdigest()
                == hashlib.sha256(copy.read()).hexdigest()), \
            "the sandbox prune script is not byte-identical to the real one"
    return repo, script


def _run_prune(tmp, repo, script, log_dir, build_job_id,
               retention_days=None):
    """Run the sandboxed byte-identical prune script with stub
    docker/aws/df on PATH and PRUNE_TMP_DIR pointed at log_dir. Returns
    the CompletedProcess."""
    bin_dir = os.path.join(tmp, "bin")
    os.makedirs(bin_dir, exist_ok=True)
    _install_stub(bin_dir, "aws", _RECORDING_AWS_STUB)
    _install_stub(bin_dir, "docker", _RECORDING_DOCKER_STUB)
    _install_stub(bin_dir, "df", _ROOMY_DF_STUB)
    env = {
        "PATH": bin_dir + os.pathsep
        + os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": tmp,
        "LC_ALL": "C.UTF-8",
        "AWS_ARGV_LOG": os.path.join(tmp, "aws-argv.log"),
        "DOCKER_ARGV_LOG": os.path.join(tmp, "docker-argv.log"),
        "PRUNE_TMP_DIR": log_dir,
    }
    if retention_days is not None:
        env["PRUNE_LOG_RETENTION_DAYS"] = str(retention_days)
    return subprocess.run(
        ["bash", script, f"BUILD_JOB_ID={build_job_id}"],
        cwd=repo, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=120)


def _plant_file(log_dir, name, age_days):
    """Create log_dir/name with its own name as content (size = len(name)
    bytes, so the size-logging assertion is exact) and an mtime age_days +
    one hour in the past — the one-hour margin keeps find(1)'s whole-day
    -mtime arithmetic unambiguous: the file is over a retention of R days
    IFF age_days > R."""
    path = os.path.join(log_dir, name)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(name)
    stamp = time.time() - (age_days * 86400 + 3600)
    os.utime(path, (stamp, stamp))
    return path


def _assert_measurement_lines(stdout, observed):
    """The always-present logging contract: BEFORE/AFTER free-space
    measurements, the freed total, and one section marker per prune action
    class (images, dangling, workspaces, logs) — even on no-op runs."""
    for token in (
            "Free disk before pruning:",
            "Free disk after pruning:",
            "Total freed:",
            "--- Pruning stale ECR-confirmed image generations ---",
            "--- Pruning dangling images ---",
            "--- Pruning workspace leftovers ---",
            "--- Pruning build logs older than "):
        assert token in stdout, (
            f"missing the always-present log token {token!r} — every prune "
            "action class and the before/after/freed measurements must be "
            "logged on every run (2.5): " + observed)


# ---------------------------------------------------------------------------
# Hypothesis strategies — generated /tmp populations: pattern matches,
# near-misses, unrelated files; ages straddling the retention window.
# ---------------------------------------------------------------------------

_LOG_PREFIXES = ("gdk-build-", "gdk-publish-", "portal-build-agent-",
                 "inference-uploader-build-")

#: Safe filename chunks (no glob metacharacters, no whitespace, cannot
#: start with '-'), so find(1)'s glob and the fnmatch oracle agree exactly.
_SUFFIXES = st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789",
                    min_size=1, max_size=10)

#: Names matching one of the four build-log globs.
_MATCHING_NAMES = st.builds(lambda p, s: f"{p}{s}.log",
                            st.sampled_from(_LOG_PREFIXES), _SUFFIXES)

#: Near-misses: right prefix but wrong extension, prefixed junk, the dash
#: dropped, or a non-.log extension — all must survive whatever their age.
_NEAR_MISS_NAMES = st.one_of(
    st.builds(lambda p, s: f"{p}{s}.log.old",
              st.sampled_from(_LOG_PREFIXES), _SUFFIXES),
    st.builds(lambda p, s: f"x{p}{s}.log",
              st.sampled_from(_LOG_PREFIXES), _SUFFIXES),
    st.builds(lambda p, s: f"{p.rstrip('-')}{s}.log",
              st.sampled_from(_LOG_PREFIXES), _SUFFIXES),
    st.builds(lambda p, s: f"{p}{s}.txt",
              st.sampled_from(_LOG_PREFIXES), _SUFFIXES),
)

#: Entirely unrelated files.
_UNRELATED_NAMES = st.builds(lambda s: f"{s}.dat", _SUFFIXES)

_NAMES = st.one_of(_MATCHING_NAMES, _NEAR_MISS_NAMES, _UNRELATED_NAMES)

#: A generated population: basename -> age in whole days (0..30, straddling
#: every generated retention window).
_AGE_DAYS = st.integers(min_value=0, max_value=30)
_POPULATIONS = st.dictionaries(keys=_NAMES, values=_AGE_DAYS, max_size=10)

#: Retention windows 0..10 days (0 exercised too: only age > 0 days prunes).
_RETENTIONS = st.integers(min_value=0, max_value=10)

_JOB_IDS = st.builds(
    lambda a, b: f"bj-{a}-{b}",
    st.text(alphabet="0123456789abcdef", min_size=4, max_size=12),
    st.integers(0, 999))


# ---------------------------------------------------------------------------
# Leg 1 — the exact-set property: delete EXACTLY the over-age pattern
# matches, never the current job's log, never any non-matching file; every
# deletion logged with its size; the measurement lines always present.
# ---------------------------------------------------------------------------

class TestLogPruningExactSet:
    """Property 5 — pattern-scoped, age-gated, current-job-immune, and
    fully logged, for any generated /tmp population.

    # Validates: Requirements 2.1, 2.5
    """

    @settings(deadline=None,
              suppress_health_check=[HealthCheck.too_slow])
    @given(population=_POPULATIONS, retention=_RETENTIONS,
           job_id=_JOB_IDS, current_age_days=_AGE_DAYS)
    def test_prunes_exactly_the_over_age_pattern_matches(
            self, population, retention, job_id, current_age_days):
        # Validates: Requirements 2.1, 2.5
        current_log_name = f"portal-build-agent-{job_id}.log"
        population = dict(population)
        population.pop(current_log_name, None)  # planted separately below

        with tempfile.TemporaryDirectory(prefix="dda-prune-logs-") as tmp:
            log_dir = os.path.join(tmp, "fake-tmp")
            os.makedirs(log_dir)
            for name, age in population.items():
                _plant_file(log_dir, name, age)
            _plant_file(log_dir, current_log_name, current_age_days)

            repo, script = _make_sandbox_repo(tmp)
            result = _run_prune(tmp, repo, script, log_dir, job_id,
                                retention_days=retention)
            survivors = set(os.listdir(log_dir))

        stdout = result.stdout.decode("utf-8", "replace")
        expected_deleted = {
            name for name, age in population.items()
            if _matches_build_log_patterns(name) and age > retention}
        deleted = (set(population) | {current_log_name}) - survivors
        observed = json.dumps({
            "population": population, "retention": retention,
            "job_id": job_id, "current_age_days": current_age_days,
            "expected_deleted": sorted(expected_deleted),
            "deleted": sorted(deleted), "exit": result.returncode,
            "stdout": stdout}, indent=2)

        assert result.returncode == 0, (
            "the prune script did not exit 0 on a roomy volume: "
            + observed)
        # The current job's log is IMMUNE, whatever its age (2.1 / 3.3).
        assert current_log_name in survivors, (
            "the prune script DELETED the current job's log "
            f"({current_log_name}) — it must never be pruned, even "
            "over-age: " + observed)
        # Exactly the over-age matches of the four patterns — nothing else.
        assert deleted == expected_deleted, (
            "the deleted set is not EXACTLY the over-age matches of the "
            "four build-log patterns (2.1): " + observed)

        # Every deletion logged with the size freed (content = name, so
        # the byte count is exact).
        for name in sorted(expected_deleted):
            expected_line = (f"PRUNED log {os.path.join(log_dir, name)} "
                             f"({len(name.encode('utf-8'))} bytes)")
            assert expected_line in stdout, (
                f"missing the per-deletion log line {expected_line!r} — "
                "every log prune action must be logged with its size "
                "(2.5): " + observed)
        # The over-age current-job log surfaces as a logged RETENTION.
        if current_age_days > retention:
            assert re.search(
                r"RETAINED log .*" + re.escape(current_log_name)
                + r" \(current job's log", stdout), (
                "the over-age current-job log was not logged as retained "
                "(2.5): " + observed)
        # The retention window in force is logged on the section marker.
        assert (f"--- Pruning build logs older than {retention} days in "
                f"{log_dir} ---") in stdout, (
            "the log-pruning section marker does not state the retention "
            "window and scan root in force (2.5): " + observed)
        _assert_measurement_lines(stdout, observed)


# ---------------------------------------------------------------------------
# Leg 2 — the logging contract on a NO-OP run: zero deletions still log the
# measurements, the freed total, and every prune-action section.
# ---------------------------------------------------------------------------

class TestNoOpRunLogging:
    """Zero-deletion runs still log the BEFORE/AFTER measurements, the
    freed total, and one section per prune action class.

    # Validates: Requirements 2.5
    """

    def test_noop_run_logs_measurements_and_every_section(self):
        # Validates: Requirements 2.5
        with tempfile.TemporaryDirectory(prefix="dda-prune-noop-") as tmp:
            log_dir = os.path.join(tmp, "fake-tmp")
            os.makedirs(log_dir)  # empty population, no custom-build/
            repo, script = _make_sandbox_repo(tmp)
            result = _run_prune(tmp, repo, script, log_dir, "bj-noop")

        stdout = result.stdout.decode("utf-8", "replace")
        observed = json.dumps({"exit": result.returncode,
                               "stdout": stdout}, indent=2)
        assert result.returncode == 0, observed
        _assert_measurement_lines(stdout, observed)
        # The dangling prune's reclaimed-space report is echoed verbatim.
        assert "Total reclaimed space" in stdout, (
            "the dangling-image prune report was not logged (2.5): "
            + observed)
        # No-op means no-op: nothing was pruned.
        assert "PRUNED " not in stdout, (
            "a no-op run reported a prune action — nothing stale existed: "
            + observed)
        assert re.search(r"No workspace leftovers at .*custom-build/",
                         stdout), (
            "the workspace no-op was not logged (2.5): " + observed)


# ---------------------------------------------------------------------------
# Leg 3 — scan-depth pin: the log scan is -maxdepth 1; an over-age matching
# file in a SUBDIRECTORY of the scan root must survive.
# ---------------------------------------------------------------------------

class TestScanDepth:
    """Only the scan root's own entries are candidates — nested files are
    non-matching by construction (find -maxdepth 1).

    # Validates: Requirements 2.1
    """

    def test_nested_matching_file_survives(self):
        # Validates: Requirements 2.1
        with tempfile.TemporaryDirectory(prefix="dda-prune-depth-") as tmp:
            log_dir = os.path.join(tmp, "fake-tmp")
            nested_dir = os.path.join(log_dir, "nested")
            os.makedirs(nested_dir)
            # Top-level over-age match: MUST be pruned (proves the run
            # actually pruned something).
            top = _plant_file(log_dir, "gdk-build-old.log", 20)
            # Nested over-age match: MUST survive.
            nested = _plant_file(nested_dir, "gdk-build-nested.log", 20)

            repo, script = _make_sandbox_repo(tmp)
            result = _run_prune(tmp, repo, script, log_dir, "bj-depth")
            top_survived = os.path.exists(top)
            nested_survived = os.path.exists(nested)

        observed = json.dumps({
            "exit": result.returncode, "top_survived": top_survived,
            "nested_survived": nested_survived,
            "stdout": result.stdout.decode("utf-8", "replace")}, indent=2)
        assert result.returncode == 0, observed
        assert not top_survived, (
            "the over-age top-level match was not pruned — the scenario "
            "did not exercise the scan: " + observed)
        assert nested_survived, (
            "the prune script deleted a file BELOW the scan root — the "
            "log scan must be -maxdepth 1 (2.1): " + observed)


# ---------------------------------------------------------------------------
# Leg 4 — workspace leftovers: custom-build/ is removed with its size
# logged FIRST; greengrass-build/ and .gdk/ are NOT the prune script's job.
# ---------------------------------------------------------------------------

class TestWorkspacePruning:
    """custom-build/ removal is size-logged-first; greengrass-build/ and
    .gdk/ remain portal-build.sh step [3/7]'s job — never referenced,
    never touched.

    # Validates: Requirements 2.1, 2.5
    """

    def test_custom_build_removed_with_size_logged_first(self):
        # Validates: Requirements 2.1, 2.5
        with tempfile.TemporaryDirectory(prefix="dda-prune-ws-") as tmp:
            log_dir = os.path.join(tmp, "fake-tmp")
            os.makedirs(log_dir)
            repo, script = _make_sandbox_repo(tmp)
            workspace = os.path.join(repo, "custom-build")
            os.makedirs(os.path.join(workspace, "staging"))
            with open(os.path.join(workspace, "staging", "leftover.tar"),
                      "wb") as handle:
                handle.write(b"\0" * (1 << 20))  # 1 MiB so du reports a size

            result = _run_prune(tmp, repo, script, log_dir, "bj-ws")
            workspace_survived = os.path.exists(workspace)

        stdout = result.stdout.decode("utf-8", "replace")
        observed = json.dumps({
            "exit": result.returncode,
            "workspace_survived": workspace_survived,
            "stdout": stdout}, indent=2)
        assert result.returncode == 0, observed
        assert not workspace_survived, (
            "custom-build/ leftovers were not removed (2.1): " + observed)
        removing_match = re.search(
            r"Removing stale workspace .*custom-build/ \(size (\S+)\)",
            stdout)
        pruned_match = re.search(
            r"PRUNED workspace .*custom-build/ \((\S+)\)", stdout)
        assert removing_match, (
            "the size-first removal announcement is missing (2.5): "
            + observed)
        assert pruned_match, (
            "the workspace PRUNED line with the size freed is missing "
            "(2.5): " + observed)
        assert removing_match.start() < pruned_match.start(), (
            "the workspace size must be logged BEFORE the removal (2.5): "
            + observed)
        assert removing_match.group(1) != "unknown", (
            "the workspace size was logged as unknown despite real "
            "content (2.5): " + observed)

    def test_greengrass_build_and_gdk_are_not_the_prune_scripts_job(self):
        # Validates: Requirements 2.1
        # Structural pin: no reference anywhere in the script.
        with open(PRUNE_SCRIPT, encoding="utf-8") as handle:
            text = handle.read()
        assert "greengrass-build" not in text, (
            "the prune script references greengrass-build/ — that cleanup "
            "is portal-build.sh step [3/7]'s job, not the prune's (2.1)")
        assert ".gdk" not in text, (
            "the prune script references .gdk/ — that cleanup is "
            "portal-build.sh step [3/7]'s job, not the prune's (2.1)")

        # Behavioral pin: both survive a run that DOES prune custom-build/.
        with tempfile.TemporaryDirectory(prefix="dda-prune-gg-") as tmp:
            log_dir = os.path.join(tmp, "fake-tmp")
            os.makedirs(log_dir)
            repo, script = _make_sandbox_repo(tmp)
            os.makedirs(os.path.join(repo, "custom-build"))
            gg_dir = os.path.join(repo, "greengrass-build")
            gdk_dir = os.path.join(repo, ".gdk")
            os.makedirs(gg_dir)
            os.makedirs(gdk_dir)
            with open(os.path.join(gg_dir, "artifact.zip"), "w",
                      encoding="utf-8") as handle:
                handle.write("artifact")
            with open(os.path.join(gdk_dir, "config"), "w",
                      encoding="utf-8") as handle:
                handle.write("config")

            result = _run_prune(tmp, repo, script, log_dir, "bj-gg")
            custom_survived = os.path.exists(
                os.path.join(repo, "custom-build"))
            gg_survived = os.path.exists(
                os.path.join(gg_dir, "artifact.zip"))
            gdk_survived = os.path.exists(os.path.join(gdk_dir, "config"))

        observed = json.dumps({
            "exit": result.returncode, "custom_survived": custom_survived,
            "gg_survived": gg_survived, "gdk_survived": gdk_survived,
            "stdout": result.stdout.decode("utf-8", "replace")}, indent=2)
        assert result.returncode == 0, observed
        assert not custom_survived, (
            "custom-build/ was not pruned — the scenario did not exercise "
            "the workspace step: " + observed)
        assert gg_survived and gdk_survived, (
            "the prune script touched greengrass-build/ or .gdk/ — those "
            "remain step [3/7]'s job (2.1): " + observed)


# ---------------------------------------------------------------------------
# Leg 5 — default-retention pin: with PRUNE_LOG_RETENTION_DAYS unset the
# window is 7 days (an 8-day-old match is pruned; a 7-day-old one is not).
# ---------------------------------------------------------------------------

class TestDefaultRetention:
    """The unset-knob default retention is 7 days.

    # Validates: Requirements 2.1
    """

    def test_unset_retention_defaults_to_seven_days(self):
        # Validates: Requirements 2.1
        with tempfile.TemporaryDirectory(prefix="dda-prune-def-") as tmp:
            log_dir = os.path.join(tmp, "fake-tmp")
            os.makedirs(log_dir)
            over = _plant_file(log_dir, "gdk-build-overage.log", 8)
            under = _plant_file(log_dir, "gdk-build-recent.log", 7)

            repo, script = _make_sandbox_repo(tmp)
            # retention_days=None → PRUNE_LOG_RETENTION_DAYS unset.
            result = _run_prune(tmp, repo, script, log_dir, "bj-default",
                                retention_days=None)
            over_survived = os.path.exists(over)
            under_survived = os.path.exists(under)

        stdout = result.stdout.decode("utf-8", "replace")
        observed = json.dumps({
            "exit": result.returncode, "over_survived": over_survived,
            "under_survived": under_survived, "stdout": stdout}, indent=2)
        assert result.returncode == 0, observed
        assert (f"--- Pruning build logs older than "
                f"{DEFAULT_RETENTION_DAYS} days in ") in stdout, (
            "the unset-knob retention default is not 7 days (2.1): "
            + observed)
        assert not over_survived, (
            "an 8-day-old build log survived the default 7-day window "
            "(2.1): " + observed)
        assert under_survived, (
            "a 7-day-old build log was pruned inside the default 7-day "
            "window (2.1): " + observed)
