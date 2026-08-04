# Copyright 2025 Amazon Web Services, Inc.
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
"""Defect E preservation property tests (Task 7) for edge-deploy-reliability.

Property 10: Preservation — Cold-start and health-gate semantics unchanged
(Defect E).

**Validates: Requirements 3.10, 3.11, 3.12, 3.13**

Observation-first (observed on the E-UNFIXED tree, i.e. the post-Defect-B
recipes shipped by tasks 1-5): the ``goldens/recipe_*_defect_e_baseline
.golden.json`` fixtures record the FULL parsed structure of each of the
four LocalServer recipe variants — Install, Startup ``SetEnv``, the host
setup script invocations, the health-gated
``up -d --no-build --wait --wait-timeout 600`` Startup line with
``Timeout: 900``, and the Shutdown ``down`` + ``/tmp/.dda.env`` export
(+ ``systemctl stop nvidia-csi-capture`` on the arm variants).

The Defect E fix (task 8) is allowed to make exactly FIVE edits per
variant, applied identically to all four:

  1. add a ``Timeout`` key to the Shutdown block;
  2. add a ``compose_lifecycle.sh wait-empty`` invocation line after the
     Shutdown ``down``;
  3. record ``STARTUP_EPOCH=$(date +%s)`` as a Startup script line;
  4. add the ``--force-recreate`` flag to the Startup compose up line;
  5. add a ``compose_lifecycle.sh verify-fresh`` invocation line after the
     ``--wait`` gate.

``strip_defect_e_edits`` removes exactly those five edits; the result must
be deep-equal to the recorded golden. On the E-unfixed tree none of the
edits exist, so the strip is the identity and the comparison passes
trivially — after the fix, any drift beyond the five intended edits is a
preservation regression (Requirements 3.11, 3.12).

Also recorded from the E-unfixed tree: the sha256 of
``src/docker-compose.yaml`` — Defect E must not touch the compose file at
all (Requirement 3.10; no security-baseline rebaseline is needed for this
defect).

The cold-start helper-contract tests assert the Property 10 no-op
guarantee of ``src/host_scripts/compose_lifecycle.sh`` (``wait-empty``
returns 0 within one poll interval with zero project containers;
``verify-fresh`` returns 0 with nothing to check) using a stubbed
``docker`` on PATH. They are written now and skip-as-absent while the
helper does not exist, binding automatically once task 8.1 lands
(Requirements 3.10, 3.13).

PROPERTY-BASED (Hypothesis, repo convention): the equality-modulo-edits
guarantee is universal — "for any script the fix could have started from,
applying the five Defect E edits and then stripping them recovers the
original byte-identically" — so Hypothesis generates synthetic lifecycle
scripts and random edit placements to pin the strip transform itself.

GOLDEN REGENERATION NOTE: these goldens are separate from (and additive
to) the Defect B ``recipe_*_structure.golden.json`` fixtures owned by
test_config_structure_preservation.py — do NOT overwrite those. The
regeneration entry point applies ``strip_defect_e_edits`` before writing,
so regenerating on a post-fix tree reconstructs the same pre-E baseline.
Regenerate ONLY for intentional, reviewed structure changes:

    python3 test/backend-test/deploy_reliability/test_defect_e_preservation.py --regenerate
"""
import copy
import hashlib
import json
import os
import stat
import subprocess
import time

import pytest
import yaml
from hypothesis import given, settings
from hypothesis import strategies as st

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
GOLDENS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "goldens")

COMPOSE_PATH = os.path.join(REPO_ROOT, "src", "docker-compose.yaml")
HELPER_PATH = os.path.join(REPO_ROOT, "src", "host_scripts",
                           "compose_lifecycle.sh")

RECIPE_VARIANTS = (
    "recipe-arm64-jp6.yaml",
    "recipe-arm64-jp5.yaml",
    "recipe-arm64.yaml",
    "recipe-amd64.yaml",
)

#: Poll interval the compose-lifecycle helper uses between `ps -aq` checks
#: (task 8.1 contract). "Within one poll interval" allows interpreter and
#: fork overhead on top of at most one sleep.
HELPER_POLL_INTERVAL_SECONDS = 2
COLD_START_BUDGET_SECONDS = HELPER_POLL_INTERVAL_SECONDS + 2

FORCE_RECREATE_FLAG = "--force-recreate"


# ---------------------------------------------------------------------------
# The strip transform: remove exactly the five intended Defect E edits.
# ---------------------------------------------------------------------------

def _is_wait_empty_line(line):
    return "compose_lifecycle.sh" in line and "wait-empty" in line


