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
"""Preservation property tests (Task 2.1) for mqtt-authz-model-visibility.

Property 3: Preservation — recipe access control and structure outside the
new policy (Defect 1).

**These tests PASS on the UNFIXED recipes** (observation-first: they encode
the baseline recorded from the unfixed tree) and MUST STILL PASS after the
Defect 1 fix adds the publish-only `...:mqttproxy:2` policy in task 3.1.

Baseline observed on the UNFIXED tree (all four variants):
- Exactly one `aws.greengrass.ipc.mqttproxy` policy,
  `'<ComponentName>:mqttproxy:1'`, with
  - policyDescription: ``Allows access to shadow pubsub topics``
  - operations: ``aws.greengrass#SubscribeToIoTCore``,
    ``aws.greengrass#PublishToIoTCore`` (in that order)
  - resources: ``$aws/things/*/shadow/name/*`` (only entry)
- Therefore the complete set of `SubscribeToIoTCore`-authorized resources
  across ALL mqttproxy policies is exactly
  ``{"$aws/things/*/shadow/name/*"}``.

What must hold before AND after the fix (Requirements 3.1, 3.3, 3.4):
a) shadow-style topics remain authorized for BOTH `SubscribeToIoTCore` and
   `PublishToIoTCore` (shadow pubsub keeps working — Req 3.1);
b) the `mqttproxy:1` entry equals the recorded baseline exactly (the fix may
   only ADD `mqttproxy:2`, never touch `mqttproxy:1` — Req 3.1);
c) no `SubscribeToIoTCore` broadening anywhere: the subscribe-authorized
   resource set stays exactly the baseline set (the new policy is
   publish-only — Req 3.3).

Recipe STRUCTURE preservation (lifecycle, dependencies, other access-control
blocks — Req 3.2/3.4) is covered by the deploy_reliability structure goldens
(`test_config_structure_preservation.py`), intentionally regenerated in task
3.1 for exactly the reviewed mqttproxy addition.

Validates: Requirements 3.1, 3.3, 3.4
"""
import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

from test_publish_authorization_exploration import (
    PUBLISH_OPERATION,
    RECIPE_VARIANTS,
    SHADOW_RESOURCE_PATTERN,
    _load_recipe,
    _mqttproxy_policies,
    greengrass_resource_matches,
)

SUBSCRIBE_OPERATION = "aws.greengrass#SubscribeToIoTCore"

#: Baseline `mqttproxy:1` entry recorded from the UNFIXED recipes — identical
#: in all four variants (only the policy key's component-name prefix varies).
BASELINE_SHADOW_POLICY = {
    "policyDescription": "Allows access to shadow pubsub topics",
    "operations": [SUBSCRIBE_OPERATION, PUBLISH_OPERATION],
    "resources": [SHADOW_RESOURCE_PATTERN],
}

#: Baseline set of resources authorized for `SubscribeToIoTCore` across ALL
#: mqttproxy policies, recorded from the UNFIXED recipes. The Defect 1 fix is
#: publish-only, so this set must never grow.
BASELINE_SUBSCRIBE_RESOURCES = frozenset({SHADOW_RESOURCE_PATTERN})


def _operation_authorized(policies, operation, topic):
    """True when some mqttproxy policy authorizes `operation` on `topic`."""
    for policy in policies.values():
        if operation not in (policy.get("operations") or []):
            continue
        for resource in (policy.get("resources") or []):
            if greengrass_resource_matches(resource, topic):
                return True
    return False


# ---------------------------------------------------------------------------
# Shadow-style topic generator:
#   $aws/things/<thing>/shadow/name/<shadow>[/suffix...]
# Segments use realistic thing/shadow-name characters (no `/`, no `*`, no
# `$`), so every generated topic matches `$aws/things/*/shadow/name/*` and
# stays inside the preserved (non-bug-condition) input space.
# ---------------------------------------------------------------------------

_NAME_SEGMENT = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
             "0123456789_-.:",
    min_size=1, max_size=16)
_SHADOW_TOPIC = st.tuples(
    _NAME_SEGMENT,                                     # thing name
    _NAME_SEGMENT,                                     # shadow name
    st.lists(_NAME_SEGMENT, min_size=0, max_size=2),   # optional suffix
).map(lambda t: "/".join(
    ["$aws/things/{}/shadow/name/{}".format(t[0], t[1])] + t[2]))


