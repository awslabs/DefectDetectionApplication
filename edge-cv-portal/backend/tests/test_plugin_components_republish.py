"""
Unit tests for Plugin_Component republish after artifact changes in
plugin_components.py (custom-node-source-lifecycle task 3.3).

Covers artifact-set change detection (8.1, 8.2), Component_Revision
numbering and the pointer fields (8.3), immutability of prior component
versions (8.4), the deployment gate following the updated pointer (8.5),
the workflow pin range tolerating patch versions (8.6), the arm64_jp7
platform manifest (7.5), and legacy pointer compatibility (10.2).
"""
import hashlib
import json
import sys

import pytest

from test_plugin_components import PluginComponentsEnv, components_module  # noqa: F401


@pytest.fixture
def cenv(aws_stack, components_module, monkeypatch):
    return PluginComponentsEnv(aws_stack, components_module, monkeypatch)


def rebuild_arch(cenv, plugin, arch, data):
    """Simulate a rebuilt (or newly built) artifact for `arch`."""
    key = f"workflow-plugins/custom/{cenv.usecase_id}/{arch}/blur-regions.so"
    cenv.s3.put_object(Bucket=cenv.bucket, Key=key, Body=data)
    cenv.stack.tables.plugin_records.update_item(
        Key={"plugin_id": plugin["plugin_id"], "version": plugin["version"]},
        UpdateExpression="SET artifacts.#a = :e",
        ExpressionAttributeNames={"#a": arch},
        ExpressionAttributeValues={":e": {
            "buildStatus": "succeeded", "s3Key": key,
            "checksum": hashlib.sha256(data).hexdigest(),
            "signature": "c2ln", "logTail": ""}})


