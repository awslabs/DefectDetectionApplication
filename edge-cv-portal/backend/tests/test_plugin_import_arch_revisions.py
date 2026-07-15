"""
Unit tests for per-architecture import revisions (plugin_importer
arch_revisions + plugin_builds per-arch source resolution).

Motivating scenario: importing gst-plugins-good for every platform
needs DIFFERENT source revisions per platform generation — the default
branch for the GStreamer 1.20+ platforms (x86_64 / x86_64_nvidia /
arm64_jp6), branch '1.16' for arm64_jp5, branch '1.14' for arm64_jp4.

Covers:
- validation of the optional POST /plugins/import `arch_revisions`
  map (unknown architectures, non-string / empty values)
- the pure revision plan (revision_slug, revision_fetch_plan): a single
  distinct effective revision collapses to today's single-fetch flat
  layout exactly; multiple distinct revisions fetch once each into
  rev-{slug}/ prefixes with every arch mapped to its slug
- multi-revision fetch fan-out: one StartBuild per DISTINCT effective
  revision with the right DEST_PREFIX / REVISION / REVISION_SLUG env
  overrides, archs sharing a revision sharing the fetch
- handle_fetch_result partial settling: one fetch settles -> the record
  stays 'fetching' with the slug recorded; all succeed -> imported (or
  pending_selection for plugin sets) with builds started; any failure ->
  'failed' with a finding naming the failing revision while per-fetch
  statuses stay visible; per-slug idempotency on duplicate deliveries
- plugin_builds.start_builds resolving each arch's
  sourceLocationOverride through arch_revisions -> fetches[slug]
  .source_prefix, falling back to source_s3_prefix
- version_detail exposing arch_revisions and fetches when present

Runs against the moto-backed stack from conftest.py, reusing the
facades from test_plugin_importer.py / test_plugin_builds.py.
"""
import pytest

from conftest import TEST_ENV
from test_plugin_importer import (
    ImporterEnv,
    MESON_PLUGIN,
    RecordingCodeBuild,
    REPO_URL,
)
from test_plugin_builds import PluginBuildsEnv


@pytest.fixture
def ienv(aws_stack):
    return ImporterEnv(aws_stack)


@pytest.fixture
def admin_setup(ienv):
    usecase_id = ienv.create_usecase()
    admin = ienv.make_user(role="Viewer")
    ienv.assign_role(admin, usecase_id, "UseCaseAdmin")
    return usecase_id, admin


#: gst-plugins-good style plugin-set tree: two individual plugin
#: targets under gst/, so the import lands in pending_selection.
PLUGIN_SET_FILES = {
    "meson.build": MESON_PLUGIN,
    "gst/rtsp/meson.build": MESON_PLUGIN,
    "gst/udp/meson.build": MESON_PLUGIN,
}


def multi_fetch_detail(plugin, build_id, slug, status="SUCCEEDED"):
    """EventBridge fetch detail carrying the REVISION_SLUG override."""
    return {
        "build-status": status,
        "project-name": TEST_ENV["FETCH_PROJECT_NAME"],
        "build-id": ImporterEnv.FETCH_BUILD_ARN_PREFIX + build_id,
        "additional-information": {
            "environment": {
                "environment-variables": [
                    {"name": "USECASE_ID", "value": plugin["usecase_id"]},
                    {"name": "PLUGIN_ID", "value": plugin["plugin_id"]},
                    {"name": "PLUGIN_VERSION",
                     "value": str(plugin["version"])},
                    {"name": "REVISION_SLUG", "value": slug},
                ],
            },
        },
    }


# =====================================================================
# Pure functions (no AWS)
# =====================================================================

