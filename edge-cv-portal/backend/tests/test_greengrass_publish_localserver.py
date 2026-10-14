"""
Publish-side LocalServer dependency resolution (functions/greengrass_publish.py).

Regression coverage for the localserver-arch-naming feature: the model
publisher must map every known Compile_Target to its explicit JetPack-tagged
(or amd64) LocalServer variant and fail closed on an unresolved aarch64
target. The bare ``aws.edgeml.dda.LocalServer.arm64`` name is the generic
arm64 CPU (non-Jetson) build: it is produced ONLY for the explicit
``arm64-cpu`` target, never as a catch-all. JetPack 4 (the bare
``jetson-xavier`` target and its ``arm64JP4`` LocalServer) is no longer
supported and fails closed like any other unknown aarch64 target.

These are exercised against the real functions/greengrass_publish.py module.
The resolver and recipe generators are pure, so no AWS calls are made; the
module is loaded under the moto-backed conftest env so its module-level boto3
clients bind harmlessly to the test stack.
"""
import importlib.util
import os
import sys

import pytest

ARM64_CPU = "aws.edgeml.dda.LocalServer.arm64"
RETIRED_JP4 = "aws.edgeml.dda.LocalServer.arm64JP4"
JP5 = "aws.edgeml.dda.LocalServer.arm64JP5"
JP6 = "aws.edgeml.dda.LocalServer.arm64JP6"
JP7 = "aws.edgeml.dda.LocalServer.arm64JP7"
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
# Property 1: known targets resolve to their explicit variant
# ---------------------------------------------------------------------------

class TestResolverMatrix:
    def test_full_known_target_matrix(self, pub):
        resolve = pub.resolve_local_server_component
        plat = pub.TARGET_TO_PLATFORM
        cases = {
            "jetson-xavier-jp5": JP5,
            "jetson-xavier-jp6": JP6,
            "jetson-xavier-jp7": JP7,
            "arm64-cpu": ARM64_CPU,        # the generic arm64 CPU build
            "x86_64-cpu": AMD64,
            "x86_64-cuda": AMD64,
        }
        for target, expected in cases.items():
            assert resolve(target, plat[target]) == expected

    def test_only_arm64_cpu_maps_to_the_bare_arm64_name(self, pub):
        bare = [target for target, name in pub.TARGET_TO_LOCAL_SERVER.items()
                if name == ARM64_CPU]
        assert bare == ["arm64-cpu"]
        for name in pub.TARGET_TO_LOCAL_SERVER.values():
            assert name.startswith("aws.edgeml.dda.LocalServer.")
            assert name != RETIRED_JP4

    # -----------------------------------------------------------------
    # Property 2: unresolvable aarch64 targets fail closed
    # -----------------------------------------------------------------
    def test_unknown_aarch64_target_raises(self, pub):
        with pytest.raises(pub.PublishError):
            pub.resolve_local_server_component("some-new-jetson", "aarch64")

    def test_retired_jetpack4_target_fails_closed(self, pub):
        # The bare 'jetson-xavier' (JetPack 4) target has no mapping any
        # more: it must neither resolve to the arm64 CPU build nor to a
        # JetPack variant.
        assert "jetson-xavier" not in pub.TARGET_TO_LOCAL_SERVER
        assert "jetson-xavier" not in pub.TARGET_TO_PLATFORM
        with pytest.raises(pub.PublishError):
            pub.resolve_local_server_component("jetson-xavier", "aarch64")
        with pytest.raises(pub.PublishError):
            pub.resolve_target_platform("jetson-xavier")

    def test_unknown_target_unknown_platform_raises(self, pub):
        with pytest.raises(pub.PublishError):
            pub.resolve_local_server_component("mystery", "riscv64")

    def test_missing_target_aarch64_raises(self, pub):
        # A missing/None target on aarch64 must not silently pick a variant
        # (neither a JetPack one nor the arm64 CPU build).
        with pytest.raises(pub.PublishError):
            pub.resolve_local_server_component(None, "aarch64")

    def test_unknown_amd64_target_resolves_to_amd64(self, pub):
        # x86 has a single variant, so an unknown amd64 target is safe.
        assert pub.resolve_local_server_component(
            "future-x86", "amd64") == AMD64
        assert pub.resolve_local_server_component(None, "amd64") == AMD64


# ---------------------------------------------------------------------------
# Property 4: published recipes (vision or vLLM) carry the explicit variant
# ---------------------------------------------------------------------------

def _dep_names(recipe):
    return set(recipe["ComponentDependencies"].keys())


class TestRecipeDependencies:
    @pytest.mark.parametrize("target,expected", [
        ("jetson-xavier-jp5", JP5),
        ("jetson-xavier-jp6", JP6),
        ("jetson-xavier-jp7", JP7),
        ("arm64-cpu", ARM64_CPU),
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
        assert RETIRED_JP4 not in deps
        if target != "arm64-cpu":
            assert ARM64_CPU not in deps

    @pytest.mark.parametrize("target,expected", [
        ("jetson-xavier-jp5", JP5),
        ("jetson-xavier-jp6", JP6),
        ("jetson-xavier-jp7", JP7),
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
        assert ARM64_CPU not in deps
        assert RETIRED_JP4 not in deps

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
# The bare arm64 name is produced only through the arm64-cpu mapping.
# ---------------------------------------------------------------------------

def test_module_source_names_the_bare_arm64_build_exactly_once():
    with open(os.path.abspath(_PUBLISH_PATH), encoding="utf-8") as fh:
        source = fh.read()
    import re
    # A *quoted* bare name is a produced string literal. It may appear
    # exactly once — the ARM64_CPU_LOCAL_SERVER constant that only the
    # 'arm64-cpu' target maps to — so no other code path can fall back to
    # it. Tagged names (arm64JP5/JP6/JP7) have JP before the closing quote
    # and are not matched.
    stray = re.findall(
        r"['\"]aws\.edgeml\.dda\.LocalServer\.arm64['\"]", source)
    assert len(stray) == 1, f"bare arm64 LocalServer literals: {stray}"
    assert re.search(
        r"^ARM64_CPU_LOCAL_SERVER = ['\"]aws\.edgeml\.dda\.LocalServer\.arm64['\"]$",
        source, re.M)
    # The retired JetPack 4 LocalServer is never a produced literal.
    assert not re.findall(r"['\"][^'\"\n]*arm64JP4[^'\"\n]*['\"]", source)
