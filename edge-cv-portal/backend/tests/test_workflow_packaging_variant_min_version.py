"""
Per-arch minimum LocalServer version stamped into Workflow_Component
manifests (functions/workflow_packaging.py).

LocalServer ships as independently-versioned per-architecture variants
whose version lineages are not comparable, so a single global minimum
falsely blocks the JetPack variants. build_manifest stamps an arch-scoped
scalar (minLocalServerVersion, for the package's own targetArch) plus the
full per-arch map (minLocalServerVersions) so a variant-aware device
selects the floor for its own lineage.
"""
import sys

import pytest


@pytest.fixture(scope="module")
def packaging(aws_stack):
    sys.modules.pop("workflow_packaging", None)
    import workflow_packaging

    return workflow_packaging


def _manifest(packaging, arch):
    return packaging.build_manifest(
        "wf-1", 1, arch,
        gst_plugins=[], python_packages=[],
        custom_python_nodes=[], user={"user_id": "user-1"})


class TestMinLocalServerVersionFor:
    def test_returns_per_arch_override_when_present(self, packaging, monkeypatch):
        monkeypatch.setattr(
            packaging, "MIN_LOCAL_SERVER_VERSIONS",
            {"arm64_jp6": "1.0.0", "arm64_jp5": "1.0.5"})
        assert packaging.min_local_server_version_for("arm64_jp6") == "1.0.0"
        assert packaging.min_local_server_version_for("arm64_jp5") == "1.0.5"

    def test_falls_back_to_scalar_for_unmapped_arch(self, packaging, monkeypatch):
        monkeypatch.setattr(
            packaging, "MIN_LOCAL_SERVER_VERSIONS", {"arm64_jp6": "1.0.0"})
        monkeypatch.setattr(
            packaging, "MIN_LOCAL_SERVER_VERSION", "1.0.63")
        assert packaging.min_local_server_version_for("x86_64") == "1.0.63"
        assert packaging.min_local_server_version_for(None) == "1.0.63"

    def test_arm64_jp4_keyed_like_other_arches(self, packaging, monkeypatch):
        """The arm64_jp4 lineage is gated by its own per-arch floor when
        present, independent of the JP5/JP6/x86 lineages (localserver-arch-
        naming Requirement 4.1)."""
        monkeypatch.setattr(
            packaging, "MIN_LOCAL_SERVER_VERSIONS",
            {"arm64_jp4": "1.0.10", "arm64_jp5": "1.0.5", "arm64_jp6": "1.0.0"})
        monkeypatch.setattr(
            packaging, "MIN_LOCAL_SERVER_VERSION", "1.0.63")
        # Its own floor, not the scalar and not another arch's floor.
        assert packaging.min_local_server_version_for("arm64_jp4") == "1.0.10"
        # JP5/JP6/x86 unchanged (Requirement 4.2).
        assert packaging.min_local_server_version_for("arm64_jp5") == "1.0.5"
        assert packaging.min_local_server_version_for("arm64_jp6") == "1.0.0"
        assert packaging.min_local_server_version_for("x86_64") == "1.0.63"

    def test_arm64_jp4_falls_back_to_scalar_when_unmapped(self, packaging, monkeypatch):
        """With no arm64_jp4 entry the scalar default applies, exactly as for
        any other unmapped arch (Requirement 4.1 scalar fallback)."""
        monkeypatch.setattr(
            packaging, "MIN_LOCAL_SERVER_VERSIONS", {"arm64_jp6": "1.0.0"})
        monkeypatch.setattr(
            packaging, "MIN_LOCAL_SERVER_VERSION", "1.0.63")
        assert packaging.min_local_server_version_for("arm64_jp4") == "1.0.63"
        # JP6 still gated by its own floor.
        assert packaging.min_local_server_version_for("arm64_jp6") == "1.0.0"


class TestManifestStampsVariantMinimums:
    def test_manifest_carries_arch_scalar_and_full_map(self, packaging, monkeypatch):
        monkeypatch.setattr(
            packaging, "MIN_LOCAL_SERVER_VERSIONS",
            {"arm64_jp6": "1.0.0", "arm64_jp5": "1.0.5"})
        monkeypatch.setattr(
            packaging, "MIN_LOCAL_SERVER_VERSION", "1.0.63")

        jp6 = _manifest(packaging, "arm64_jp6")
        # Scalar is the JP6-lineage floor (not the arm64-derived 1.0.63).
        assert jp6["minLocalServerVersion"] == "1.0.0"
        # Full map travels with the package for variant-aware devices.
        assert jp6["minLocalServerVersions"] == {
            "arm64_jp6": "1.0.0", "arm64_jp5": "1.0.5"}

        # A package for an unmapped arch falls back to the scalar default.
        x86 = _manifest(packaging, "x86_64")
        assert x86["minLocalServerVersion"] == "1.0.63"

    def test_parse_min_versions_map_rejects_non_json(self, packaging, monkeypatch):
        monkeypatch.setenv("WORKFLOW_MIN_LOCAL_SERVER_VERSIONS", "not-json{")
        assert packaging._parse_min_versions_map() == {}

    def test_parse_min_versions_map_reads_json_object(self, packaging, monkeypatch):
        monkeypatch.setenv(
            "WORKFLOW_MIN_LOCAL_SERVER_VERSIONS", '{"arm64_jp6": "1.0.0"}')
        assert packaging._parse_min_versions_map() == {"arm64_jp6": "1.0.0"}