class TestValidateArchRevisions:
    ARCHS = ["arm64_jp4", "arm64_jp5", "x86_64"]

    def test_absent_is_valid(self, aws_stack):
        mod = aws_stack.plugin_importer
        assert mod.validate_arch_revisions(None, self.ARCHS) is None

    def test_subset_of_requested_architectures_is_valid(self, aws_stack):
        mod = aws_stack.plugin_importer
        assert mod.validate_arch_revisions(
            {"arm64_jp5": "1.16"}, self.ARCHS) is None

    def test_non_dict_rejected(self, aws_stack):
        mod = aws_stack.plugin_importer
        assert mod.validate_arch_revisions(["1.16"], self.ARCHS) is not None

    def test_unknown_architecture_rejected(self, aws_stack):
        mod = aws_stack.plugin_importer
        error = mod.validate_arch_revisions(
            {"sparc64": "1.16"}, self.ARCHS)
        assert error is not None and "sparc64" in error

    def test_unrequested_architecture_rejected(self, aws_stack):
        """Keys must be a subset of the REQUESTED architectures."""
        mod = aws_stack.plugin_importer
        error = mod.validate_arch_revisions(
            {"arm64_jp6": "1.16"}, self.ARCHS)
        assert error is not None and "arm64_jp6" in error

    @pytest.mark.parametrize("value", [None, 1.16, "", "   ", ["1.16"]])
    def test_non_string_or_empty_values_rejected(self, aws_stack, value):
        mod = aws_stack.plugin_importer
        error = mod.validate_arch_revisions(
            {"arm64_jp5": value}, self.ARCHS)
        assert error is not None and "arm64_jp5" in error


class TestRevisionSlug:
    def test_absent_revision_is_the_default_slug(self, aws_stack):
        mod = aws_stack.plugin_importer
        assert mod.revision_slug(None) == mod.DEFAULT_REVISION
        assert mod.revision_slug("") == mod.DEFAULT_REVISION

    def test_safe_revisions_pass_through(self, aws_stack):
        mod = aws_stack.plugin_importer
        assert mod.revision_slug("1.16") == "1.16"
        assert mod.revision_slug("main") == "main"

    def test_unsafe_characters_collapse_to_hyphens(self, aws_stack):
        mod = aws_stack.plugin_importer
        assert mod.revision_slug("feature/per arch") == "feature-per-arch"
        assert mod.revision_slug("///") == "rev"


class TestRevisionFetchPlan:
    BASE = "plugin-sources/uc/p/1/"

    def _plan(self, aws_stack, revision, arch_revisions, archs):
        return aws_stack.plugin_importer.revision_fetch_plan(
            revision, arch_revisions, archs, self.BASE)

    def test_no_overrides_is_single_mode(self, aws_stack):
        plan = self._plan(aws_stack, "main", None, ["x86_64", "arm64_jp5"])
        assert plan == {"mode": "single", "revision": "main"}

    def test_overrides_all_equal_to_the_revision_collapse_to_single(
            self, aws_stack):
        plan = self._plan(aws_stack, "1.16",
                          {"x86_64": "1.16", "arm64_jp5": "1.16"},
                          ["x86_64", "arm64_jp5"])
        assert plan == {"mode": "single", "revision": "1.16"}

    def test_uniform_overrides_without_top_revision_collapse_to_single(
            self, aws_stack):
        """All archs overridden to the same revision: one fetch of THAT
        revision, flat layout."""
        plan = self._plan(aws_stack, None,
                          {"x86_64": "1.16", "arm64_jp5": "1.16"},
                          ["x86_64", "arm64_jp5"])
        assert plan == {"mode": "single", "revision": "1.16"}

    def test_distinct_revisions_fetch_once_each(self, aws_stack):
        archs = ["arm64_jp4", "arm64_jp5", "arm64_jp6", "x86_64"]
        plan = self._plan(aws_stack, None,
                          {"arm64_jp4": "1.14", "arm64_jp5": "1.16"}, archs)

        assert plan["mode"] == "multi"
        # One fetch per DISTINCT revision; archs sharing one share it.
        assert sorted(plan["fetches"]) == ["1.14", "1.16", "default"]
        assert plan["arch_revisions"] == {
            "arm64_jp4": "1.14",
            "arm64_jp5": "1.16",
            "arm64_jp6": "default",
            "x86_64": "default",
        }
        assert plan["fetches"]["1.16"] == {
            "revision": "1.16",
            "source_prefix": self.BASE + "rev-1.16/",
            "status": "fetching",
        }
        # The default tree is the top-level revision's (default branch).
        assert plan["default_slug"] == "default"
        assert plan["fetches"]["default"]["revision"] == \
            aws_stack.plugin_importer.DEFAULT_REVISION

    def test_default_slug_falls_back_deterministically(self, aws_stack):
        """Every arch overridden: the top-level revision is never
        fetched, so the first slug (sorted) is the default tree."""
        plan = self._plan(aws_stack, "main",
                          {"arm64_jp4": "1.14", "arm64_jp5": "1.16"},
                          ["arm64_jp4", "arm64_jp5"])
        assert sorted(plan["fetches"]) == ["1.14", "1.16"]
        assert plan["default_slug"] == "1.14"

    def test_slug_collisions_disambiguate(self, aws_stack):
        plan = self._plan(aws_stack, None,
                          {"arm64_jp4": "a/b", "arm64_jp5": "a b"},
                          ["arm64_jp4", "arm64_jp5"])
        assert plan["mode"] == "multi"
        assert sorted(plan["fetches"]) == ["a-b", "a-b-2"]
        # Both archs still resolve to their own revision's slug.
        revisions = {plan["fetches"][plan["arch_revisions"][arch]]["revision"]
                     for arch in ("arm64_jp4", "arm64_jp5")}
        assert revisions == {"a/b", "a b"}


