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
"""Prune-safety property tests (Task 4.1) for build-server-disk-pruning.

**Property 3: Fix Checking** — Prune Safety (ECR-Confirmed Deletions Only,
Cache Retained).

_For any_ generated local image inventory (arbitrary repositories including
ECR-pattern ``dda/*`` per-version generations, ECR ``:latest`` tags,
unqualified ``flask-app``/``react-webapp``/``edgemlsdk`` lineage, ``<none>``
dangling rows, junk and near-miss repos; arbitrary digests and sizes) × any
generated ECR state (digests present/absent/mismatched, transient ``aws``
errors, total ECR outage), the REAL ``scripts/prune-build-server-disk.sh``
run with stub ``docker``/``aws``/``df`` binaries on PATH SHALL:

* delete (via ``docker rmi <repo>:<tag>``) EXACTLY the safe set — the tags
  satisfying ALL of {ECR-registry ``dda/*`` repository, tag ≠ latest, local
  RepoDigest present for that repository, digest confirmed in ECR} — and
  nothing else, ever;
* never invoke ``rmi -f``, deletion by image ID, ``system prune``,
  ``builder prune``, or ``image prune -a`` (dangling-only ``image prune -f``
  is the single allowed prune subcommand);
* on ANY ECR/docker query failure RETAIN the affected candidates (zero
  deletions of unverifiable images), log the retention reason
  (``NOT_IN_ECR`` / ``NO_REPODIGEST`` / ``ECR_UNVERIFIABLE``), and exit
  without blocking the build;
* treat the unpushed-image case (per-version tag whose push failed → no
  RepoDigest) as structurally undeletable — ECR is not even consulted;
* on ``DDA_PRUNE_DISABLE=1`` exit 0 immediately with zero docker mutations.

# Validates: Requirements 2.1, 2.2, 3.2, 3.3

SAFETY / honesty guard (tasks.md Overview): nothing here runs real docker,
aws, gdk, or touches the build server. The REAL prune script is executed as
a byte-identical sandbox copy (sha256-verified) so its ``REPO_DIR``
resolution — and therefore its ``custom-build/`` workspace removal — stays
inside the sandbox; ``PRUNE_TMP_DIR`` points at a sandboxed empty dir so the
log-pruning step never scans the real ``/tmp``; ``docker``/``aws``/``df``
are recording stubs on PATH.

Helpers are module-local by design (tasks 4.2/4.3 are written concurrently
in this directory — no shared conftest).
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from collections import namedtuple

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

_HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.normpath(os.path.join(_HERE, "..", "..", ".."))
REAL_PRUNE_SCRIPT = os.path.join(REPO_ROOT, "scripts",
                                 "prune-build-server-disk.sh")

# ---------------------------------------------------------------------------
# Stub binaries (the deploy_reliability / csi_nvargus_optional pattern).
#
# The docker stub serves the generated inventory on `image ls`, per-ref
# RepoDigest state files on `image inspect` (first line = exit code, rest =
# digest lines), records every invocation, and succeeds on `rmi` / `image
# prune`. The aws stub resolves per-digest ECR state files (present /
# absent / error — default error, which doubles as the total-outage model
# when no state files are written). The df stub reports a roomy volume so
# the threshold gate (Property 4, task 4.2's surface) never interferes.
# ---------------------------------------------------------------------------

_DOCKER_STUB = """\
#!/usr/bin/env bash
echo "$@" >> "$DOCKER_ARGV_LOG"
if [ "$1" = "rmi" ]; then
    exit 0
fi
case "$1 $2" in
    "image ls")
        if [ "${DOCKER_LS_EXIT:-0}" -ne 0 ]; then
            exit "${DOCKER_LS_EXIT}"
        fi
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