def _is_verify_fresh_line(line):
    return "compose_lifecycle.sh" in line and "verify-fresh" in line


def _is_startup_epoch_assignment(line):
    return line.lstrip().startswith("STARTUP_EPOCH=")


def _strip_script_lines(script, drop_predicates, remove_tokens=()):
    """Remove whole lines matching any predicate and excise tokens from the
    surviving lines, preserving the script's trailing-newline shape."""
    had_trailing_newline = script.endswith("\n")
    lines = script.split("\n")
    if had_trailing_newline:
        lines = lines[:-1]
    kept = []
    for line in lines:
        if any(pred(line) for pred in drop_predicates):
            continue
        for token in remove_tokens:
            line = line.replace(" " + token, "").replace(token, "")
        kept.append(line)
    result = "\n".join(kept)
    if had_trailing_newline:
        result += "\n"
    return result


def strip_defect_e_edits(recipe):
    """The recipe document minus ONLY the five intended Defect E edits.

    Identity on the E-unfixed recipes (none of the edits exist there);
    after the fix, reconstructs the pre-E baseline iff the fix made
    exactly the intended edits.
    """
    stripped = copy.deepcopy(recipe)
    for manifest in stripped.get("Manifests") or []:
        lifecycle = manifest.get("Lifecycle")
        if not isinstance(lifecycle, dict):
            continue
        shutdown = lifecycle.get("Shutdown")
        if isinstance(shutdown, dict):
            shutdown.pop("Timeout", None)  # edit 1
            if isinstance(shutdown.get("Script"), str):
                shutdown["Script"] = _strip_script_lines(
                    shutdown["Script"],
                    drop_predicates=[_is_wait_empty_line])  # edit 2
        startup = lifecycle.get("Startup")
        if isinstance(startup, dict) and isinstance(startup.get("Script"),
                                                    str):
            startup["Script"] = _strip_script_lines(
                startup["Script"],
                drop_predicates=[_is_verify_fresh_line,        # edit 5
                                 _is_startup_epoch_assignment],  # edit 3
                remove_tokens=(FORCE_RECREATE_FLAG,))          # edit 4
    return stripped


# ---------------------------------------------------------------------------
# Shared loading helpers.
# ---------------------------------------------------------------------------

def _load_yaml(path):
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _golden_path(name):
    return os.path.join(GOLDENS_DIR, name)


def _recipe_golden_name(variant):
    return (variant.replace("-", "_").replace(".yaml", "")
            + "_defect_e_baseline.golden.json")


COMPOSE_SHA256_GOLDEN = "docker_compose_defect_e_baseline.sha256.golden.txt"


