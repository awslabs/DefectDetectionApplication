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
"""Bug-condition exploration tests (Task 6, cases 6 and 7) for
edge-deploy-reliability Defect E.

Property 9: Bug Condition — Startup never trusts a previous incarnation's
container (Defect E, `isBugCondition_E`).

**These tests assert the FIXED (post-fix) lifecycle, so they are EXPECTED TO
FAIL on the E-unfixed tree.** The failures are the counterexamples confirming
the defect verified on device ryan-orin-nano (v1.0.46): after a reboot +
nucleus restart, Shutdown's `docker compose down` exited while the backend was
still in its ~24s post-SIGKILL dying window (the Shutdown block declares no
Timeout, so Greengrass's 15s default truncates any wait); Startup's
`up -d --wait` then ADOPTED the dying container, the `--wait` gate trusted its
stale pre-kill 'healthy' healthcheck state, Startup exited 0, and the
container finished dying 3 seconds later — no backend container at all behind
a RUNNING component.

The SAME tests are re-run in task 8.3 against the fixed tree, where they must
PASS: every variant's Shutdown declares a Timeout and waits (bounded) for zero
project containers via `compose_lifecycle.sh wait-empty`; every Startup
records `STARTUP_EPOCH`, force-recreates (never adopts), and gates RUNNING on
`compose_lifecycle.sh verify-fresh`.

Config tests as the testable seam (per the design): case 6 parses the four
recipe variants and asserts the teardown-race-critical lifecycle shape; case 7
exercises the compose-lifecycle helper contract with a stubbed `docker` on
PATH simulating the incident's dying window.

Validates: Requirements 1.10, 1.11, 1.12, 1.13
"""
import os
import re
import stat
import subprocess
import textwrap
import time

import pytest
import yaml

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

#: All four LocalServer recipe variants must receive the Defect E edits
#: identically (Requirement 3.12 keeps them in sync).
RECIPE_VARIANTS = (
    "recipe-arm64-jp6.yaml",
    "recipe-arm64-jp5.yaml",
    "recipe-arm64.yaml",
    "recipe-amd64.yaml",
)

#: The compose-lifecycle helper (design Fix Implementation §5) — does not
#: exist on the E-unfixed tree.
HELPER_PATH = os.path.join(
    REPO_ROOT, "src", "host_scripts", "compose_lifecycle.sh")


def _load_recipe(recipe_name):
    path = os.path.join(REPO_ROOT, recipe_name)
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _lifecycles(recipe, recipe_name):
    manifests = recipe.get("Manifests") or []
    assert manifests, "{}: recipe declares no Manifests".format(recipe_name)
    return [(i, m.get("Lifecycle") or {}) for i, m in enumerate(manifests)]


def _script_commands(script):
    """Every logical command of a lifecycle script (lines split on ';')."""
    commands = []
    for raw_line in script.splitlines():
        for part in raw_line.split(";"):
            part = part.strip()
            if part:
                commands.append(part)
    return commands


def _first_index(commands, pattern):
    for i, command in enumerate(commands):
        if re.search(pattern, command):
            return i
    return None