# =====================================================================
# POST /plugins/import (moto-backed)
# =====================================================================

class TestImportArchRevisionsValidation:
    def test_unknown_architecture_rejected(self, ienv, admin_setup):
        usecase_id, admin = admin_setup
        status, body = ienv.import_plugin(admin, {
            "usecase_id": usecase_id,
            "repo_url": REPO_URL,
            "architectures": ["x86_64", "arm64_jp5"],
            "arch_revisions": {"arm64_jp6": "1.20"},
        })
        assert status == 400
        assert body["error"]["code"] == "INVALID_ARCH_REVISIONS"
        assert "arm64_jp6" in body["error"]["message"]
        assert ienv.records_for(usecase_id) == []

    def test_non_string_revision_rejected(self, ienv, admin_setup):
        usecase_id, admin = admin_setup
        status, body = ienv.import_plugin(admin, {
            "usecase_id": usecase_id,
            "repo_url": REPO_URL,
            "architectures": ["arm64_jp5"],
            "arch_revisions": {"arm64_jp5": 1.16},
        })
        assert status == 400
        assert body["error"]["code"] == "INVALID_ARCH_REVISIONS"
        assert ienv.records_for(usecase_id) == []


class TestSingleRevisionUnchanged:
    """Absent (or collapsing) arch_revisions keep today's behavior
    exactly: one fetch, flat source_s3_prefix, top-level
    fetch_build_id, no fetches/arch_revisions on the record."""

    def test_absent_arch_revisions_is_todays_flow(
            self, ienv, admin_setup, monkeypatch):
        usecase_id, admin = admin_setup
        recorder = RecordingCodeBuild(ienv.module.codebuild)
        monkeypatch.setattr(ienv.module, "codebuild", recorder)

        status, body = ienv.import_plugin(admin, {
            "usecase_id": usecase_id,
            "repo_url": REPO_URL,
            "revision": "1.24.2",
            "architectures": ["x86_64", "arm64_jp5"],
        })

        assert status == 202
        assert len(recorder.calls) == 1
        env = {v["name"]: v["value"]
               for v in recorder.calls[0]["environmentVariablesOverride"]}
        assert "REVISION_SLUG" not in env
        plugin = body["plugin"]
        assert "/rev-" not in plugin["source_s3_prefix"]
        assert "fetches" not in plugin
        assert "arch_revisions" not in plugin
        assert body["import"]["buildId"]
        record = ienv.get_record(plugin["plugin_id"])
        assert record["fetch_build_id"] == body["import"]["buildId"]
        assert "fetches" not in record

    def test_uniform_overrides_collapse_to_one_fetch(
            self, ienv, admin_setup, monkeypatch):
        """arch_revisions naming one revision for every arch is a
        single-revision import of that revision."""
        usecase_id, admin = admin_setup
        recorder = RecordingCodeBuild(ienv.module.codebuild)
        monkeypatch.setattr(ienv.module, "codebuild", recorder)

        status, body = ienv.import_plugin(admin, {
            "usecase_id": usecase_id,
            "repo_url": REPO_URL,
            "architectures": ["arm64_jp4", "arm64_jp5"],
            "arch_revisions": {"arm64_jp4": "1.16", "arm64_jp5": "1.16"},
        })

        assert status == 202
        assert len(recorder.calls) == 1
        env = {v["name"]: v["value"]
               for v in recorder.calls[0]["environmentVariablesOverride"]}
        assert env["REVISION"] == "1.16"
        assert "REVISION_SLUG" not in env
        assert body["plugin"]["provenance"]["revision"] == "1.16"
        assert "fetches" not in body["plugin"]

    def test_single_revision_import_still_completes(
            self, ienv, admin_setup):
        """End to end: the settled single-revision flow is untouched."""
        usecase_id, admin = admin_setup
        _, result, record = ienv.complete_import(admin, {
            "usecase_id": usecase_id,
            "repo_url": REPO_URL,
            "architectures": ["x86_64"],
            "arch_revisions": {"x86_64": "1.24.2"},
        }, files={"meson.build": MESON_PLUGIN})

        assert result == {"recorded": True, "import_status": "imported"}
        entry = record["artifacts"]["x86_64"]
        assert entry["buildStatus"] == "building"
        assert entry["buildId"].startswith("dda-plugin-build-x86_64:")


