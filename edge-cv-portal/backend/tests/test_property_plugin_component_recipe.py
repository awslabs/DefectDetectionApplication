"""Property test for Plugin_Component recipe assembly (task 6.4).

**Feature: custom-node-designer, Property 20: Plugin_Component manifests are exactly the built architectures**

For all Plugin_Record versions with random per-architecture build
outcomes (over x86_64, x86_64_nvidia, arm64_jp4/jp5/jp6, at least one
success), the assembled Plugin_Component recipe is named
``dda.plugin.{pluginId}`` at version ``{pluginVersion}.0.0``, is
install-only, and contains exactly one platform manifest per
successfully built Target_Architecture - each with the correct
Greengrass platform attributes (amd64/aarch64, ``variant`` for the
JetPack builds, ``runtime: nvidia`` for x86_64_nvidia) - and no
manifest for any failed or unselected architecture.

**Validates: Requirements 16.1**

``build_plugin_recipe`` and its helpers are pure over
(plugin_id, plugin_version, bucket, arch_so_names), so the recipe is
exercised directly with no AWS involvement. The module is imported
through the shared moto-backed session fixture only so the real
``shared_utils`` / ``workflow_packaging`` layers (not test fakes) back
the import, mirroring test_plugin_components.py.
"""

from __future__ import annotations

import sys

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st


@pytest.fixture(scope="module")
def components_module(aws_stack):
    """Import plugin_components inside the moto mock so its module-level
    boto3 clients (and workflow_packaging's) are intercepted."""
    for name in ("plugin_components", "workflow_packaging"):
        sys.modules.pop(name, None)
    import plugin_components

    return plugin_components


# ---------------------------------------------------------------------------
# Reference expectations, restated from Requirement 16.1 / the design
# (not imported from the implementation, so the test cannot silently
# agree with a wrong platform map).
# ---------------------------------------------------------------------------

ARCHS = ("x86_64", "x86_64_nvidia", "arm64_jp4", "arm64_jp5", "arm64_jp6")

EXPECTED_PLATFORM = {
    "x86_64": {"os": "linux", "architecture": "amd64"},
    "x86_64_nvidia": {"os": "linux", "architecture": "amd64",
                      "runtime": "nvidia"},
    "arm64_jp4": {"os": "linux", "architecture": "aarch64",
                  "variant": "arm64_jp4"},
    "arm64_jp5": {"os": "linux", "architecture": "aarch64",
                  "variant": "arm64_jp5"},
    "arm64_jp6": {"os": "linux", "architecture": "aarch64",
                  "variant": "arm64_jp6"},
}

PLUGIN_MANIFEST_FILENAME = "plugin-manifest.json"


def _arch_of(platform):
    """Recover the Target_Architecture a manifest platform targets.

    The expected platform blocks are pairwise distinct, so this mapping
    is well-defined; an unrecognized platform fails the test.
    """
    matches = [arch for arch, expected in EXPECTED_PLATFORM.items()
               if platform == expected]
    assert len(matches) == 1, f"unrecognized platform block: {platform}"
    return matches[0]


# ---------------------------------------------------------------------------
# Strategies: random nonempty built-arch subsets with random ids,
# versions, bucket, and per-arch .so names.
# ---------------------------------------------------------------------------

_name_alphabet = "abcdefghijklmnopqrstuvwxyz0123456789-"
_names = st.text(alphabet=_name_alphabet, min_size=1, max_size=24)

built_arch_sets = st.frozensets(st.sampled_from(ARCHS), min_size=1)


@st.composite
def recipe_inputs(draw):
    archs = draw(built_arch_sets)
    return {
        "plugin_id": draw(_names),
        "plugin_version": draw(st.integers(min_value=1, max_value=9999)),
        "bucket": draw(_names),
        "arch_so_names": {arch: draw(_names) + ".so" for arch in archs},
    }


# ---------------------------------------------------------------------------
# Property 20
# ---------------------------------------------------------------------------

@settings(max_examples=25, deadline=None)
@given(inputs=recipe_inputs())
def test_manifests_are_exactly_the_built_architectures(components_module,
                                                       inputs):
    """**Feature: custom-node-designer, Property 20: Plugin_Component manifests are exactly the built architectures**

    For all nonempty built-architecture subsets with random plugin ids,
    versions, buckets, and .so names, build_plugin_recipe produces a
    dda.plugin.{pluginId} v{pluginVersion}.0.0 install-only recipe whose
    platform manifests correspond bijectively to the built
    architectures, each with the correct platform attributes, artifact
    URIs, and install lifecycle, with plain x86_64 ordered after
    x86_64_nvidia.

    **Validates: Requirements 16.1**
    """
    plugin_id = inputs["plugin_id"]
    plugin_version = inputs["plugin_version"]
    bucket = inputs["bucket"]
    arch_so_names = inputs["arch_so_names"]
    built = set(arch_so_names)

    recipe = components_module.build_plugin_recipe(
        plugin_id, plugin_version, bucket, arch_so_names)

    # Component identity derived from the Plugin_Record (16.1).
    assert recipe["ComponentName"] == f"dda.plugin.{plugin_id}"
    assert recipe["ComponentVersion"] == f"{plugin_version}.0.0"

    # Install-only: no top-level Run lifecycle exists.
    assert recipe["Lifecycle"] == {}

    # Bijection: exactly one manifest per built architecture, none for
    # any failed or unselected architecture.
    derived_archs = [_arch_of(m["Platform"]) for m in recipe["Manifests"]]
    assert len(derived_archs) == len(built)
    assert set(derived_archs) == built

    # Ordering: the plain x86_64 manifest comes after x86_64_nvidia so
    # NVIDIA amd64 devices match the more specific manifest first.
    if "x86_64" in built and "x86_64_nvidia" in built:
        assert (derived_archs.index("x86_64")
                > derived_archs.index("x86_64_nvidia"))

    for manifest, arch in zip(recipe["Manifests"], derived_archs):
        so_name = arch_so_names[arch]

        # Platform attributes exactly as required (amd64/aarch64,
        # variant for JetPack arm64, runtime: nvidia for x86_64_nvidia).
        assert manifest["Platform"] == EXPECTED_PLATFORM[arch]

        # Install-only lifecycle per manifest.
        assert list(manifest["Lifecycle"].keys()) == ["Install"]
        install = manifest["Lifecycle"]["Install"]
        assert install["requiresPrivilege"] is True

        # Install script targets the versioned per-arch device dir and
        # copies both artifacts there.
        install_dir = (f"/aws_dda/plugins/{plugin_id}/{plugin_version}/"
                       f"{arch}")
        script = install["Script"]
        assert install_dir in script
        assert so_name in script
        assert PLUGIN_MANIFEST_FILENAME in script

        # Artifacts: the signed .so plus plugin-manifest.json under the
        # account bucket's versioned per-arch component prefix.
        final_prefix = (f"plugins/components/{plugin_id}/{plugin_version}/"
                        f"{arch}")
        assert [a["Uri"] for a in manifest["Artifacts"]] == [
            f"s3://{bucket}/{final_prefix}/{so_name}",
            f"s3://{bucket}/{final_prefix}/{PLUGIN_MANIFEST_FILENAME}",
        ]