# ---------------------------------------------------------------------------
# Exploration case 6 — teardown-race lifecycle exposure (isBugCondition_E
# structurally): the unfixed recipes run a bare asynchronous `down` (no
# Shutdown Timeout, no post-down wait) and an adoption-permitting `up` (no
# --force-recreate, no freshness gate).
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("recipe_name", RECIPE_VARIANTS)
def test_shutdown_declares_timeout_and_waits_for_zero_containers(recipe_name):
    """isBugCondition_E (Shutdown side): the unfixed Shutdown exits after a
    bare `docker compose ... down` while a slow-dying container from this
    incarnation may still exist, and declares no Timeout — so Greengrass's
    15-second default would truncate any wait it attempted. The fixed
    Shutdown declares a Timeout sized above the wait-empty bound and invokes
    `compose_lifecycle.sh wait-empty` AFTER the down, so Shutdown does not
    exit until the project reports zero containers (or the bounded wait
    elapses).

    Validates: Requirements 1.10
    """
    recipe = _load_recipe(recipe_name)
    for index, lifecycle in _lifecycles(recipe, recipe_name):
        assert "Shutdown" in lifecycle, (
            "{} manifest {} has no Shutdown block".format(recipe_name, index))
        shutdown = lifecycle["Shutdown"]
        script = shutdown.get("Script") or ""
        commands = _script_commands(script)

        down_index = _first_index(commands, r"docker\s+compose\b.*\bdown\b")
        assert down_index is not None, (
            "{} manifest {} Shutdown runs no `docker compose ... down`"
            .format(recipe_name, index))

        wait_index = _first_index(
            commands, r"compose_lifecycle\.sh\s+wait-empty\b")
        assert wait_index is not None, (
            "COUNTEREXAMPLE (Defect E): {} manifest {} Shutdown exits after "
            "a bare `docker compose down` — no `compose_lifecycle.sh "
            "wait-empty` after the down, so Shutdown returns while a "
            "slow-dying container (backend: ~24s post-SIGKILL GPU/Triton "
            "teardown) still exists, and Startup can race it"
            .format(recipe_name, index))
        assert wait_index > down_index, (
            "COUNTEREXAMPLE (Defect E): {} manifest {} invokes wait-empty "
            "BEFORE the compose down line — the wait must follow the down"
            .format(recipe_name, index))

        wait_match = re.search(
            r"compose_lifecycle\.sh\s+wait-empty\s+(\d+)\b",
            commands[wait_index])
        assert wait_match, (
            "{} manifest {}: wait-empty invocation carries no numeric "
            "timeout bound: {!r}".format(recipe_name, index,
                                         commands[wait_index]))
        wait_bound = int(wait_match.group(1))

        timeout = shutdown.get("Timeout")
        assert timeout, (
            "COUNTEREXAMPLE (Defect E): {} manifest {} Shutdown declares no "
            "Timeout — Greengrass's 15s default truncates any teardown "
            "wait, so Shutdown cannot outlast the backend's ~24s dying "
            "window".format(recipe_name, index))
        assert int(timeout) >= wait_bound, (
            "COUNTEREXAMPLE (Defect E): {} manifest {} Shutdown Timeout "
            "({}) is below the wait-empty bound ({}) — Greengrass would "
            "truncate the wait".format(recipe_name, index, timeout,
                                       wait_bound))


@pytest.mark.parametrize("recipe_name", RECIPE_VARIANTS)
def test_startup_force_recreates_instead_of_adopting(recipe_name):
    """isBugCondition_E (adoption): the unfixed Startup's
    `up -d --no-build --wait` ADOPTS an existing project container that
    matches the service config — on the device it adopted the still-dying
    backend and the `--wait` gate trusted its stale pre-kill 'healthy'
    state. The fixed compose up line carries `--force-recreate`, so every
    pre-existing container (running, dying, or stopped) is recreated and the
    health gate never evaluates a previous incarnation's container.

    Validates: Requirements 1.11, 1.12
    """
    recipe = _load_recipe(recipe_name)
    for index, lifecycle in _lifecycles(recipe, recipe_name):
        assert "Startup" in lifecycle, (
            "{} manifest {} has no Startup block".format(recipe_name, index))
        script = lifecycle["Startup"].get("Script") or ""
        commands = _script_commands(script)

        up_indexes = [i for i, c in enumerate(commands)
                      if re.search(r"docker\s+compose\b.*\bup\b", c)]
        assert up_indexes, (
            "{} manifest {} Startup runs no `docker compose ... up`"
            .format(recipe_name, index))
        for i in up_indexes:
            assert "--force-recreate" in commands[i], (
                "COUNTEREXAMPLE (Defect E): {} manifest {} compose up "
                "permits adoption (no --force-recreate): {!r} — a "
                "still-dying previous-incarnation container is adopted as "
                "an existing running service and its stale 'healthy' "
                "healthcheck state satisfies --wait"
                .format(recipe_name, index, commands[i]))


