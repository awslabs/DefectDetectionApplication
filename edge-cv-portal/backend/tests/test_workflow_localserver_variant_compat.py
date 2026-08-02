"""
Variant-aware LocalServer compatibility (functions/deployments.py).

LocalServer ships as independently-versioned per-architecture variants
(aws.edgeml.dda.LocalServer.arm64 / .arm64JP5 / .arm64JP6 / .amd64) whose
version lineages are NOT comparable: the arm64 variant may be at 1.0.124
while arm64JP6 is at 1.0.35. A single global minimum is therefore
variant-blind and falsely blocks the JetPack variants (a JP6 device on
1.0.35 can never satisfy an arm64-derived "1.0.63").

These tests cover the fix: check_local_server_compatibility gates each
device against the minimum for its OWN variant lineage (derived from the
installed LocalServer component name via local_server_component_arch),
falling back to the scalar for archs not in the map.
"""
import sys

import pytest

from test_workflow_packaging_deployment_integration import FakeGreengrass


@pytest.fixture(scope="module")
def deployments(aws_stack):
    for module_name in ("deployments", "workflow_guards"):
        sys.modules.pop(module_name, None)
    import deployments

    return deployments


class TestLocalServerComponentArch:
    def test_maps_variant_suffixes_to_workflow_core_arch(self, deployments):
        f = deployments.local_server_component_arch
        p = "aws.edgeml.dda.LocalServer."
        assert f(p + "arm64JP6") == "arm64_jp6"
        assert f(p + "arm64JP5") == "arm64_jp5"
        assert f(p + "arm64") == "arm64_jp4"
        assert f(p + "aarch64") == "arm64_jp4"
        assert f(p + "amd64") == "x86_64"
        assert f(p + "x86_64") == "x86_64"

    def test_explicit_arm64jp4_maps_to_arm64_jp4(self, deployments):
        """The renamed explicit JetPack 4 variant (localserver-arch-naming
        Requirement 3.1)."""
        f = deployments.local_server_component_arch
        p = "aws.edgeml.dda.LocalServer."
        assert f(p + "arm64JP4") == "arm64_jp4"

    def test_legacy_bare_names_still_recognized(self, deployments):
        """Already-provisioned JP4 devices run the bare-named variant; the
        read side must keep recognizing it as arm64_jp4 (Requirement 3.3)."""
        f = deployments.local_server_component_arch
        p = "aws.edgeml.dda.LocalServer."
        assert f(p + "arm64") == "arm64_jp4"
        assert f(p + "aarch64") == "arm64_jp4"

    def test_arm64jp4_not_misread_as_legacy_arm64(self, deployments):
        """Token ordering: the longer arm64JP4 token must be matched before
        the bare arm64 prefix. Both resolve to arm64_jp4, but arm64JP4 must
        be recognized via its explicit JetPack token, not the legacy alias
        (Requirement 3, Property 3)."""
        f = deployments.local_server_component_arch
        p = "aws.edgeml.dda.LocalServer."
        # arm64JP4/JP5/JP6 must never fall through to the bare-arm64 branch.
        assert f(p + "arm64JP4") == "arm64_jp4"
        assert f(p + "arm64JP5") == "arm64_jp5"
        assert f(p + "arm64JP6") == "arm64_jp6"

    def test_jp5_jp6_x86_unchanged(self, deployments):
        f = deployments.local_server_component_arch
        p = "aws.edgeml.dda.LocalServer."
        assert f(p + "arm64JP5") == "arm64_jp5"
        assert f(p + "arm64JP6") == "arm64_jp6"
        assert f(p + "amd64") == "x86_64"
        assert f(p + "x86_64") == "x86_64"

    def test_non_localserver_or_empty_is_none(self, deployments):
        assert deployments.local_server_component_arch(None) is None
        assert deployments.local_server_component_arch("") is None
        assert deployments.local_server_component_arch(
            "aws.greengrass.Nucleus") is None


class TestVariantAwareCompatibility:
    def test_jp6_unblocked_by_per_arch_floor_despite_high_scalar(self, deployments):
        """The regression: a JP6 device on 1.0.35 must NOT be blocked by an
        arm64-lineage scalar of 1.0.63 when the per-arch map gives arm64_jp6
        its own (satisfiable) floor."""
        gg = FakeGreengrass()
        gg.register_device("jp6-dev", local_server_version="1.0.35",
                            arch="arm64JP6")
        incompatible = deployments.check_local_server_compatibility(
            gg, ["jp6-dev"], "1.0.63", {"arm64_jp6": "1.0.0"})
        assert incompatible == []

    def test_jp6_would_be_blocked_by_scalar_without_map(self, deployments):
        """Without a per-arch entry (empty map) the scalar applies and the
        same JP6 device is (incorrectly, pre-fix) reported incompatible —
        proving the map is what unblocks it."""
        gg = FakeGreengrass()
        gg.register_device("jp6-dev", local_server_version="1.0.35",
                            arch="arm64JP6")
        incompatible = deployments.check_local_server_compatibility(
            gg, ["jp6-dev"], "1.0.63", {})
        assert len(incompatible) == 1
        assert incompatible[0]["device"] == "jp6-dev"
        assert incompatible[0]["min_local_server_version"] == "1.0.63"
        assert "older" in incompatible[0]["reason"]

    def test_per_arch_floor_still_enforced_when_too_old(self, deployments):
        """A device below its OWN variant floor is still reported, with the
        arch-specific minimum in the reason (not the scalar)."""
        gg = FakeGreengrass()
        gg.register_device("jp6-dev", local_server_version="1.0.20",
                            arch="arm64JP6")
        incompatible = deployments.check_local_server_compatibility(
            gg, ["jp6-dev"], "1.0.0", {"arm64_jp6": "1.0.30"})
        assert len(incompatible) == 1
        assert incompatible[0]["min_local_server_version"] == "1.0.30"
        assert "1.0.30" in incompatible[0]["reason"]

    def test_arch_absent_from_map_falls_back_to_scalar(self, deployments):
        """An x86 device with no map entry is gated against the scalar."""
        gg = FakeGreengrass()
        gg.register_device("x86-dev", local_server_version="1.0.10",
                            arch="x86_64")
        incompatible = deployments.check_local_server_compatibility(
            gg, ["x86-dev"], "1.0.63", {"arm64_jp6": "1.0.0"})
        assert len(incompatible) == 1
        assert incompatible[0]["min_local_server_version"] == "1.0.63"

    def test_mixed_fleet_each_gated_against_own_lineage(self, deployments):
        """A mixed JP6 + x86 fleet: JP6 passes on its own low floor while a
        too-old x86 device is reported against the scalar."""
        gg = FakeGreengrass()
        gg.register_device("jp6-dev", local_server_version="1.0.35",
                            arch="arm64JP6")
        gg.register_device("x86-old", local_server_version="0.9.0",
                            arch="x86_64")
        incompatible = deployments.check_local_server_compatibility(
            gg, ["jp6-dev", "x86-old"], "1.0.63", {"arm64_jp6": "1.0.0"})
        assert [d["device"] for d in incompatible] == ["x86-old"]
