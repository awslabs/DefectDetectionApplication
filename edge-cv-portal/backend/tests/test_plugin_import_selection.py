"""
Unit tests for the plugin-set import selection flow (custom-node-designer
import enhancement).

Covers:
- enumerate_plugins as a pure function over the source-tree file
  mapping: the meson monorepo plugin-set layout (individual plugin
  directories under gst/, ext/, sys/), single-plugin repositories
  (one entry, selection skipped), and empty/no-plugin trees
- validate_plugin_selection: non-empty and a subset of what the
  enumeration found
- POST /plugins/import against the moto-backed stack (asynchronous
  flow: 202 'fetching', then the EventBridge fetch result advances the
  record): a plugin-set import lands in import status pending_selection
  with plugins_found, submitting no builds; a single-plugin import
  skips selection and queues builds as before
- POST /plugins/{id}/versions/{v}/select-plugins: a valid subset is
  recorded as selected_plugins (record + provenance) and builds start
  for the requested Target_Architectures; empty or non-subset
  selections are rejected; the endpoint conflicts when no selection is
  pending and enforces node-designer:import permission

The PLUGIN_TARGETS pass-through to CodeBuild lives in
test_plugin_builds.py (TestPluginTargetsPassThrough).
"""
import json

import pytest

from test_plugin_importer import ImporterEnv, MESON_PLUGIN

REPO_URL = "https://gitlab.freedesktop.org/gstreamer/gst-plugins-good.git"


def plugin_meson(name):
    """A plugin directory's meson.build declaring a GStreamer plugin
    library target, mirroring the gst-plugins-good layout."""
    return (
        f"gst{name}_sources = ['gst{name}.c']\n"
        f"gst{name} = library('gst{name}', gst{name}_sources,\n"
        "  dependencies : [gst_dep, gstbase_dep],\n"
        "  install : true,\n"
        ")\n"
    )


#: gst-plugins-good style monorepo: individual plugin directories under
#: gst/, ext/, and sys/, plus non-plugin meson.build files that must
#: not enumerate (root, gst-libs, tests, deeper nesting).
MONOREPO_FILES = {
    "meson.build": (
        "project('gst-plugins-good', 'c', version : '1.24.2')\n"
        "subdir('gst')\nsubdir('ext')\nsubdir('sys')\n"
    ),
    "gst/rtp/meson.build": plugin_meson("rtp"),
    "gst/rtp/gstrtp.c": None,
    "gst/udp/meson.build": plugin_meson("udp"),
    "gst/udp/gstudp.c": None,
    "ext/jpeg/meson.build": plugin_meson("jpeg"),
    "sys/v4l2/meson.build": plugin_meson("v4l2"),
    # Non-plugin meson.build files: no plugin library target, wrong
    # root, or nested deeper than {root}/{plugin}/meson.build.
    "gst/nonplugin/meson.build": "install_data('helpers.txt')\n",
    "gst-libs/gst/meson.build": plugin_meson("shared"),
    "tests/check/meson.build": "test_sources = ['check.c']\n",
    "gst/rtp/tests/meson.build": plugin_meson("rtptests"),
    "README.md": None,
}


# =====================================================================
# Pure functions (no AWS)
# =====================================================================