#: Roomy volume: 134217728 KB avail = 128 GB — far above the default
#: BUILD_MIN_FREE_DISK_GB=60, so exit codes here reflect ONLY the pruning
#: surface (Property 3), never the threshold gate (Property 4).
_ROOMY_DF_STUB = """\
#!/usr/bin/env bash
echo "1K-blocks Used Available"
echo "201326592 67108864 134217728"
exit 0
"""

_SANITIZE_RE = re.compile(r"[^A-Za-z0-9._-]")


def _sanitize(ref):
    """Mirror of the docker stub's `tr -c 'A-Za-z0-9._-' '_'` key scheme."""
    return _SANITIZE_RE.sub("_", ref)


def _install_stub(bin_dir, name, content):
    path = os.path.join(bin_dir, name)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(content)
    os.chmod(path, 0o755)


def _sandbox_prune_script(root):
    """A byte-identical copy of the REAL prune script inside a sandbox
    <repo>/scripts/ dir, so REPO_DIR (and the custom-build/ removal, step 4)
    resolves inside the sandbox — never the live checkout."""
    repo = os.path.join(root, "repo")
    os.makedirs(os.path.join(repo, "scripts"))
    copy = os.path.join(repo, "scripts", "prune-build-server-disk.sh")
    shutil.copyfile(REAL_PRUNE_SCRIPT, copy)
    os.chmod(copy, 0o755)
    with open(REAL_PRUNE_SCRIPT, "rb") as real, open(copy, "rb") as sandbox:
        assert (hashlib.sha256(real.read()).hexdigest()
                == hashlib.sha256(sandbox.read()).hexdigest()), \
            "sandbox prune script is not byte-identical to the real script"
    return copy


# ---------------------------------------------------------------------------
# Scenario materialization: generated inventory → docker `image ls` rows,
# per-ref inspect state, per-digest ECR state, and the ORACLE (the exact
# safe set + the exact retention log line owed to every retained candidate).
# ---------------------------------------------------------------------------

Scenario = namedtuple(
    "Scenario",
    ["rows", "inspect_states", "aws_states",
     "expected_deleted", "expected_retained_lines"])

RunResult = namedtuple(
    "RunResult", ["returncode", "stdout", "docker_lines", "aws_lines"])

#: Retention-reason log lines, verbatim from the script.
_REASON_INSPECT_FAIL = ("RETAINED image {ref} reason=NO_REPODIGEST "
                        "(docker image inspect failed)")
_REASON_NO_REPODIGEST = "RETAINED image {ref} reason=NO_REPODIGEST"
_REASON_NOT_IN_ECR = "RETAINED image {ref} reason=NOT_IN_ECR"
_REASON_ECR_UNVERIFIABLE = ("RETAINED image {ref} reason=ECR_UNVERIFIABLE "
                            "(aws exit 255)")

_NOISE_DIGEST_LINE = "docker.io/library/noise@sha256:" + "f" * 64