def _compose_sha256():
    with open(COMPOSE_PATH, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def _manifest_lifecycles(recipe):
    for manifest in recipe.get("Manifests") or []:
        lifecycle = manifest.get("Lifecycle")
        if isinstance(lifecycle, dict):
            yield lifecycle


# ---------------------------------------------------------------------------
# Recipe equality modulo the Defect E edits (golden compare).
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("variant", RECIPE_VARIANTS,
                         ids=[v.replace(".yaml", "")
                              for v in RECIPE_VARIANTS])
def test_recipe_equals_defect_e_baseline_modulo_intended_edits(variant):
    """Property 10: after removing ONLY the five intended Defect E edits
    (Shutdown Timeout, wait-empty line, STARTUP_EPOCH line,
    --force-recreate flag, verify-fresh line), every variant's parsed
    structure equals the golden recorded from the E-unfixed tree — the
    `--wait --wait-timeout 600` health gate, `Timeout: 900` on Startup,
    the `down` command, Install, SetEnv, host setup scripts, `/tmp/.dda.env`
    exports and `systemctl stop nvidia-csi-capture` (arm) all byte-identical.

    **Validates: Requirements 3.11, 3.12**
    """
    golden_path = _golden_path(_recipe_golden_name(variant))
    assert os.path.isfile(golden_path), (
        "golden fixture missing: {} — regenerate with `python3 {} "
        "--regenerate` on a KNOWN-GOOD (pre-Defect-E-fix baseline) tree"
        .format(golden_path, os.path.abspath(__file__)))
    with open(golden_path, encoding="utf-8") as f:
        golden = json.load(f)

    stripped = strip_defect_e_edits(
        _load_yaml(os.path.join(REPO_ROOT, variant)))
    assert stripped == golden, (
        "PRESERVATION REGRESSION (Property 10): {} changed beyond the five "
        "intended Defect E edits (Shutdown Timeout, wait-empty invocation, "
        "STARTUP_EPOCH line, --force-recreate flag, verify-fresh "
        "invocation). Everything else must stay byte-identical to the "
        "recorded pre-E baseline.".format(variant))


def test_defect_e_edits_applied_identically_across_all_variants():
    """Property 10 (Requirement 3.12): whatever Defect E edit state a
    variant is in, all four variants must be in the SAME state — same
    Shutdown Timeout value (or none), same presence of the wait-empty,
    STARTUP_EPOCH, --force-recreate and verify-fresh edits. Passes on the
    E-unfixed tree (no variant has any edit) and after the fix (every
    variant has all five); fails on any partial or divergent application.

    **Validates: Requirements 3.12**
    """
    signatures = {}
    for variant in RECIPE_VARIANTS:
        recipe = _load_yaml(os.path.join(REPO_ROOT, variant))
        for lifecycle in _manifest_lifecycles(recipe):
            shutdown = lifecycle.get("Shutdown") or {}
            startup = lifecycle.get("Startup") or {}
            shutdown_script = shutdown.get("Script") or ""
            startup_script = startup.get("Script") or ""
            signatures[variant] = {
                "shutdown_timeout": shutdown.get("Timeout"),
                "shutdown_waits_for_empty": any(
                    _is_wait_empty_line(l)
                    for l in shutdown_script.split("\n")),
                "startup_records_epoch": any(
                    _is_startup_epoch_assignment(l)
                    for l in startup_script.split("\n")),
                "startup_force_recreates":
                    FORCE_RECREATE_FLAG in startup_script,
                "startup_verifies_freshness": any(
                    _is_verify_fresh_line(l)
                    for l in startup_script.split("\n")),
            }
    baseline_variant = RECIPE_VARIANTS[0]
    for variant, signature in signatures.items():
        assert signature == signatures[baseline_variant], (
            "PRESERVATION REGRESSION (Requirement 3.12): Defect E edits "
            "diverge between variants — {} has {} but {} has {}".format(
                variant, signature, baseline_variant,
                signatures[baseline_variant]))


def test_health_gate_and_down_semantics_present_in_every_variant():
    """Property 10 (Requirement 3.11): the Defect B health-gate semantics
    are never relaxed — every variant's Startup keeps the detached,
    health-gated `up -d --no-build ... --wait --wait-timeout 600` line with
    `Timeout: 900`, and every Shutdown keeps its `docker compose ... down`.

    **Validates: Requirements 3.11**
    """
    for variant in RECIPE_VARIANTS:
        recipe = _load_yaml(os.path.join(REPO_ROOT, variant))
        for lifecycle in _manifest_lifecycles(recipe):
            startup = lifecycle.get("Startup") or {}
            startup_script = startup.get("Script") or ""
            up_lines = [l for l in startup_script.split("\n")
                        if "docker compose" in l and " up " in l]
            assert len(up_lines) == 1, (
                "{}: expected exactly one compose up line in Startup, "
                "found {}".format(variant, len(up_lines)))
            for required in ("up -d", "--no-build", "--wait",
                             "--wait-timeout 600"):
                assert required in up_lines[0], (
                    "PRESERVATION REGRESSION (Requirement 3.11): {} Startup "
                    "compose up line lost '{}': {!r}".format(
                        variant, required, up_lines[0]))
            assert startup.get("Timeout") == 900, (
                "{}: Startup Timeout must stay 900 (was {!r})".format(
                    variant, startup.get("Timeout")))
            shutdown_script = (lifecycle.get("Shutdown") or {}).get(
                "Script") or ""
            down_lines = [l for l in shutdown_script.split("\n")
                          if "docker compose" in l
                          and l.rstrip().endswith("down")]
            assert len(down_lines) == 1, (
                "PRESERVATION REGRESSION (Requirement 3.11): {} Shutdown "
                "must keep exactly one `docker compose ... down` line"
                .format(variant))


# ---------------------------------------------------------------------------
# Compose byte-identity: Defect E must not touch src/docker-compose.yaml.
# ---------------------------------------------------------------------------

def test_docker_compose_is_byte_identical_to_defect_e_baseline():
    """Property 10 (Requirement 3.10): the Defect E fix lives entirely in
    the recipes and the new host_scripts helper; `src/docker-compose.yaml`
    must remain byte-identical (sha256) to the recorded E-unfixed baseline
    before AND after the fix — which is also why no security-baseline
    rebaseline is needed for this defect.

    **Validates: Requirements 3.10**
    """
    golden_path = _golden_path(COMPOSE_SHA256_GOLDEN)
    assert os.path.isfile(golden_path), (
        "golden fixture missing: {} — regenerate with `python3 {} "
        "--regenerate` on a KNOWN-GOOD tree".format(
            golden_path, os.path.abspath(__file__)))
    with open(golden_path, encoding="utf-8") as f:
        golden_sha = f.read().strip()
    actual_sha = _compose_sha256()
    assert actual_sha == golden_sha, (
        "PRESERVATION REGRESSION (Requirement 3.10): src/docker-compose.yaml "
        "changed (sha256 {} != baseline {}). Defect E must not touch the "
        "compose file.".format(actual_sha, golden_sha))


# ---------------------------------------------------------------------------
# Hypothesis: the strip transform exactly inverts the five intended edits.
# ---------------------------------------------------------------------------

#: Script-line alphabet that cannot collide with the Defect E edit markers
#: (no way to spell "compose_lifecycle.sh", "STARTUP_EPOCH=" or
#: "--force-recreate" — '-', '_', '=' and '.' are excluded).
_SAFE_LINE = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz {}$/;",
    min_size=0, max_size=60,
).filter(lambda l: "compose_lifecycle" not in l)