class TestRepublish:
    def test_unchanged_artifacts_short_circuit(self, cenv):
        plugin = cenv.seed_plugin(["x86_64"])
        gg = cenv.patch_usecase_clients()
        first = cenv.package(plugin)
        assert first["packaged"] is True
        assert first["component_version"] == "1.0.0"
        assert first["component_revision"] == 0
        retry = cenv.package(plugin)
        assert retry["short_circuited"] is True
        assert retry["component_version"] == "1.0.0"
        assert gg.create_component_version.call_count == 1

        pointer = cenv.get_item(plugin)["component"]
        assert pointer["revision"] == 0
        assert pointer["artifact_checksums"] == {
            "x86_64": hashlib.sha256(cenv.so_bytes["x86_64"]).hexdigest()}

    def test_rebuilt_checksum_publishes_next_revision_and_keeps_prior(self, cenv):
        plugin = cenv.seed_plugin(["x86_64"])
        gg = cenv.patch_usecase_clients()
        first = cenv.package(plugin)
        prior_keys = cenv.account_keys(
            f"plugins/components/{plugin['plugin_id']}/1/")
        prior_bodies = {
            k: cenv.s3.get_object(Bucket=cenv.usecase_bucket, Key=k)["Body"].read()
            for k in prior_keys}

        rebuild_arch(cenv, plugin, "x86_64", b"\x7fELF rebuilt")
        second = cenv.package(plugin)
        assert second["packaged"] is True and "short_circuited" not in second
        assert second["component_version"] == "1.0.1"
        assert second["component_revision"] == 1
        assert gg.create_component_version.call_count == 2

        recipe, _tags = cenv.sent_recipe(gg)
        assert recipe["ComponentVersion"] == "1.0.1"
        for manifest in recipe["Manifests"]:
            for artifact in manifest["Artifacts"]:
                assert f"/plugins/components/{plugin['plugin_id']}/1/r1/" in artifact["Uri"]
            # The device install directory is revision-independent.
            assert f"/aws_dda/plugins/{plugin['plugin_id']}/1/x86_64" in \
                manifest["Lifecycle"]["Install"]["Script"]

        # v1.0.0's artifacts are byte-identical after the republish (8.4).
        for key, body in prior_bodies.items():
            assert cenv.s3.get_object(Bucket=cenv.usecase_bucket, Key=key)["Body"].read() == body
        gg.delete_component.assert_not_called()

        pointer = cenv.get_item(plugin)["component"]
        assert pointer["version"] == "1.0.1" and pointer["revision"] == 1
        assert pointer["artifact_checksums"]["x86_64"] == hashlib.sha256(
            b"\x7fELF rebuilt").hexdigest()
        # Third call: nothing changed again.
        assert cenv.package(plugin)["short_circuited"] is True

    def test_added_architecture_publishes_union_of_manifests(self, cenv):
        plugin = cenv.seed_plugin(["x86_64"])
        gg = cenv.patch_usecase_clients()
        cenv.package(plugin)
        rebuild_arch(cenv, plugin, "arm64_jp7", b"\x7fELF jp7")
        result = cenv.package(plugin)
        assert result["component_version"] == "1.0.1"
        assert result["architectures"] == ["arm64_jp7", "x86_64"]
        recipe, _ = cenv.sent_recipe(gg)
        platforms = [m["Platform"] for m in recipe["Manifests"]]
        assert {"os": "linux", "architecture": "aarch64", "variant": "arm64_jp7"} in platforms
        assert {"os": "linux", "architecture": "amd64"} in platforms
        pointer = cenv.get_item(plugin)["component"]
        assert pointer["architectures"] == ["arm64_jp7", "x86_64"]

    def test_legacy_registered_pointer_compares_by_architecture_set(self, cenv):
        plugin = cenv.seed_plugin(["x86_64"])
        gg = cenv.patch_usecase_clients()
        cenv.package(plugin)
        # Strip the fields this feature introduced to emulate a component
        # published before revisions existed.
        cenv.stack.tables.plugin_records.update_item(
            Key={"plugin_id": plugin["plugin_id"], "version": 1},
            UpdateExpression="REMOVE component.revision, component.artifact_checksums")
        assert cenv.package(plugin)["short_circuited"] is True
        rebuild_arch(cenv, plugin, "arm64_jp6", b"\x7fELF jp6")
        result = cenv.package(plugin)
        assert result["component_version"] == "1.0.1"
        assert gg.create_component_version.call_count == 2

    def test_failed_publish_retries_on_the_same_revision(self, cenv):
        plugin = cenv.seed_plugin(["x86_64"])
        gg = cenv.patch_usecase_clients()
        cenv.package(plugin)
        rebuild_arch(cenv, plugin, "x86_64", b"\x7fELF v2")
        gg.describe_component.return_value = {
            "status": {"componentState": "FAILED", "message": "boom"}}
        failed = cenv.package(plugin)
        assert failed["packaged"] is False
        pointer = cenv.get_item(plugin)["component"]
        assert pointer["status"] == "failed" and pointer["revision"] == 1
        assert pointer["version"] == "1.0.1"
        # Nothing of the failed r1 publish remains, v1.0.0 is intact.
        assert cenv.account_keys(f"plugins/components/{plugin['plugin_id']}/1/r1/") == []
        assert cenv.account_keys(f"plugins/components/{plugin['plugin_id']}/1/x86_64/")
        gg.describe_component.return_value = {
            "status": {"componentState": "DEPLOYABLE", "message": ""}}
        retried = cenv.package(plugin)
        assert retried["component_version"] == "1.0.1"


class TestDownstreamCompatibility:
    def test_deployment_gate_reads_the_updated_pointer(self, cenv):
        sys.modules.pop("deployments", None)
        import deployments
        plugin = cenv.seed_plugin(["x86_64"])
        cenv.patch_usecase_clients()
        cenv.package(plugin)
        rebuild_arch(cenv, plugin, "arm64_jp7", b"\x7fELF jp7")
        cenv.package(plugin)
        record = cenv.get_item(plugin)
        assert deployments.plugin_component_architectures(record) == ["arm64_jp7", "x86_64"]
        plugin_id, record_version = deployments.parse_plugin_component_ref(
            f"dda.plugin.{plugin['plugin_id']}", "1.0.1")
        assert (plugin_id, record_version) == (plugin["plugin_id"], 1)

    def test_workflow_pin_range_covers_every_revision(self, components_module):
        sys.modules.pop("workflow_packaging", None)
        import workflow_packaging
        assert workflow_packaging.plugin_version_requirement(3) == ">=3.0.0 <4.0.0"
        for revision in (0, 1, 7):
            version = components_module.component_version_for(3, revision)
            major, minor, patch = (int(x) for x in version.split("."))
            assert (major, minor, patch) >= (3, 0, 0)
            assert (major, minor, patch) < (4, 0, 0)

    def test_components_listing_parses_patch_versions(self, components_module):
        sys.modules.pop("components", None)
        import components
        assert components.plugin_version_from_component_version("5.0.3") == 5
