"""
Unit tests for plugin_importer.py repository import (custom-node-designer
task 4.1).

Covers:
- URL validation and plugin-name derivation (pure, 4.1)
- The buildability scan as a pure function over a file listing (4.5)
- Import provenance {repoUrl, revision, importedBy, importedAt,
  classification} with classification via classify_plugin_set (4.2,
  15.4, 15.5)
- POST /plugins/import against the moto-backed stack, asynchronous
  flow: the import answers 202 immediately with the Plugin_Record
  created in import_status 'fetching' (lifecycle dev, review pending,
  provenance recorded, no polling in the request path — API Gateway
  caps REST integrations at 29 s), and the EventBridge-delivered fetch
  result (plugin_builds.py delegating to
  plugin_importer.handle_fetch_result) advances the record: builds
  queued and auto-started for the selected Target_Architectures on
  success (4.2, 4.3, plugin_builds.start_queued_builds),
  failed with the REPO_FETCH_FAILED finding when the repository is
  unreachable or the revision missing (4.4 — the record now exists, a
  deliberate change from the old synchronous no-record behavior), or
  failed with the buildability finding for unbuildable trees (4.5)
- handle_fetch_result idempotency on the fetch build id (mocked
  EventBridge details), mirroring handle_build_result's guards
- start_fetch StartBuild (no polling) with the PLUGIN_ID /
  PLUGIN_VERSION / USECASE_ID attribution env overrides
- GET /plugin-modules Module_Listing (task 4.4): the pure page parse
  into {name, description, repoUrl, classification} entries (6.1, 6.2),
  ModuleIndexCache hit/miss/expiry (6.4), and the
  MODULE_LISTING_UNAVAILABLE failure path (6.3)

Task 4.6 (import error paths and cache behavior) is covered here too:
unreachable-repository and missing-revision fetch failures marking the
record failed (4.4), unbuildable source marking the record failed with
the finding persisted (4.5), MODULE_LISTING_UNAVAILABLE on fetch/parse
failure (6.3), and the cache TTL boundary exactly at 24 hours (6.4).
The property coverage belongs to tasks 4.2, 4.3, and 4.5.
"""
import json
import uuid

import pytest

from conftest import TEST_ENV

MESON_PLUGIN = """\
project('gst-myfilter', 'c')
gst_dep = dependency('gstreamer-1.0')
shared_library('gstmyfilter', 'gstmyfilter.c', dependencies: [gst_dep])
"""

CONFIGURE_AC_PLUGIN = """\
AC_INIT([gst-myfilter], [1.0])
PKG_CHECK_MODULES(GST, gstreamer-1.0 >= 1.20)
AG_GST_CHECK_PLUGIN(myfilter)
"""


# =====================================================================
# Pure functions (no AWS)
# =====================================================================

class TestValidateRepoUrl:
    def _module(self):
        import plugin_importer
        return plugin_importer

    @pytest.mark.parametrize("url", [
        "https://gitlab.freedesktop.org/gstreamer/gst-plugins-good.git",
        "http://example.com/repo",
        "git://anongit.freedesktop.org/gstreamer/gst-plugins-good",
    ])
    def test_accepts_public_repo_urls(self, aws_stack, url):
        assert aws_stack.plugin_importer.validate_repo_url(url) is None

    @pytest.mark.parametrize("url", [
        None,
        "",
        "/local/path/repo",
        "ssh://host/repo",
        "git@github.com:user/repo.git",
        "https://",
        "https://example.com/repo with space",
        "file:///etc/passwd",
    ])
    def test_rejects_invalid_urls(self, aws_stack, url):
        assert aws_stack.plugin_importer.validate_repo_url(url) is not None

    def test_default_plugin_name_from_last_segment(self, aws_stack):
        mod = aws_stack.plugin_importer
        assert mod.default_plugin_name(
            "https://gitlab.freedesktop.org/gstreamer/gst-plugins-good.git"
        ) == "gst-plugins-good"
        assert mod.default_plugin_name("https://example.com") == "example.com"


class TestScanBuildability:
    def test_prebuilt_so_is_buildable(self, aws_stack):
        scan = aws_stack.plugin_importer.scan_buildability(
            {"README.md": None, "prebuilt/libgstmyfilter.so": None})
        assert scan["buildable"] is True
        assert scan["kind"] == "prebuilt"
        assert scan["evidence"] == ["prebuilt/libgstmyfilter.so"]

    def test_meson_with_gst_plugin_target_is_buildable(self, aws_stack):
        scan = aws_stack.plugin_importer.scan_buildability(
            {"meson.build": MESON_PLUGIN, "gstmyfilter.c": None})
        assert scan["buildable"] is True
        assert scan["kind"] == "meson"
        assert scan["evidence"] == ["meson.build"]

    def test_configure_ac_with_gst_references_is_buildable(self, aws_stack):
        scan = aws_stack.plugin_importer.scan_buildability(
            {"configure.ac": CONFIGURE_AC_PLUGIN, "src/filter.c": None})
        assert scan["buildable"] is True
        assert scan["kind"] == "autotools"

    def test_meson_without_gstreamer_is_not_buildable(self, aws_stack):
        scan = aws_stack.plugin_importer.scan_buildability(
            {"meson.build": "project('hello', 'c')\nexecutable('hello', 'hello.c')\n"})
        assert scan["buildable"] is False
        assert "meson.build" in scan["finding"]

    def test_tree_without_build_definition_is_not_buildable(self, aws_stack):
        scan = aws_stack.plugin_importer.scan_buildability(
            {"README.md": None, "src/filter.c": None})
        assert scan["buildable"] is False
        assert scan["kind"] is None
        assert scan["finding"]  # the finding is reported (4.5)


class TestImportProvenance:
    def test_records_all_fields_and_classification(self, aws_stack):
        mod = aws_stack.plugin_importer
        url = "https://gitlab.freedesktop.org/gstreamer/gst-plugins-good.git"
        prov = mod.import_provenance(url, "1.24.2", None, "user-1", 1234)
        assert prov == {
            "repoUrl": url,
            "revision": "1.24.2",
            "importedBy": "user-1",
            "importedAt": 1234,
            "classification": "good",
        }

    def test_default_revision_and_unclassified(self, aws_stack):
        mod = aws_stack.plugin_importer
        prov = mod.import_provenance(
            "https://github.com/someone/gst-thing.git", None, None, "u", 1)
        assert prov["revision"] == mod.DEFAULT_REVISION
        assert prov["classification"] == "unclassified"


# =====================================================================
# Handler tests (moto-backed)
# =====================================================================