class TestEnumeratePlugins:
    def test_meson_monorepo_enumerates_one_entry_per_plugin_dir(
            self, aws_stack):
        entries = aws_stack.plugin_importer.enumerate_plugins(MONOREPO_FILES)
        assert entries == [
            {"name": "jpeg", "path": "ext/jpeg"},
            {"name": "rtp", "path": "gst/rtp"},
            {"name": "udp", "path": "gst/udp"},
            {"name": "v4l2", "path": "sys/v4l2"},
        ]

    def test_non_plugin_and_nested_meson_files_do_not_enumerate(
            self, aws_stack):
        entries = aws_stack.plugin_importer.enumerate_plugins(MONOREPO_FILES)
        names = [e["name"] for e in entries]
        assert "nonplugin" not in names  # no plugin library target
        assert "shared" not in names     # gst-libs/ is not a plugin root
        assert "check" not in names      # tests/ is not a plugin root
        assert "rtptests" not in names   # deeper than {root}/{plugin}/

    def test_single_plugin_repo_enumerates_as_one_entry(self, aws_stack):
        entries = aws_stack.plugin_importer.enumerate_plugins(
            {"meson.build": MESON_PLUGIN, "gstmyfilter.c": None},
            single_plugin_name="gst-myfilter")
        assert entries == [{"name": "gst-myfilter", "path": ""}]

    def test_prebuilt_only_repo_enumerates_as_one_entry(self, aws_stack):
        entries = aws_stack.plugin_importer.enumerate_plugins(
            {"prebuilt/libgstmyfilter.so": None},
            single_plugin_name="myfilter")
        assert entries == [{"name": "myfilter", "path": ""}]

    def test_unbuildable_tree_enumerates_nothing(self, aws_stack):
        mod = aws_stack.plugin_importer
        assert mod.enumerate_plugins({}) == []
        assert mod.enumerate_plugins(
            {"README.md": None, "src/main.c": None}) == []


class TestValidatePluginSelection:
    FOUND = ["jpeg", "rtp", "udp", "v4l2"]

    def _validate(self, aws_stack, selected):
        return aws_stack.plugin_importer.validate_plugin_selection(
            selected, self.FOUND)

    def test_accepts_a_non_empty_subset(self, aws_stack):
        assert self._validate(aws_stack, ["rtp"]) is None
        assert self._validate(aws_stack, ["rtp", "udp", "jpeg"]) is None
        assert self._validate(aws_stack, list(self.FOUND)) is None

    @pytest.mark.parametrize("selected", [None, [], "rtp", {"rtp": True}])
    def test_rejects_empty_or_non_list_selection(self, aws_stack, selected):
        assert self._validate(aws_stack, selected) is not None

    @pytest.mark.parametrize("selected", [[""], [None], ["rtp", 3]])
    def test_rejects_non_string_entries(self, aws_stack, selected):
        assert self._validate(aws_stack, selected) is not None

    def test_rejects_names_outside_the_enumeration(self, aws_stack):
        error = self._validate(aws_stack, ["rtp", "nope"])
        assert error is not None
        assert "nope" in error


# =====================================================================
# Handler tests (moto-backed)
# =====================================================================

class SelectionEnv(ImporterEnv):
    """ImporterEnv plus the select-plugins route invocation."""

    def select_plugins(self, user, plugin_id, version, body):
        event = {
            "httpMethod": "POST",
            "resource": "/plugins/{id}/versions/{v}/select-plugins",
            "path": f"/plugins/{plugin_id}/versions/{version}/select-plugins",
            "pathParameters": {"id": plugin_id, "v": str(version)},
            "queryStringParameters": None,
            "body": json.dumps(body),
            "requestContext": {
                "authorizer": {
                    "claims": {
                        "sub": user["user_id"],
                        "email": user["email"],
                        "cognito:username": user["username"],
                        "custom:role": user["role"],
                    }
                }
            },
        }
        response = self.module.handler(event, None)
        return response["statusCode"], json.loads(response["body"])

    def get_record(self, plugin_id, version):
        return self.stack.plugin_records.get_version_item(plugin_id, version)


@pytest.fixture
def senv(aws_stack):
    return SelectionEnv(aws_stack)


@pytest.fixture
def admin_setup(senv):
    usecase_id = senv.create_usecase("Selection Test Use Case")
    admin = senv.make_user(role="Viewer")
    senv.assign_role(admin, usecase_id, "UseCaseAdmin")
    return usecase_id, admin


def import_plugin_set(senv, usecase_id, admin,
                      architectures=("x86_64", "arm64_jp5"), files=None,
                      extra=None):
    """Asynchronously import the monorepo fixture (POST /plugins/import
    answers 202 'fetching'; the delivered fetch result advances the
    record); returns the settled Plugin_Record item."""
    _, result, record = senv.complete_import(admin, {
        "usecase_id": usecase_id,
        "repo_url": REPO_URL,
        "architectures": list(architectures),
        **(extra or {}),
    }, files=files or MONOREPO_FILES)
    assert result["recorded"] is True, result
    return record


