"""
Unit tests for the Source_Editor backend routes in plugin_records.py
(custom-node-source-lifecycle task 1.8).

Covers the bulk source read (1.1, 1.2), merge/replace/delete saves with
path confinement (1.3, 1.4), the lifecycle lock (1.5), the revision
conflict guard, scaffold validation writing nothing (1.7), staleness
reporting (1.8), save-as-new-version (1.6), RBAC (9.1, 9.3), and the
wizard-style full-map save on a fresh dev v1 (10.3).

Runs against the moto-backed stack from conftest.py.
"""
import json

import pytest

from conftest import TEST_ENV
from test_plugin_records import PluginRecordsEnv, make_scaffold_declaration

SOURCE = "/plugins/{id}/versions/{v}/source"
NEW_VERSION = "/plugins/{id}/versions/{v}/new-version"
HOOK = "plugin/frame_processing_hook.py"


@pytest.fixture
def penv(aws_stack):
    return PluginRecordsEnv(aws_stack)


@pytest.fixture
def scaffold(penv):
    """A scaffold-kind dev v1 with its rendered files, plus the admin."""
    usecase_id = penv.create_usecase()
    admin = penv.make_user(role="Viewer")
    penv.assign_role(admin, usecase_id, "UseCaseAdmin")
    status, body = penv.create_plugin(
        admin, usecase_id, declaration=make_scaffold_declaration())
    assert status == 201
    return {
        "usecase_id": usecase_id,
        "admin": admin,
        "plugin_id": body["plugin"]["plugin_id"],
        "files": body["files"],
    }


def stored_keys(penv, usecase_id, plugin_id, version):
    prefix = f"plugin-sources/{usecase_id}/{plugin_id}/{version}/"
    response = penv.s3.list_objects_v2(Bucket=penv.bucket, Prefix=prefix)
    return sorted(o["Key"][len(prefix):] for o in response.get("Contents", []))


def stored_text(penv, usecase_id, plugin_id, version, path):
    key = f"plugin-sources/{usecase_id}/{plugin_id}/{version}/{path}"
    return penv.s3.get_object(Bucket=penv.bucket, Key=key)["Body"].read().decode()


def set_lifecycle(penv, plugin_id, version, state):
    penv.stack.tables.plugin_records.update_item(
        Key={"plugin_id": plugin_id, "version": version},
        UpdateExpression="SET lifecycle_state = :s",
        ExpressionAttributeValues={":s": state},
    )


def put_raw(penv, usecase_id, plugin_id, version, path, data):
    penv.s3.put_object(
        Bucket=penv.bucket,
        Key=f"plugin-sources/{usecase_id}/{plugin_id}/{version}/{path}",
        Body=data)


# ------------------------------------------------------- bulk read (1.1, 1.2)