class ImporterEnv:
    """Facade for invoking the Plugin_Importer API in tests."""

    def __init__(self, stack):
        self.stack = stack
        self.module = stack.plugin_importer
        self.s3 = stack.s3
        self.bucket = TEST_ENV["PORTAL_ARTIFACTS_BUCKET"]

    def create_usecase(self, name="Importer Test Use Case"):
        usecase_id = f"uc-{uuid.uuid4()}"
        self.stack.tables.usecases.put_item(Item={
            "usecase_id": usecase_id,
            "name": name,
            "account_id": "123456789012",
        })
        return usecase_id

    def make_user(self, role="Viewer"):
        user_id = f"user-{uuid.uuid4()}"
        return {
            "user_id": user_id,
            "email": f"{user_id}@example.com",
            "username": user_id,
            "role": role,
        }

    def assign_role(self, user, usecase_id, role):
        self.stack.tables.user_roles.put_item(Item={
            "user_id": user["user_id"],
            "usecase_id": usecase_id,
            "role": role,
        })

    # --------------------------------------------- async fetch helpers

    FETCH_BUILD_ARN_PREFIX = "arn:aws:codebuild:us-east-1:123456789012:build/"

    def sync_source(self, plugin, files):
        """Sync `files` under the record's plugin-sources prefix exactly
        like the CodeBuild fetch step would."""
        prefix = plugin["source_s3_prefix"]
        for path, content in files.items():
            self.s3.put_object(
                Bucket=self.bucket,
                Key=f"{prefix}{path}",
                Body=(content or "").encode("utf-8"),
            )

    def fetch_result_detail(self, plugin, build_id, status="SUCCEEDED"):
        """Synthetic EventBridge CodeBuild Build State Change detail for
        the fetch project, carrying the attribution env overrides."""
        return {
            "build-status": status,
            "project-name": TEST_ENV["FETCH_PROJECT_NAME"],
            "build-id": self.FETCH_BUILD_ARN_PREFIX + build_id,
            "additional-information": {
                "environment": {
                    "environment-variables": [
                        {"name": "USECASE_ID", "value": plugin["usecase_id"]},
                        {"name": "PLUGIN_ID", "value": plugin["plugin_id"]},
                        {"name": "PLUGIN_VERSION",
                         "value": str(plugin["version"])},
                    ],
                },
            },
        }

    def deliver_fetch_result(self, detail):
        """Deliver via plugin_builds.py's handler (the EventBridge rule
        target), which delegates fetch results to handle_fetch_result."""
        return self.stack.plugin_builds.handler({
            "source": "aws.codebuild",
            "detail-type": "CodeBuild Build State Change",
            "detail": detail,
        }, None)

    def get_record(self, plugin_id, version=1):
        return self.stack.plugin_records.get_version_item(plugin_id, version)

    def complete_import(self, user, body, files=None,
                        fetch_status="SUCCEEDED"):
        """POST /plugins/import (202 'fetching'), sync `files` like the
        fetch step, deliver the fetch result; returns
        (import_response_body, fetch_result, record)."""
        status, response = self.import_plugin(user, body)
        assert status == 202, response
        plugin = response["plugin"]
        if fetch_status == "SUCCEEDED":
            self.sync_source(plugin, files or {})
        result = self.deliver_fetch_result(self.fetch_result_detail(
            plugin, response["import"]["buildId"], status=fetch_status))
        record = self.get_record(plugin["plugin_id"], plugin["version"])
        return response, result, record

    def records_for(self, usecase_id):
        from boto3.dynamodb.conditions import Key
        response = self.stack.tables.plugin_records.query(
            IndexName="usecase-plugins-index",
            KeyConditionExpression=Key("usecase_id").eq(usecase_id),
        )
        return response["Items"]

    def import_plugin(self, user, body):
        event = {
            "httpMethod": "POST",
            "resource": "/plugins/import",
            "path": "/plugins/import",
            "pathParameters": None,
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


@pytest.fixture
def ienv(aws_stack):
    return ImporterEnv(aws_stack)


@pytest.fixture
def admin_setup(ienv):
    usecase_id = ienv.create_usecase()
    admin = ienv.make_user(role="Viewer")
    ienv.assign_role(admin, usecase_id, "UseCaseAdmin")
    return usecase_id, admin


REPO_URL = "https://gitlab.freedesktop.org/gstreamer/gst-plugins-good.git"


class RecordingCodeBuild:
    """Wraps the module's codebuild client, recording StartBuild calls."""

    def __init__(self, inner):
        self._inner = inner
        self.calls = []

    def start_build(self, **kwargs):
        self.calls.append(kwargs)
        return self._inner.start_build(**kwargs)

    def __getattr__(self, name):
        return getattr(self._inner, name)


class TestImportRepository:
    def test_import_answers_202_fetching_with_the_record_created(
            self, ienv, admin_setup):
        usecase_id, admin = admin_setup

        status, body = ienv.import_plugin(admin, {
            "usecase_id": usecase_id,
            "repo_url": REPO_URL,
            "revision": "1.24.2",
            "architectures": ["x86_64", "arm64_jp5"],
        })

        # 202 immediately: the fetch runs asynchronously — API Gateway
        # caps REST integrations at 29 s, so the request path never polls.
        assert status == 202
        plugin = body["plugin"]
        assert plugin["import_status"] == "fetching"
        assert body["import"]["status"] == "fetching"
        assert body["import"]["buildId"].startswith(
            TEST_ENV["FETCH_PROJECT_NAME"] + ":")
        # Provenance {repoUrl, revision, importedBy, importedAt,
        # classification} recorded at import time (4.2, 15.4, 15.5)
        assert plugin["provenance"]["repoUrl"] == REPO_URL
        assert plugin["provenance"]["revision"] == "1.24.2"
        assert plugin["provenance"]["importedBy"] == admin["user_id"]
        assert plugin["provenance"]["importedAt"] > 0
        assert plugin["provenance"]["classification"] == "good"
        # New record: lifecycle dev, review pending (9.1, 10.1)
        assert plugin["lifecycle_state"] == "dev"
        assert plugin["review"]["decision"] == "pending"
        assert plugin["kind"] == "imported"
        # No plugins_found and no builds until the fetch settles
        assert plugin["artifacts"] == {}
        assert "plugins_found" not in plugin
        # The record is persisted in 'fetching' with the fetch build id
        record = ienv.get_record(plugin["plugin_id"])
        assert record["import_status"] == "fetching"
        assert record["fetch_build_id"] == body["import"]["buildId"]

    def test_fetch_start_carries_attribution_env_overrides(
            self, ienv, admin_setup, monkeypatch):
        """PLUGIN_ID / PLUGIN_VERSION / USECASE_ID ride the StartBuild
        env overrides so handle_fetch_result can attribute the result."""
        usecase_id, admin = admin_setup
        recorder = RecordingCodeBuild(ienv.module.codebuild)
        monkeypatch.setattr(ienv.module, "codebuild", recorder)

        status, body = ienv.import_plugin(admin, {
            "usecase_id": usecase_id,
            "repo_url": REPO_URL,
            "revision": "1.24.2",
            "architectures": ["x86_64"],
        })

        assert status == 202
        (call,) = recorder.calls
        assert call["projectName"] == TEST_ENV["FETCH_PROJECT_NAME"]
        env = {v["name"]: v["value"]
               for v in call["environmentVariablesOverride"]}
        plugin = body["plugin"]
        assert env["REPO_URL"] == REPO_URL
        assert env["REVISION"] == "1.24.2"
        assert env["DEST_PREFIX"] == plugin["source_s3_prefix"].rstrip("/")
        assert env["USECASE_ID"] == usecase_id
        assert env["PLUGIN_ID"] == plugin["plugin_id"]
        assert env["PLUGIN_VERSION"] == "1"

    def test_successful_fetch_advances_to_imported_and_starts_builds(
            self, ienv, admin_setup):
        usecase_id, admin = admin_setup

        _, result, record = ienv.complete_import(admin, {
            "usecase_id": usecase_id,
            "repo_url": REPO_URL,
            "revision": "1.24.2",
            "architectures": ["x86_64", "arm64_jp5"],
        }, files={"meson.build": MESON_PLUGIN, "gstmyfilter.c": None})

        assert result == {"recorded": True, "import_status": "imported"}
        assert record["import_status"] == "imported"
        # Builds submitted for the selected Target_Architectures (4.3):
        # the queued entries auto-start once the fetch settles.
        assert sorted(record["artifacts"]) == ["arm64_jp5", "x86_64"]
        for arch, entry in record["artifacts"].items():
            assert entry["buildStatus"] == "building"
            assert entry["buildId"].startswith(f"dda-plugin-build-{arch}:")
        # Single-plugin repository: one enumerated entry, no selection
        assert [p["path"] for p in record["plugins_found"]] == [""]

    def test_default_branch_when_revision_omitted(
            self, ienv, admin_setup, monkeypatch):
        usecase_id, admin = admin_setup
        recorder = RecordingCodeBuild(ienv.module.codebuild)
        monkeypatch.setattr(ienv.module, "codebuild", recorder)

        status, body = ienv.import_plugin(admin, {
            "usecase_id": usecase_id,
            "repo_url": REPO_URL,
            "architectures": ["x86_64"],
        })

        assert status == 202
        # The fetch clones the default branch (4.1)...
        env = {v["name"]: v["value"]
               for v in recorder.calls[0]["environmentVariablesOverride"]}
        assert env["REVISION"] == ""
        # ...and provenance records the default-revision marker
        assert body["plugin"]["provenance"]["revision"] == \
            ienv.module.DEFAULT_REVISION

    def test_fetch_failure_marks_the_record_failed(
            self, ienv, admin_setup):
        """An unreachable repository or missing revision fails the
        asynchronous fetch: the record — which now exists, a deliberate
        change from the old synchronous no-record behavior — is marked
        failed with the REPO_FETCH_FAILED finding (4.4)."""
        usecase_id, admin = admin_setup

        _, result, record = ienv.complete_import(admin, {
            "usecase_id": usecase_id,
            "repo_url": "https://github.com/someone/gone.git",
            "revision": "no-such-tag",
            "architectures": ["x86_64"],
        }, fetch_status="FAILED")

        assert result == {"recorded": True, "import_status": "failed"}
        assert record["import_status"] == "failed"
        assert record["import_error_code"] == "REPO_FETCH_FAILED"
        assert "unreachable" in record["import_finding"]
        assert record["artifacts"] == {}  # no builds submitted

    def test_unbuildable_source_marks_record_failed_with_finding(
            self, ienv, admin_setup):
        usecase_id, admin = admin_setup

        _, result, record = ienv.complete_import(admin, {
            "usecase_id": usecase_id,
            "repo_url": "https://github.com/someone/not-a-plugin.git",
            "architectures": ["x86_64"],
        }, files={"README.md": "docs only", "src/main.c": None})

        # Record marked failed with the finding persisted (4.5)
        assert result == {"recorded": True, "import_status": "failed"}
        assert record["import_status"] == "failed"
        assert record["import_finding"]
        assert record["artifacts"] == {}  # no builds submitted

    def test_get_version_detail_exposes_the_import_status(
            self, ienv, admin_setup):
        """GET /plugins/{id}/versions/{v} responses include
        import_status (and the finding once failed) so the UI can poll
        while the fetch runs."""
        usecase_id, admin = admin_setup
        status, body = ienv.import_plugin(admin, {
            "usecase_id": usecase_id,
            "repo_url": REPO_URL,
            "architectures": ["x86_64"],
        })
        assert status == 202

        record = ienv.get_record(body["plugin"]["plugin_id"])
        detail = ienv.stack.plugin_records.version_detail(record)
        assert detail["import_status"] == "fetching"

    def test_invalid_url_rejected_before_fetch(
            self, ienv, admin_setup, monkeypatch):
        usecase_id, admin = admin_setup

        def exploding_fetch(*args, **kwargs):
            raise AssertionError("fetch must not start for an invalid URL")

        monkeypatch.setattr(ienv.module, "start_fetch", exploding_fetch)

        status, body = ienv.import_plugin(admin, {
            "usecase_id": usecase_id,
            "repo_url": "git@github.com:user/repo.git",
            "architectures": ["x86_64"],
        })
        assert status == 400
        assert body["error"]["code"] == "INVALID_REPO_URL"
        assert ienv.records_for(usecase_id) == []

    def test_fetch_start_failure_creates_no_record(
            self, ienv, admin_setup, monkeypatch):
        """A StartBuild failure (e.g. throttling) still answers 502
        REPO_FETCH_FAILED with no record created."""
        usecase_id, admin = admin_setup

        def failing_fetch(*args, **kwargs):
            raise RuntimeError("StartBuild throttled")

        monkeypatch.setattr(ienv.module, "start_fetch", failing_fetch)

        status, body = ienv.import_plugin(admin, {
            "usecase_id": usecase_id,
            "repo_url": REPO_URL,
            "architectures": ["x86_64"],
        })
        assert status == 502
        assert body["error"]["code"] == "REPO_FETCH_FAILED"
        assert ienv.records_for(usecase_id) == []

    def test_unknown_architecture_rejected(self, ienv, admin_setup):
        usecase_id, admin = admin_setup
        status, body = ienv.import_plugin(admin, {
            "usecase_id": usecase_id,
            "repo_url": REPO_URL,
            "architectures": ["sparc64"],
        })
        assert status == 400
        assert body["error"]["code"] == "INVALID_ARCHITECTURES"

    def test_deepstream_restricted_to_jetson_architectures(
            self, ienv, admin_setup):
        usecase_id, admin = admin_setup
        status, body = ienv.import_plugin(admin, {
            "usecase_id": usecase_id,
            "repo_url": REPO_URL,
            "architectures": ["x86_64"],
            "deepstream": True,
        })
        assert status == 400
        assert body["error"]["code"] == "INVALID_ARCHITECTURES"

    def test_import_denied_without_import_permission(self, ienv, admin_setup):
        usecase_id, _ = admin_setup
        scientist = ienv.make_user(role="Viewer")
        ienv.assign_role(scientist, usecase_id, "DataScientist")

        status, body = ienv.import_plugin(scientist, {
            "usecase_id": usecase_id,
            "repo_url": REPO_URL,
            "architectures": ["x86_64"],
        })
        assert status == 403
        assert body["error"]["code"] == "FORBIDDEN"
        assert ienv.records_for(usecase_id) == []


class TestHandleFetchResult:
    """Fetch-result attribution and idempotency guards, mirroring
    handle_build_result (mocked EventBridge details delivered through
    plugin_builds.py's handler, the actual rule target)."""

    def _start_import(self, ienv, admin_setup):
        usecase_id, admin = admin_setup
        status, body = ienv.import_plugin(admin, {
            "usecase_id": usecase_id,
            "repo_url": REPO_URL,
            "architectures": ["x86_64"],
        })
        assert status == 202, body
        return body

    def test_duplicate_delivery_is_idempotent_on_the_build_id(
            self, ienv, admin_setup):
        body = self._start_import(ienv, admin_setup)
        plugin = body["plugin"]
        ienv.sync_source(plugin, {"meson.build": MESON_PLUGIN})
        detail = ienv.fetch_result_detail(plugin, body["import"]["buildId"])

        first = ienv.deliver_fetch_result(detail)
        record_after_first = ienv.get_record(plugin["plugin_id"])
        duplicate = ienv.deliver_fetch_result(detail)

        assert first["recorded"] is True
        assert duplicate == {"recorded": False, "reason": "already recorded"}
        # The duplicate changed nothing.
        assert ienv.get_record(plugin["plugin_id"]) == record_after_first

    def test_superseded_build_ids_are_skipped(self, ienv, admin_setup):
        body = self._start_import(ienv, admin_setup)
        plugin = body["plugin"]

        result = ienv.deliver_fetch_result(ienv.fetch_result_detail(
            plugin, "dda-plugin-fetch:someone-else", status="FAILED"))

        assert result == {"recorded": False, "reason": "superseded build"}
        assert ienv.get_record(plugin["plugin_id"])["import_status"] == \
            "fetching"

    def test_missing_attribution_is_skipped(self, ienv):
        result = ienv.deliver_fetch_result({
            "build-status": "SUCCEEDED",
            "project-name": TEST_ENV["FETCH_PROJECT_NAME"],
            "build-id": (ienv.FETCH_BUILD_ARN_PREFIX
                         + "dda-plugin-fetch:no-env"),
        })
        assert result == {"recorded": False,
                          "reason": "missing fetch metadata"}

    def test_unknown_record_is_skipped(self, ienv):
        detail = ienv.fetch_result_detail(
            {"plugin_id": "no-such-plugin", "version": 1,
             "usecase_id": "uc-x"},
            "dda-plugin-fetch:orphan")
        result = ienv.deliver_fetch_result(detail)
        assert result == {"recorded": False,
                          "reason": "plugin record not found"}


class TestImportAutoStartBuilds:
    """The fetch-result path auto-starts the builds it queued
    (plugin_builds.start_queued_builds, called by handle_fetch_result
    once the record settles to 'imported'): imported plugins build
    without a manual POST /plugins/{id}/versions/{v}/build."""

    ARCHS = ["x86_64", "arm64_jp5"]

    def _build_calls(self, recorder):
        return [c for c in recorder.calls
                if c["projectName"].startswith("dda-plugin-build-")]

    def test_settled_import_starts_one_build_per_architecture(
            self, ienv, admin_setup, monkeypatch):
        usecase_id, admin = admin_setup
        builds_module = ienv.stack.plugin_builds
        recorder = RecordingCodeBuild(builds_module.codebuild)
        monkeypatch.setattr(builds_module, "codebuild", recorder)

        _, result, record = ienv.complete_import(admin, {
            "usecase_id": usecase_id,
            "repo_url": REPO_URL,
            "architectures": self.ARCHS,
        }, files={"meson.build": MESON_PLUGIN, "gstmyfilter.c": None})

        assert result == {"recorded": True, "import_status": "imported"}
        # One StartBuild per requested Target_Architecture, sourcing the
        # synced tree with the attribution env overrides.
        calls = self._build_calls(recorder)
        assert sorted(c["projectName"] for c in calls) == \
            ["dda-plugin-build-arm64_jp5", "dda-plugin-build-x86_64"]
        bucket = TEST_ENV["PORTAL_ARTIFACTS_BUCKET"]
        for call in calls:
            assert call["sourceLocationOverride"] == \
                f"{bucket}/{record['source_s3_prefix']}"
            env = {v["name"]: v["value"]
                   for v in call["environmentVariablesOverride"]}
            assert env["USECASE_ID"] == usecase_id
            assert env["PLUGIN_ID"] == record["plugin_id"]
            assert env["PLUGIN_VERSION"] == "1"
        # The queued artifact entries flipped to building with the
        # started build ids recorded.
        for arch in self.ARCHS:
            entry = record["artifacts"][arch]
            assert entry["buildStatus"] == "building"
            assert entry["buildId"].startswith(f"dda-plugin-build-{arch}:")
        # The started CodeBuild builds actually exist.
        builds = ienv.stack.codebuild.batch_get_builds(
            ids=[record["artifacts"][a]["buildId"]
                 for a in self.ARCHS])["builds"]
        assert len(builds) == len(self.ARCHS)

    def test_fetch_failure_starts_no_builds(
            self, ienv, admin_setup, monkeypatch):
        usecase_id, admin = admin_setup
        builds_module = ienv.stack.plugin_builds
        recorder = RecordingCodeBuild(builds_module.codebuild)
        monkeypatch.setattr(builds_module, "codebuild", recorder)

        _, result, record = ienv.complete_import(admin, {
            "usecase_id": usecase_id,
            "repo_url": REPO_URL,
            "architectures": self.ARCHS,
        }, fetch_status="FAILED")

        assert result == {"recorded": True, "import_status": "failed"}
        assert self._build_calls(recorder) == []
        assert record["artifacts"] == {}

    class _ExplodingCodeBuild:
        def start_build(self, **kwargs):
            raise RuntimeError("StartBuild throttled")

    def test_auto_start_failure_never_fails_the_fetch_result(
            self, ienv, admin_setup, monkeypatch):
        """A StartBuild failure is recorded on the arch entry instead
        of failing the fetch-result handler (the import still settles
        to 'imported')."""
        usecase_id, admin = admin_setup
        monkeypatch.setattr(ienv.stack.plugin_builds, "codebuild",
                            self._ExplodingCodeBuild())

        _, result, record = ienv.complete_import(admin, {
            "usecase_id": usecase_id,
            "repo_url": REPO_URL,
            "architectures": ["x86_64"],
        }, files={"meson.build": MESON_PLUGIN})

        assert result == {"recorded": True, "import_status": "imported"}
        assert record["import_status"] == "imported"
        entry = record["artifacts"]["x86_64"]
        assert entry["buildStatus"] == "failed"
        assert "StartBuild throttled" in entry["logTail"]

    def test_unconfigured_architecture_is_left_queued(
            self, aws_stack, ienv, admin_setup, monkeypatch):
        """Architectures without a configured CodeBuild project are
        skipped (left queued) while the rest start."""
        usecase_id, admin = admin_setup
        builds_module = ienv.stack.plugin_builds
        monkeypatch.setattr(
            builds_module, "BUILD_PROJECTS",
            {"x86_64": builds_module.BUILD_PROJECTS["x86_64"]})

        _, result, record = ienv.complete_import(admin, {
            "usecase_id": usecase_id,
            "repo_url": REPO_URL,
            "architectures": ["x86_64", "arm64_jp5"],
        }, files={"meson.build": MESON_PLUGIN})

        assert result == {"recorded": True, "import_status": "imported"}
        assert record["artifacts"]["x86_64"]["buildStatus"] == "building"
        assert record["artifacts"]["arm64_jp5"] == {"buildStatus": "queued"}


class TestStartFetch:
    def test_start_fetch_starts_the_fetch_project_without_polling(
            self, aws_stack):
        """start_fetch StartBuilds FETCH_PROJECT_NAME and returns the
        build id immediately (no batch_get_builds polling loop)."""
        build_id = aws_stack.plugin_importer.start_fetch(
            "https://example.com/repo.git", "main",
            "plugin-sources/uc/p/1",
            usecase_id="uc", plugin_id="p", version=1)

        assert build_id.startswith(TEST_ENV["FETCH_PROJECT_NAME"] + ":")


# =====================================================================
# Module_Listing (GET /plugin-modules, task 4.4)
# =====================================================================

#: Synthetic Module_Listing page mirroring the real page structure:
#: layout/navigation tables without a "module" header row, then the
#: module index table (header row: module | description | ...).
MODULE_LISTING_PAGE = """\
<html><body>
<table border="0"><tr><td><a href="/">Home</a></td></tr>
<tr><td><a href="/features/">Features</a></td></tr></table>
<h2>Modules</h2>
<table width="95%" border="1">
<tr><th>module</th><th>description</th><th>stable version</th>
<th>devel version</th><th>status</th></tr>
<tr><td><a href="gstreamer.html">gstreamer</a></td>
<td>core library and elements</td>
<td><a href="/releases/1.28/#1.28.5">1.28.5</a></td>
<td><a href="/releases/gstreamer/1.29.2.html">1.29.2</a></td>
<td>active</td></tr>
<tr><td><a href="gst-plugins-good.html">gst-plugins-good</a></td>
<td>a set of good-quality plug-ins under our preferred license, LGPL</td>
<td>1.28.5</td><td>1.29.2</td><td>active</td></tr>
<tr><td><a href="gst-plugins-ugly.html">gst-plugins-ugly</a></td>
<td>a set of good-quality plug-ins that might pose distribution problems</td>
<td>1.28.5</td><td>1.29.2</td><td>active</td></tr>
<tr><td><a href="gst-plugins-bad.html">gst-plugins-bad</a></td>
<td>a set of plug-ins that need more quality, testing or documentation</td>
<td>1.28.5</td><td>1.29.2</td><td>active</td></tr>
<tr><td><a href="qt-gstreamer.html">qt-gstreamer</a></td>
<td>QtGStreamer</td>
<td>
            N/A
          </td>
<td>
            N/A
          </td>
<td>abandoned</td></tr>
</table>
</body></html>
"""


class TestParseModuleListing:
    """The parse is a pure function over the page content (6.1)."""

    def test_parses_every_module_row(self, aws_stack):
        modules = aws_stack.plugin_importer.parse_module_listing(
            MODULE_LISTING_PAGE)
        assert [m["name"] for m in modules] == [
            "gstreamer", "gst-plugins-good", "gst-plugins-ugly",
            "gst-plugins-bad", "qt-gstreamer",
        ]

    def test_entries_carry_description_repo_url_and_classification(
            self, aws_stack):
        modules = aws_stack.plugin_importer.parse_module_listing(
            MODULE_LISTING_PAGE)
        by_name = {m["name"]: m for m in modules}

        good = by_name["gst-plugins-good"]
        assert good["description"].startswith("a set of good-quality")
        # Published repository location fed into the import path (6.2)
        assert good["repoUrl"] == \
            "https://gitlab.freedesktop.org/gstreamer/gst-plugins-good.git"
        # Classification via classify_plugin_set (15.1)
        assert good["classification"] == "good"
        assert by_name["gst-plugins-bad"]["classification"] == "bad"
        assert by_name["gst-plugins-ugly"]["classification"] == "ugly"
        assert by_name["gstreamer"]["classification"] == "unclassified"

    def test_layout_tables_are_ignored(self, aws_stack):
        modules = aws_stack.plugin_importer.parse_module_listing(
            MODULE_LISTING_PAGE)
        names = [m["name"] for m in modules]
        assert "Home" not in names
        assert "Features" not in names

    @pytest.mark.parametrize("page", [
        "",
        "<html><body><p>maintenance</p></body></html>",
        "<html><table><tr><th>foo</th></tr><tr><td>bar</td></tr></table></html>",
    ])
    def test_unparseable_content_raises(self, aws_stack, page):
        with pytest.raises(aws_stack.plugin_importer.ModuleListingParseError):
            aws_stack.plugin_importer.parse_module_listing(page)


class ModuleListingEnv(ImporterEnv):
    """ImporterEnv plus GET /plugin-modules invocation and cache access."""

    def clear_cache(self):
        self.stack.tables.module_index_cache.delete_item(
            Key={"cache_key": self.module.MODULE_INDEX_CACHE_KEY})

    def cache_item(self):
        return self.stack.tables.module_index_cache.get_item(
            Key={"cache_key": self.module.MODULE_INDEX_CACHE_KEY}
        ).get("Item")

    def list_modules(self, user=None):
        user = user or self.make_user()
        event = {
            "httpMethod": "GET",
            "resource": "/plugin-modules",
            "path": "/plugin-modules",
            "pathParameters": None,
            "queryStringParameters": None,
            "body": None,
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


@pytest.fixture
def menv(aws_stack):
    env = ModuleListingEnv(aws_stack)
    env.clear_cache()
    return env


class TestListPluginModules:
    def test_cache_miss_fetches_parses_and_caches(self, menv, monkeypatch):
        monkeypatch.setattr(menv.module, "fetch_module_listing",
                            lambda: MODULE_LISTING_PAGE)

        status, body = menv.list_modules()

        assert status == 200
        assert body["cached"] is False
        assert body["fetchedAt"] > 0
        names = [m["name"] for m in body["modules"]]
        assert "gst-plugins-good" in names
        # The parsed index is cached with fetchedAt and the 24 h TTL (6.4)
        item = menv.cache_item()
        assert item is not None
        assert int(item["fetchedAt"]) == body["fetchedAt"]
        assert int(item["ttl"]) == (int(item["fetchedAt"]) // 1000
                                    + menv.module.MODULE_INDEX_TTL_SECONDS)
        assert len(item["modules"]) == len(body["modules"])

    def test_fresh_cache_is_reused_without_fetching(self, menv, monkeypatch):
        monkeypatch.setattr(menv.module, "fetch_module_listing",
                            lambda: MODULE_LISTING_PAGE)
        status1, body1 = menv.list_modules()
        assert status1 == 200

        def exploding_fetch():
            raise AssertionError("a fresh cache must be reused (6.4)")

        monkeypatch.setattr(menv.module, "fetch_module_listing",
                            exploding_fetch)
        status2, body2 = menv.list_modules()

        assert status2 == 200
        assert body2["cached"] is True
        assert body2["fetchedAt"] == body1["fetchedAt"]
        assert body2["modules"] == body1["modules"]

    def test_expired_cache_is_refetched(self, menv, monkeypatch):
        stale_fetched_at = (menv.module.now_ms()
                            - menv.module.MODULE_INDEX_TTL_SECONDS * 1000
                            - 1)
        menv.stack.tables.module_index_cache.put_item(Item={
            "cache_key": menv.module.MODULE_INDEX_CACHE_KEY,
            "modules": [{"name": "stale", "description": "",
                         "repoUrl": "https://example.com/stale.git",
                         "classification": "unclassified"}],
            "fetchedAt": stale_fetched_at,
            "ttl": stale_fetched_at // 1000,
        })
        monkeypatch.setattr(menv.module, "fetch_module_listing",
                            lambda: MODULE_LISTING_PAGE)

        status, body = menv.list_modules()

        assert status == 200
        assert body["cached"] is False
        assert "stale" not in [m["name"] for m in body["modules"]]

    def _seed_cache(self, menv, fetched_at):
        menv.stack.tables.module_index_cache.put_item(Item={
            "cache_key": menv.module.MODULE_INDEX_CACHE_KEY,
            "modules": [{"name": "seeded", "description": "",
                         "repoUrl": "https://example.com/seeded.git",
                         "classification": "unclassified"}],
            "fetchedAt": fetched_at,
            "ttl": fetched_at // 1000 + menv.module.MODULE_INDEX_TTL_SECONDS,
        })

    def test_cache_exactly_24h_old_is_stale_and_refetched(
            self, menv, monkeypatch):
        """At exactly 24 hours the cached index is no longer reused:
        the TTL bound is 'at most 24 hours' (6.4)."""
        fixed_now = menv.module.now_ms()
        monkeypatch.setattr(menv.module, "now_ms", lambda: fixed_now)
        self._seed_cache(
            menv, fixed_now - menv.module.MODULE_INDEX_TTL_SECONDS * 1000)
        monkeypatch.setattr(menv.module, "fetch_module_listing",
                            lambda: MODULE_LISTING_PAGE)

        status, body = menv.list_modules()

        assert status == 200
        assert body["cached"] is False
        assert "seeded" not in [m["name"] for m in body["modules"]]

    def test_cache_one_ms_younger_than_24h_is_fresh_and_reused(
            self, menv, monkeypatch):
        """One millisecond inside the 24-hour window the cache is still
        fresh and reused without fetching (6.4)."""
        fixed_now = menv.module.now_ms()
        monkeypatch.setattr(menv.module, "now_ms", lambda: fixed_now)
        fetched_at = (fixed_now
                      - menv.module.MODULE_INDEX_TTL_SECONDS * 1000 + 1)
        self._seed_cache(menv, fetched_at)

        def exploding_fetch():
            raise AssertionError(
                "a cache younger than 24 h must be reused (6.4)")

        monkeypatch.setattr(menv.module, "fetch_module_listing",
                            exploding_fetch)

        status, body = menv.list_modules()

        assert status == 200
        assert body["cached"] is True
        assert body["fetchedAt"] == fetched_at
        assert [m["name"] for m in body["modules"]] == ["seeded"]

    def test_fetch_failure_returns_module_listing_unavailable(
            self, menv, monkeypatch):
        def failing_fetch():
            raise ConnectionError("upstream unreachable")

        monkeypatch.setattr(menv.module, "fetch_module_listing",
                            failing_fetch)

        status, body = menv.list_modules()

        # The distinct code lets the UI offer manual URL entry (6.3)
        assert status == 502
        assert body["error"]["code"] == "MODULE_LISTING_UNAVAILABLE"

    def test_unparseable_response_returns_module_listing_unavailable(
            self, menv, monkeypatch):
        monkeypatch.setattr(menv.module, "fetch_module_listing",
                            lambda: "<html><body>maintenance</body></html>")

        status, body = menv.list_modules()

        assert status == 502
        assert body["error"]["code"] == "MODULE_LISTING_UNAVAILABLE"


# =====================================================================
# Per-module plugin list (GET /plugin-modules?module=<name>)
# =====================================================================

def _tree_entry(name, entry_type="tree"):
    return {"id": f"sha-{name}", "name": name, "type": entry_type,
            "path": f"subprojects/x/{name}", "mode": "040000"}


#: GitLab tree listings for a plugin-set module: one plugin per 'tree'
#: entry under gst/, ext/, sys/; blob entries (meson.build etc.) must
#: not enumerate.
MODULE_PLUGIN_TREES = {
    "gst": [_tree_entry("rtp"), _tree_entry("udp"),
            _tree_entry("meson.build", "blob")],
    "ext": [_tree_entry("jpeg"), _tree_entry("rtp")],  # duplicate name
    "sys": [_tree_entry("v4l2")],
}


class TestModulePluginsFromTrees:
    """The parse is a pure function over the GitLab tree listings."""

    def test_one_entry_per_directory_sorted_and_deduped(self, aws_stack):
        plugins = aws_stack.plugin_importer.module_plugins_from_trees(
            MODULE_PLUGIN_TREES)
        assert plugins == [{"name": "jpeg"}, {"name": "rtp"},
                           {"name": "udp"}, {"name": "v4l2"}]

    def test_blob_entries_and_missing_roots_are_ignored(self, aws_stack):
        mod = aws_stack.plugin_importer
        assert mod.module_plugins_from_trees(
            {"gst": [_tree_entry("meson.build", "blob")]}) == []
        # A module without some roots (e.g. no sys/) parses fine.
        assert mod.module_plugins_from_trees(
            {"gst": [_tree_entry("rtp")]}) == [{"name": "rtp"}]
        assert mod.module_plugins_from_trees({}) == []


class ModulePluginsEnv(ModuleListingEnv):
    """ModuleListingEnv plus GET /plugin-modules?module=... invocation."""

    def clear_module_cache(self, module):
        self.stack.tables.module_index_cache.delete_item(
            Key={"cache_key": self.module.module_plugins_cache_key(module)})

    def module_cache_item(self, module):
        return self.stack.tables.module_index_cache.get_item(
            Key={"cache_key": self.module.module_plugins_cache_key(module)}
        ).get("Item")

    def list_module_plugins(self, module, user=None):
        user = user or self.make_user()
        event = {
            "httpMethod": "GET",
            "resource": "/plugin-modules",
            "path": "/plugin-modules",
            "pathParameters": None,
            "queryStringParameters": {"module": module},
            "body": None,
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


@pytest.fixture
def penv(aws_stack, monkeypatch):
    env = ModulePluginsEnv(aws_stack)
    env.clear_module_cache("gst-plugins-good")
    # Per-plugin descriptions are an enhancement fetched separately from
    # the monorepo's docs/gst_plugins_cache.json; default to none so
    # these tests never touch the network. Description-join tests
    # re-patch this with concrete values.
    monkeypatch.setattr(env.module, "fetch_module_plugin_descriptions",
                        lambda module: {})
    return env


class TestListModulePlugins:
    def test_fetches_parses_and_caches_the_module_plugin_list(
            self, penv, monkeypatch):
        monkeypatch.setattr(penv.module, "fetch_module_plugin_trees",
                            lambda module: MODULE_PLUGIN_TREES)

        status, body = penv.list_module_plugins("gst-plugins-good")

        assert status == 200
        assert body["module"] == "gst-plugins-good"
        assert body["plugins"] == [{"name": "jpeg"}, {"name": "rtp"},
                                   {"name": "udp"}, {"name": "v4l2"}]
        assert body["cached"] is False
        assert body["fetchedAt"] > 0
        # Cached per module with fetchedAt and the 24 h TTL pattern.
        item = penv.module_cache_item("gst-plugins-good")
        assert item is not None
        assert int(item["fetchedAt"]) == body["fetchedAt"]
        assert int(item["ttl"]) == (int(item["fetchedAt"]) // 1000
                                    + penv.module.MODULE_INDEX_TTL_SECONDS)
        assert len(item["plugins"]) == 4

    def test_fresh_cache_is_reused_without_fetching(self, penv, monkeypatch):
        monkeypatch.setattr(penv.module, "fetch_module_plugin_trees",
                            lambda module: MODULE_PLUGIN_TREES)
        status1, body1 = penv.list_module_plugins("gst-plugins-good")
        assert status1 == 200

        def exploding_fetch(module):
            raise AssertionError("a fresh per-module cache must be reused")

        monkeypatch.setattr(penv.module, "fetch_module_plugin_trees",
                            exploding_fetch)
        status2, body2 = penv.list_module_plugins("gst-plugins-good")

        assert status2 == 200
        assert body2["cached"] is True
        assert body2["fetchedAt"] == body1["fetchedAt"]
        assert body2["plugins"] == body1["plugins"]

    def test_per_module_cache_keys_are_distinct(self, penv, monkeypatch):
        penv.clear_module_cache("gst-plugins-bad")
        monkeypatch.setattr(
            penv.module, "fetch_module_plugin_trees",
            lambda module: {"gst": [_tree_entry(f"{module}-plugin")]})

        _, good = penv.list_module_plugins("gst-plugins-good")
        _, bad = penv.list_module_plugins("gst-plugins-bad")

        assert good["plugins"] == [{"name": "gst-plugins-good-plugin"}]
        assert bad["plugins"] == [{"name": "gst-plugins-bad-plugin"}]
        # ...and the module index item itself is untouched.
        assert penv.module_cache_item("gst-plugins-good")["cache_key"] != \
            penv.module_cache_item("gst-plugins-bad")["cache_key"]

    def test_fetch_failure_returns_module_listing_unavailable(
            self, penv, monkeypatch):
        def failing_fetch(module):
            raise ConnectionError("gitlab unreachable")

        monkeypatch.setattr(penv.module, "fetch_module_plugin_trees",
                            failing_fetch)

        status, body = penv.list_module_plugins("gst-plugins-good")

        # The existing distinct code lets the UI fall back to importing
        # the full plugin set.
        assert status == 502
        assert body["error"]["code"] == "MODULE_LISTING_UNAVAILABLE"

    def test_module_without_plugins_is_unavailable(self, penv, monkeypatch):
        monkeypatch.setattr(penv.module, "fetch_module_plugin_trees",
                            lambda module: {"gst": [], "ext": [], "sys": []})

        status, body = penv.list_module_plugins("gst-plugins-good")

        assert status == 502
        assert body["error"]["code"] == "MODULE_LISTING_UNAVAILABLE"

    @pytest.mark.parametrize("module", ["", "  ", "../etc", "a b",
                                        "mod/../../x"])
    def test_invalid_module_names_are_rejected(self, penv, module):
        status, body = penv.list_module_plugins(module)
        assert status == 400
        assert body["error"]["code"] == "INVALID_MODULE"

    def test_plain_listing_still_answers_without_the_module_param(
            self, penv, monkeypatch):
        """GET /plugin-modules without ?module= keeps returning the
        module index (backward compatible, no new route)."""
        penv.clear_cache()
        monkeypatch.setattr(penv.module, "fetch_module_listing",
                            lambda: MODULE_LISTING_PAGE)
        status, body = penv.list_modules()
        assert status == 200
        assert "modules" in body


# =====================================================================
# Per-plugin descriptions in the module plugin list
# =====================================================================
#
# GET /plugin-modules?module=<name> joins per-plugin descriptions from
# the monorepo's docs/gst_plugins_cache.json onto the [{name}] entries.
# Descriptions are an enhancement: every failure path (fetch error,
# oversized file, malformed JSON) degrades to entries without
# descriptions and never fails the listing.

MODULE_PLUGIN_DESCRIPTIONS = {
    "rtp": "Real-time Transport Protocol plugin",
    "jpeg": "JPeg plugin library",
    # udp / v4l2 intentionally absent: their entries stay without a
    # description.
}


class TestListModulePluginsDescriptions:
    def test_descriptions_join_onto_the_listed_plugins(
            self, penv, monkeypatch):
        monkeypatch.setattr(penv.module, "fetch_module_plugin_trees",
                            lambda module: MODULE_PLUGIN_TREES)
        monkeypatch.setattr(penv.module, "fetch_module_plugin_descriptions",
                            lambda module: dict(MODULE_PLUGIN_DESCRIPTIONS))

        status, body = penv.list_module_plugins("gst-plugins-good")

        assert status == 200
        assert body["plugins"] == [
            {"name": "jpeg", "description": "JPeg plugin library"},
            {"name": "rtp",
             "description": "Real-time Transport Protocol plugin"},
            {"name": "udp"},
            {"name": "v4l2"},
        ]

    def test_descriptions_are_included_in_the_cached_item(
            self, penv, monkeypatch):
        monkeypatch.setattr(penv.module, "fetch_module_plugin_trees",
                            lambda module: MODULE_PLUGIN_TREES)
        monkeypatch.setattr(penv.module, "fetch_module_plugin_descriptions",
                            lambda module: dict(MODULE_PLUGIN_DESCRIPTIONS))
        status1, body1 = penv.list_module_plugins("gst-plugins-good")
        assert status1 == 200

        item = penv.module_cache_item("gst-plugins-good")
        cached_by_name = {p["name"]: p for p in item["plugins"]}
        assert cached_by_name["rtp"]["description"] == \
            "Real-time Transport Protocol plugin"

        # A fresh cache is reused without re-fetching anything —
        # descriptions included.
        def exploding(*args):
            raise AssertionError("a fresh per-module cache must be reused")

        monkeypatch.setattr(penv.module, "fetch_module_plugin_trees",
                            exploding)
        monkeypatch.setattr(penv.module, "fetch_module_plugin_descriptions",
                            exploding)
        status2, body2 = penv.list_module_plugins("gst-plugins-good")
        assert status2 == 200
        assert body2["cached"] is True
        assert body2["plugins"] == body1["plugins"]

    def test_description_fetch_failure_never_fails_the_listing(
            self, penv, monkeypatch):
        """Descriptions degrade to nothing: even a raising description
        source must not fail the listing (the fetch helper itself never
        raises, but the join must tolerate an empty result)."""
        monkeypatch.setattr(penv.module, "fetch_module_plugin_trees",
                            lambda module: MODULE_PLUGIN_TREES)
        monkeypatch.setattr(penv.module, "fetch_module_plugin_descriptions",
                            lambda module: {})

        status, body = penv.list_module_plugins("gst-plugins-good")

        assert status == 200
        assert body["plugins"] == [{"name": "jpeg"}, {"name": "rtp"},
                                   {"name": "udp"}, {"name": "v4l2"}]


class _FakeDescriptionsResponse:
    """Stand-in for requests.get(..., stream=True) responses."""

    def __init__(self, payload: bytes, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise ConnectionError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size):
        for i in range(0, len(self.payload), chunk_size):
            yield self.payload[i:i + chunk_size]


class TestFetchModulePluginDescriptions:
    """fetch_module_plugin_descriptions never raises: every failure
    returns {} so descriptions never block a listing."""

    def _patch(self, aws_stack, monkeypatch, response=None, exc=None):
        def fake_get(url, **kwargs):
            if exc is not None:
                raise exc
            assert "gst-plugins-good/docs/gst_plugins_cache.json" in url
            return response

        monkeypatch.setattr(aws_stack.plugin_importer.requests, "get",
                            fake_get)
        return aws_stack.plugin_importer.fetch_module_plugin_descriptions(
            "gst-plugins-good")

    def test_parses_the_cache_into_descriptions(self, aws_stack,
                                                monkeypatch):
        payload = json.dumps({
            "rtp": {"description": "Real-time Transport Protocol plugin",
                    "elements": {}},
            "udp": {"description": "", "elements": {}},
        }).encode("utf-8")
        result = self._patch(aws_stack, monkeypatch,
                             response=_FakeDescriptionsResponse(payload))
        assert result == {
            "rtp": "Real-time Transport Protocol plugin"}

    def test_http_failure_returns_empty(self, aws_stack, monkeypatch):
        result = self._patch(
            aws_stack, monkeypatch,
            response=_FakeDescriptionsResponse(b"", status_code=404))
        assert result == {}

    def test_network_error_returns_empty(self, aws_stack, monkeypatch):
        result = self._patch(aws_stack, monkeypatch,
                             exc=ConnectionError("unreachable"))
        assert result == {}

    def test_oversized_cache_returns_empty(self, aws_stack, monkeypatch):
        mod = aws_stack.plugin_importer
        monkeypatch.setattr(mod, "MAX_PLUGIN_DESCRIPTION_CACHE_BYTES", 64)
        payload = json.dumps(
            {"rtp": {"description": "x" * 256}}).encode("utf-8")
        result = self._patch(aws_stack, monkeypatch,
                             response=_FakeDescriptionsResponse(payload))
        assert result == {}

    def test_malformed_json_returns_empty(self, aws_stack, monkeypatch):
        result = self._patch(
            aws_stack, monkeypatch,
            response=_FakeDescriptionsResponse(b"{not json"))
        assert result == {}


# =====================================================================
# Platform requirements/compatibility check (advisory, pure)
# =====================================================================

# gst-plugins-good main-branch style: the requirement is the project's
# own major.minor series via the '.format(...)' form (requires
# GStreamer >= 1.24 -> fails on arm64_jp4/1.14 and arm64_jp5/1.16).
GST_GOOD_MAIN_MESON = """\
project('gst-plugins-good', 'c',
  version : '1.24.2',
  meson_version : '>= 1.1',
  default_options : [ 'warning_level=1', 'buildtype=debugoptimized' ])

gst_version = meson.project_version()
version_arr = gst_version.split('.')
gst_version_major = version_arr[0].to_int()
gst_version_minor = version_arr[1].to_int()
gst_version_micro = version_arr[2].to_int()

gst_req = '>= @0@.@1@.0'.format(gst_version_major, gst_version_minor)

gst_dep = dependency('gstreamer-1.0', version : gst_req,
  fallback : ['gstreamer', 'gst_dep'])
"""

# gst-plugins-good 1.16 release-branch style (same '.format(...)' form,
# older meson syntax and project version).
GST_GOOD_116_MESON = """\
project('gst-plugins-good', 'c',
  version : '1.16.3',
  meson_version : '>= 0.47',
  default_options : [ 'warning_level=1', 'buildtype=debugoptimized' ])

gst_version = meson.project_version()
version_arr = gst_version.split('.')
gst_version_major = version_arr[0].to_int()
gst_version_minor = version_arr[1].to_int()

gst_req = '>= @0@.@1@.0'.format(gst_version_major, gst_version_minor)

gst_dep = dependency('gstreamer-1.0', version : gst_req,
  fallback : ['gstreamer', 'gst_dep'])
"""

CONFIGURE_AC_GST_REQUIRED = """\
AC_INIT([gst-myfilter], [1.0])
dnl minimum GStreamer the plugin needs
GST_REQUIRED=1.16.2
PKG_CHECK_MODULES(GST, gstreamer-1.0 >= $GST_REQUIRED)
AC_SUBST(GST_REQUIRED)
"""


class TestGstreamerRequirement:
    """gstreamer_requirement parsing over real-ish build definitions."""

    def test_gst_plugins_good_main_format_pattern(self, aws_stack):
        files = {"meson.build": GST_GOOD_MAIN_MESON, "gst/rtsp/meson.build": None}
        assert aws_stack.plugin_importer.gstreamer_requirement(files) == "1.24.0"

    def test_gst_plugins_good_116_format_pattern(self, aws_stack):
        files = {"meson.build": GST_GOOD_116_MESON}
        assert aws_stack.plugin_importer.gstreamer_requirement(files) == "1.16.0"

    @pytest.mark.parametrize("line,expected", [
        ("gst_req = '>= 1.24'", "1.24"),
        ('gst_req = ">= 1.18.0"', "1.18.0"),
        ("gst_req = '>=1.20'", "1.20"),
    ])
    def test_gst_req_literal(self, aws_stack, line, expected):
        files = {"meson.build": f"project('p', 'c')\n{line}\n"}
        assert aws_stack.plugin_importer.gstreamer_requirement(files) == expected

    def test_dependency_inline_version(self, aws_stack):
        files = {"meson.build":
                 "project('p', 'c')\n"
                 "gst_dep = dependency('gstreamer-1.0', version : '>= 1.20',\n"
                 "  required : true)\n"}
        assert aws_stack.plugin_importer.gstreamer_requirement(files) == "1.20"

    def test_autotools_gst_required_assignment(self, aws_stack):
        files = {"configure.ac": CONFIGURE_AC_GST_REQUIRED}
        assert aws_stack.plugin_importer.gstreamer_requirement(files) == "1.16.2"

    def test_autotools_ac_subst_pattern(self, aws_stack):
        files = {"configure.in":
                 "AC_INIT([p], [1.0])\nAC_SUBST(GST_REQUIRED, 1.14)\n"}
        assert aws_stack.plugin_importer.gstreamer_requirement(files) == "1.14"

    def test_autotools_pkg_check_inline_version(self, aws_stack):
        files = {"configure.ac":
                 "AC_INIT([p], [1.0])\n"
                 "PKG_CHECK_MODULES(GST, gstreamer-1.0 >= 1.18)\n"}
        assert aws_stack.plugin_importer.gstreamer_requirement(files) == "1.18"

    def test_no_requirement_returns_none(self, aws_stack):
        # A dependency without a version constraint carries no
        # requirement: assume compatible everywhere.
        assert aws_stack.plugin_importer.gstreamer_requirement(
            {"meson.build": MESON_PLUGIN}) is None

    def test_missing_root_build_definition_returns_none(self, aws_stack):
        # Only non-root meson.build files: the root requirement is
        # undeterminable (parsing degrades, never blocks).
        assert aws_stack.plugin_importer.gstreamer_requirement(
            {"gst/rtsp/meson.build": GST_GOOD_MAIN_MESON,
             "README.md": None}) is None
        assert aws_stack.plugin_importer.gstreamer_requirement({}) is None

    def test_format_pattern_without_project_version_returns_none(self, aws_stack):
        files = {"meson.build":
                 "gst_req = '>= @0@.@1@.0'.format(gst_version_major, "
                 "gst_version_minor)\n"}
        assert aws_stack.plugin_importer.gstreamer_requirement(files) is None


class TestPlatformCompatibility:
    """platform_compatibility over the requested Target_Architectures."""

    ALL_ARCHES = ["x86_64", "x86_64_nvidia", "arm64_jp4", "arm64_jp5",
                  "arm64_jp6"]

    def test_1_24_requirement_matches_production_incident(self, aws_stack):
        # gst-plugins-good main (>= 1.24): fails on arm64_jp4 (1.14)
        # and arm64_jp5 (1.16), succeeds on x86_64 / x86_64_nvidia /
        # arm64_jp6 (1.20).
        result = aws_stack.plugin_importer.platform_compatibility(
            "1.24.0", self.ALL_ARCHES, "gst-plugins-good")
        assert {a: e["compatible"] for a, e in result.items()} == {
            "x86_64": True, "x86_64_nvidia": True,
            "arm64_jp4": False, "arm64_jp5": False, "arm64_jp6": True,
        }
        assert result["arm64_jp5"]["reason"] == (
            "The source requires GStreamer >= 1.24.0; "
            "arm64 JetPack 5 provides 1.16")
        # Official module: the release branch matching the platform's
        # GStreamer minor is suggested (verified working in production).
        assert result["arm64_jp4"]["suggestedRevision"] == "1.14"
        assert result["arm64_jp5"]["suggestedRevision"] == "1.16"
        # Compatible platforms carry no reason and no suggestion.
        assert result["arm64_jp6"]["reason"] is None
        assert result["arm64_jp6"]["suggestedRevision"] is None
        assert result["arm64_jp5"]["platformVersion"] == "1.16"
        assert result["arm64_jp5"]["requiredVersion"] == "1.24.0"

    def test_classification_also_marks_official(self, aws_stack):
        # A repo classified good/bad/ugly (no moduleName) still gets
        # branch suggestions.
        result = aws_stack.plugin_importer.platform_compatibility(
            "1.24.0", ["arm64_jp5"], "ugly")
        assert result["arm64_jp5"]["suggestedRevision"] == "1.16"

    @pytest.mark.parametrize("classification_or_module", [None, "",
                                                          "unclassified"])
    def test_non_official_repo_gets_no_suggestion(self, aws_stack,
                                                  classification_or_module):
        result = aws_stack.plugin_importer.platform_compatibility(
            "1.24.0", ["arm64_jp4", "arm64_jp5"], classification_or_module)
        for entry in result.values():
            assert entry["compatible"] is False
            assert entry["reason"]  # the why is still explained
            assert entry["suggestedRevision"] is None

    def test_no_requirement_is_compatible_everywhere(self, aws_stack):
        result = aws_stack.plugin_importer.platform_compatibility(
            None, self.ALL_ARCHES, "gst-plugins-good")
        assert all(e["compatible"] for e in result.values())
        assert all(e["reason"] is None for e in result.values())
        assert all(e["suggestedRevision"] is None for e in result.values())

    def test_1_16_requirement_splits_jp4_from_jp5(self, aws_stack):
        result = aws_stack.plugin_importer.platform_compatibility(
            "1.16.0", ["arm64_jp4", "arm64_jp5"], "gst-plugins-good")
        assert result["arm64_jp4"]["compatible"] is False
        assert result["arm64_jp4"]["suggestedRevision"] == "1.14"
        assert result["arm64_jp5"]["compatible"] is True

    def test_unknown_architecture_counts_compatible(self, aws_stack):
        result = aws_stack.plugin_importer.platform_compatibility(
            "1.24.0", ["riscv64"], "gst-plugins-good")
        assert result["riscv64"]["compatible"] is True
        assert result["riscv64"]["platformVersion"] is None

    def test_unparseable_requirement_counts_compatible(self, aws_stack):
        result = aws_stack.plugin_importer.platform_compatibility(
            "banana", ["arm64_jp4"], "gst-plugins-good")
        assert result["arm64_jp4"]["compatible"] is True


class TestEvaluateFetchedTreeCompatibility:
    """evaluate_fetched_tree carries the advisory map in every outcome
    and never blocks builds on it."""

    BUILDABLE_1_24 = {
        "meson.build":
            "project('gst-myfilter', 'c', version : '1.0')\n"
            "gst_req = '>= 1.24'\n"
            "gst_dep = dependency('gstreamer-1.0', version : gst_req)\n"
            "shared_library('gstmyfilter', 'gstmyfilter.c', "
            "dependencies: [gst_dep])\n",
        "gstmyfilter.c": None,
    }

    def test_updates_carry_map_and_builds_still_queue(self, aws_stack):
        mod = aws_stack.plugin_importer
        scan, updates = mod.evaluate_fetched_tree(
            self.BUILDABLE_1_24, "gst-myfilter", [],
            ["x86_64", "arm64_jp5"],
            classification_or_module="gst-plugins-good")
        assert scan["buildable"] is True
        compat = updates["platform_compatibility"]
        assert compat["arm64_jp5"]["compatible"] is False
        assert compat["x86_64"]["compatible"] is True
        # Advisory only: the incompatible architecture still queues.
        assert updates["import_status"] == mod.IMPORT_STATUS_IMPORTED
        assert updates["artifacts"] == {
            "x86_64": {"buildStatus": "queued"},
            "arm64_jp5": {"buildStatus": "queued"},
        }

    def test_unbuildable_tree_still_carries_map(self, aws_stack):
        mod = aws_stack.plugin_importer
        scan, updates = mod.evaluate_fetched_tree(
            {"README.md": None}, "p", [], ["arm64_jp4"])
        assert scan["buildable"] is False
        assert updates["import_status"] == mod.IMPORT_STATUS_FAILED
        # No requirement determinable: compatible (advisory degrade).
        assert updates["platform_compatibility"]["arm64_jp4"]["compatible"] is True

    def test_no_requirement_yields_all_compatible_map(self, aws_stack):
        mod = aws_stack.plugin_importer
        _, updates = mod.evaluate_fetched_tree(
            {"meson.build": MESON_PLUGIN}, "p", [], ["arm64_jp4", "x86_64"])
        compat = updates["platform_compatibility"]
        assert all(entry["compatible"] for entry in compat.values())


class TestVersionDetailPlatformCompatibility:
    """version_detail / import_detail expose the recorded map additively."""

    def _item(self, **extra):
        return {
            "plugin_id": "p-1", "version": 1, "usecase_id": "uc-1",
            "name": "gst-plugins-good", "kind": "imported",
            "provenance": {}, "lifecycle_state": "dev", "review": {},
            "artifacts": {}, "component": {},
            "source_s3_prefix": "plugin-sources/uc-1/p-1/1/",
            "created_by": "u-1", "created_at": 1, "updated_at": 1,
            **extra,
        }

    def test_version_detail_includes_recorded_map(self, aws_stack):
        compat = {"arm64_jp5": {
            "compatible": False, "platformVersion": "1.16",
            "requiredVersion": "1.24.0",
            "reason": "The source requires GStreamer >= 1.24.0; "
                      "arm64 JetPack 5 provides 1.16",
            "suggestedRevision": "1.16"}}
        detail = aws_stack.plugin_records.version_detail(
            self._item(platform_compatibility=compat))
        assert detail["platform_compatibility"] == compat

    def test_version_detail_omits_absent_map(self, aws_stack):
        detail = aws_stack.plugin_records.version_detail(self._item())
        assert "platform_compatibility" not in detail

    def test_import_detail_includes_recorded_map(self, aws_stack):
        compat = {"x86_64": {
            "compatible": True, "platformVersion": "1.20",
            "requiredVersion": None, "reason": None,
            "suggestedRevision": None}}
        detail = aws_stack.plugin_importer.import_detail(
            self._item(import_status="imported",
                       platform_compatibility=compat))
        assert detail["platform_compatibility"] == compat