class TestPluginSetImport:
    def test_plugin_set_import_pends_selection_without_queuing_builds(
            self, senv, admin_setup):
        usecase_id, admin = admin_setup
        record = import_plugin_set(senv, usecase_id, admin)

        # The enumeration is persisted for the selection dialog...
        assert record["import_status"] == "pending_selection"
        assert [p["name"] for p in record["plugins_found"]] == \
            ["jpeg", "rtp", "udp", "v4l2"]
        # ...and no builds are submitted until the user selects.
        assert record["artifacts"] == {}
        # The version detail the UI polls carries both fields.
        detail = senv.stack.plugin_records.version_detail(record)
        assert detail["import_status"] == "pending_selection"
        assert [p["name"] for p in detail["plugins_found"]] == \
            ["jpeg", "rtp", "udp", "v4l2"]

    def test_single_plugin_import_skips_selection_and_queues_builds(
            self, senv, admin_setup):
        usecase_id, admin = admin_setup

        _, result, record = senv.complete_import(admin, {
            "usecase_id": usecase_id,
            "repo_url": "https://example.com/gst-myfilter.git",
            "architectures": ["x86_64"],
        }, files={"meson.build": MESON_PLUGIN, "gstmyfilter.c": None})

        # Single-plugin repositories enumerate as one entry and skip
        # the selection step: builds queue immediately as before.
        assert result == {"recorded": True, "import_status": "imported"}
        assert record["import_status"] == "imported"
        assert len(record["plugins_found"]) == 1
        assert record["plugins_found"][0]["path"] == ""
        entry = record["artifacts"]["x86_64"]
        assert entry["buildStatus"] == "building"
        assert entry["buildId"].startswith("dda-plugin-build-x86_64:")