class TestBulkRead:
    def test_all_returns_every_text_file_with_content(self, penv, scaffold):
        s = scaffold
        status, body = penv.invoke("GET", SOURCE, s["admin"], s["plugin_id"], 1,
                                   query={"all": "true"})
        assert status == 200
        assert body["source_revision"] == 1
        assert body["truncated"] is False
        by_path = {f["file"]: f for f in body["files"]}
        assert set(by_path) == set(s["files"])
        for path, content in s["files"].items():
            assert by_path[path]["content"] == content
            assert by_path[path]["size"] == len(content.encode())
            assert "binary" not in by_path[path]

    def test_oversize_and_non_utf8_files_are_binary(self, penv, scaffold):
        s = scaffold
        put_raw(penv, s["usecase_id"], s["plugin_id"], 1, "assets/blob.bin",
                b"\xff\xfe\x00\x01" * 8)
        put_raw(penv, s["usecase_id"], s["plugin_id"], 1, "assets/huge.txt",
                b"x" * (512 * 1024 + 1))
        status, body = penv.invoke("GET", SOURCE, s["admin"], s["plugin_id"], 1,
                                   query={"all": "1"})
        assert status == 200
        by_path = {f["file"]: f for f in body["files"]}
        assert by_path["assets/blob.bin"]["binary"] is True
        assert "content" not in by_path["assets/blob.bin"]
        assert by_path["assets/huge.txt"]["binary"] is True
        assert by_path["assets/huge.txt"]["size"] == 512 * 1024 + 1
        # Text files still carry content alongside the binary ones.
        assert by_path[HOOK]["content"] == s["files"][HOOK]

    def test_inline_budget_truncates_deterministically(self, penv, scaffold, monkeypatch):
        s = scaffold
        module = penv.module
        monkeypatch.setattr(module, "MAX_SOURCE_TREE_INLINE_BYTES", 64)
        status, body = penv.invoke("GET", SOURCE, s["admin"], s["plugin_id"], 1,
                                   query={"all": "true"})
        assert status == 200
        assert body["truncated"] is True
        assert body["count"] == len(s["files"])
        with_content = [f for f in body["files"] if "content" in f]
        assert sum(len(f["content"].encode()) for f in with_content) <= 64

    def test_listing_and_single_file_unchanged(self, penv, scaffold):
        s = scaffold
        status, body = penv.invoke("GET", SOURCE, s["admin"], s["plugin_id"], 1)
        assert status == 200
        assert {f["file"] for f in body["files"]} == set(s["files"])
        assert all("content" not in f for f in body["files"])
        status, body = penv.invoke("GET", SOURCE, s["admin"], s["plugin_id"], 1,
                                   query={"file": HOOK})
        assert status == 200
        assert body["content"] == s["files"][HOOK]


# ---------------------------------------------- saves (1.3, 1.4, 1.7, 1.8, 10.3)

