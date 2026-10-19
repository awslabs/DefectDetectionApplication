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
"""Exploration (triton-model-cleanup-wedge task 5.1, Property 10): LocalServer's
dependency on ``aws.greengrass.Cli`` is SOFT in every recipe variant.

A HARD dependency makes every CLI restart (one per configuration-merging
deployment) restart LocalServer and, through their HARD dependency on it,
every model component, whose Shutdown then deletes a model Triton may still
be loading.

On the unfixed tree it FAILS: the recipes declare no ``DependencyType``, so
Greengrass treats the dependency as HARD.
"""

import os

import pytest
import yaml

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))

RECIPES = (
    "recipe.yaml",
    "recipe-arm64-jp5.yaml",
    "recipe-arm64-jp6.yaml",
    "recipe-arm64-jp7.yaml",
    "recipe-arm64.yaml",
    "recipe-amd64.yaml",
    "recipe-amd64-nvidia.yaml",
)


@pytest.mark.parametrize("recipe", RECIPES)
def test_cli_dependency_is_soft(recipe):
    with open(os.path.join(REPO, recipe), encoding="utf-8") as f:
        doc = yaml.safe_load(f)
    deps = doc.get("ComponentDependencies") or {}
    assert "aws.greengrass.Cli" in deps, "%s does not declare aws.greengrass.Cli" % recipe
    cli = deps["aws.greengrass.Cli"]
    assert cli.get("DependencyType", "HARD") == "SOFT", (
        "%s: aws.greengrass.Cli is %s; a CLI restart restarts LocalServer and every model component"
        % (recipe, cli.get("DependencyType", "HARD (no DependencyType)")))
