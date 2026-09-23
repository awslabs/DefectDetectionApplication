"""
Unit tests for the Architecture_Addition route and the build-round
changes in plugin_builds.py (custom-node-source-lifecycle task 2.6).

Covers POST /plugins/{id}/versions/{v}/architectures (6.1-6.4), the
monotonic requested-architecture union on retries (6.5), the
Build_Target_Registry exposure and the 400 replacing the old 500 (6.7,
6.8), Source_Revision stamping and staleness in the builds view (1.8,
1.9), and imported-kind additions without scaffold rendering (10.4).
"""
import json

import pytest

from conftest import TEST_ENV
from test_plugin_builds import PluginBuildsEnv
from test_plugin_records import make_scaffold_declaration

ARCHS_ROUTE = "/plugins/{id}/versions/{v}/architectures"


@pytest.fixture
def benv(aws_stack):
    return PluginBuildsEnv(aws_stack)


def post_architectures(benv, user, plugin_id, version, architectures):
    event = benv._event("POST", ARCHS_ROUTE, user, plugin_id, version,
                        {"architectures": architectures})
    response = benv.module.handler(event, None)
    return response["statusCode"], json.loads(response["body"])


def source_keys(benv, plugin):
    prefix = plugin["source_s3_prefix"]
    response = benv.s3.list_objects_v2(Bucket=benv.bucket, Prefix=prefix)
    return sorted(o["Key"][len(prefix):] for o in response.get("Contents", []))


@pytest.fixture
def scaffold(benv):
    usecase_id = benv.create_usecase()
    admin = benv.make_admin(usecase_id)
    plugin = benv.create_plugin(
        admin, usecase_id,
        declaration=make_scaffold_declaration(architectures=["x86_64"]))
    return usecase_id, admin, plugin