_SAFE_LINES = st.lists(_SAFE_LINE, min_size=1, max_size=8)

_UP_LINE_TEMPLATE = (
    "docker compose --profile $DOCKER_PROFILE -f {path}/docker-compose.yaml "
    "up -d --no-build{force_recreate} --wait --wait-timeout 600 ;")

_ARTIFACT_PATH = st.text(alphabet="abcdefghijklmnopqrstuvwxyz/", min_size=1,
                         max_size=30)


def _script(lines):
    return "\n".join(lines) + "\n"


@settings(max_examples=25, deadline=None)
@given(setup_lines=_SAFE_LINES, path=_ARTIFACT_PATH,
       epoch_position=st.integers(min_value=0, max_value=8))
def test_strip_recovers_any_startup_script_from_its_defect_e_edited_form(
        setup_lines, path, epoch_position):
    """Property 10 metaproperty: for ANY Startup script shape (random setup
    lines, random artifact path), applying the three Startup-side Defect E
    edits (STARTUP_EPOCH line at any position, --force-recreate on the up
    line, trailing verify-fresh line) and then stripping them recovers the
    original script byte-identically — so the golden-equality test above
    tolerates exactly the intended edits and nothing else. Also asserts the
    strip is the identity on unedited scripts (the E-unfixed baseline).

    **Validates: Requirements 3.11, 3.12**
    """
    original_lines = setup_lines + [
        _UP_LINE_TEMPLATE.format(path=path, force_recreate="")]
    original = _script(original_lines)

    edited_lines = list(setup_lines)
    edited_lines.insert(min(epoch_position, len(edited_lines)),
                        "STARTUP_EPOCH=$(date +%s)")
    edited_lines.append(_UP_LINE_TEMPLATE.format(
        path=path, force_recreate=" " + FORCE_RECREATE_FLAG))
    edited_lines.append(
        "bash {}/host_scripts/compose_lifecycle.sh verify-fresh "
        "$STARTUP_EPOCH -- --profile $DOCKER_PROFILE -f "
        "{}/docker-compose.yaml".format(path, path))
    edited = _script(edited_lines)

    strip = lambda s: _strip_script_lines(  # noqa: E731 — mirrors transform
        s,
        drop_predicates=[_is_verify_fresh_line,
                         _is_startup_epoch_assignment],
        remove_tokens=(FORCE_RECREATE_FLAG,))
    assert strip(edited) == original
    assert strip(original) == original  # identity on the pre-E baseline
    assert strip(strip(edited)) == strip(edited)  # idempotent


@settings(max_examples=25, deadline=None)
@given(setup_lines=_SAFE_LINES, path=_ARTIFACT_PATH,
       wait_bound=st.integers(min_value=1, max_value=600))
def test_strip_recovers_any_shutdown_script_from_its_defect_e_edited_form(
        setup_lines, path, wait_bound):
    """Property 10 metaproperty (Shutdown side): for ANY Shutdown script
    shape, appending the wait-empty invocation and stripping it recovers
    the original byte-identically; the `down` line itself is never touched.

    **Validates: Requirements 3.11, 3.12**
    """
    down_line = ("docker compose --profile ${{DOCKER_PROFILE:-tegra}} -f "
                 "{}/docker-compose.yaml down".format(path))
    original = _script(setup_lines + [down_line])
    edited = _script(setup_lines + [down_line] + [
        "bash {}/host_scripts/compose_lifecycle.sh wait-empty {} -- "
        "--profile ${{DOCKER_PROFILE:-tegra}} -f {}/docker-compose.yaml "
        "|| true".format(path, wait_bound, path)])

    strip = lambda s: _strip_script_lines(  # noqa: E731 — mirrors transform
        s, drop_predicates=[_is_wait_empty_line])
    assert strip(edited) == original
    assert strip(original) == original
    assert strip(strip(edited)) == strip(edited)