class TestSave:
    def test_wizard_full_map_save_is_a_replace(self, penv, scaffold):
        """The wizards send the complete file map without a mode (10.3)."""
        s = scaffold
        files = dict(s["files"])
        files[HOOK] = "def process_frame(frame, params):\n    return frame\n"
        status, body = penv.invoke("PUT", SOURCE, s["admin"], s["plugin_id"], 1,
                                   body={"files": files})
        assert status == 200
        assert body["source_revision"] == 2
        assert body["deleted"] == []
        assert body["stale_architectures"] == []
        assert stored_text(penv, s["usecase_id"], s["plugin_id"], 1, HOOK) == files[HOOK]

    def test_merge_writes_only_submitted_files(self, penv, scaffold):
        s = scaffold
        status, body = penv.invoke(
            "PUT", SOURCE, s["admin"], s["plugin_id"], 1,
            body={"files": {"docs/NOTES.md": "notes\n"}, "mode": "merge"})
        assert status == 200
        assert body["files"] == ["docs/NOTES.md"]
        keys = stored_keys(penv, s["usecase_id"], s["plugin_id"], 1)
        assert set(keys) == set(s["files"]) | {"docs/NOTES.md"}

    def test_replace_deletes_paths_absent_from_the_map(self, penv, scaffold):
        s = scaffold
        put_raw(penv, s["usecase_id"], s["plugin_id"], 1, "extra/old.txt", b"old")
        status, body = penv.invoke(
            "PUT", SOURCE, s["admin"], s["plugin_id"], 1,
            body={"files": s["files"], "mode": "replace"})
        assert status == 200
        assert body["deleted"] == ["extra/old.txt"]
        assert "extra/old.txt" not in stored_keys(penv, s["usecase_id"], s["plugin_id"], 1)

    def test_delete_list_removes_files_in_merge_mode(self, penv, scaffold):
        s = scaffold
        put_raw(penv, s["usecase_id"], s["plugin_id"], 1, "extra/a.txt", b"a")
        put_raw(penv, s["usecase_id"], s["plugin_id"], 1, "extra/b.txt", b"b")
        status, body = penv.invoke(
            "PUT", SOURCE, s["admin"], s["plugin_id"], 1,
            body={"files": {}, "delete": ["extra/a.txt"], "mode": "merge"})
        assert status == 200
        assert body["deleted"] == ["extra/a.txt"]
        keys = stored_keys(penv, s["usecase_id"], s["plugin_id"], 1)
        assert "extra/a.txt" not in keys and "extra/b.txt" in keys

    def test_empty_body_rejected(self, penv, scaffold):
        s = scaffold
        status, body = penv.invoke("PUT", SOURCE, s["admin"], s["plugin_id"], 1,
                                   body={"files": {}, "mode": "merge"})
        assert status == 400
        assert body["error"]["code"] == "INVALID_FILES"

    @pytest.mark.parametrize("bad", ["../x.py", "/abs.py", "a/../../y", ".", "", "  "])
    def test_invalid_paths_rejected_in_files_and_delete(self, penv, scaffold, bad):
        s = scaffold
        status, body = penv.invoke(
            "PUT", SOURCE, s["admin"], s["plugin_id"], 1,
            body={"files": {bad: "x"}, "mode": "merge"})
        assert status == 400 and body["error"]["code"] == "INVALID_FILE_PATH"
        status, body = penv.invoke(
            "PUT", SOURCE, s["admin"], s["plugin_id"], 1,
            body={"files": {}, "delete": [bad], "mode": "merge"})
        assert status == 400 and body["error"]["code"] == "INVALID_FILE_PATH"
        assert stored_keys(penv, s["usecase_id"], s["plugin_id"], 1) == sorted(s["files"])

    def test_invalid_mode_rejected(self, penv, scaffold):
        s = scaffold
        status, body = penv.invoke("PUT", SOURCE, s["admin"], s["plugin_id"], 1,
                                   body={"files": s["files"], "mode": "upsert"})
        assert status == 400
        assert body["error"]["code"] == "INVALID_MODE"

    def test_scaffold_defect_in_resulting_tree_writes_nothing(self, penv, scaffold):
        """Deleting the hook via `delete` in merge mode yields an unbuildable
        tree even though the submitted map itself is fine (1.7)."""
        s = scaffold
        status, body = penv.invoke(
            "PUT", SOURCE, s["admin"], s["plugin_id"], 1,
            body={"files": {"docs/NOTES.md": "n"}, "delete": [HOOK], "mode": "merge"})
        assert status == 422
        assert body["error"]["code"] == "SCAFFOLD_INVALID"
        assert any("frame_processing_hook" in d for d in body["error"]["details"]["defects"])
        keys = stored_keys(penv, s["usecase_id"], s["plugin_id"], 1)
        assert HOOK in keys and "docs/NOTES.md" not in keys
        _, detail = penv.invoke("GET", "/plugins/{id}/versions/{v}", s["admin"],
                                s["plugin_id"], 1)
        assert detail["plugin"]["source_revision"] == 1

    def test_imported_kind_skips_scaffold_validation(self, penv):
        usecase_id = penv.create_usecase()
        admin = penv.make_user(role="Viewer")
        penv.assign_role(admin, usecase_id, "UseCaseAdmin")
        _, body = penv.create_plugin(admin, usecase_id, kind="imported")
        plugin_id = body["plugin"]["plugin_id"]
        status, body = penv.invoke("PUT", SOURCE, admin, plugin_id, 1,
                                   body={"files": {"meson.build": "project('x', 'c')\n"}})
        assert status == 200
        assert body["source_revision"] == 2

    def test_save_marks_existing_artifacts_stale(self, penv, scaffold):
        s = scaffold
        penv.seed_artifact(s["plugin_id"], 1, arch="x86_64")
        penv.seed_artifact(s["plugin_id"], 1, arch="arm64_jp6", build_status="failed")
        status, body = penv.invoke("PUT", SOURCE, s["admin"], s["plugin_id"], 1,
                                   body={"files": s["files"]})
        assert status == 200
        assert body["stale_architectures"] == ["arm64_jp6", "x86_64"]
        _, detail = penv.invoke("GET", "/plugins/{id}/versions/{v}", s["admin"],
                                s["plugin_id"], 1)
        assert detail["plugin"]["stale_architectures"] == ["arm64_jp6", "x86_64"]
        assert detail["plugin"]["source_revision"] == 2

    def test_legacy_item_without_revision_reports_nothing_stale(self, penv, scaffold):
        s = scaffold
        penv.seed_artifact(s["plugin_id"], 1, arch="x86_64")
        penv.stack.tables.plugin_records.update_item(
            Key={"plugin_id": s["plugin_id"], "version": 1},
            UpdateExpression="REMOVE source_revision")
        _, detail = penv.invoke("GET", "/plugins/{id}/versions/{v}", s["admin"],
                                s["plugin_id"], 1)
        assert detail["plugin"]["source_revision"] == 1
        assert detail["plugin"]["stale_architectures"] == []
        # The first save on a legacy item starts counting from 1.
        status, body = penv.invoke("PUT", SOURCE, s["admin"], s["plugin_id"], 1,
                                   body={"files": s["files"]})
        assert status == 200 and body["source_revision"] == 2


