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
"""Bug-condition exploration test (Task 1, case 2) for edge-deploy-reliability.

Property 2: Bug Condition — Greengrass RUNNING implies healthy backend
(Defect B, `isBugCondition_B`).

**These tests assert the FIXED (post-fix) recipe lifecycle, so they are
EXPECTED TO FAIL on the UNFIXED tree.** The failure is the counterexample
confirming the defect: every LocalServer recipe variant runs an attached
`Run` lifecycle (`docker compose ... up --no-build`) that stays alive while
ANY service runs — the incident's frontend kept it alive, Greengrass reported
LocalServer RUNNING over a dead backend, and every HARD dependency on
LocalServer was satisfied against a dead vLLM runtime.

The SAME tests are re-run in task 3.5 against the fixed recipes, where they
must PASS: each variant gates RUNNING on health via a `Startup` block whose
compose invocation is detached and health-gated
(`docker compose ... up -d --no-build --wait ...`), with an explicit Timeout
sized for a cold JP6 boot.

Config test as the testable seam (per the design): the recipe defect is a
configuration defect, so the tests parse the four recipe variants and assert
the reliability-critical lifecycle shape directly.

Validates: Requirements 1.4, 1.5
"""
import os
import re

import pytest
import yaml

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

#: All four LocalServer recipe variants share the compose file and must share
#: the same health-gated lifecycle shape (Requirement 3.5).
RECIPE_VARIANTS = (
    "recipe-arm64-jp6.yaml",
    "recipe-arm64-jp5.yaml",
    "recipe-arm64.yaml",
    "recipe-amd64.yaml",
)


def _load_recipe(recipe_name):
    path = os.path.join(REPO_ROOT, recipe_name)
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _lifecycles(recipe, recipe_name):
    manifests = recipe.get("Manifests") or []
    assert manifests, "{}: recipe declares no Manifests".format(recipe_name)
    return [(i, m.get("Lifecycle") or {}) for i, m in enumerate(manifests)]


def _compose_up_lines(script):
    """Every line of the lifecycle script that runs `docker compose ... up`."""
    lines = []
    for raw_line in script.splitlines():
        # A single logical recipe line may chain commands with ';'.
        for part in raw_line.split(";"):
            if re.search(r"docker\s+compose\b.*\bup\b", part):
                lines.append(part.strip())
    return lines


@pytest.mark.parametrize("recipe_name", RECIPE_VARIANTS)
def test_recipe_gates_running_on_health_via_detached_startup(recipe_name):
    """isBugCondition_B: the unfixed recipes keep the component RUNNING via an
    attached `Run` (`compose up --no-build`) that only proves *some* service
    is alive. The fixed lifecycle replaces `Run` with a `Startup` block whose
    final compose command is `up -d ... --wait`, so the lifecycle exits 0 —
    and Greengrass reports RUNNING — only when all started services pass
    their healthchecks.

    Validates: Requirements 1.4, 1.5 (expected behavior 2.4, 2.7)
    """
    recipe = _load_recipe(recipe_name)
    for index, lifecycle in _lifecycles(recipe, recipe_name):
        assert "Startup" in lifecycle, (
            "COUNTEREXAMPLE (Defect B): {} manifest {} has lifecycle blocks "
            "{} — no Startup block; the attached Run keeps LocalServer "
            "RUNNING while only the frontend serves (backend dead, "
            "Exited(137))".format(recipe_name, index,
                                  sorted(lifecycle.keys())))
        assert "Run" not in lifecycle, (
            "COUNTEREXAMPLE (Defect B): {} manifest {} still declares a Run "
            "block alongside Startup (Greengrass allows Run OR Startup, not "
            "both)".format(recipe_name, index))

        startup = lifecycle["Startup"]
        script = startup.get("Script") or ""
        up_lines = _compose_up_lines(script)
        assert up_lines, (
            "COUNTEREXAMPLE (Defect B): {} manifest {} Startup script runs "
            "no `docker compose ... up`".format(recipe_name, index))
        for up_line in up_lines:
            assert re.search(r"(\s-d\b|--detach\b)", up_line), (
                "COUNTEREXAMPLE (Defect B): {} manifest {} compose up is "
                "attached (no -d): {!r} — the script stays alive while ANY "
                "service runs, hiding a dead backend"
                .format(recipe_name, index, up_line))
            assert "--wait" in up_line, (
                "COUNTEREXAMPLE (Defect B): {} manifest {} compose up has no "
                "--wait: {!r} — the lifecycle cannot gate RUNNING on service "
                "health".format(recipe_name, index, up_line))

        # Greengrass's default Startup timeout (120s) is far below a cold JP6
        # backend boot; the fixed Startup declares an explicit Timeout.
        assert startup.get("Timeout"), (
            "COUNTEREXAMPLE (Defect B): {} manifest {} Startup declares no "
            "Timeout — the Greengrass 120s default would kill a cold JP6 "
            "boot mid-wait".format(recipe_name, index))