# ---------------------------------------------------------------------------
# Cold-start no-op helper contract (skip-as-absent until task 8.1 lands).
# ---------------------------------------------------------------------------

_STUB_DOCKER = """#!/bin/sh
# Stubbed docker for the cold-start contract: ZERO project containers.
# `docker compose ... ps -aq` / `ps -q` print nothing; everything exits 0.
exit 0
"""


def _require_helper():
    if not os.path.isfile(HELPER_PATH):
        pytest.skip(
            "src/host_scripts/compose_lifecycle.sh does not exist yet "
            "(created by task 8.1) — the cold-start contract binds once "
            "it lands")


def _run_helper_with_stub_docker(args, tmp_path, timeout=30):
    stub_dir = tmp_path / "stub-bin"
    stub_dir.mkdir(exist_ok=True)
    stub_docker = stub_dir / "docker"
    stub_docker.write_text(_STUB_DOCKER, encoding="utf-8")
    stub_docker.chmod(stub_docker.stat().st_mode | stat.S_IXUSR
                      | stat.S_IXGRP | stat.S_IXOTH)
    env = dict(os.environ)
    env["PATH"] = "{}{}{}".format(stub_dir, os.pathsep, env.get("PATH", ""))
    return subprocess.run(
        ["bash", HELPER_PATH] + args,
        env=env, capture_output=True, text=True, timeout=timeout)


COMPOSE_ARGS = ["--", "--profile", "tegra", "-f",
                "/tmp/does-not-matter/docker-compose.yaml"]


def test_wait_empty_is_an_immediate_noop_with_zero_project_containers(
        tmp_path):
    """Property 10 (Requirements 3.10, 3.13): on a cold start / fast
    teardown (zero project containers), `wait-empty` returns 0 within one
    poll interval — the teardown-wait logic adds no material delay to the
    normal deployment flow.

    **Validates: Requirements 3.10, 3.13**
    """
    _require_helper()
    started = time.monotonic()
    result = _run_helper_with_stub_docker(
        ["wait-empty", "240"] + COMPOSE_ARGS, tmp_path)
    elapsed = time.monotonic() - started
    assert result.returncode == 0, (
        "wait-empty must exit 0 with zero project containers; got rc={} "
        "stdout={!r} stderr={!r}".format(result.returncode, result.stdout,
                                         result.stderr))
    assert elapsed < COLD_START_BUDGET_SECONDS, (
        "wait-empty took {:.1f}s with zero containers — must return within "
        "one poll interval (~{}s)".format(elapsed,
                                          HELPER_POLL_INTERVAL_SECONDS))


def test_verify_fresh_is_a_noop_with_zero_project_containers(tmp_path):
    """Property 10 (Requirements 3.10, 3.13): with zero project containers
    there is nothing to check — `verify-fresh` returns 0 immediately, so
    the freshness gate never blocks the normal cold-start flow.

    **Validates: Requirements 3.10, 3.13**
    """
    _require_helper()
    reference_epoch = str(int(time.time()))
    result = _run_helper_with_stub_docker(
        ["verify-fresh", reference_epoch] + COMPOSE_ARGS, tmp_path)
    assert result.returncode == 0, (
        "verify-fresh must exit 0 with zero project containers; got rc={} "
        "stdout={!r} stderr={!r}".format(result.returncode, result.stdout,
                                         result.stderr))


# ---------------------------------------------------------------------------
# Golden regeneration (intentional, reviewed changes only).
# ---------------------------------------------------------------------------

def _regenerate():
    os.makedirs(GOLDENS_DIR, exist_ok=True)
    for variant in RECIPE_VARIANTS:
        baseline = strip_defect_e_edits(
            _load_yaml(os.path.join(REPO_ROOT, variant)))
        path = _golden_path(_recipe_golden_name(variant))
        with open(path, "w", encoding="utf-8") as f:
            json.dump(baseline, f, indent=2, sort_keys=True,
                      ensure_ascii=True)
            f.write("\n")
        print("wrote {}".format(path))
    sha_path = _golden_path(COMPOSE_SHA256_GOLDEN)
    with open(sha_path, "w", encoding="utf-8") as f:
        f.write(_compose_sha256() + "\n")
    print("wrote {}".format(sha_path))


if __name__ == "__main__":
    import sys
    if "--regenerate" in sys.argv:
        _regenerate()
    else:
        print("usage: python3 {} --regenerate".format(sys.argv[0]))
