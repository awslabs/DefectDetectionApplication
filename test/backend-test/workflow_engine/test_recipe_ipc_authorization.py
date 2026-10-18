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
"""The IPC authorization the LocalServer recipes grant the telemetry agents
(workflow-telemetry-dashboards design §5.3, A10/Q8; tasks.md 1.6).

Every ``recipe*.yaml`` at the repository root carries ``mqttproxy:1``
(``aws.greengrass#PublishToIoTCore`` and ``aws.greengrass#SubscribeToIoTCore``
on ``$aws/things/*/shadow/name/*``: the Control_Shadow read and the
``reported`` write) and ``mqttproxy:2`` with ``aws.greengrass#PublishToIoTCore``
on ``*`` (the Summaries, Events and live messages). ``EXCEPTION_RECIPES`` is
the list of variants known to lack the publish statement; it is empty since
the trunk integration gave ``recipe-amd64-nvidia.yaml`` its ``mqttproxy:2``
(A10/Q8 resolved). The test documents the recipes; it does not change them:
it fails when a recipe lacks a statement, so that a new exception is listed
(and A10 reopened) deliberately.

Validates: R5.4, R18.4, R19.4, R20.3.
"""
import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
RECIPES = sorted(REPO_ROOT.glob("recipe*.yaml"))
#: Variants known to lack the publish statement (design §5.3, A10/Q8): none
#: since the trunk integration; list a recipe here only as a deliberate,
#: documented exception.
EXCEPTION_RECIPES = frozenset()
SHADOW_RESOURCE = "$aws/things/*/shadow/name/*"
PUBLISH = "aws.greengrass#PublishToIoTCore"
SUBSCRIBE = "aws.greengrass#SubscribeToIoTCore"
#: The topic roots a narrowed ``mqttproxy:2`` must keep (design §5.3).
TELEMETRY_PREFIXES = ("dda/telemetry/", "dda/debug/")


def _mqttproxy_policies(recipe_path):
    with open(str(recipe_path), "r", encoding="utf-8") as handle:
        document = yaml.safe_load(handle)
    access_control = document["ComponentConfiguration"]["DefaultConfiguration"]["accessControl"]
    policies = access_control.get("aws.greengrass.ipc.mqttproxy") or {}
    component = document["ComponentName"]
    by_suffix = {}
    for policy_id, policy in policies.items():
        assert policy_id.startswith(component + ":mqttproxy:"), (
            "%s: policy id %r does not follow <component>:mqttproxy:<n>" % (recipe_path.name, policy_id))
        by_suffix[policy_id.split(":")[-1]] = policy
    return by_suffix


def _covers_topic(resources, topic):
    """Greengrass access-control matching: ``*`` in a resource matches any
    run of characters, anywhere in the string."""
    for resource in resources:
        pattern = "^" + ".*".join(re.escape(part) for part in resource.split("*")) + "$"
        if re.match(pattern, topic):
            return True
    return False


def test_there_are_seven_recipes_at_the_repository_root():
    assert [p.name for p in RECIPES] == [
        "recipe-amd64-nvidia.yaml", "recipe-amd64.yaml", "recipe-arm64-jp5.yaml", "recipe-arm64-jp6.yaml",
        "recipe-arm64-jp7.yaml", "recipe-arm64.yaml", "recipe.yaml",
    ]


def test_no_recipe_is_exempt_from_the_publish_statement():
    """A10/Q8 resolved: the exception list is empty, so every variant below
    is checked for ``mqttproxy:2``."""
    assert EXCEPTION_RECIPES == frozenset()


@pytest.mark.parametrize("recipe", RECIPES, ids=lambda p: p.name)
def test_every_recipe_carries_mqttproxy_1_on_the_shadow_topics(recipe):
    policies = _mqttproxy_policies(recipe)
    assert "1" in policies, "%s lacks mqttproxy:1" % recipe.name
    policy = policies["1"]
    assert set(policy["operations"]) == {PUBLISH, SUBSCRIBE}, recipe.name
    assert policy["resources"] == [SHADOW_RESOURCE], recipe.name
    for shadow in ("dda-workflow-telemetry", "dda-workflow-debug"):
        topic = "$aws/things/thing-1/shadow/name/%s/get" % shadow
        assert _covers_topic(policy["resources"], topic), (recipe.name, topic)


@pytest.mark.parametrize("recipe", [p for p in RECIPES if p.name not in EXCEPTION_RECIPES], ids=lambda p: p.name)
def test_every_recipe_carries_mqttproxy_2_publish_on_the_wildcard(recipe):
    policies = _mqttproxy_policies(recipe)
    assert "2" in policies, "%s lacks mqttproxy:2 (aws.greengrass#PublishToIoTCore on *)" % recipe.name
    policy = policies["2"]
    assert policy["operations"] == [PUBLISH], recipe.name
    assert policy["resources"] == ["*"], recipe.name
    for prefix in TELEMETRY_PREFIXES:
        assert _covers_topic(policy["resources"], prefix + "uc/wf/summary"), (recipe.name, prefix)


def test_the_seven_publishing_variants_are_the_documented_ones():
    publishing = sorted(p.name for p in RECIPES if "2" in _mqttproxy_policies(p))
    assert publishing == ["recipe-amd64-nvidia.yaml", "recipe-amd64.yaml", "recipe-arm64-jp5.yaml",
                          "recipe-arm64-jp6.yaml", "recipe-arm64-jp7.yaml", "recipe-arm64.yaml",
                          "recipe.yaml"]
