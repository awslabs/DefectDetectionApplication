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
"""Preservation golden tests (Task 2) for edge-deploy-reliability.

Property 6: Preservation — Compose and recipe structure unchanged beyond
the intended edits.

**Validates: Requirements 3.4, 3.5, 3.7**

Observation-first (observed on UNFIXED code): the goldens committed in
``goldens/`` record the full parsed structure of ``src/docker-compose.yaml``
and of each of the four LocalServer recipe variants, MASKED of exactly the
places the fix is allowed to touch:

  * compose services: the ``stop_grace_period``, ``restart``, and
    ``healthcheck`` keys (the three keys the Defect A/B fix adds/changes);
  * recipes: the ``Run``/``Startup`` lifecycle block of each manifest (the
    Defect B fix replaces Run with Startup).

Everything OUTSIDE the mask — services, profiles, images, build args,
volumes, environment, ports, network/runtime settings; recipe Install and
Shutdown blocks, ComponentDependencies, ComponentConfiguration, artifacts —
is the golden structure that must be byte-identical before AND after the
fix. These tests PASS on the unfixed tree (the goldens were captured from
it) and must still PASS after the fix; any drift outside the masked keys is
a preservation regression.

GOLDEN REGENERATION NOTE (on-hardware verification finding 1): the recipe
goldens were intentionally regenerated after the Shutdown env-export fix —
Greengrass runs Shutdown in a fresh environment, so the pre-existing
`docker compose --profile $DOCKER_PROFILE ... down` failed every cycle with
"no configuration file provided: not found" ($DOCKER_PROFILE empty,
`--profile` consumed `-f`). The new Shutdown contract (export /tmp/.dda.env
+ ${DOCKER_PROFILE:-<arch default>}) is asserted explicitly in
test_recipe_lifecycle_exploration.py::test_recipe_shutdown_exports_env_and_defaults_profile.

Regenerating goldens (ONLY for intentional, reviewed structure changes):

    python3 test/backend-test/deploy_reliability/test_config_structure_preservation.py --regenerate
"""
import copy
import json
import os

import pytest
import yaml

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
GOLDENS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "goldens")

COMPOSE_PATH = os.path.join(REPO_ROOT, "src", "docker-compose.yaml")

#: Compose service keys the fix is allowed to add or change (masked from
#: the golden): stop_grace_period + restart (Defect A), healthcheck
#: (Defect B). Everything else must be untouched.
COMPOSE_MASKED_SERVICE_KEYS = ("stop_grace_period", "restart", "healthcheck")

#: Recipe lifecycle blocks the fix is allowed to replace (masked from the
#: golden): the attached Run becomes a detached health-gated Startup.
#: Install and Shutdown must be untouched.
RECIPE_MASKED_LIFECYCLE_KEYS = ("Run", "Startup")

RECIPE_VARIANTS = (
    "recipe-arm64-jp6.yaml",
    "recipe-arm64-jp5.yaml",
    "recipe-arm64.yaml",
    "recipe-amd64.yaml",
)

#: Restart policies that auto-recover a crashed (self-exited) container.
#: `always` is a strict superset of `unless-stopped` for crash exits, so
#: crash auto-recovery (the AWS CRT SIGABRT protection) is preserved by
#: either value (Requirement 3.6).
CRASH_RECOVERING_RESTART_POLICIES = ("unless-stopped", "always")


def _load_yaml(path):
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def mask_compose(compose):
    """The compose document minus ONLY the keys the fix may add/change."""
    masked = copy.deepcopy(compose)
    for service in (masked.get("services") or {}).values():
        for key in COMPOSE_MASKED_SERVICE_KEYS:
            service.pop(key, None)
    return masked


def mask_recipe(recipe):
    """The recipe document minus ONLY each manifest's Run/Startup block."""
    masked = copy.deepcopy(recipe)
    for manifest in masked.get("Manifests") or []:
        lifecycle = manifest.get("Lifecycle")
        if isinstance(lifecycle, dict):
            for key in RECIPE_MASKED_LIFECYCLE_KEYS:
                lifecycle.pop(key, None)
    return masked


def _golden_path(name):
    return os.path.join(GOLDENS_DIR, name)


#: (golden fixture name, source path, masking function)
GOLDEN_CASES = [
    ("docker_compose_structure.golden.json", COMPOSE_PATH, mask_compose),
] + [
    (variant.replace("-", "_").replace(".yaml", "") + "_structure.golden.json",
     os.path.join(REPO_ROOT, variant), mask_recipe)
    for variant in RECIPE_VARIANTS
]


@pytest.mark.parametrize(
    "golden_name,source_path,mask", GOLDEN_CASES,
    ids=[case[0].replace("_structure.golden.json", "")
         for case in GOLDEN_CASES])
def test_structure_matches_golden_outside_the_intended_edits(
        golden_name, source_path, mask):
    """Property 6: after masking exactly the keys the fix is allowed to
    touch, the current file's parsed structure equals the golden captured
    from the unfixed tree — services/profiles/arch selection, the shared
    compose contract for the JP5/x86 variants, and every recipe's Install,
    Shutdown, dependencies, configuration and artifacts are unchanged.

    Validates: Requirements 3.4, 3.5, 3.7
    """
    golden_path = _golden_path(golden_name)
    assert os.path.isfile(golden_path), (
        "golden fixture missing: {} — regenerate with `python3 {} "
        "--regenerate` on a KNOWN-GOOD tree".format(
            golden_path, os.path.abspath(__file__)))
    with open(golden_path, encoding="utf-8") as f:
        golden = json.load(f)

    masked = mask(_load_yaml(source_path))
    assert masked == golden, (
        "PRESERVATION REGRESSION (Property 6): {} changed outside the "
        "intended edits (the masked keys {}). Only stop_grace_period/"
        "restart/healthcheck (compose) and the Run->Startup lifecycle "
        "replacement (recipes) may differ from the recorded golden."
        .format(os.path.relpath(source_path, REPO_ROOT),
                COMPOSE_MASKED_SERVICE_KEYS
                if mask is mask_compose else RECIPE_MASKED_LIFECYCLE_KEYS))


def test_every_compose_service_keeps_a_crash_recovering_restart_policy():
    """Property 5/6 corollary (Requirement 3.6): the restart key is masked
    from the golden because the fix changes its VALUE on the backend
    services — but whatever the value, every service must keep a policy
    that auto-recovers a crashed container (`unless-stopped` today,
    `always` after the fix — a strict superset for crash exits). This
    preserves the AWS CRT event-stream SIGABRT protection.

    Validates: Requirements 3.6
    """
    compose = _load_yaml(COMPOSE_PATH)
    for name, service in (compose.get("services") or {}).items():
        assert service.get("restart") in CRASH_RECOVERING_RESTART_POLICIES, (
            "PRESERVATION REGRESSION (Requirement 3.6): service '{}' has "
            "restart={!r}; a self-crashed backend would no longer be "
            "auto-recovered".format(name, service.get("restart")))


def _regenerate():
    os.makedirs(GOLDENS_DIR, exist_ok=True)
    for golden_name, source_path, mask in GOLDEN_CASES:
        masked = mask(_load_yaml(source_path))
        with open(_golden_path(golden_name), "w", encoding="utf-8") as f:
            json.dump(masked, f, indent=2, sort_keys=True, ensure_ascii=True)
            f.write("\n")
        print("wrote {}".format(_golden_path(golden_name)))


if __name__ == "__main__":
    import sys
    if "--regenerate" in sys.argv:
        _regenerate()
    else:
        print("usage: python3 {} --regenerate".format(sys.argv[0]))