# ------------------------------------------------------------- guards (1.5)

class TestGuards:
    @pytest.mark.parametrize("state", ["test", "prod"])
    def test_non_dev_versions_are_locked(self, penv, scaffold, state):
        s = scaffold
        set_lifecycle(penv, s["plugin_id"], 1, state)
        status, body = penv.invoke("PUT", SOURCE, s["admin"], s["plugin_id"], 1,
                                   body={"files": s["files"]})
        assert status == 409
        assert body["error"]["code"] == "SOURCE_LOCKED"
        assert body["error"]["details"]["lifecycle_state"] == state
        assert "new version" in body["error"]["details"]["hint"]

    def test_stale_expected_revision_conflicts(self, penv, scaffold):
        s = scaffold
        status, _ = penv.invoke("PUT", SOURCE, s["admin"], s["plugin_id"], 1,
                                body={"files": s["files"], "expected_source_revision": 1})
        assert status == 200
        status, body = penv.invoke("PUT", SOURCE, s["admin"], s["plugin_id"], 1,
                                   body={"files": s["files"], "expected_source_revision": 1})
        assert status == 409
        assert body["error"]["code"] == "SOURCE_REVISION_CONFLICT"
        assert body["error"]["details"] == {"current": 2, "expected": 1}

    def test_read_only_roles_are_denied_with_audit(self, penv, scaffold):
        s = scaffold
        for role in ("Viewer", "Operator", "DataScientist"):
            user = penv.make_user(role="Viewer")
            penv.assign_role(user, s["usecase_id"], role)
            status, body = penv.invoke("PUT", SOURCE, user, s["plugin_id"], 1,
                                       body={"files": s["files"]})
            assert status == 403, role
            assert body["error"]["code"] == "FORBIDDEN"
            status, _ = penv.invoke("POST", NEW_VERSION, user, s["plugin_id"], 1,
                                    body={})
            assert status == 403, role
        assert penv.audit_entries("unauthorized_access")


# ---------------------------------------------------- new version (1.6, 1.7)