class TestSelectPlugins:
    def test_valid_subset_is_recorded_and_builds_queue(
            self, senv, admin_setup):
        usecase_id, admin = admin_setup
        plugin_id = import_plugin_set(senv, usecase_id, admin)["plugin_id"]

        status, body = senv.select_plugins(admin, plugin_id, 1, {
            "selected_plugins": ["udp", "rtp"]})

        assert status == 200
        assert body["import"]["status"] == "imported"
        # Selection normalized to the enumeration's display order.
        assert body["import"]["selected_plugins"] == ["rtp", "udp"]
        assert sorted(body["import"]["submitted_architectures"]) == \
            ["arm64_jp5", "x86_64"]

        record = senv.get_record(plugin_id, 1)
        # The chosen subset is recorded on the Plugin_Record and in
        # provenance, and builds start for the requested architectures.
        assert record["selected_plugins"] == ["rtp", "udp"]
        assert record["provenance"]["selectedPlugins"] == ["rtp", "udp"]
        assert record["import_status"] == "imported"
        for arch in ("x86_64", "arm64_jp5"):
            entry = record["artifacts"][arch]
            assert entry["buildStatus"] == "building"
            assert entry["buildId"].startswith(f"dda-plugin-build-{arch}:")

    def test_selection_auto_starts_builds_with_the_plugin_targets(
            self, senv, admin_setup, monkeypatch):
        """select-plugins auto-starts the builds it queues, passing the
        recorded selection to CodeBuild as PLUGIN_TARGETS."""
        usecase_id, admin = admin_setup
        plugin_id = import_plugin_set(senv, usecase_id, admin)["plugin_id"]
        from test_plugin_importer import RecordingCodeBuild
        builds_module = senv.stack.plugin_builds
        recorder = RecordingCodeBuild(builds_module.codebuild)
        monkeypatch.setattr(builds_module, "codebuild", recorder)

        status, _ = senv.select_plugins(admin, plugin_id, 1, {
            "selected_plugins": ["udp", "rtp"]})

        assert status == 200
        assert sorted(c["projectName"] for c in recorder.calls) == \
            ["dda-plugin-build-arm64_jp5", "dda-plugin-build-x86_64"]
        for call in recorder.calls:
            env = {v["name"]: v["value"]
                   for v in call["environmentVariablesOverride"]}
            assert env["PLUGIN_TARGETS"] == "rtp,udp"
            assert env["SELECTED_PLUGINS"] == "rtp,udp"

    def test_empty_selection_is_rejected(self, senv, admin_setup):
        usecase_id, admin = admin_setup
        plugin_id = import_plugin_set(senv, usecase_id, admin)["plugin_id"]

        status, body = senv.select_plugins(admin, plugin_id, 1, {
            "selected_plugins": []})

        assert status == 400
        assert body["error"]["code"] == "INVALID_PLUGIN_SELECTION"
        record = senv.get_record(plugin_id, 1)
        assert record["import_status"] == "pending_selection"
        assert record["artifacts"] == {}

    def test_selection_outside_the_enumeration_is_rejected(
            self, senv, admin_setup):
        usecase_id, admin = admin_setup
        plugin_id = import_plugin_set(senv, usecase_id, admin)["plugin_id"]

        status, body = senv.select_plugins(admin, plugin_id, 1, {
            "selected_plugins": ["rtp", "not-a-plugin"]})

        assert status == 400
        assert body["error"]["code"] == "INVALID_PLUGIN_SELECTION"
        assert "not-a-plugin" in body["error"]["message"]
        assert senv.get_record(plugin_id, 1)["import_status"] == \
            "pending_selection"

    def test_conflicts_when_no_selection_is_pending(
            self, senv, admin_setup):
        usecase_id, admin = admin_setup
        _, result, record = senv.complete_import(admin, {
            "usecase_id": usecase_id,
            "repo_url": "https://example.com/gst-myfilter.git",
            "architectures": ["x86_64"],
        }, files={"meson.build": MESON_PLUGIN})
        assert record["import_status"] == "imported"
        plugin_id = record["plugin_id"]

        status, body = senv.select_plugins(admin, plugin_id, 1, {
            "selected_plugins": ["gst-myfilter"]})

        assert status == 409
        assert body["error"]["code"] == "SELECTION_NOT_PENDING"

    def test_missing_record_returns_not_found(self, senv, admin_setup):
        _, admin = admin_setup
        status, body = senv.select_plugins(admin, "no-such-plugin", 1, {
            "selected_plugins": ["rtp"]})
        assert status == 404

    def test_selection_denied_without_import_permission(
            self, senv, admin_setup):
        usecase_id, admin = admin_setup
        plugin_id = import_plugin_set(senv, usecase_id, admin)["plugin_id"]

        scientist = senv.make_user(role="Viewer")
        senv.assign_role(scientist, usecase_id, "DataScientist")

        status, body = senv.select_plugins(scientist, plugin_id, 1, {
            "selected_plugins": ["rtp"]})

        assert status == 403
        assert body["error"]["code"] == "FORBIDDEN"
        assert senv.get_record(plugin_id, 1)["import_status"] == \
            "pending_selection"


# =====================================================================
# Import-time selection (POST /plugins/import selected_plugins)
# =====================================================================
#
# The import view loads the module's plugin list from GET
# /plugin-modules?module=... and sends the chosen subset as
# selected_plugins on POST /plugins/import: the selection is recorded
# on the Plugin_Record (and provenance.selectedPlugins) and builds
# submit immediately — the pending-selection step is skipped. Absent or
# empty selected_plugins keeps today's behavior exactly.

class TestValidateImportSelectedPlugins:
    def _validate(self, aws_stack, selected):
        return aws_stack.plugin_importer.validate_import_selected_plugins(
            selected)

    def test_absent_and_empty_mean_whole_module(self, aws_stack):
        assert self._validate(aws_stack, None) is None
        assert self._validate(aws_stack, []) is None

    def test_accepts_a_list_of_non_empty_strings(self, aws_stack):
        assert self._validate(aws_stack, ["rtp"]) is None
        assert self._validate(aws_stack, ["rtp", "udp", "jpeg"]) is None

    @pytest.mark.parametrize("selected", ["rtp", {"rtp": True}, 42])
    def test_rejects_non_list_values(self, aws_stack, selected):
        assert self._validate(aws_stack, selected) is not None

    @pytest.mark.parametrize("selected", [[""], [None], ["rtp", 3]])
    def test_rejects_non_string_or_empty_entries(self, aws_stack, selected):
        assert self._validate(aws_stack, selected) is not None

    def test_dedupe_preserves_order(self, aws_stack):
        mod = aws_stack.plugin_importer
        assert mod.dedupe_selected_plugins(
            ["udp", "rtp", "udp", "jpeg", "rtp"]) == ["udp", "rtp", "jpeg"]
        assert mod.dedupe_selected_plugins(None) == []
        assert mod.dedupe_selected_plugins([]) == []


