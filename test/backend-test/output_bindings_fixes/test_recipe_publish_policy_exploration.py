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
"""Bug-condition exploration test — case 1: recipe policy exposure
(workflow-output-bindings-fixes, Defect A, ``isBugCondition_A``).

Property 1: Bug Condition — Greengrass workflow-topic publish authorized.

**This test asserts the FIXED (post-fix) recipe access control, so it is
EXPECTED TO FAIL on a tree whose recipes are unfixed.** The failure is the
counterexample confirming Defect A: every LocalServer recipe variant's
``aws.greengrass.ipc.mqttproxy`` accessControl grants
``PublishToIoTCore``/``SubscribeToIoTCore`` only on the shadow topic filter
``$aws/things/*/shadow/name/*``, while the mqtt_publish node's ``topic``
parameter is a user-configured free string (catalog example
``factory/line1/inspection``). Any workflow topic therefore matches no policy
and the nucleus denies the IPC publish with ``UnauthorizedError`` — observed
on live JP6 run 85bf7a61-a126-484d-9074-08fbb73f209e.

Expected counterexample on truly-unfixed recipes (all four variants):
    mqttproxy policies == {'...:mqttproxy:1':
    {'operations': [SubscribeToIoTCore, PublishToIoTCore],
     'resources': ['$aws/things/*/shadow/name/*']}} — no resource covers
    'factory/line1/inspection'.

RUN OUTCOME NOTE (documented at exploration time): on THIS working tree the
test PASSES, because the sibling spec ``mqtt-authz-model-visibility``
(task 3.1, uncommitted working-tree change) already added the identical
publish-only ``...:mqttproxy:2`` wildcard entry to all four variants. The
recipe half of Defect A is therefore already fixed in-tree; the engine-side
denial diagnostics half (case 2) still reproduces. Verified against git:
``git diff recipe-arm64-jp6.yaml`` shows exactly the added mqttproxy:2 block.

The SAME test is re-run in task 3.4, where it must PASS.

Validates: Requirements 1.1, 1.2 (expected behavior 2.1)
"""
import os
import re

import pytest
import yaml

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

#: The four editable LocalServer recipe variants (recipe.yaml is the gdk
#: build-time working copy and is never edited or asserted on).
RECIPE_VARIANTS = (
    "recipe-arm64-jp6.yaml",
    "recipe-arm64-jp5.yaml",
    "recipe-arm64.yaml",
    "recipe-amd64.yaml",
)

#: The node catalog's documented example for the free-string mqtt_publish
#: ``topic`` parameter — stands in for "any user-configured workflow topic".
WORKFLOW_TOPIC = "factory/line1/inspection"

PUBLISH_OPERATION = "aws.greengrass#PublishToIoTCore"
SUBSCRIBE_OPERATION = "aws.greengrass#SubscribeToIoTCore"


def _load_recipe(recipe_name):
    with open(os.path.join(REPO_ROOT, recipe_name), encoding="utf-8") as f:
        return yaml.safe_load(f)


def _mqttproxy_policies(recipe, recipe_name):
    access_control = (
        (recipe.get("ComponentConfiguration") or {})
        .get("DefaultConfiguration", {})
        .get("accessControl", {})
    )
    policies = access_control.get("aws.greengrass.ipc.mqttproxy")
    assert isinstance(policies, dict) and policies, (
        "{0}: no aws.greengrass.ipc.mqttproxy access-control block"
        .format(recipe_name))
    return policies


def _resource_covers_topic(resource, topic):
    """Whether a Greengrass mqttproxy resource authorizes ``topic``
    (``*`` wildcard within the resource string; MQTT ``#``/``+`` filters)."""
    resource = str(resource)
    if resource == "*":
        return True
    pattern = re.escape(resource)
    pattern = pattern.replace(re.escape("#"), ".*")
    pattern = pattern.replace(re.escape("+"), "[^/]+")
    pattern = pattern.replace(re.escape("*"), ".*")
    return re.fullmatch(pattern, topic) is not None


@pytest.mark.parametrize("recipe_name", RECIPE_VARIANTS)
def test_recipe_authorizes_publish_to_workflow_topics(recipe_name):
    """isBugCondition_A: greengrass-enabled mqtt_publish to any topic not
    matching ``$aws/things/*/shadow/name/*``. The fixed recipe carries a
    publish-only policy whose resources cover an arbitrary workflow topic
    without broadening SubscribeToIoTCore beyond the shadow filter.

    Validates: Requirements 1.1, 1.2 (expected behavior 2.1)
    """
    recipe = _load_recipe(recipe_name)
    policies = _mqttproxy_policies(recipe, recipe_name)

    covering_publish_only = []
    observed = {}
    for policy_id, policy in policies.items():
        operations = (policy or {}).get("operations") or []
        resources = (policy or {}).get("resources") or []
        observed[policy_id] = {
            "operations": list(operations), "resources": list(resources)}
        if PUBLISH_OPERATION not in operations:
            continue
        if not any(_resource_covers_topic(r, WORKFLOW_TOPIC)
                   for r in resources):
            continue
        if SUBSCRIBE_OPERATION in operations:
            # A covering policy that also grants subscribe would broaden
            # SubscribeToIoTCore beyond the shadow resource — not allowed
            # by the design's publish-only scope decision.
            continue
        covering_publish_only.append(policy_id)

    assert covering_publish_only, (
        "COUNTEREXAMPLE (Defect A): {0} authorizes PublishToIoTCore on no "
        "resource covering the workflow topic {1!r}; mqttproxy policies are "
        "{2} — only the shadow filter exists, so the nucleus denies every "
        "workflow mqtt_publish with UnauthorizedError"
        .format(recipe_name, WORKFLOW_TOPIC, observed))