def _materialize(inventory, ecr_outage=False):
    """Build the stub state and the oracle for a generated inventory.

    ``ecr_outage=True`` models total ECR unreachability: NO aws state files
    are written, so the aws stub's default (exit 255, network error) answers
    every describe-images — every fully-pushed candidate must be RETAINED as
    ECR_UNVERIFIABLE and the deleted set must be empty.
    """
    rows = []
    inspect_states = {}   # ref -> (exit_code, [digest lines])
    aws_states = {}       # digest hex -> present|absent|error
    expected_deleted = set()
    expected_retained_lines = []
    counter = 0
    for entry in inventory:
        rows.append(f"{entry['repo']}\t{entry['tag']}\t{entry['size']}")
        if entry["kind"] != "candidate":
            continue
        repo, tag = entry["repo"], entry["tag"]
        ref = f"{repo}:{tag}"
        digest = "sha256:" + format(counter, "064x")
        counter += 1
        rd_mode = entry["rd_mode"]
        if rd_mode == "inspect-fail":
            # docker image inspect itself errs → fail open, retain.
            inspect_states[ref] = (1, [])
            expected_retained_lines.append(
                (ref, _REASON_INSPECT_FAIL.format(ref=ref)))
            continue
        if rd_mode == "absent":
            # No RepoDigest at all (push never completed) — noise digests
            # for unrelated repos must not be mistaken for one.
            inspect_states[ref] = (0, [_NOISE_DIGEST_LINE])
            expected_retained_lines.append(
                (ref, _REASON_NO_REPODIGEST.format(ref=ref)))
            continue
        if rd_mode == "other-repo":
            # A RepoDigest exists, but for a DIFFERENT repository — not
            # proof this repo's push completed. Structurally undeletable.
            inspect_states[ref] = (0, [f"{repo}-other@{digest}"])
            expected_retained_lines.append(
                (ref, _REASON_NO_REPODIGEST.format(ref=ref)))
            continue
        # rd_mode == "match": a genuine local RepoDigest for this repo.
        inspect_states[ref] = (0, [_NOISE_DIGEST_LINE, f"{repo}@{digest}"])
        if ecr_outage:
            expected_retained_lines.append(
                (ref, _REASON_ECR_UNVERIFIABLE.format(ref=ref)))
            continue
        ecr_mode = entry["ecr_mode"]
        aws_states[digest.split(":", 1)[1]] = ecr_mode
        if ecr_mode == "present":
            # THE safe set: ECR dda/* repo, tag ≠ latest, RepoDigest
            # present, digest confirmed in ECR.
            expected_deleted.add(ref)
        elif ecr_mode == "absent":
            expected_retained_lines.append(
                (ref, _REASON_NOT_IN_ECR.format(ref=ref)))
        else:
            expected_retained_lines.append(
                (ref, _REASON_ECR_UNVERIFIABLE.format(ref=ref)))
    return Scenario(rows, inspect_states, aws_states,
                    expected_deleted, expected_retained_lines)


def _run_prune(scenario, extra_env=None, build_job_id="bj-prop-safety"):
    """Run the REAL prune script (sandbox byte-identical copy) against the
    materialized scenario. Returns RunResult with the recorded stub argv
    transcripts."""
    with tempfile.TemporaryDirectory(prefix="dda-prune-safety-") as tmp:
        script = _sandbox_prune_script(tmp)
        bin_dir = os.path.join(tmp, "bin")
        state_dir = os.path.join(tmp, "state")
        fake_tmp = os.path.join(tmp, "fake-tmp")   # sandboxed /tmp scan root
        for path in (bin_dir, state_dir, fake_tmp):
            os.makedirs(path)
        docker_log = os.path.join(tmp, "docker-argv.log")
        aws_log = os.path.join(tmp, "aws-argv.log")
        _install_stub(bin_dir, "docker", _DOCKER_STUB)
        _install_stub(bin_dir, "aws", _AWS_STUB)
        _install_stub(bin_dir, "df", _ROOMY_DF_STUB)
        for ref, (exit_code, digest_lines) in scenario.inspect_states.items():
            with open(os.path.join(state_dir, f"inspect_{_sanitize(ref)}"),
                      "w", encoding="utf-8") as handle:
                handle.write(str(exit_code) + "\n")
                for line in digest_lines:
                    handle.write(line + "\n")
        for digest_hex, mode in scenario.aws_states.items():
            with open(os.path.join(state_dir, f"aws_{digest_hex}"), "w",
                      encoding="utf-8") as handle:
                handle.write(mode)
        env = {
            "PATH": bin_dir + os.pathsep
            + os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "HOME": tmp,
            "LC_ALL": "C.UTF-8",
            "DOCKER_ARGV_LOG": docker_log,
            "AWS_ARGV_LOG": aws_log,
            "STUB_STATE_DIR": state_dir,
            "DOCKER_IMAGE_LS": "\n".join(scenario.rows),
            "PRUNE_TMP_DIR": fake_tmp,
        }
        if extra_env:
            env.update(extra_env)
        result = subprocess.run(
            ["bash", script, f"BUILD_JOB_ID={build_job_id}"],
            cwd=tmp, env=env, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=120)

        def _lines(path):
            if not os.path.exists(path):
                return []
            with open(path, encoding="utf-8") as handle:
                return handle.read().splitlines()

        return RunResult(result.returncode,
                         result.stdout.decode("utf-8", "replace"),
                         _lines(docker_log), _lines(aws_log))