class MultiImportEnv:
    """One multi-revision import: default branch for x86_64/arm64_jp6,
    '1.16' for arm64_jp5, '1.14' for arm64_jp4 (the gst-plugins-good
    scenario)."""

    ARCHS = ["arm64_jp4", "arm64_jp5", "arm64_jp6", "x86_64"]
    OVERRIDES = {"arm64_jp4": "1.14", "arm64_jp5": "1.16"}

    def __init__(self, ienv, admin_setup, body_extra=None):
        self.ienv = ienv
        usecase_id, admin = admin_setup
        self.usecase_id, self.admin = usecase_id, admin
        status, self.body = ienv.import_plugin(admin, {
            "usecase_id": usecase_id,
            "repo_url": REPO_URL,
            "architectures": self.ARCHS,
            "arch_revisions": self.OVERRIDES,
            **(body_extra or {}),
        })
        assert status == 202, self.body
        self.plugin = self.body["plugin"]
        self.build_ids = self.body["import"]["fetchBuildIds"]

    def record(self):
        return self.ienv.get_record(self.plugin["plugin_id"])

    def deliver(self, slug, status="SUCCEEDED"):
        return self.ienv.deliver_fetch_result(multi_fetch_detail(
            self.plugin, self.build_ids[slug], slug, status=status))

    def sync_default_tree(self, files):
        """Sync `files` under the DEFAULT revision's tree (the record's
        source_s3_prefix), like the fetch CodeBuild step would."""
        self.ienv.sync_source(self.plugin, files)


