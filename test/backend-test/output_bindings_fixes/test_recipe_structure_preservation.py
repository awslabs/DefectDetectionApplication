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
"""Preservation golden tests (Task 2) for workflow-output-bindings-fixes.

Property 4: Preservation — recipe structure unchanged beyond the added
mqttproxy publish policy entry.

**Validates: Requirements 3.1, 3.7**

Observation-first: the goldens in ``goldens/`` record the full parsed
structure of each of the four editable LocalServer recipe variants as
OBSERVED on the current working tree. NOTE the ``...:mqttproxy:2``
publish-only entry is ALREADY present in that tree (added by the sibling
``mqtt-authz-model-visibility`` spec), so the goldens include it; the
deep-equality test therefore deletes exactly that entry from BOTH sides
before comparing. That makes the assertion: the fixed recipes may differ
from the recorded baseline ONLY in the ``mqttproxy:2`` entry itself —
every other byte of structure (the ``mqttproxy:1`` shadow policy, the
ShadowManager and Cli accessControl sections, lifecycle scripts,
dependencies, configuration, artifacts) is pinned (Requirement 3.7).

Two sharper invariants are pinned on top of the deep equality (folded in
from the prior session's ``workflow_output_bindings`` suite):

* the ``...:mqttproxy:1`` shadow pub/sub policy is byte-identical to the
  recorded content (Publish + Subscribe on exactly the shadow topic
  filter) — the ShadowManager StreamShadow/AppRunnerShadow flows depend
  on it (Requirement 3.1);
* NO mqttproxy policy in ANY variant grants
  ``aws.greengrass#SubscribeToIoTCore`` on a resource beyond the shadow
  filter — the workflow-publish authorization is deliberately
  publish-only and no edit may broaden the device's subscribe capability
  (Requirement 3.7). This holds even if the goldens are regenerated.

``recipe.yaml`` is the gdk build-time working copy (overwritten by
``gdk-component-build-and-publish.sh``) and is never asserted on.

Regenerating goldens (ONLY for intentional, reviewed structure changes):

    python3 test/backend-test/output_bindings_fixes/test_recipe_structure_preservation.py --regenerate
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

#: The four editable LocalServer recipe variants.
RECIPE_VARIANTS = (
    "recipe-arm64-jp6.yaml",
    "recipe-arm64-jp5.yaml",
    "recipe-arm64.yaml",
    "recipe-amd64.yaml",
)

MQTTPROXY_SECTION = "aws.greengrass.ipc.mqttproxy"
PUBLISH_OPERATION = "aws.greengrass#PublishToIoTCore"
SUBSCRIBE_OPERATION = "aws.greengrass#SubscribeToIoTCore"
SHADOW_TOPIC_FILTER = "$aws/things/*/shadow/name/*"

#: OBSERVED on the current tree: every variant's shadow pub/sub policy,
#: keyed ``{ComponentName}:mqttproxy:1``. This exact content (description,
#: operations in this order, the single shadow resource) is what the
#: ShadowManager flows were granted and must never change.
EXPECTED_MQTTPROXY_1 = {
    "policyDescription": "Allows access to shadow pubsub topics",
    "operations": [SUBSCRIBE_OPERATION, PUBLISH_OPERATION],
    "resources": [SHADOW_TOPIC_FILTER],
}


def _load_recipe(recipe_name):
    with open(os.path.join(REPO_ROOT, recipe_name), encoding="utf-8") as f:
        return yaml.safe_load(f)


def _access_control(recipe, recipe_name):
    access_control = (
        (recipe.get("ComponentConfiguration") or {})
        .get("DefaultConfiguration", {})
        .get("accessControl", {})
    )
    assert access_control, "{0}: no accessControl block".format(recipe_name)
    return access_control


def _mqttproxy_policies(recipe, recipe_name):
    policies = _access_control(recipe, recipe_name).get(MQTTPROXY_SECTION)
    assert isinstance(policies, dict) and policies, (
        "{0}: no {1} access-control section".format(
            recipe_name, MQTTPROXY_SECTION))
    return policies


def strip_added_publish_entry(recipe):
    """The recipe minus ONLY the added ``{ComponentName}:mqttproxy:2``
    publish policy entry — the one structural difference this spec (via
    the sibling mqtt-authz-model-visibility change) is allowed to make."""
    stripped = copy.deepcopy(recipe)
    component_name = stripped.get("ComponentName")
    policies = (
        (stripped.get("ComponentConfiguration") or {})
        .get("DefaultConfiguration", {})
        .get("accessControl", {})
        .get(MQTTPROXY_SECTION)
    )
    if isinstance(policies, dict) and component_name:
        policies.pop("{0}:mqttproxy:2".format(component_name), None)
    return stripped


def _golden_path(recipe_name):
    return os.path.join(
        GOLDENS_DIR,
        recipe_name.replace("-", "_").replace(".yaml", "")
        + "_full.golden.json",
    )


@pytest.mark.parametrize("recipe_name", RECIPE_VARIANTS)
def test_recipe_deep_equals_golden_modulo_added_publish_entry(recipe_name):
    """Property 4: after deleting exactly the ``...:mqttproxy:2`` entry
    from both sides, the variant's parsed structure deep-equals the golden
    recorded from the current tree — no other recipe drift is permitted
    (shadow/CLI policies, lifecycle, dependencies, configuration and
    artifacts byte-equal).

    Validates: Requirements 3.7
    """
    golden_path = _golden_path(recipe_name)
    assert os.path.isfile(golden_path), (
        "golden fixture missing: {0} — regenerate with `python3 {1} "
        "--regenerate` on a KNOWN-GOOD tree".format(
            golden_path, os.path.abspath(__file__)))
    with open(golden_path, encoding="utf-8") as f:
        golden = json.load(f)

    current = strip_added_publish_entry(_load_recipe(recipe_name))
    recorded = strip_added_publish_entry(golden)
    assert current == recorded, (
        "PRESERVATION REGRESSION (Property 4): {0} changed beyond the "
        "added mqttproxy:2 publish policy entry — every other part of the "
        "recipe (mqttproxy:1 shadow policy, ShadowManager/Cli policies, "
        "lifecycle, configuration, artifacts) must equal the recorded "
        "golden".format(recipe_name))


@pytest.mark.parametrize("recipe_name", RECIPE_VARIANTS)
def test_shadow_mqttproxy_policy_is_byte_identical(recipe_name):
    """Property 4: the ``...:mqttproxy:1`` shadow pub/sub policy of every
    variant equals the recorded content exactly — same description, same
    operations (Subscribe then Publish), and exactly the one shadow topic
    filter resource. The ShadowManager StreamShadow/AppRunnerShadow flows
    rely on this policy being untouched.

    Validates: Requirements 3.1
    """
    recipe = _load_recipe(recipe_name)
    component_name = recipe.get("ComponentName")
    policies = _mqttproxy_policies(recipe, recipe_name)

    shadow_key = "{0}:mqttproxy:1".format(component_name)
    assert shadow_key in policies, (
        "PRESERVATION REGRESSION (Property 4): {0} lost its shadow "
        "pub/sub policy {1!r} (present policies: {2})".format(
            recipe_name, shadow_key, sorted(policies)))
    assert policies[shadow_key] == EXPECTED_MQTTPROXY_1, (
        "PRESERVATION REGRESSION (Property 4): {0}'s shadow pub/sub "
        "policy {1!r} changed:\n  observed: {2!r}\n  recorded: {3!r}"
        .format(recipe_name, shadow_key, policies[shadow_key],
                EXPECTED_MQTTPROXY_1))


@pytest.mark.parametrize("recipe_name", RECIPE_VARIANTS)
def test_subscribe_is_never_authorized_beyond_the_shadow_resource(
        recipe_name):
    """Property 4: across ALL mqttproxy policies of every variant, any
    policy granting ``SubscribeToIoTCore`` must scope every one of its
    resources to exactly the shadow topic filter. The workflow-publish
    authorization is publish-only by design; no edit in this spec (or a
    later one) may broaden the device's subscribe capability. Unlike the
    golden comparison, this invariant survives golden regeneration.

    Validates: Requirements 3.7
    """
    recipe = _load_recipe(recipe_name)
    policies = _mqttproxy_policies(recipe, recipe_name)

    for policy_id, policy in policies.items():
        operations = (policy or {}).get("operations") or []
        if SUBSCRIBE_OPERATION not in operations:
            continue
        resources = (policy or {}).get("resources") or []
        offending = [r for r in resources if str(r) != SHADOW_TOPIC_FILTER]
        assert not offending, (
            "PRESERVATION REGRESSION (Property 4): {0} policy {1!r} "
            "grants SubscribeToIoTCore on non-shadow resource(s) {2!r} — "
            "subscribe must stay bound to {3!r}".format(
                recipe_name, policy_id, offending, SHADOW_TOPIC_FILTER))


def _regenerate():
    os.makedirs(GOLDENS_DIR, exist_ok=True)
    for recipe_name in RECIPE_VARIANTS:
        recipe = _load_recipe(recipe_name)
        with open(_golden_path(recipe_name), "w", encoding="utf-8") as f:
            json.dump(recipe, f, indent=2, sort_keys=True, ensure_ascii=True)
            f.write("\n")
        print("wrote {0}".format(_golden_path(recipe_name)))


if __name__ == "__main__":
    import sys
    if "--regenerate" in sys.argv:
        _regenerate()
    else:
        print("usage: python3 {0} --regenerate".format(sys.argv[0]))