class TestAddArchitectures:
    def test_adds_only_new_arches_and_renders_missing_meson(self, benv, scaffold):
        usecase_id, admin, plugin = scaffold
        plugin_id = plugin["plugin_id"]
        benv.submit_and_succeed(admin, plugin, arch="x86_64")
        before = benv.get_item(plugin_id, 1)
        assert source_keys(benv, plugin) == sorted([
            "README.md", "builds/x86_64/meson.build",
            "plugin/frame_processing_hook.py", "plugin/gstcustomblurregions.c"])

        status, body = post_architectures(benv, admin, plugin_id, 1,
                                          ["arm64_jp6", "x86_64_nvidia"])
        assert status == 202, body
        assert body["requested_architectures"] == ["arm64_jp6", "x86_64", "x86_64_nvidia"]
        assert body["builds"]["arm64_jp6"]["buildStatus"] == "building"
        assert body["builds"]["x86_64_nvidia"]["buildStatus"] == "building"
        # The existing artifact is untouched byte-for-byte.
        after = benv.get_item(plugin_id, 1)
        assert after["artifacts"]["x86_64"] == before["artifacts"]["x86_64"]
        assert "components_triggered" not in after

        # Build configurations rendered for the added architectures only.
        keys = source_keys(benv, plugin)
        assert "builds/arm64_jp6/meson.build" in keys
        assert "builds/x86_64_nvidia/meson.build" in keys
        declaration = json.loads(after["provenance"]["scaffoldDeclaration"])
        assert declaration["architectures"] == ["x86_64", "arm64_jp6", "x86_64_nvidia"]
        # Files were written, so the Source_Revision advanced and the old
        # x86_64 artifact (built at revision 1) now reads stale.
        assert after["source_revision"] == 2
        assert body["source_revision"] == 2
        assert body["builds"]["x86_64"]["stale"] is True
        assert body["builds"]["arm64_jp6"]["stale"] is False
        assert body["builds"]["arm64_jp6"]["sourceRevision"] == 2

        audit = benv.stack.tables.audit_log.scan()["Items"]
        entries = [e for e in audit if e["action"] == "add_plugin_architectures"]
        assert entries and sorted(entries[-1]["details"]["files_written"]) == [
            "builds/arm64_jp6/meson.build", "builds/x86_64_nvidia/meson.build"]

    def test_existing_meson_is_never_overwritten_and_no_revision_bump(self, benv, scaffold):
        usecase_id, admin, plugin = scaffold
        plugin_id = plugin["plugin_id"]
        key = plugin["source_s3_prefix"] + "builds/arm64_jp5/meson.build"
        benv.s3.put_object(Bucket=benv.bucket, Key=key, Body=b"# hand-written\n")

        status, body = post_architectures(benv, admin, plugin_id, 1, ["arm64_jp5"])
        assert status == 202, body
        stored = benv.s3.get_object(Bucket=benv.bucket, Key=key)["Body"].read()
        assert stored == b"# hand-written\n"
        assert body["source_revision"] == 1
        after = benv.get_item(plugin_id, 1)
        declaration = json.loads(after["provenance"]["scaffoldDeclaration"])
        assert declaration["architectures"] == ["x86_64", "arm64_jp5"]

    def test_imported_kind_adds_without_scaffold_rendering(self, benv):
        usecase_id = benv.create_usecase()
        admin = benv.make_admin(usecase_id)
        status, body = benv.invoke_records(
            "POST", "/plugins", admin,
            body={"usecase_id": usecase_id, "name": "imported-plugin",
                  "kind": "imported"})
        plugin = body["plugin"]
        status, body = post_architectures(benv, admin, plugin["plugin_id"], 1,
                                          ["arm64_jp6"])
        assert status == 202, body
        assert body["requested_architectures"] == ["arm64_jp6"]
        assert source_keys(benv, plugin) == []
        assert body["source_revision"] == 1

    @pytest.mark.parametrize("arches, reason", [
        (["riscv64"], "unknown"),
        (["x86_64"], "already_requested"),
        (["arm64_jp7"], "unavailable"),
    ])
    def test_rejections_carry_a_reason_per_arch(self, benv, scaffold, arches, reason):
        usecase_id, admin, plugin = scaffold
        plugin_id = plugin["plugin_id"]
        benv.post_build(admin, plugin_id, 1, {"architectures": ["x86_64"]})
        # Settle the x86_64 build so BUILDS_IN_PROGRESS does not mask the
        # validation outcome.
        benv.stack.tables.plugin_records.update_item(
            Key={"plugin_id": plugin_id, "version": 1},
            UpdateExpression="SET artifacts.#a.buildStatus = :s",
            ExpressionAttributeNames={"#a": "x86_64"},
            ExpressionAttributeValues={":s": "failed"})
        status, body = post_architectures(benv, admin, plugin_id, 1, arches)
        assert status == 400
        assert body["error"]["code"] == "INVALID_ARCHITECTURES"
        assert body["error"]["details"]["rejected"] == {arches[0]: reason}
        assert body["error"]["details"]["buildable"] == sorted(
            json.loads(TEST_ENV["BUILD_PROJECTS_JSON"]))

    def test_deepstream_restriction(self, benv):
        usecase_id = benv.create_usecase()
        admin = benv.make_admin(usecase_id)
        plugin = benv.create_plugin(admin, usecase_id, deepstream=True)
        status, body = post_architectures(benv, admin, plugin["plugin_id"], 1,
                                          ["x86_64", "arm64_jp6"])
        assert status == 400
        assert body["error"]["details"]["rejected"] == {"x86_64": "deepstream_restricted"}

    def test_prod_versions_are_locked(self, benv, scaffold):
        usecase_id, admin, plugin = scaffold
        benv.stack.tables.plugin_records.update_item(
            Key={"plugin_id": plugin["plugin_id"], "version": 1},
            UpdateExpression="SET lifecycle_state = :s",
            ExpressionAttributeValues={":s": "prod"})
        status, body = post_architectures(benv, admin, plugin["plugin_id"], 1,
                                          ["arm64_jp6"])
        assert status == 409
        assert body["error"]["code"] == "LIFECYCLE_LOCKED"

    def test_test_versions_are_allowed(self, benv, scaffold):
        usecase_id, admin, plugin = scaffold
        benv.stack.tables.plugin_records.update_item(
            Key={"plugin_id": plugin["plugin_id"], "version": 1},
            UpdateExpression="SET lifecycle_state = :s",
            ExpressionAttributeValues={":s": "test"})
        status, _ = post_architectures(benv, admin, plugin["plugin_id"], 1,
                                       ["arm64_jp6"])
        assert status == 202

    def test_in_flight_builds_block_additions(self, benv, scaffold):
        usecase_id, admin, plugin = scaffold
        benv.post_build(admin, plugin["plugin_id"], 1, {"architectures": ["x86_64"]})
        status, body = post_architectures(benv, admin, plugin["plugin_id"], 1,
                                          ["arm64_jp6"])
        assert status == 409
        assert body["error"]["code"] == "BUILDS_IN_PROGRESS"
        assert body["error"]["details"]["architectures"] == ["x86_64"]

    def test_requires_manage_permission(self, benv, scaffold):
        usecase_id, admin, plugin = scaffold
        operator = benv.make_user()
        benv.assign_role(operator, usecase_id, "Operator")
        status, body = post_architectures(benv, operator, plugin["plugin_id"], 1,
                                          ["arm64_jp6"])
        assert status == 403

    def test_invalid_body(self, benv, scaffold):
        usecase_id, admin, plugin = scaffold
        for bad in ({}, {"architectures": []}, {"architectures": "x86_64"}):
            event = benv._event("POST", ARCHS_ROUTE, admin, plugin["plugin_id"], 1, bad)
            response = benv.module.handler(event, None)
            assert response["statusCode"] == 400
            assert json.loads(response["body"])["error"]["code"] == "INVALID_ARCHITECTURES"


