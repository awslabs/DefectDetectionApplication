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
"""Bug-condition exploration test (Task 1.1) for mqtt-authz-model-visibility.

Property 1: Bug Condition — workflow topics authorized for Greengrass publish
(Defect 1, `isBugCondition_1` in the design).

**These tests assert the FIXED (post-fix) recipe access control, so they are
EXPECTED TO FAIL on the UNFIXED tree.** The failure is the counterexample
confirming the defect: every LocalServer recipe variant carries exactly one
`aws.greengrass.ipc.mqttproxy` policy (`...:mqttproxy:1`) whose only resource
is `$aws/things/*/shadow/name/*`. The workflow `mqtt_publish` node's `topic`
parameter is free-form user input (e.g. `factory/line1/inspection`, workflow
execution `85bf7a61` on the JP6 device), so every publish through Greengrass
IPC `PublishToIoTCore` (`output_bindings.py` `_default_greengrass_publisher`)
to a non-shadow topic is denied with
`awsiot.greengrasscoreipc.model.UnauthorizedError`.

The SAME tests are re-run in task 3.3 against the fixed recipes, where they
must PASS: each variant gains a publish-only mqttproxy policy whose resources
cover any workflow topic.

Config test as the testable seam (per the design): the defect is a recipe
access-control defect, so the tests parse the four recipe variants, implement
Greengrass resource wildcard matching (`*` matches any character sequence),
and assert that some mqttproxy policy with operation
`aws.greengrass#PublishToIoTCore` matches each generated topic.

Counterexamples found on the UNFIXED recipes (documented per task 1.1):
- `factory/line1/inspection` (the concrete device case from execution
  `85bf7a61`) is unmatched by every mqttproxy policy in ALL FOUR variants —
  the only policy resource is `$aws/things/*/shadow/name/*`. Hypothesis
  reports it as "Falsifying explicit example" in every variant.
- Hypothesis-generated minimal counterexample (property run without the
  explicit example): topic `'0'` — a single-segment, single-character
  workflow topic is already unauthorized, i.e. EVERY non-shadow topic falls
  outside the recipes' publish authorization, not just deep/multi-segment
  ones.

Validates: Requirements 1.1, 1.2
"""
import os
import re

import pytest
import yaml
from hypothesis import example, given, settings
from hypothesis import strategies as st

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

#: All four LocalServer recipe variants (source of truth; `recipe.yaml` is a
#: generated build artifact and is deliberately NOT parsed here).
RECIPE_VARIANTS = (
    "recipe-arm64-jp6.yaml",
    "recipe-arm64-jp5.yaml",
    "recipe-arm64.yaml",
    "recipe-amd64.yaml",
)

PUBLISH_OPERATION = "aws.greengrass#PublishToIoTCore"

#: The only resource the unfixed recipes authorize — used to keep the
#: generated topics in the bug-condition space (non-shadow topics).
SHADOW_RESOURCE_PATTERN = "$aws/things/*/shadow/name/*"


def _load_recipe(recipe_name):
    path = os.path.join(REPO_ROOT, recipe_name)
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _mqttproxy_policies(recipe, recipe_name):
    """The `aws.greengrass.ipc.mqttproxy` access-control block of a recipe."""
    config = (recipe.get("ComponentConfiguration") or {}).get(
        "DefaultConfiguration") or {}
    access_control = config.get("accessControl") or {}
    policies = access_control.get("aws.greengrass.ipc.mqttproxy") or {}
    assert policies, (
        "{}: recipe declares no aws.greengrass.ipc.mqttproxy access control"
        .format(recipe_name))
    return policies


def greengrass_resource_matches(pattern, topic):
    """Greengrass access-control resource matching.

    In a component recipe's accessControl `resources` list, `*` matches any
    sequence of characters (including `/`); every other character is literal.
    """
    regex = ".*".join(re.escape(part) for part in pattern.split("*"))
    return re.fullmatch(regex, topic) is not None