class TestMultiRevisionFanOut:
    def test_one_fetch_per_distinct_revision_with_slug_and_prefix(
            self, ienv, admin_setup, monkeypatch):
        recorder = RecordingCodeBuild(ienv.module.codebuild)
        monkeypatch.setattr(ienv.module, "codebuild", recorder)

        env_setup = MultiImportEnv(ienv, admin_setup)
        plugin = env_setup.plugin

        # Three DISTINCT effective revisions -> exactly three fetches.
        assert len(recorder.calls) == 3
        by_slug = {}
        for call in recorder.calls:
            assert call["projectName"] == TEST_ENV["FETCH_PROJECT_NAME"]
            env = {v["name"]: v["value"]
                   for v in call["environmentVariablesOverride"]}
            by_slug[env["REVISION_SLUG"]] = env
        base = (f"plugin-sources/{env_setup.usecase_id}/"
                f"{plugin['plugin_id']}/1")
        assert by_slug["default"]["REVISION"] == ""
        assert by_slug["default"]["DEST_PREFIX"] == f"{base}/rev-default"
        assert by_slug["1.16"]["REVISION"] == "1.16"
        assert by_slug["1.16"]["DEST_PREFIX"] == f"{base}/rev-1.16"
        assert by_slug["1.14"]["REVISION"] == "1.14"
        assert by_slug["1.14"]["DEST_PREFIX"] == f"{base}/rev-1.14"

        # The record maps every arch to its slug; archs sharing the
        # default revision share the fetch.
        record = env_setup.record()
        assert record["arch_revisions"] == {
            "arm64_jp4": "1.14", "arm64_jp5": "1.16",
            "arm64_jp6": "default", "x86_64": "default",
        }
        assert record["import_status"] == "fetching"
        assert "fetch_build_id" not in record
        for slug in ("default", "1.14", "1.16"):
            entry = record["fetches"][slug]
            assert entry["status"] == "fetching"
            assert entry["fetch_build_id"] == env_setup.build_ids[slug]
        # Scan/inspection read the DEFAULT revision's tree.
        assert record["source_s3_prefix"] == f"{base}/rev-default/"
        assert record["default_fetch_slug"] == "default"
        # version_detail exposes the new fields (record display).
        detail = ienv.stack.plugin_records.version_detail(record)
        assert detail["arch_revisions"] == record["arch_revisions"]
        assert set(detail["fetches"]) == {"default", "1.14", "1.16"}