class TestImportTimeSelection:
    def test_selection_is_recorded_and_builds_queue_immediately(
            self, senv, admin_setup):
        usecase_id, admin = admin_setup
        record = import_plugin_set(
            senv, usecase_id, admin,
            extra={"selected_plugins": ["udp", "rtp", "udp"]})  # dupe collapses

        # No pending-selection step: the fetch result completes the
        # import at once, recorded on the Plugin_Record and its
        # provenance so plugin_builds.py passes SELECTED_PLUGINS
        # through to CodeBuild.
        assert record["import_status"] == "imported"
        assert record["selected_plugins"] == ["udp", "rtp"]
        assert record["provenance"]["selectedPlugins"] == ["udp", "rtp"]
        for arch in ("x86_64", "arm64_jp5"):
            entry = record["artifacts"][arch]
            assert entry["buildStatus"] == "building"
            assert entry["buildId"].startswith(f"dda-plugin-build-{arch}:")
        # The transient pending field is gone once the fetch settles.
        assert "pending_selected_plugins" not in record

    def test_absent_selection_keeps_pending_selection_behavior(
            self, senv, admin_setup):
        """No selected_plugins: identical to today — a plugin set still
        pends selection and submits no builds."""
        usecase_id, admin = admin_setup
        record = import_plugin_set(senv, usecase_id, admin)
        assert record["import_status"] == "pending_selection"
        assert "selected_plugins" not in record
        assert "selectedPlugins" not in record["provenance"]

    def test_empty_selection_means_whole_module(
            self, senv, admin_setup):
        usecase_id, admin = admin_setup
        record = import_plugin_set(senv, usecase_id, admin,
                                   architectures=("x86_64",),
                                   extra={"selected_plugins": []})

        assert record["import_status"] == "pending_selection"
        assert "selected_plugins" not in record
        assert "selectedPlugins" not in record["provenance"]

    @pytest.mark.parametrize("selected", ["rtp", [""], ["rtp", 3]])
    def test_invalid_selection_is_rejected_before_any_fetch(
            self, senv, admin_setup, monkeypatch, selected):
        usecase_id, admin = admin_setup

        def exploding_fetch(*args, **kwargs):
            raise AssertionError(
                "fetch must not start for an invalid selection")

        monkeypatch.setattr(senv.module, "start_fetch", exploding_fetch)

        status, body = senv.import_plugin(admin, {
            "usecase_id": usecase_id,
            "repo_url": REPO_URL,
            "architectures": ["x86_64"],
            "selected_plugins": selected,
        })

        assert status == 400
        assert body["error"]["code"] == "INVALID_PLUGIN_SELECTION"
        assert senv.records_for(usecase_id) == []

    def test_selection_on_unbuildable_source_is_not_recorded(
            self, senv, admin_setup):
        """An unbuildable tree still lands the record failed with the
        finding (4.5); the selection is meaningless there and is not
        recorded."""
        usecase_id, admin = admin_setup
        record = import_plugin_set(senv, usecase_id, admin,
                                   architectures=("x86_64",),
                                   files={"README.md": "docs only"},
                                   extra={"selected_plugins": ["rtp"]})

        assert record["import_status"] == "failed"
        assert "selected_plugins" not in record
        assert "selectedPlugins" not in record["provenance"]
        assert "pending_selected_plugins" not in record