class TestBuildRoundSemantics:
    def test_retry_of_a_subset_keeps_the_union(self, benv, scaffold):
        usecase_id, admin, plugin = scaffold
        plugin_id = plugin["plugin_id"]
        status, body = benv.post_build(admin, plugin_id, 1,
                                       {"architectures": ["x86_64", "arm64_jp5"]})
        assert status == 202
        assert body["requested_architectures"] == ["arm64_jp5", "x86_64"]
        status, body = benv.post_build(admin, plugin_id, 1,
                                       {"architectures": ["arm64_jp5"]})
        assert status == 202
        assert body["requested_architectures"] == ["arm64_jp5", "x86_64"]
        assert body["settled"] is False

    def test_unavailable_target_is_a_client_error(self, benv, scaffold):
        usecase_id, admin, plugin = scaffold
        status, body = benv.post_build(admin, plugin["plugin_id"], 1,
                                       {"architectures": ["arm64_jp7"]})
        assert status == 400
        assert body["error"]["code"] == "BUILD_TARGET_UNAVAILABLE"
        assert body["error"]["details"]["architectures"] == ["arm64_jp7"]
        assert "x86_64" in body["error"]["details"]["buildable"]

    def test_builds_view_exposes_registry_revision_and_component(self, benv, scaffold):
        usecase_id, admin, plugin = scaffold
        plugin_id = plugin["plugin_id"]
        benv.submit_and_succeed(admin, plugin, arch="x86_64")
        status, body = benv.get_builds(admin, plugin_id, 1)
        assert status == 200
        assert body["buildable_architectures"] == sorted(
            json.loads(TEST_ENV["BUILD_PROJECTS_JSON"]))
        assert body["source_revision"] == 1
        assert body["stale_architectures"] == []
        assert body["builds"]["x86_64"]["sourceRevision"] == 1
        assert body["builds"]["x86_64"]["stale"] is False
        assert body["component"]["revision"] == 0
        assert body["component"]["architectures"] == []

    def test_save_then_rebuild_clears_staleness(self, benv, scaffold):
        usecase_id, admin, plugin = scaffold
        plugin_id = plugin["plugin_id"]
        benv.submit_and_succeed(admin, plugin, arch="x86_64")
        # A save bumps the revision: the artifact goes stale.
        source_route = "/plugins/{id}/versions/{v}/source"
        status, body = benv.invoke_records(
            "PUT", source_route, admin, plugin_id, 1,
            body={"files": {"docs/NOTE.md": "n"}, "mode": "merge"})
        assert status == 200 and body["stale_architectures"] == ["x86_64"]
        status, body = benv.get_builds(admin, plugin_id, 1)
        assert body["builds"]["x86_64"]["stale"] is True
        # Rebuilding stamps the new revision and the settled result keeps it.
        build_id, _ = benv.submit_and_succeed(admin, plugin, arch="x86_64")
        status, body = benv.get_builds(admin, plugin_id, 1)
        assert body["builds"]["x86_64"]["sourceRevision"] == 2
        assert body["builds"]["x86_64"]["stale"] is False
        assert body["stale_architectures"] == []

    def test_legacy_entries_without_revision_are_not_stale(self, benv, scaffold):
        usecase_id, admin, plugin = scaffold
        plugin_id = plugin["plugin_id"]
        benv.stack.tables.plugin_records.update_item(
            Key={"plugin_id": plugin_id, "version": 1},
            UpdateExpression="SET artifacts.#a = :e REMOVE source_revision",
            ExpressionAttributeNames={"#a": "x86_64"},
            ExpressionAttributeValues={":e": {"buildStatus": "succeeded",
                                              "checksum": "ab" * 32}})
        status, body = benv.get_builds(admin, plugin_id, 1)
        assert body["source_revision"] == 1
        assert body["builds"]["x86_64"]["stale"] is False