@pytest.mark.parametrize("recipe_name", RECIPE_VARIANTS)
def test_startup_gates_running_on_container_freshness(recipe_name):
    """isBugCondition_E (freshness): the unfixed Startup reports RUNNING
    (exits 0) without ever verifying that the containers the health gate
    evaluated were created by THIS Startup invocation — on the device the
    adopted backend was destroyed 3s after Startup exited 0, leaving no
    backend behind a RUNNING component. The fixed Startup records
    STARTUP_EPOCH as its first action and invokes `compose_lifecycle.sh
    verify-fresh $STARTUP_EPOCH` AFTER the `--wait` gate (not best-effort),
    so a stale container fails Startup non-zero and Greengrass retries.

    Validates: Requirements 1.12, 1.13
    """
    recipe = _load_recipe(recipe_name)
    for index, lifecycle in _lifecycles(recipe, recipe_name):
        assert "Startup" in lifecycle, (
            "{} manifest {} has no Startup block".format(recipe_name, index))
        script = lifecycle["Startup"].get("Script") or ""
        commands = _script_commands(script)

        epoch_index = _first_index(
            commands, r"STARTUP_EPOCH=\$\(date\s+\+%s\)")
        assert epoch_index is not None, (
            "COUNTEREXAMPLE (Defect E): {} manifest {} Startup never "
            "records STARTUP_EPOCH — there is no reference time against "
            "which container freshness could be verified"
            .format(recipe_name, index))

        up_index = _first_index(commands, r"docker\s+compose\b.*\bup\b")
        assert up_index is not None, (
            "{} manifest {} Startup runs no `docker compose ... up`"
            .format(recipe_name, index))
        assert epoch_index < up_index, (
            "{} manifest {}: STARTUP_EPOCH must be recorded before the "
            "compose up so it lower-bounds every fresh container's "
            "StartedAt".format(recipe_name, index))

        fresh_index = _first_index(
            commands, r"compose_lifecycle\.sh\s+verify-fresh\b")
        assert fresh_index is not None, (
            "COUNTEREXAMPLE (Defect E): {} manifest {} Startup exits 0 "
            "straight after the --wait gate — no `compose_lifecycle.sh "
            "verify-fresh`, so RUNNING can be reported over an adopted "
            "previous-incarnation container that dies moments later"
            .format(recipe_name, index))
        assert fresh_index > up_index, (
            "{} manifest {}: verify-fresh must run AFTER the health-gated "
            "compose up so it evaluates the final set of started containers"
            .format(recipe_name, index))
        assert re.search(r"verify-fresh\s+\$STARTUP_EPOCH\b",
                         commands[fresh_index]), (
            "{} manifest {}: verify-fresh is not anchored to "
            "$STARTUP_EPOCH: {!r}".format(recipe_name, index,
                                          commands[fresh_index]))
        assert "|| true" not in commands[fresh_index], (
            "COUNTEREXAMPLE (Defect E): {} manifest {} verify-fresh is "
            "best-effort ({!r}) — a stale container must fail Startup "
            "non-zero so Greengrass retries instead of reporting RUNNING"
            .format(recipe_name, index, commands[fresh_index]))


# ---------------------------------------------------------------------------
# Exploration case 7 — missing helper exposure (isBugCondition_E
# behaviorally): the compose-lifecycle helper does not exist on the E-unfixed
# tree. The behavior tests stub `docker` on PATH to simulate the incident's
# dying window (`compose ps -aq` non-empty for several polls before emptying;
# `inspect` reporting a StartedAt older than the reference epoch).
# ---------------------------------------------------------------------------

#: Container ID standing in for the incident's dying backend container.
DYING_CONTAINER_ID = "cafebabe1234"

_STUB_DOCKER_TEMPLATE = textwrap.dedent("""\
    #!/usr/bin/env bash
    # Stubbed docker CLI simulating the ryan-orin-nano incident's dying
    # window: `compose ... ps` reports the dying backend container for the
    # first {polls_before_empty} polls, then reports the project empty.
    # `inspect` reports the (stale) StartedAt of the previous incarnation.
    state_dir="{state_dir}"
    if [ "${{1:-}}" = "compose" ]; then
        for arg in "$@"; do
            if [ "$arg" = "ps" ]; then
                count_file="$state_dir/ps_calls"
                n=0
                [ -f "$count_file" ] && n=$(cat "$count_file")
                n=$((n + 1))
                echo "$n" > "$count_file"
                if [ "$n" -le {polls_before_empty} ]; then
                    echo "{container_id}"
                fi
                exit 0
            fi
        done
        exit 0
    fi
    if [ "${{1:-}}" = "inspect" ]; then
        echo "{started_at}"
        exit 0
    fi
    exit 0
    """)


def _make_stub_docker(tmp_path, polls_before_empty, started_at=""):
    """Install a stubbed `docker` in tmp_path/bin and return an env whose
    PATH resolves it first."""
    bin_dir = tmp_path / "bin"
    state_dir = tmp_path / "state"
    bin_dir.mkdir()
    state_dir.mkdir()
    stub = bin_dir / "docker"
    stub.write_text(_STUB_DOCKER_TEMPLATE.format(
        state_dir=str(state_dir),
        polls_before_empty=polls_before_empty,
        container_id=DYING_CONTAINER_ID,
        started_at=started_at,
    ))
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP
               | stat.S_IXOTH)
    env = dict(os.environ)
    env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")
    return env, state_dir