# =====================================================================
# Per-plugin descriptions (gst_plugins_cache.json join)
# =====================================================================
#
# enumerate_plugins joins per-plugin descriptions from any
# docs/gst_plugins_cache.json whose content is present in the source
# tree. The cache parse (plugin_descriptions_from_cache) and the join
# are pure functions; every malformed input degrades to entries without
# descriptions — descriptions never fail an enumeration.

PLUGINS_CACHE_JSON = json.dumps({
    "rtp": {"description": "Real-time Transport Protocol plugin",
            "elements": {"rtpbin": {}}},
    "jpeg": {"description": "JPeg plugin library"},
    "udp": {"description": "   "},          # blank: no description
    "v4l2": {"elements": {}},               # missing description field
})


class TestPluginDescriptionsFromCache:
    def _parse(self, aws_stack, content):
        return aws_stack.plugin_importer.plugin_descriptions_from_cache(
            content)

    def test_parses_descriptions_keyed_by_plugin_name(self, aws_stack):
        assert self._parse(aws_stack, PLUGINS_CACHE_JSON) == {
            "rtp": "Real-time Transport Protocol plugin",
            "jpeg": "JPeg plugin library",
        }

    def test_accepts_an_already_parsed_mapping_and_bytes(self, aws_stack):
        assert self._parse(
            aws_stack, {"rtp": {"description": "RTP"}}) == {"rtp": "RTP"}
        assert self._parse(
            aws_stack, PLUGINS_CACHE_JSON.encode("utf-8")) == {
                "rtp": "Real-time Transport Protocol plugin",
                "jpeg": "JPeg plugin library",
            }

    @pytest.mark.parametrize("content", [
        "", "{not json", "[1, 2]", '"a string"', "null", None, 42,
        json.dumps({"rtp": "not-a-dict", "udp": ["x"]}),
        json.dumps({"rtp": {"description": 42}}),
    ])
    def test_malformed_or_unexpected_content_parses_to_empty(
            self, aws_stack, content):
        assert self._parse(aws_stack, content) == {}

    def test_truncated_oversized_content_parses_to_empty(self, aws_stack):
        # A size-capped (Range) read of an oversized cache yields
        # truncated JSON: it must parse to {} rather than raise.
        truncated = PLUGINS_CACHE_JSON[:len(PLUGINS_CACHE_JSON) // 2]
        assert self._parse(aws_stack, truncated) == {}


class TestJoinPluginDescriptions:
    def test_joins_known_names_and_leaves_others_untouched(self, aws_stack):
        mod = aws_stack.plugin_importer
        entries = [{"name": "rtp", "path": "gst/rtp"},
                   {"name": "udp", "path": "gst/udp"}]
        joined = mod.join_plugin_descriptions(entries, {"rtp": "RTP"})
        assert joined == [
            {"name": "rtp", "path": "gst/rtp", "description": "RTP"},
            {"name": "udp", "path": "gst/udp"},
        ]
        # The input entries are not mutated.
        assert entries == [{"name": "rtp", "path": "gst/rtp"},
                           {"name": "udp", "path": "gst/udp"}]

    def test_empty_descriptions_change_nothing(self, aws_stack):
        mod = aws_stack.plugin_importer
        entries = [{"name": "rtp", "path": "gst/rtp"}]
        assert mod.join_plugin_descriptions(entries, {}) == entries


class TestEnumeratePluginsDescriptions:
    def test_descriptions_join_from_the_docs_cache_file(self, aws_stack):
        files = dict(MONOREPO_FILES)
        files["docs/gst_plugins_cache.json"] = PLUGINS_CACHE_JSON
        entries = aws_stack.plugin_importer.enumerate_plugins(files)
        assert entries == [
            {"name": "jpeg", "path": "ext/jpeg",
             "description": "JPeg plugin library"},
            {"name": "rtp", "path": "gst/rtp",
             "description": "Real-time Transport Protocol plugin"},
            {"name": "udp", "path": "gst/udp"},
            {"name": "v4l2", "path": "sys/v4l2"},
        ]

    def test_malformed_cache_file_never_fails_the_enumeration(
            self, aws_stack):
        files = dict(MONOREPO_FILES)
        files["docs/gst_plugins_cache.json"] = "{truncated"
        entries = aws_stack.plugin_importer.enumerate_plugins(files)
        assert [e["name"] for e in entries] == ["jpeg", "rtp", "udp", "v4l2"]
        assert all("description" not in e for e in entries)

    def test_content_less_cache_file_is_ignored(self, aws_stack):
        files = dict(MONOREPO_FILES)
        files["docs/gst_plugins_cache.json"] = None  # fetch failed
        entries = aws_stack.plugin_importer.enumerate_plugins(files)
        assert all("description" not in e for e in entries)

    def test_single_plugin_repo_can_carry_a_description(self, aws_stack):
        entries = aws_stack.plugin_importer.enumerate_plugins(
            {"meson.build": MESON_PLUGIN,
             "docs/gst_plugins_cache.json": json.dumps(
                 {"gst-myfilter": {"description": "My filter plugin"}})},
            single_plugin_name="gst-myfilter")
        assert entries == [{"name": "gst-myfilter", "path": "",
                            "description": "My filter plugin"}]


class TestPluginSetImportDescriptions:
    def test_import_surfaces_descriptions_in_plugins_found(
            self, senv, admin_setup):
        """The post-fetch enumeration carries descriptions from the
        repository's docs/gst_plugins_cache.json into plugins_found so
        the selection dialog can show them."""
        usecase_id, admin = admin_setup
        files = dict(MONOREPO_FILES)
        files["docs/gst_plugins_cache.json"] = PLUGINS_CACHE_JSON
        record = import_plugin_set(senv, usecase_id, admin,
                                   architectures=("x86_64",), files=files)

        # Persisted on the Plugin_Record for the selection step.
        assert record["import_status"] == "pending_selection"
        by_name = {p["name"]: p for p in record["plugins_found"]}
        assert by_name["rtp"]["description"] == \
            "Real-time Transport Protocol plugin"
        assert by_name["jpeg"]["description"] == "JPeg plugin library"
        assert "description" not in by_name["udp"]


# =====================================================================
# Single-plugin selection naming (presentation)
# =====================================================================
#
# A record importing one plugin out of a plugin set used to keep the
# set's base name ("gst-plugins-good") with nothing showing what was
# actually imported. Both selection paths now surface a single-plugin
# selection in the record name ("{base}-{plugin}"): derive_import_name
# at import time and selection_rename on the post-fetch select-plugins
# endpoint. An explicitly provided name always wins; multi-plugin and
# absent selections keep the base name.

class TestDeriveImportName:
    def _derive(self, aws_stack, explicit, selected):
        return aws_stack.plugin_importer.derive_import_name(
            explicit, REPO_URL, selected)

    def test_single_selection_appends_the_plugin(self, aws_stack):
        assert self._derive(aws_stack, None, ["rtp"]) == \
            "gst-plugins-good-rtp"

    def test_multi_and_absent_selections_keep_the_base_name(
            self, aws_stack):
        assert self._derive(aws_stack, None, ["rtp", "udp"]) == \
            "gst-plugins-good"
        assert self._derive(aws_stack, None, []) == "gst-plugins-good"

    def test_explicit_name_always_wins(self, aws_stack):
        assert self._derive(aws_stack, "My Import", ["rtp"]) == "My Import"
        assert self._derive(aws_stack, "My Import", []) == "My Import"


class TestSelectionRename:
    def _rename(self, aws_stack, current, selected, repo_url=REPO_URL):
        return aws_stack.plugin_importer.selection_rename(
            current, repo_url, selected)

    def test_single_selection_renames_the_derived_default(self, aws_stack):
        assert self._rename(aws_stack, "gst-plugins-good", ["rtp"]) == \
            "gst-plugins-good-rtp"

    def test_multi_selection_never_renames(self, aws_stack):
        assert self._rename(
            aws_stack, "gst-plugins-good", ["rtp", "udp"]) is None

    def test_explicitly_named_record_keeps_its_name(self, aws_stack):
        assert self._rename(aws_stack, "My Import", ["rtp"]) is None

    def test_missing_repo_url_renames_unconditionally(self, aws_stack):
        # Without a provenance repoUrl the derived default cannot be
        # recomputed: the rename applies on any single selection.
        assert self._rename(aws_stack, "whatever", ["rtp"],
                            repo_url=None) == "whatever-rtp"


class TestImportTimeSelectionNaming:
    def test_single_selection_derives_the_record_name(
            self, senv, admin_setup):
        usecase_id, admin = admin_setup
        record = import_plugin_set(senv, usecase_id, admin,
                                   extra={"selected_plugins": ["rtp"]})

        assert record["import_status"] == "imported"
        assert record["name"] == "gst-plugins-good-rtp"
        assert record["selected_plugins"] == ["rtp"]

    def test_explicit_name_wins_over_the_derived_name(
            self, senv, admin_setup):
        usecase_id, admin = admin_setup
        record = import_plugin_set(
            senv, usecase_id, admin,
            extra={"name": "My RTP Import", "selected_plugins": ["rtp"]})

        assert record["name"] == "My RTP Import"

    def test_multi_selection_keeps_the_base_name(self, senv, admin_setup):
        usecase_id, admin = admin_setup
        record = import_plugin_set(
            senv, usecase_id, admin,
            extra={"selected_plugins": ["rtp", "udp"]})

        assert record["name"] == "gst-plugins-good"


class TestSelectPluginsNaming:
    def test_single_selection_renames_the_record(self, senv, admin_setup):
        usecase_id, admin = admin_setup
        plugin_id = import_plugin_set(senv, usecase_id, admin)["plugin_id"]

        status, body = senv.select_plugins(admin, plugin_id, 1, {
            "selected_plugins": ["rtp"]})

        assert status == 200
        record = senv.get_record(plugin_id, 1)
        assert record["name"] == "gst-plugins-good-rtp"
        assert record["selected_plugins"] == ["rtp"]
        assert body["plugin"]["name"] == "gst-plugins-good-rtp"

    def test_multi_selection_keeps_the_name(self, senv, admin_setup):
        usecase_id, admin = admin_setup
        plugin_id = import_plugin_set(senv, usecase_id, admin)["plugin_id"]

        status, _ = senv.select_plugins(admin, plugin_id, 1, {
            "selected_plugins": ["rtp", "udp"]})

        assert status == 200
        assert senv.get_record(plugin_id, 1)["name"] == "gst-plugins-good"

    def test_explicitly_named_import_keeps_its_name(self, senv, admin_setup):
        usecase_id, admin = admin_setup
        plugin_id = import_plugin_set(
            senv, usecase_id, admin,
            extra={"name": "My Plugin Set"})["plugin_id"]

        status, _ = senv.select_plugins(admin, plugin_id, 1, {
            "selected_plugins": ["rtp"]})

        assert status == 200
        assert senv.get_record(plugin_id, 1)["name"] == "My Plugin Set"


class TestRecordSummarySelection:
    def test_summary_carries_selection_and_enumeration_count(
            self, senv, admin_setup):
        """The library list (record_summary) additively carries
        selected_plugins and plugins_found_count so the UI can show
        which plugins an import covers."""
        usecase_id, admin = admin_setup
        plugin_id = import_plugin_set(senv, usecase_id, admin)["plugin_id"]
        senv.select_plugins(admin, plugin_id, 1, {
            "selected_plugins": ["rtp"]})

        summary = senv.stack.plugin_records.record_summary(
            senv.get_record(plugin_id, 1))
        assert summary["selected_plugins"] == ["rtp"]
        assert summary["plugins_found_count"] == 4
        assert summary["name"] == "gst-plugins-good-rtp"

    def test_summary_of_non_imports_carries_no_selection_fields(
            self, senv, admin_setup):
        usecase_id, _ = admin_setup
        item = senv.stack.plugin_records.new_version_item(
            plugin_id="p-1", version=1, usecase_id=usecase_id,
            name="scaffolded", kind="scaffold", user_id="u-1",
            timestamp=1)
        summary = senv.stack.plugin_records.record_summary(item)
        assert "selected_plugins" not in summary
        assert "plugins_found_count" not in summary