def _matching_publish_policy(policies, topic):
    """Name of the first mqttproxy policy authorizing a publish to `topic`,
    or None when no policy covers it (the bug condition)."""
    for policy_name, policy in policies.items():
        if PUBLISH_OPERATION not in (policy.get("operations") or []):
            continue
        for resource in (policy.get("resources") or []):
            if greengrass_resource_matches(resource, topic):
                return policy_name
    return None


def _assert_publish_authorized(recipe_name, topic):
    policies = _mqttproxy_policies(_load_recipe(recipe_name), recipe_name)
    policy_name = _matching_publish_policy(policies, topic)
    assert policy_name is not None, (
        "COUNTEREXAMPLE (Defect 1, isBugCondition_1): {} authorizes no "
        "{} policy for the workflow topic {!r} — mqttproxy resources are "
        "{} — so the workflow engine's Greengrass IPC publish "
        "(_default_greengrass_publisher) is denied with UnauthorizedError"
        .format(
            recipe_name, PUBLISH_OPERATION, topic,
            sorted(r for p in policies.values()
                   for r in (p.get("resources") or []))))


# ---------------------------------------------------------------------------
# Concrete device counterexample (workflow execution 85bf7a61)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("recipe_name", RECIPE_VARIANTS)
def test_concrete_workflow_topic_is_publish_authorized(recipe_name):
    """The `mqtt_publish` catalog example topic `factory/line1/inspection`
    (the semantics of the JP6 device failure, execution `85bf7a61`) must be
    covered by some PublishToIoTCore mqttproxy policy in every variant.

    EXPECTED TO FAIL on unfixed recipes: the only policy resource is
    `$aws/things/*/shadow/name/*`, which no workflow topic matches.

    Validates: Requirements 1.1, 1.2 (expected behavior 2.1, 2.2)
    """
    _assert_publish_authorized(recipe_name, "factory/line1/inspection")


# ---------------------------------------------------------------------------
# Property test: ANY non-empty, non-shadow workflow topic is authorized
# ---------------------------------------------------------------------------

#: The `mqtt_publish` node's `topic` is free-form user input with only a
#: `min_length: 1` constraint. Generate realistic MQTT-style topics: 1-4
#: segments of printable characters joined by `/`. The alphabet excludes `$`
#: so no generated topic can fall inside the reserved `$aws/...` shadow
#: namespace (the one resource the unfixed recipes DO authorize).
_TOPIC_SEGMENT = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
             "0123456789_-. ",
    min_size=1, max_size=12)
_WORKFLOW_TOPIC = st.lists(_TOPIC_SEGMENT, min_size=1, max_size=4).map(
    "/".join)


@pytest.mark.parametrize("recipe_name", RECIPE_VARIANTS)
@settings(max_examples=25, deadline=None)
@given(topic=_WORKFLOW_TOPIC)
@example(topic="factory/line1/inspection")
def test_any_workflow_topic_is_publish_authorized(recipe_name, topic):
    """Property 1 (Bug Condition): for ANY topic string the `mqtt_publish`
    node accepts (any non-empty non-shadow topic), each recipe variant must
    contain an `aws.greengrass.ipc.mqttproxy` policy whose operations include
    `aws.greengrass#PublishToIoTCore` and whose resources match the topic
    under Greengrass wildcard matching.

    EXPECTED TO FAIL on unfixed recipes: hypothesis falsifies the explicit
    example `factory/line1/inspection` first; without it, generated topics
    shrink to the minimal counterexample `topic='0'` — every non-shadow
    topic is unauthorized.

    Validates: Requirements 1.1, 1.2 (expected behavior 2.1, 2.2)

    **Validates: Requirements 1.1, 1.2**
    """
    # Keep the generated topic inside the bug-condition space: non-shadow
    # (isBugCondition_1 requires the topic NOT to match the shadow resource).
    assert not greengrass_resource_matches(SHADOW_RESOURCE_PATTERN, topic), (
        "generator invariant: topics must be non-shadow")
    _assert_publish_authorized(recipe_name, topic)