# ---------------------------------------------------------------------------
# (a) Shadow topics stay authorized for BOTH subscribe and publish
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("recipe_name", RECIPE_VARIANTS)
@settings(max_examples=25, deadline=None)
@given(topic=_SHADOW_TOPIC)
@example(topic="$aws/things/ryanorinagxdevkithomelabjp622/shadow/name/"
               "streamConfigurationShadow/update")
def test_shadow_topics_remain_authorized_for_subscribe_and_publish(
        recipe_name, topic):
    """Property 3 (Preservation): for ANY shadow-style topic
    `$aws/things/<thing>/shadow/name/<shadow>[/suffix]`, every recipe variant
    authorizes BOTH `SubscribeToIoTCore` and `PublishToIoTCore` via its
    mqttproxy access control — the shadow pubsub path must keep working
    unchanged before and after the Defect 1 fix.

    **Validates: Requirements 3.1**
    """
    # Generator invariant: the topic is inside the shadow namespace, i.e.
    # NOT in the bug-condition space (isBugCondition_1 is false).
    assert greengrass_resource_matches(SHADOW_RESOURCE_PATTERN, topic), (
        "generator invariant: topics must be shadow-style")

    policies = _mqttproxy_policies(_load_recipe(recipe_name), recipe_name)
    for operation in (SUBSCRIBE_OPERATION, PUBLISH_OPERATION):
        assert _operation_authorized(policies, operation, topic), (
            "PRESERVATION VIOLATION: {} no longer authorizes {} for the "
            "shadow topic {!r}".format(recipe_name, operation, topic))


# ---------------------------------------------------------------------------
# (b) The mqttproxy:1 entry equals the recorded baseline exactly
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("recipe_name", RECIPE_VARIANTS)
def test_mqttproxy_1_entry_equals_baseline(recipe_name):
    """Property 3 (Preservation): the `'<ComponentName>:mqttproxy:1'` shadow
    policy must equal the baseline recorded on the unfixed tree exactly —
    same policyDescription, same operations (order included), same resources.
    The Defect 1 fix may only ADD a `mqttproxy:2` entry.

    **Validates: Requirements 3.1**
    """
    recipe = _load_recipe(recipe_name)
    component_name = recipe["ComponentName"]
    policies = _mqttproxy_policies(recipe, recipe_name)
    key = "{}:mqttproxy:1".format(component_name)
    assert key in policies, (
        "PRESERVATION VIOLATION: {} lost its {!r} policy entry"
        .format(recipe_name, key))
    assert policies[key] == BASELINE_SHADOW_POLICY, (
        "PRESERVATION VIOLATION: {} {!r} differs from the recorded "
        "baseline.\n  observed: {!r}\n  baseline: {!r}"
        .format(recipe_name, key, policies[key], BASELINE_SHADOW_POLICY))


# ---------------------------------------------------------------------------
# (c) No SubscribeToIoTCore broadening across ALL mqttproxy policies
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("recipe_name", RECIPE_VARIANTS)
def test_subscribe_resource_set_is_exactly_baseline(recipe_name):
    """Property 3 (Preservation): the set of resources authorized for
    `SubscribeToIoTCore` across ALL mqttproxy policies must be exactly the
    baseline set — the Defect 1 fix is publish-only, so no policy may add
    subscribe scope anywhere.

    **Validates: Requirements 3.3**
    """
    policies = _mqttproxy_policies(_load_recipe(recipe_name), recipe_name)
    subscribe_resources = frozenset(
        resource
        for policy in policies.values()
        if SUBSCRIBE_OPERATION in (policy.get("operations") or [])
        for resource in (policy.get("resources") or []))
    assert subscribe_resources == BASELINE_SUBSCRIBE_RESOURCES, (
        "PRESERVATION VIOLATION (subscribe broadening): {} authorizes "
        "SubscribeToIoTCore on {!r}, baseline is {!r}"
        .format(recipe_name, sorted(subscribe_resources),
                sorted(BASELINE_SUBSCRIBE_RESOURCES)))
