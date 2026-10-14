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
"""Bug condition for finding 25 (rtsp-rtmp-stream-cameras task 30.5;
Requirement 17.1).

Greengrass sets ``AWS_CONTAINER_CREDENTIALS_FULL_URI`` only for a component
that depends on ``aws.greengrass.TokenExchangeService``, and the backend
fetches Portal-managed stream credentials with those device credentials. The
ECR publish path adds the dependency when it rewrites a recipe, but
``gdk component publish`` publishes the repo recipe as it is, so every repo
recipe must declare it.

- ``test_recipe_declares_the_token_exchange_service[<recipe>]``: one case
  per LocalServer recipe. All seven FAIL on the unfixed tree (``ac74c0b``) on
  their assertion: no recipe declares TES.
- ``test_recipe_yaml_is_byte_identical_to_the_jp6_recipe`` and
  ``test_no_recipe_declares_the_docker_application_manager`` pass before and
  after the fix: ``recipe.yaml`` stays the tracked copy of
  ``recipe-arm64-jp6.yaml``, and DockerApplicationManager stays on the ECR
  path only.

Static: the recipes are read from the repo root of the tree this file sits in.
"""
import os

import pytest
import yaml

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

#: Every LocalServer recipe at the repo root.
RECIPES = (
    "recipe-amd64.yaml",
    "recipe-amd64-nvidia.yaml",
    "recipe-arm64.yaml",
    "recipe-arm64-jp5.yaml",
    "recipe-arm64-jp6.yaml",
    "recipe-arm64-jp7.yaml",
    "recipe.yaml",
)

TOKEN_EXCHANGE_SERVICE = "aws.greengrass.TokenExchangeService"
DOCKER_APPLICATION_MANAGER = "aws.greengrass.DockerApplicationManager"


def _dependencies(recipe):
    with open(os.path.join(REPO_ROOT, recipe), encoding="utf-8") as handle:
        document = yaml.safe_load(handle)
    return document.get("ComponentDependencies") or {}


@pytest.mark.parametrize("recipe", RECIPES, ids=RECIPES)
def test_recipe_declares_the_token_exchange_service(recipe):
    dependencies = _dependencies(recipe)
    assert dependencies.get(TOKEN_EXCHANGE_SERVICE) == {"VersionRequirement": "~2.0.0"}, (
        "{} does not declare {} ~2.0.0 (finding 25): without it Greengrass gives the "
        "component no TES credentials, so the backend cannot fetch Portal stream "
        "credentials; its ComponentDependencies are {}".format(
            recipe, TOKEN_EXCHANGE_SERVICE, sorted(dependencies)))


def test_recipe_yaml_is_byte_identical_to_the_jp6_recipe():
    with open(os.path.join(REPO_ROOT, "recipe.yaml"), "rb") as handle:
        copy = handle.read()
    with open(os.path.join(REPO_ROOT, "recipe-arm64-jp6.yaml"), "rb") as handle:
        original = handle.read()
    assert copy == original, "recipe.yaml is no longer byte-identical to recipe-arm64-jp6.yaml"


def test_no_recipe_declares_the_docker_application_manager():
    declaring = [recipe for recipe in RECIPES if DOCKER_APPLICATION_MANAGER in _dependencies(recipe)]
    assert declaring == [], (
        "{} must stay on the ECR publish path only, but these recipes declare it: {}".format(
            DOCKER_APPLICATION_MANAGER, declaring))