class TestMultiRevisionFetchSettling:
    def test_one_settled_fetch_keeps_the_record_fetching(
            self, ienv, admin_setup):
        env_setup = MultiImportEnv(ienv, admin_setup)

        result = env_setup.deliver("1.16")

        assert result["recorded"] is True
        assert result["import_status"] == "fetching"
        record = env_setup.record()
        assert record["import_status"] == "fetching"
        assert record["fetches"]["1.16"]["status"] == "succeeded"
        assert record["fetches"]["1.14"]["status"] == "fetching"
        assert record["fetches"]["default"]["status"] == "fetching"
        assert record.get("artifacts") == {}  # no builds yet

    def test_all_succeeded_advances_to_imported_with_builds_started(
            self, ienv, admin_setup, monkeypatch):
        builds_module = ienv.stack.plugin_builds
        recorder = RecordingCodeBuild(builds_module.codebuild)
        monkeypatch.setattr(builds_module, "codebuild", recorder)
        env_setup = MultiImportEnv(ienv, admin_setup)
        env_setup.sync_default_tree({"meson.build": MESON_PLUGIN})

        env_setup.deliver("1.16")
        env_setup.deliver("1.14")
        result = env_setup.deliver("default")

        assert result == {"recorded": True, "import_status": "imported"}
        record = env_setup.record()
        assert record["import_status"] == "imported"
        # Builds start for every requested arch once the last fetch
        # settles, each arch building from its own revision's tree.
        assert sorted(record["artifacts"]) == MultiImportEnv.ARCHS
        for arch, entry in record["artifacts"].items():
            assert entry["buildStatus"] == "building"
            assert entry["buildId"].startswith(f"dda-plugin-build-{arch}:")
        bucket = TEST_ENV["PORTAL_ARTIFACTS_BUCKET"]
        overrides = {call["projectName"]: call["sourceLocationOverride"]
                     for call in recorder.calls}
        flat = record["source_s3_prefix"].replace("rev-default/", "")
        assert overrides["dda-plugin-build-arm64_jp4"] == \
            f"{bucket}/{flat}rev-1.14/"
        assert overrides["dda-plugin-build-arm64_jp5"] == \
            f"{bucket}/{flat}rev-1.16/"
        assert overrides["dda-plugin-build-x86_64"] == \
            f"{bucket}/{flat}rev-default/"
        assert overrides["dda-plugin-build-arm64_jp6"] == \
            f"{bucket}/{flat}rev-default/"

    def test_plugin_set_default_tree_lands_in_pending_selection(
            self, ienv, admin_setup):
        """Enumeration runs on the DEFAULT revision's tree; a plugin
        set waits for selection exactly like a single-revision import."""
        env_setup = MultiImportEnv(ienv, admin_setup)
        env_setup.sync_default_tree(PLUGIN_SET_FILES)

        env_setup.deliver("1.14")
        env_setup.deliver("1.16")
        result = env_setup.deliver("default")

        assert result == {"recorded": True,
                          "import_status": "pending_selection"}
        record = env_setup.record()
        assert record["import_status"] == "pending_selection"
        assert sorted(p["name"] for p in record["plugins_found"]) == \
            ["rtsp", "udp"]
        assert record.get("artifacts") == {}  # selection first

    def test_one_failed_fetch_fails_the_import_naming_the_revision(
            self, ienv, admin_setup):
        env_setup = MultiImportEnv(ienv, admin_setup)
        env_setup.sync_default_tree({"meson.build": MESON_PLUGIN})

        env_setup.deliver("default")
        env_setup.deliver("1.14")
        result = env_setup.deliver("1.16", status="FAILED")

        assert result == {"recorded": True, "import_status": "failed"}
        record = env_setup.record()
        assert record["import_status"] == "failed"
        assert record["import_error_code"] == "REPO_FETCH_FAILED"
        # The finding names the failing revision...
        assert "1.16" in record["import_finding"]
        assert "1.14" not in record["import_finding"]
        # ...and the per-fetch statuses stay visible for the UI.
        assert record["fetches"]["1.16"]["status"] == "failed"
        assert record["fetches"]["1.14"]["status"] == "succeeded"
        assert record["fetches"]["default"]["status"] == "succeeded"
        assert record.get("artifacts") == {}  # no builds submitted

    def test_duplicate_slug_delivery_is_idempotent(self, ienv, admin_setup):
        env_setup = MultiImportEnv(ienv, admin_setup)

        first = env_setup.deliver("1.16")
        record_after_first = env_setup.record()
        duplicate = env_setup.deliver("1.16")

        assert first["recorded"] is True
        assert duplicate == {"recorded": False, "reason": "already recorded"}
        assert env_setup.record() == record_after_first

    def test_superseded_slug_build_is_skipped(self, ienv, admin_setup):
        env_setup = MultiImportEnv(ienv, admin_setup)

        result = env_setup.ienv.deliver_fetch_result(multi_fetch_detail(
            env_setup.plugin, "dda-plugin-fetch:someone-else", "1.16",
            status="FAILED"))

        assert result == {"recorded": False, "reason": "superseded build"}
        assert env_setup.record()["fetches"]["1.16"]["status"] == "fetching"

    def test_unknown_slug_is_skipped(self, ienv, admin_setup):
        env_setup = MultiImportEnv(ienv, admin_setup)

        result = env_setup.ienv.deliver_fetch_result(multi_fetch_detail(
            env_setup.plugin, env_setup.build_ids["1.16"], "no-such-slug"))

        assert result == {"recorded": False,
                          "reason": "missing fetch metadata"}
        assert env_setup.record()["import_status"] == "fetching"