# ---------------------------------------------------------------------------
# Shutdown env-export contract (on-hardware verification finding 1)
# ---------------------------------------------------------------------------
# Greengrass runs the Shutdown script in a fresh environment: unlike Startup
# (whose script exports /tmp/.dda.env before calling compose), the unfixed
# Shutdown ran `docker compose --profile $DOCKER_PROFILE -f <file> down` with
# $DOCKER_PROFILE unset, so `--profile` consumed `-f` and EVERY shutdown
# failed with "no configuration file provided: not found" (reproduced on the
# live JP6). The fixed Shutdown exports /tmp/.dda.env the same way Startup
# does and defaults the profile per architecture
# (get_nvidia_libs_versions.sh selects tegra on gpu+aarch64, generic
# otherwise) so the down works even when /tmp/.dda.env is missing.

#: Arch-appropriate default profile per variant (matches the
#: get_nvidia_libs_versions.sh decision for that target hardware).
RECIPE_DEFAULT_PROFILE = {
    "recipe-arm64-jp6.yaml": "tegra",
    "recipe-arm64-jp5.yaml": "tegra",
    "recipe-arm64.yaml": "tegra",
    "recipe-amd64.yaml": "generic",
}


def _compose_down_lines(script):
    """Every command of the lifecycle script that runs `docker compose ... down`."""
    lines = []
    for raw_line in script.splitlines():
        for part in raw_line.split(";"):
            if re.search(r"docker\s+compose\b.*\bdown\b", part):
                lines.append(part.strip())
    return lines


@pytest.mark.parametrize("recipe_name", RECIPE_VARIANTS)
def test_recipe_shutdown_exports_env_and_defaults_profile(recipe_name):
    """The Shutdown script must resolve DOCKER_PROFILE before running
    `docker compose ... down`: it exports /tmp/.dda.env exactly like Startup
    (with a fallback so a missing env file cannot fail the script) and the
    down line uses ${DOCKER_PROFILE:-<arch default>} so an empty expansion
    can never make `--profile` swallow `-f`.

    Validates: Requirements 2.7 (Shutdown lifecycle completes; no stopped
    backend left behind by a failed `compose down`)
    """
    recipe = _load_recipe(recipe_name)
    default_profile = RECIPE_DEFAULT_PROFILE[recipe_name]
    for index, lifecycle in _lifecycles(recipe, recipe_name):
        assert "Shutdown" in lifecycle, (
            "{} manifest {} has no Shutdown block".format(recipe_name, index))
        script = lifecycle["Shutdown"].get("Script") or ""
        lines = script.splitlines()

        down_lines = _compose_down_lines(script)
        assert down_lines, (
            "{} manifest {} Shutdown runs no `docker compose ... down`"
            .format(recipe_name, index))

        export_indexes = [i for i, line in enumerate(lines)
                          if re.search(r"export\s+\$\(grep\s+-v\s+'\^#'"
                                       r"\s+/tmp/\.dda\.env", line)]
        assert export_indexes, (
            "COUNTEREXAMPLE (finding 1): {} manifest {} Shutdown never "
            "exports /tmp/.dda.env — $DOCKER_PROFILE expands empty, "
            "`--profile` consumes `-f`, and `docker compose down` fails "
            "with 'no configuration file provided: not found'"
            .format(recipe_name, index))

        for down_line in down_lines:
            down_index = next(i for i, line in enumerate(lines)
                              if down_line in line)
            assert min(export_indexes) < down_index, (
                "{} manifest {}: the /tmp/.dda.env export must run BEFORE "
                "the compose down line".format(recipe_name, index))
            assert ("${DOCKER_PROFILE:-" + default_profile + "}") in down_line, (
                "COUNTEREXAMPLE (finding 1): {} manifest {} down line {!r} "
                "does not default the profile to the arch-appropriate "
                "{!r} — an unset DOCKER_PROFILE still breaks the down"
                .format(recipe_name, index, down_line, default_profile))
            assert re.search(r"--profile\s+\$DOCKER_PROFILE\b", down_line) is None, (
                "{} manifest {}: bare $DOCKER_PROFILE on the down line"
                .format(recipe_name, index))