def _rmi_refs(docker_lines):
    """The deleted set, from the stub docker's recorded `rmi` argv."""
    return {line.split(None, 1)[1] for line in docker_lines
            if line.startswith("rmi ")}


def _observed(scenario, run, **extra):
    payload = {
        "rows": scenario.rows,
        "expected_deleted": sorted(scenario.expected_deleted),
        "expected_retained": [line for _, line
                              in scenario.expected_retained_lines],
        "exit": run.returncode,
        "docker_argv": run.docker_lines,
        "aws_argv": run.aws_lines,
        "stdout": run.stdout,
    }
    payload.update(extra)
    return json.dumps(payload, indent=2)


# ---------------------------------------------------------------------------
# Strategies — the generated local image inventory × ECR state space.
# ---------------------------------------------------------------------------

_ECR_REGISTRIES = st.builds(
    lambda account, region: f"{account}.dkr.ecr.{region}.amazonaws.com",
    st.sampled_from(["123456789012", "999900001111"]),
    st.sampled_from(["us-east-1", "eu-west-2"]))

_VERSIONS = st.builds(
    lambda a, b, plain: f"1.{a}.{b}" + ("" if plain else "-rc1"),
    st.integers(0, 3), st.integers(0, 99), st.booleans())

#: Per-version ECR-repo generations — the ONLY deletion candidates. The
#: RepoDigest axis (match / absent / other-repo / inspect-fail) × the ECR
#: axis (present / absent=digest mismatch / error=transient failure) spans
#: every confirmation outcome of design Decision 2.
_CANDIDATES = st.builds(
    lambda registry, name, version, size, rd_mode, ecr_mode: {
        "kind": "candidate",
        "repo": f"{registry}/dda/{name}",
        "tag": version,
        "size": f"{size}GB",
        "rd_mode": rd_mode,
        "ecr_mode": ecr_mode},
    _ECR_REGISTRIES,
    st.sampled_from(["flask-app", "react-webapp", "custom-model"]),
    _VERSIONS,
    st.integers(1, 42),
    st.sampled_from(["match", "absent", "other-repo", "inspect-fail"]),
    st.sampled_from(["present", "absent", "error"]))

#: Everything that must NEVER be a deletion candidate: ECR :latest, ECR
#: <none>, the unqualified :latest lineage, dangling rows, junk repos, and
#: near-miss repos that almost match the ECR registry pattern.
_PROTECTED = st.one_of(
    st.builds(
        lambda registry, name: {
            "kind": "protected", "repo": f"{registry}/dda/{name}",
            "tag": "latest", "size": "30GB"},
        _ECR_REGISTRIES, st.sampled_from(["flask-app", "react-webapp"])),
    st.builds(
        lambda registry: {
            "kind": "protected", "repo": f"{registry}/dda/flask-app",
            "tag": "<none>", "size": "2GB"},
        _ECR_REGISTRIES),
    st.builds(
        lambda name, tag: {
            "kind": "protected", "repo": name, "tag": tag, "size": "29GB"},
        st.sampled_from(["flask-app", "react-webapp", "edgemlsdk"]),
        st.sampled_from(["latest", "1.0.42"])),
    st.just({"kind": "protected", "repo": "<none>", "tag": "<none>",
             "size": "1GB"}),
    st.builds(
        lambda repo, tag: {
            "kind": "protected", "repo": repo, "tag": tag, "size": "100MB"},
        st.sampled_from([
            "ubuntu", "python", "moby/buildkit",
            # near-miss: no leading digit — pattern must not match
            "x123.dkr.ecr.us-east-1.amazonaws.com/dda/flask-app",
            # near-miss: ECR registry but not the dda/ namespace
            "123456789012.dkr.ecr.us-east-1.amazonaws.com/other/flask-app"]),
        st.sampled_from(["1.0", "latest"]))
)