# =====================================================================
# plugin_builds.start_builds per-arch source resolution
# =====================================================================

class TestPerArchBuildSource:
    def test_arch_source_prefix_resolves_the_slug_tree(self, aws_stack):
        mod = aws_stack.plugin_builds
        item = {
            "source_s3_prefix": "plugin-sources/uc/p/1/rev-default/",
            "arch_revisions": {"arm64_jp5": "1.16", "x86_64": "default"},
            "fetches": {
                "default": {"source_prefix":
                            "plugin-sources/uc/p/1/rev-default/"},
                "1.16": {"source_prefix": "plugin-sources/uc/p/1/rev-1.16/"},
            },
        }
        assert mod.arch_source_prefix(item, "arm64_jp5") == \
            "plugin-sources/uc/p/1/rev-1.16/"
        assert mod.arch_source_prefix(item, "x86_64") == \
            "plugin-sources/uc/p/1/rev-default/"
        # Arch without a mapping falls back to source_s3_prefix.
        assert mod.arch_source_prefix(item, "arm64_jp6") == \
            "plugin-sources/uc/p/1/rev-default/"

    def test_flat_records_fall_back_to_source_s3_prefix(self, aws_stack):
        mod = aws_stack.plugin_builds
        item = {"source_s3_prefix": "plugin-sources/uc/p/1/"}
        assert mod.arch_source_prefix(item, "x86_64") == \
            "plugin-sources/uc/p/1/"

    def test_start_builds_uses_each_archs_revision_tree(
            self, aws_stack, monkeypatch):
        """POST /build StartBuilds each arch with its own revision's
        sourceLocationOverride (arch_revisions -> fetches[slug]
        .source_prefix, falling back to source_s3_prefix)."""
        benv = PluginBuildsEnv(aws_stack)
        usecase_id = benv.create_usecase()
        admin = benv.make_admin(usecase_id)
        plugin = benv.create_plugin(admin, usecase_id)
        flat_prefix = plugin["source_s3_prefix"]
        benv.stack.tables.plugin_records.update_item(
            Key={"plugin_id": plugin["plugin_id"],
                 "version": plugin["version"]},
            UpdateExpression=("SET arch_revisions = :ar, fetches = :f, "
                              "source_s3_prefix = :sp"),
            ExpressionAttributeValues={
                ":ar": {"arm64_jp5": "1.16", "x86_64": "default"},
                ":f": {
                    "default": {"revision": "default",
                                "source_prefix": f"{flat_prefix}rev-default/",
                                "status": "succeeded"},
                    "1.16": {"revision": "1.16",
                             "source_prefix": f"{flat_prefix}rev-1.16/",
                             "status": "succeeded"},
                },
                ":sp": f"{flat_prefix}rev-default/",
            },
        )
        from test_plugin_builds import _RecordingCodeBuild
        recorder = _RecordingCodeBuild(benv.module.codebuild)
        monkeypatch.setattr(benv.module, "codebuild", recorder)

        status, _ = benv.post_build(admin, plugin["plugin_id"],
                                    plugin["version"],
                                    {"architectures":
                                     ["x86_64", "arm64_jp5", "arm64_jp6"]})

        assert status == 202
        bucket = TEST_ENV["PORTAL_ARTIFACTS_BUCKET"]
        overrides = {call["projectName"]: call["sourceLocationOverride"]
                     for call in recorder.calls}
        assert overrides["dda-plugin-build-arm64_jp5"] == \
            f"{bucket}/{flat_prefix}rev-1.16/"
        assert overrides["dda-plugin-build-x86_64"] == \
            f"{bucket}/{flat_prefix}rev-default/"
        # Unmapped arch falls back to the record's source_s3_prefix.
        assert overrides["dda-plugin-build-arm64_jp6"] == \
            f"{bucket}/{flat_prefix}rev-default/"
