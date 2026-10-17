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
"""Property 10 (triton-model-cleanup-wedge, D10): LocalServer's dependency on
``aws.greengrass.Cli`` is SOFT in every recipe variant and in the output of
each ECR publish rewriter, and the recipes ship
``ModelLoadSelfHealEnabled: "true"``.

The rewriters are the Python heredocs embedded in
``gdk-component-build-and-publish.sh``, ``portal-build.sh`` and
``publish-ecr-only.sh`` (between ``<<'PYEOF'`` and ``PYEOF``). Each is run on
a fixture recipe whose CLI dependency is HARD (or absent) and must write
SOFT, so a stale ``recipe.yaml`` can never republish the HARD dependency.
"""

import os
import subprocess
import sys

import pytest
import yaml

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))

CLI = "aws.greengrass.Cli"
SWITCH_KEY = "ModelLoadSelfHealEnabled"

RECIPES = (
    "recipe.yaml",
    "recipe-arm64-jp5.yaml",
    "recipe-arm64-jp6.yaml",
    "recipe-arm64-jp7.yaml",
    "recipe-arm64.yaml",
    "recipe-amd64.yaml",
    "recipe-amd64-nvidia.yaml",
)

# script -> True when its heredoc takes the component name argument
REWRITERS = {
    "gdk-component-build-and-publish.sh": False,
    "portal-build.sh": False,
    "publish-ecr-only.sh": True,
}

FIXTURE_RECIPE = "recipe-arm64-jp5.yaml"
FIXTURE_COMPONENT = "aws.edgeml.dda.LocalServer.arm64JP5"


def _load(rel):
    with open(os.path.join(REPO, rel), encoding="utf-8") as f:
        return yaml.safe_load(f)


def _heredoc(script):
    with open(os.path.join(REPO, script), encoding="utf-8") as f:
        text = f.read()
    start_marker = "<<'PYEOF'\n"
    assert text.count(start_marker) == 1, "%s: expected one <<'PYEOF' heredoc" % script
    body = text.split(start_marker, 1)[1]
    end = body.find("\nPYEOF\n")
    assert end >= 0, "%s: unterminated PYEOF heredoc" % script
    return body[:end + 1]


def _run_rewriter(script, takes_name, recipe_doc, tmp_path):
    src = tmp_path / "recipe.yaml"
    out = tmp_path / "recipe-ecr.yaml"
    prog = tmp_path / "rewriter.py"
    src.write_text(yaml.safe_dump(recipe_doc, sort_keys=False), encoding="utf-8")
    prog.write_text(_heredoc(script), encoding="utf-8")
    argv = [sys.executable, str(prog), str(src), str(out),
            "example.invalid/flask-app", "example.invalid/react-webapp",
            "1.0.99", "s3://example-bucket/artifacts.zip"]
    if takes_name:
        argv.append(FIXTURE_COMPONENT)
    argv.append("1.0.99-test")
    proc = subprocess.run(argv, cwd=str(tmp_path), capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, "%s rewriter failed: %s" % (script, proc.stderr)
    with open(str(out), encoding="utf-8") as f:
        return yaml.safe_load(f)


@pytest.mark.parametrize("recipe", RECIPES)
def test_recipe_declares_soft_cli_and_self_heal_default(recipe):
    doc = _load(recipe)
    cli = doc["ComponentDependencies"][CLI]
    assert cli == {"VersionRequirement": ">=2.6.0", "DependencyType": "SOFT"}
    config = doc["ComponentConfiguration"]["DefaultConfiguration"]
    assert config[SWITCH_KEY] == "true"


def test_recipe_yaml_is_the_jp6_recipe():
    with open(os.path.join(REPO, "recipe.yaml"), "rb") as a, \
            open(os.path.join(REPO, "recipe-arm64-jp6.yaml"), "rb") as b:
        assert a.read() == b.read()


@pytest.mark.parametrize("script", sorted(REWRITERS))
@pytest.mark.parametrize("fixture_type", ["HARD", None])
def test_rewriter_outputs_soft_for_a_hard_dependency(script, fixture_type, tmp_path):
    doc = _load(FIXTURE_RECIPE)
    cli = doc["ComponentDependencies"][CLI]
    if fixture_type is None:
        cli.pop("DependencyType", None)  # no type: Greengrass treats it as HARD
    else:
        cli["DependencyType"] = fixture_type
    out = _run_rewriter(script, REWRITERS[script], doc, tmp_path)
    deps = out["ComponentDependencies"]
    assert deps[CLI] == {"VersionRequirement": ">=2.6.0", "DependencyType": "SOFT"}
    # The rest of the dependency set is the rewriter's usual output.
    others = {k: v for k, v in doc["ComponentDependencies"].items() if k != CLI}
    for name, value in others.items():
        assert deps[name] == value
    assert deps["aws.greengrass.DockerApplicationManager"] == {"VersionRequirement": "~2.0.0"}
    assert out["ComponentConfiguration"]["DefaultConfiguration"][SWITCH_KEY] == "true"


@pytest.mark.parametrize("script", sorted(REWRITERS))
def test_rewriter_adds_a_soft_cli_dependency_when_absent(script, tmp_path):
    doc = _load(FIXTURE_RECIPE)
    del doc["ComponentDependencies"][CLI]
    out = _run_rewriter(script, REWRITERS[script], doc, tmp_path)
    assert out["ComponentDependencies"][CLI] == {"VersionRequirement": ">=2.6.0",
                                                 "DependencyType": "SOFT"}