_INVENTORIES = st.lists(
    st.one_of(_CANDIDATES, _PROTECTED), max_size=8,
    unique_by=lambda entry: (entry["repo"], entry["tag"]))

#: Unpushed-image inventories: every candidate lacks a usable RepoDigest
#: (push never completed) while ECR would even CONFIRM the digest if asked —
#: deletion must be structurally impossible without any ECR consultation.
_UNPUSHED_INVENTORIES = st.lists(
    st.builds(
        lambda registry, name, version, size, rd_mode: {
            "kind": "candidate",
            "repo": f"{registry}/dda/{name}",
            "tag": version,
            "size": f"{size}GB",
            "rd_mode": rd_mode,
            "ecr_mode": "present"},
        _ECR_REGISTRIES,
        st.sampled_from(["flask-app", "react-webapp"]),
        _VERSIONS,
        st.integers(1, 42),
        st.sampled_from(["absent", "other-repo", "inspect-fail"])),
    min_size=1, max_size=6,
    unique_by=lambda entry: (entry["repo"], entry["tag"]))

#: A ref the script is allowed to rmi: <ECR registry>/dda/<name>:<tag> —
#: never a bare image ID, never an unqualified repo.
_DELETABLE_REF_RE = re.compile(
    r"^[0-9][^\s]*\.dkr\.ecr\.[^\s]+\.amazonaws\.com/dda/[^\s:]+:[^\s]+$")


# ---------------------------------------------------------------------------
# Property 3, leg 1 — the deleted set is EXACTLY the safe set, retention is
# always logged with its reason, and the script never blocks the build.
# ---------------------------------------------------------------------------

