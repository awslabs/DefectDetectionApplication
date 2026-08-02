"""
Publish-side LocalServer dependency resolution (functions/greengrass_publish.py).

Regression coverage for the localserver-arch-naming feature: the model
publisher must map every known Compile_Target to its explicit JetPack-tagged
(or amd64) LocalServer variant, fail closed on an unresolved aarch64 target,
and NEVER stamp a model recipe (vision or vLLM) with the retired bare
``aws.edgeml.dda.LocalServer.arm64`` name.

These are exercised against the real functions/greengrass_publish.py module.
The resolver and recipe generators are pure, so no AWS calls are made; the
module is loaded under the moto-backed conftest env so its module-level boto3
clients bind harmlessly to the test stack.
"""
import importlib.util
import os
import sys

import pytest

BARE_ARM64 = "aws.edgeml.dda.LocalServer.arm64"
JP4 = "aws.edgeml.dda.LocalServer.arm64JP4"
JP5 = "aws.edgeml.dda.LocalServer.arm64JP5"
JP6 = "aws.edgeml.dda.LocalServer.arm64JP6"
AMD64 = "aws.edgeml.dda.LocalServer.amd64"

_PUBLISH_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..", "functions", "greengrass_publish.py")


@pytest.fixture(scope="module")
def pub(aws_stack):
    """Load functions/greengrass_publish.py under a distinct module name."""
    spec = importlib.util.spec_from_file_location(
        "portal_greengrass_publish_ls", _PUBLISH_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["portal_greengrass_publish_ls"] = module
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Property 1: known targets resolve to their explicit JetPack-tagged variant
# ---------------------------------------------------------------------------

class TestResolverMatrix:
    def test_full_known_target_matrix(self, pub):
        resolve = pub.resolve_local_server_component
        plat = pub.TARGET_TO_PLATFORM
        cases = {
            "jetson-xavier": JP4,          # was bare arm64 -> now explicit JP4
            "jetson-xavier-jp5": JP5,
            "jetson-xavier-jp6": JP6,
            "arm64-cpu": JP4,              # was bare arm64 -> now explicit JP4
            "x86_64-cpu": AMD64,
            "x86_64-cuda": AMD64,
        }
        for target, expected in cases.items():
            assert resolve(target, plat[target]) == expected

    def test_known_targets_never_return_bare_arm64(self, pub):
        for name in pub.TARGET_TO_LOCAL_SERVER.values():
            assert name != BARE_ARM64
            assert name.startswith("aws.edgeml.dda.LocalServer.")

    # -----------------------------------------------------------------
    # Property 2: unresolvable aarch64 targets fail closed
    # -----------------------------------------------------------------
    def test_unknown_aarch64_target_raises(self, pub):
        with pytest.raises(pub.PublishError):
            pub.resolve_local_server_component("some-new-jetson", "aarch64")

    def test_unknown_target_unknown_platform_raises(self, pub):
        with pytest.raises(pub.PublishError):
            pub.resolve_local_server_component("mystery", "riscv64")

    def test_missing_target_aarch64_raises(self, pub):
        # A missing/None target on aarch64 must not silently pick JP4.
        with pytest.raises(pub.PublishError):
            pub.resolve_local_server_component(None, "aarch64")

    def test_unknown_amd64_target_resolves_to_amd64(self, pub):
        # x86 has a single variant, so an unknown amd64 target is safe.
        assert pub.resolve_local_server_component(
            "future-x86", "amd64") == AMD64
        assert pub.resolve_local_server_component(None, "amd64") == AMD64


# ---------------------------------------------------------------------------
# Property 4: no published recipe (vision or vLLM) carries a bare-arm64 dep
# ---------------------------------------------------------------------------

def _dep_names(recipe):
    return set(recipe["ComponentDependencies"].keys())


class TestRecipeDependencies:
    @pytest.mark.parametrize("target,expected", [
        ("jetson-xavier", JP4),
        ("jetson-xavier-jp5", JP5),
        ("jetson-xavier-jp6", JP6),
        ("arm64-cpu", JP4),
        ("x86_64-cpu", AMD64),
        ("x86_64-cuda", AMD64),
    ])
    def test_vision_recipe_carries_explicit_variant(self, pub, target, expected):
        platform = pub.TARGET_TO_PLATFORM[target]
        recipe = pub.generate_component_recipe(
            component_name="model-example",
            component_version="1.0.0",
            friendly_name="Example",
            platform=platform,
            artifact_s3_uri="s3://bucket/model-example.zip",
            model_unarchived_path="model-example",
            target=target,
        )
        deps = _dep_names(recipe)
        assert expected in deps
        assert BARE_ARM64 not in deps

    @pytest.mark.parametrize("target,expected", [
        ("jetson-xavier", JP4),
        ("jetson-xavier-jp5", JP5),
        ("jetson-xavier-jp6", JP6),
        ("x86_64-cpu", AMD64),
    ])
    def test_vllm_recipe_carries_explicit_variant(self, pub, target, expected):
        platform = pub.TARGET_TO_PLATFORM[target]
        recipe = pub.generate_vllm_component_recipe(
            component_name="model-vllm-example",
            component_version="1.0.0",
            friendly_name="Example vLLM",
            platform=platform,
            artifact_s3_uri="s3://bucket/model-vllm-example.zip",
            repo_unarchived_path="model-vllm-example",
            model_name="example",
            target=target,
            supported_architectures=["arm64_jp6"],
        )
        deps = _dep_names(recipe)
        assert expected in deps
        assert BARE_ARM64 not in deps

    def test_vision_recipe_unresolvable_target_raises(self, pub):
        with pytest.raises(pub.PublishError):
            pub.generate_component_recipe(
                component_name="model-example",
                component_version="1.0.0",
                friendly_name="Example",
                platform="aarch64",
                artifact_s3_uri="s3://bucket/model-example.zip",
                model_unarchived_path="model-example",
                target="unknown-jetson",
            )

    def test_vllm_recipe_unresolvable_target_raises(self, pub):
        with pytest.raises(pub.PublishError):
            pub.generate_vllm_component_recipe(
                component_name="model-vllm-example",
                component_version="1.0.0",
                friendly_name="Example vLLM",
                platform="aarch64",
                artifact_s3_uri="s3://bucket/model-vllm-example.zip",
                repo_unarchived_path="model-vllm-example",
                model_name="example",
                target="unknown-jetson",
                supported_architectures=["arm64_jp6"],
            )


# ---------------------------------------------------------------------------
# The module exposes no code path that produces the bare arm64 name.
# ---------------------------------------------------------------------------

def test_module_source_has_no_bare_arm64_literal():
    with open(os.path.abspath(_PUBLISH_PATH), encoding="utf-8") as fh:
        source = fh.read()
    # The retired bare name must not appear as a produced string. It may only
    # appear (if at all) inside longer tagged names like arm64JP4 — assert no
    # standalone 'aws.edgeml.dda.LocalServer.arm64' followed by a quote/word
    # boundary (i.e. not immediately followed by JP4/JP5/JP6).
    import re
    # Match only a *quoted* bare name (a produced string literal), so the
    # retired-name reference in the module's explanatory comment (which is
    # backtick-quoted prose, allowed by the spec) does not trip the guard.
    # arm64JP4/JP5/JP6 have JP before the closing quote, so they are excluded.
    stray = re.findall(
        r"['\"]aws\.edgeml\.dda\.LocalServer\.arm64['\"]", source)
    assert stray == [], f"bare arm64 LocalServer literal(s) present: {stray}"