def _ps_call_count(state_dir):
    count_file = state_dir / "ps_calls"
    return int(count_file.read_text()) if count_file.exists() else 0


def _assert_helper_exists():
    assert os.path.isfile(HELPER_PATH), (
        "COUNTEREXAMPLE (Defect E): src/host_scripts/compose_lifecycle.sh "
        "does not exist — no Shutdown can wait for zero project containers "
        "and no Startup can verify container freshness; the lifecycle has "
        "no defense against adopting a previous incarnation's dying "
        "container")


def test_compose_lifecycle_helper_exists():
    """isBugCondition_E (structural): the helper the fixed Shutdown/Startup
    invoke is absent from the E-unfixed tree.

    Validates: Requirements 1.10, 1.11, 1.12, 1.13
    """
    _assert_helper_exists()


def test_wait_empty_blocks_through_dying_window_then_exits_zero(tmp_path):
    """isBugCondition_E (Shutdown side, behaviorally): during the incident,
    Shutdown returned while `docker compose ps -aq` still reported the dying
    backend. `wait-empty` must instead block — polling until the stubbed
    project empties (3 non-empty polls simulating the ~24s dying window) —
    and only then exit 0.

    Validates: Requirements 1.10
    """
    _assert_helper_exists()
    env, state_dir = _make_stub_docker(tmp_path, polls_before_empty=3)
    started = time.monotonic()
    result = subprocess.run(
        ["bash", HELPER_PATH, "wait-empty", "30", "--",
         "--profile", "tegra", "-f", "/tmp/does-not-matter.yaml"],
        env=env, capture_output=True, text=True, timeout=120)
    elapsed = time.monotonic() - started

    assert result.returncode == 0, (
        "wait-empty exited {} (stderr: {!r}) though the stubbed project "
        "emptied within the bound".format(result.returncode, result.stderr))
    calls = _ps_call_count(state_dir)
    assert calls >= 4, (
        "wait-empty exited 0 after only {} poll(s) while `compose ps -aq` "
        "still reported container {} — it did not wait through the dying "
        "window".format(calls, DYING_CONTAINER_ID))
    assert elapsed >= 2, (
        "wait-empty returned in {:.2f}s — it cannot have polled through "
        "the simulated dying window".format(elapsed))


def test_wait_empty_times_out_nonzero_when_containers_never_clear(tmp_path):
    """isBugCondition_E (bounded wait): a project that never empties must
    end the wait non-zero at the bound (never exit 0 with containers
    remaining), so the caller can see teardown did not complete.

    Validates: Requirements 1.10
    """
    _assert_helper_exists()
    env, _ = _make_stub_docker(tmp_path, polls_before_empty=10_000)
    result = subprocess.run(
        ["bash", HELPER_PATH, "wait-empty", "4", "--",
         "--profile", "tegra", "-f", "/tmp/does-not-matter.yaml"],
        env=env, capture_output=True, text=True, timeout=120)
    assert result.returncode != 0, (
        "wait-empty exited 0 while the stubbed `compose ps -aq` still "
        "reported container {} — it trusted a project that never emptied"
        .format(DYING_CONTAINER_ID))


def test_verify_fresh_rejects_previous_incarnation_container(tmp_path):
    """isBugCondition_E (Startup side, behaviorally): the incident's adopted
    backend had a StartedAt from the PREVIOUS incarnation — older than the
    Startup that reported it healthy. `verify-fresh` must exit non-zero for
    any project container whose StartedAt predates the reference epoch,
    failing Startup so Greengrass retries instead of reporting RUNNING.

    Validates: Requirements 1.11, 1.12, 1.13
    """
    _assert_helper_exists()
    reference_epoch = int(time.time())
    stale_epoch = reference_epoch - 3600
    stale_started_at = time.strftime(
        "%Y-%m-%dT%H:%M:%S.000000000Z", time.gmtime(stale_epoch))
    env, _ = _make_stub_docker(tmp_path, polls_before_empty=10_000,
                               started_at=stale_started_at)
    result = subprocess.run(
        ["bash", HELPER_PATH, "verify-fresh", str(reference_epoch), "--",
         "--profile", "tegra", "-f", "/tmp/does-not-matter.yaml"],
        env=env, capture_output=True, text=True, timeout=120)
    assert result.returncode != 0, (
        "verify-fresh exited 0 for container {} with StartedAt {} — older "
        "than the reference epoch {} — the exact adopted-dying-container "
        "shape from the incident".format(
            DYING_CONTAINER_ID, stale_started_at, reference_epoch))