class TestDeletedSetExactness:
    """For any inventory × ECR state: deleted == safe set, nothing else.

    # Validates: Requirements 2.1, 2.2, 3.2, 3.3
    """

    @settings(deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(inventory=_INVENTORIES)
    def test_deleted_set_equals_exactly_the_safe_set(self, inventory):
        # Validates: Requirements 2.1, 2.2, 3.2, 3.3
        scenario = _materialize(inventory)
        run = _run_prune(scenario)
        deleted = _rmi_refs(run.docker_lines)
        observed = _observed(scenario, run, deleted=sorted(deleted))

        assert deleted == scenario.expected_deleted, (
            "the deleted set is NOT exactly the safe set {ECR dda/* repo, "
            "tag != latest, local RepoDigest present, digest confirmed in "
            "ECR}: " + observed)
        # Roomy volume: pruning must complete without blocking the build,
        # whatever mix of confirmations/failures was generated (fail open).
        assert run.returncode == 0, (
            "the prune script blocked the build (nonzero exit) on a roomy "
            "volume — fail open violated: " + observed)
        stdout_lines = run.stdout.splitlines()
        # Every retained candidate owes a loud retention line, verbatim.
        for ref, line in scenario.expected_retained_lines:
            assert line in stdout_lines, (
                f"missing retention log line for {ref!r} (expected "
                f"{line!r}) — retention must be logged loudly: " + observed)
        # Every deletion owes a PRUNED log line.
        for ref in scenario.expected_deleted:
            assert f"PRUNED image {ref} " in run.stdout, (
                f"missing PRUNED log line for deleted {ref!r}: " + observed)


# ---------------------------------------------------------------------------
# Property 3, leg 2 — the forbidden-subcommand invariant: no rmi -f, no
# deletion by image ID, no system/builder prune, no image prune -a. Ever.
# ---------------------------------------------------------------------------

class TestForbiddenSubcommands:
    """The docker transcript never contains a forbidden mutation.

    # Validates: Requirements 2.2, 3.3
    """

    @settings(deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(inventory=_INVENTORIES)
    def test_transcript_never_contains_forbidden_mutations(self, inventory):
        # Validates: Requirements 2.2, 3.3
        scenario = _materialize(inventory)
        run = _run_prune(scenario)
        observed = _observed(scenario, run)

        for line in run.docker_lines:
            argv = line.split()
            assert argv[:2] != ["system", "prune"], (
                "docker system prune invoked — the BuildKit cache and "
                ":latest lineage carry the 1-2h GPU compile (2.2): "
                + observed)
            assert argv[:2] != ["builder", "prune"], (
                "docker builder prune invoked — the build cache must never "
                "be touched (2.2): " + observed)
            if argv[:2] == ["image", "prune"]:
                assert argv == ["image", "prune", "-f"], (
                    "image prune with flags beyond dangling-only -f "
                    "(e.g. -a) — forbidden (2.2): " + observed)
            if argv[0] == "rmi":
                assert "-f" not in argv, (
                    "docker rmi -f invoked — deletion must never be forced "
                    "(2.2): " + observed)
                assert len(argv) == 2, (
                    "docker rmi with multiple/unexpected arguments: "
                    + observed)
                ref = argv[1]
                assert _DELETABLE_REF_RE.match(ref), (
                    f"docker rmi of {ref!r} — not an ECR-registry dda/* "
                    "repo:tag ref (image-ID or unqualified-repo deletion "
                    "is forbidden, 2.2/3.3): " + observed)
                assert ref in scenario.expected_deleted, (
                    f"docker rmi of {ref!r} which is outside the safe set: "
                    + observed)


# ---------------------------------------------------------------------------
# Property 3, leg 3 — unpushed images are structurally undeletable: no
# usable RepoDigest means no deletion AND no ECR consultation at all, even
# when ECR would confirm the digest if asked.
# ---------------------------------------------------------------------------

class TestUnpushedImagesStructurallyUndeletable:
    """A failed push (no RepoDigest) can never lose the image.

    # Validates: Requirements 2.1, 3.2
    """

    @settings(deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(inventory=_UNPUSHED_INVENTORIES)
    def test_no_repodigest_means_no_deletion_and_no_ecr_query(
            self, inventory):
        # Validates: Requirements 2.1, 3.2
        scenario = _materialize(inventory)
        run = _run_prune(scenario)
        deleted = _rmi_refs(run.docker_lines)
        observed = _observed(scenario, run, deleted=sorted(deleted))

        assert deleted == set(), (
            "an image without a usable RepoDigest (push never completed) "
            "was DELETED — unpushed images must be structurally "
            "undeletable (2.1): " + observed)
        assert run.aws_lines == [], (
            "ECR was consulted for a candidate with no local RepoDigest — "
            "conditions 3-4 of the safety conjunction must be unreachable "
            "without one (Decision 2): " + observed)
        assert run.returncode == 0, (
            "the prune script blocked the build: " + observed)
        stdout_lines = run.stdout.splitlines()
        for ref, line in scenario.expected_retained_lines:
            assert line in stdout_lines, (
                f"missing NO_REPODIGEST retention line for {ref!r}: "
                + observed)


# ---------------------------------------------------------------------------
# Property 3, leg 4 — total ECR outage: zero deletions of unverifiable
# candidates, loud ECR_UNVERIFIABLE retentions, always-safe steps still run,
# and the build is never blocked.
# ---------------------------------------------------------------------------

class TestFailOpenOnEcrOutage:
    """ECR unreachable → retain everything, log loudly, proceed.

    # Validates: Requirements 2.1, 3.2, 3.3
    """

    @settings(deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(inventory=_INVENTORIES)
    def test_total_ecr_outage_retains_everything_and_proceeds(
            self, inventory):
        # Validates: Requirements 2.1, 3.2, 3.3
        scenario = _materialize(inventory, ecr_outage=True)
        run = _run_prune(scenario)
        deleted = _rmi_refs(run.docker_lines)
        observed = _observed(scenario, run, deleted=sorted(deleted))

        assert scenario.expected_deleted == set()  # oracle sanity
        assert deleted == set(), (
            "images were deleted while ECR was unreachable — zero "
            "deletions of unverifiable candidates (fail open, 2.1/3.2): "
            + observed)
        assert run.returncode == 0, (
            "an ECR outage blocked the build — the prune script must fail "
            "open: " + observed)
        stdout_lines = run.stdout.splitlines()
        for ref, line in scenario.expected_retained_lines:
            assert line in stdout_lines, (
                f"missing ECR_UNVERIFIABLE retention line for {ref!r}: "
                + observed)
        # The always-safe dangling prune still ran (dangling-only).
        assert any(line.split() == ["image", "prune", "-f"]
                   for line in run.docker_lines), (
            "the always-safe dangling-image prune did not run during the "
            "ECR outage: " + observed)


# ---------------------------------------------------------------------------
# Property 3, leg 5 — the escape hatch: DDA_PRUNE_DISABLE=1 exits 0
# immediately with ZERO docker/aws activity, for any inventory.
# ---------------------------------------------------------------------------

class TestDisableEscapeHatch:
    """DDA_PRUNE_DISABLE=1 → immediate exit 0, zero docker mutations.

    # Validates: Requirements 2.1, 3.3
    """

    @settings(deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(inventory=_INVENTORIES)
    def test_disable_exits_zero_with_zero_docker_mutations(self, inventory):
        # Validates: Requirements 2.1, 3.3
        scenario = _materialize(inventory)
        run = _run_prune(scenario, extra_env={"DDA_PRUNE_DISABLE": "1"})
        observed = _observed(scenario, run)

        assert run.returncode == 0, (
            "DDA_PRUNE_DISABLE=1 did not exit 0 immediately: " + observed)
        assert run.docker_lines == [], (
            "docker was invoked despite DDA_PRUNE_DISABLE=1 — the escape "
            "hatch must produce zero docker mutations: " + observed)
        assert run.aws_lines == [], (
            "aws was invoked despite DDA_PRUNE_DISABLE=1: " + observed)
        assert "disabled" in run.stdout, (
            "the disable escape hatch did not log that pruning was "
            "skipped: " + observed)


# ---------------------------------------------------------------------------
# Property 3, example leg — a docker-side query failure (image ls errs)
# prunes no generation at all (fail open) and never blocks the build.
# ---------------------------------------------------------------------------

class TestFailOpenOnDockerFailure:
    """docker image ls failure → skip generation pruning, proceed loudly.

    # Validates: Requirements 2.1, 3.3
    """

    def test_docker_image_ls_failure_prunes_nothing(self):
        # Validates: Requirements 2.1, 3.3
        # A fully-confirmable candidate that WOULD be deleted on a healthy
        # docker — the ls failure must retain it.
        inventory = [{
            "kind": "candidate",
            "repo": "123456789012.dkr.ecr.us-east-1.amazonaws.com"
                    "/dda/flask-app",
            "tag": "1.0.7", "size": "42GB",
            "rd_mode": "match", "ecr_mode": "present"}]
        scenario = _materialize(inventory)
        run = _run_prune(scenario, extra_env={"DOCKER_LS_EXIT": "1"})
        deleted = _rmi_refs(run.docker_lines)
        observed = _observed(scenario, run, deleted=sorted(deleted))

        assert deleted == set(), (
            "images were deleted although docker image ls FAILED — any "
            "docker query failure must retain the candidates (fail open): "
            + observed)
        assert run.returncode == 0, (
            "a docker failure blocked the build — fail open violated: "
            + observed)
        assert "WARNING: docker image ls failed" in run.stdout, (
            "the docker-failure skip was not logged loudly: " + observed)
