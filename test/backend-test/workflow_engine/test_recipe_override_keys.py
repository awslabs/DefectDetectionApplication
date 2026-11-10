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
"""The configuration-only workflow switches every LocalServer recipe
declares (bedrock-inspection-chain design 7): ``WorkflowNodeOverrides``
and ``WorkflowDisabled`` as empty maps under
``ComponentConfiguration.DefaultConfiguration`` of all seven recipes at the
repository root, and never referenced from ``Lifecycle``/``SetEnv`` — a
``{configuration:/Workflow...}`` interpolation there would turn every booth
switch into a component restart that loses an in-flight run.
"""
import json
from pathlib import Path

import pytest
import yaml

from workflow_engine.config_overrides import DISABLED_KEY, NODE_OVERRIDES_KEY

REPO_ROOT = Path(__file__).resolve().parents[3]
RECIPES = sorted(REPO_ROOT.glob("recipe*.yaml"))
FORBIDDEN_INTERPOLATION = "configuration:/Workflow"


def _load(recipe_path):
    with open(str(recipe_path), "r", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def test_the_seven_recipes_are_present():
    assert [p.name for p in RECIPES] == [
        "recipe-amd64-nvidia.yaml", "recipe-amd64.yaml", "recipe-arm64-jp5.yaml",
        "recipe-arm64-jp6.yaml", "recipe-arm64-jp7.yaml", "recipe-arm64.yaml", "recipe.yaml",
    ]


@pytest.mark.parametrize("recipe", RECIPES, ids=lambda p: p.name)
def test_both_keys_are_empty_maps_in_default_configuration(recipe):
    default_configuration = _load(recipe)["ComponentConfiguration"]["DefaultConfiguration"]
    assert default_configuration[NODE_OVERRIDES_KEY] == {}, recipe.name
    assert default_configuration[DISABLED_KEY] == {}, recipe.name
    # Beside the other feature switches, not inside accessControl.
    assert NODE_OVERRIDES_KEY not in default_configuration["accessControl"]
    keys = list(default_configuration)
    assert keys.index(NODE_OVERRIDES_KEY) == keys.index("ModelLoadSelfHealEnabled") + 1
    assert keys.index(DISABLED_KEY) == keys.index(NODE_OVERRIDES_KEY) + 1


@pytest.mark.parametrize("recipe", RECIPES, ids=lambda p: p.name)
def test_lifecycle_never_interpolates_the_switches(recipe):
    document = _load(recipe)
    for manifest in document["Manifests"]:
        lifecycle_text = json.dumps(manifest.get("Lifecycle", {}))
        assert FORBIDDEN_INTERPOLATION not in lifecycle_text, recipe.name
        assert "WorkflowNodeOverrides" not in lifecycle_text, recipe.name
        assert "WorkflowDisabled" not in lifecycle_text, recipe.name
    # The whole file, outside the two declarations and their comment.
    text = recipe.read_text(encoding="utf-8")
    assert FORBIDDEN_INTERPOLATION not in text, recipe.name


def test_recipe_yaml_is_the_jp6_recipe():
    """The gdk working copy stays byte-identical to the JP6 recipe (the
    triton_cleanup_wedge suite pins it too): seven files change together."""
    assert (REPO_ROOT / "recipe.yaml").read_bytes() == (REPO_ROOT / "recipe-arm64-jp6.yaml").read_bytes()