class TestNewVersion:
    def test_copies_tree_applies_edits_and_deletes(self, penv, scaffold):
        s = scaffold
        set_lifecycle(penv, s["plugin_id"], 1, "prod")
        put_raw(penv, s["usecase_id"], s["plugin_id"], 1, "extra/keep.txt", b"keep")
        put_raw(penv, s["usecase_id"], s["plugin_id"], 1, "extra/drop.txt", b"drop")
        penv.stack.tables.plugin_records.update_item(
            Key={"plugin_id": s["plugin_id"], "version": 1},
            UpdateExpression="SET requested_architectures = :r, git = :g",
            ExpressionAttributeValues={
                ":r": ["arm64_jp5", "x86_64"],
                ":g": {"connection_id": "c1", "branch": "main", "path": "p",
                       "last_sync": {"commit": "abc"}}})
        edited_hook = "def process_frame(frame, params):\n    return frame[::-1]\n"

        status, body = penv.invoke(
            "POST", NEW_VERSION, s["admin"], s["plugin_id"], 1,
            body={"files": {HOOK: edited_hook}, "delete": ["extra/drop.txt"],
                  "description": "v2"})
        assert status == 201
        plugin = body["plugin"]
        assert plugin["version"] == 2
        assert plugin["lifecycle_state"] == "dev"
        assert plugin["review"]["decision"] == "pending"
        assert plugin["source_revision"] == 1
        assert plugin["description"] == "v2"
        assert plugin["provenance"]["forkedFrom"] == 1
        assert plugin["provenance"]["scaffoldDeclaration"]
        assert plugin["git"] == {"connection_id": "c1", "branch": "main", "path": "p"}

        item = penv.stack.tables.plugin_records.get_item(
            Key={"plugin_id": s["plugin_id"], "version": 2})["Item"]
        assert item["requested_architectures"] == ["arm64_jp5", "x86_64"]

        keys = stored_keys(penv, s["usecase_id"], s["plugin_id"], 2)
        assert set(keys) == set(s["files"]) | {"extra/keep.txt"}
        assert stored_text(penv, s["usecase_id"], s["plugin_id"], 2, HOOK) == edited_hook
        assert stored_text(penv, s["usecase_id"], s["plugin_id"], 2, "extra/keep.txt") == "keep"
        # The original version's tree is untouched.
        assert stored_text(penv, s["usecase_id"], s["plugin_id"], 1, HOOK) == s["files"][HOOK]
        assert "extra/drop.txt" in stored_keys(penv, s["usecase_id"], s["plugin_id"], 1)

        entries = penv.audit_entries("create_plugin_record_version")
        assert any(e["details"].get("forked_from") == 1 for e in entries)

    def test_empty_body_copies_the_tree_verbatim(self, penv, scaffold):
        s = scaffold
        status, body = penv.invoke("POST", NEW_VERSION, s["admin"], s["plugin_id"], 1,
                                   body={})
        assert status == 201
        assert stored_keys(penv, s["usecase_id"], s["plugin_id"], 2) == sorted(s["files"])

    def test_forks_from_an_older_version_to_latest_plus_one(self, penv, scaffold):
        s = scaffold
        status, _ = penv.invoke("POST", NEW_VERSION, s["admin"], s["plugin_id"], 1, body={})
        assert status == 201
        status, body = penv.invoke("POST", NEW_VERSION, s["admin"], s["plugin_id"], 1,
                                   body={})
        assert status == 201
        assert body["plugin"]["version"] == 3
        assert body["plugin"]["provenance"]["forkedFrom"] == 1

    def test_scaffold_defects_create_no_version(self, penv, scaffold):
        s = scaffold
        status, body = penv.invoke("POST", NEW_VERSION, s["admin"], s["plugin_id"], 1,
                                   body={"delete": [HOOK]})
        assert status == 422
        assert body["error"]["code"] == "SCAFFOLD_INVALID"
        assert stored_keys(penv, s["usecase_id"], s["plugin_id"], 2) == []
        _, detail = penv.invoke("GET", "/plugins/{id}", s["admin"], s["plugin_id"])
        assert [v["version"] for v in detail["versions"]] == [1]

    def test_conditional_failure_cleans_copied_objects(self, penv, scaffold, monkeypatch):
        s = scaffold
        module = penv.module
        real_latest = module.get_latest_version_item

        # Pretend the latest version is still 1 while version 2 already exists,
        # so the conditional put fails and the copied tree must be removed.
        penv.stack.tables.plugin_records.put_item(Item={
            "plugin_id": s["plugin_id"], "version": 2, "usecase_id": s["usecase_id"],
            "name": "blur-regions", "kind": "scaffold", "lifecycle_state": "dev",
            "review": {"decision": "pending"}, "artifacts": {}, "component": {},
            "provenance": {}, "source_s3_prefix":
                f"plugin-sources/{s['usecase_id']}/{s['plugin_id']}/2/",
            "created_by": "x", "created_at": 1, "updated_at": 1})
        monkeypatch.setattr(module, "get_latest_version_item",
                            lambda pid: module.get_version_item(pid, 1))
        try:
            status, body = penv.invoke("POST", NEW_VERSION, s["admin"], s["plugin_id"], 1,
                                       body={"files": {"docs/x.md": "x"}})
        finally:
            monkeypatch.setattr(module, "get_latest_version_item", real_latest)
        assert status == 409
        assert body["error"]["code"] == "VERSION_CONFLICT"
        assert stored_keys(penv, s["usecase_id"], s["plugin_id"], 2) == []
