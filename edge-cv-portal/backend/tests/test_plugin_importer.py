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
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

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


# =====================================================================
# Bug condition exploration: post-import per-platform revision
# adjustment (imported-plugin-revision-adjustment-fix, Property 1)
# =====================================================================
#
# **Validates: Requirements 1.1, 1.2, 1.3, 1.4**
#
# These tests encode the EXPECTED behavior of the post-import
# adjust-revision path (Property 1: applying a per-platform revision
# override fetches/reuses the adjusted tree, maps arch_revisions, and
# re-runs the affected build). On the UNFIXED code they MUST FAIL —
# the failure is the point: it surfaces the counterexamples proving
# the dead end exists (missing route, fetch-result guard on
# import_status == 'fetching', retry re-using the identical flat
# prefix). Once tasks 3.1-3.5 land, these same tests validate the fix.

#: Buildable single-plugin tree requiring GStreamer >= 1.24: with the
#: gst-plugins-good classification this settles 'imported' carrying an
#: incompatible platform_compatibility entry for arm64_jp4 (platform
#: 1.14) with suggestedRevision '1.14' — the bug condition anchor.
BUILDABLE_1_24_FILES = {
    "meson.build":
        "project('gst-myfilter', 'c', version : '1.0')\n"
        "gst_req = '>= 1.24'\n"
        "gst_dep = dependency('gstreamer-1.0', version : gst_req)\n"
        "shared_library('gstmyfilter', 'gstmyfilter.c', "
        "dependencies: [gst_dep])\n",
    "gstmyfilter.c": None,
}


def adjustable_revisions():
    """Requested revisions satisfying the bug condition: non-empty,
    different from the record's effective revision ('main'). Constrained
    to git-ref-shaped strings (release branches like the recorded
    suggestion, plus arbitrary branch/tag names)."""
    release_branches = st.sampled_from(["1.14", "1.16", "1.18",
                                        "1.20", "1.22"])
    branch_names = st.text(
        alphabet="abcdefghijklmnopqrstuvwxyz0123456789._-",
        min_size=1, max_size=16,
    ).map(lambda s: s.strip("-.")).filter(lambda s: s and s != "main")
    return st.one_of(release_branches, branch_names)


class AdjustRevisionEnv(ImporterEnv):
    """ImporterEnv plus the (expected) adjust-revision invocation and
    adjustment fetch-result delivery."""

    def adjust_revision(self, user, plugin_id, version, architecture,
                        revision):
        event = {
            "httpMethod": "POST",
            "resource": "/plugins/{id}/versions/{v}/adjust-revision",
            "path": f"/plugins/{plugin_id}/versions/{version}"
                    "/adjust-revision",
            "pathParameters": {"id": plugin_id, "v": str(version)},
            "queryStringParameters": None,
            "body": json.dumps({"architecture": architecture,
                                "revision": revision}),
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

    def settled_incompatible_import(self, admin, usecase_id):
        """Settled ('imported') flat single-revision record whose
        arm64_jp4 platform_compatibility entry is incompatible with
        suggestedRevision '1.14' (isBugCondition holds for any
        requested revision != 'main')."""
        _, result, record = self.complete_import(admin, {
            "usecase_id": usecase_id,
            "repo_url": REPO_URL,
            "revision": "main",
            "architectures": ["x86_64", "arm64_jp4"],
        }, files=BUILDABLE_1_24_FILES)
        assert result == {"recorded": True, "import_status": "imported"}
        compat = record["platform_compatibility"]["arm64_jp4"]
        assert compat["compatible"] is False
        assert compat["suggestedRevision"] == "1.14"
        return record

    def fetch_result_detail_with_slug(self, plugin, build_id, slug,
                                      status="SUCCEEDED"):
        detail = self.fetch_result_detail(plugin, build_id, status=status)
        (detail["additional-information"]["environment"]
         ["environment-variables"]).append(
            {"name": "REVISION_SLUG", "value": slug})
        return detail


@pytest.fixture
def adj_env(aws_stack):
    return AdjustRevisionEnv(aws_stack)


@pytest.fixture
def settled_incompatible(adj_env, admin_setup):
    """(admin, settled record) with the arm64_jp4 bug condition."""
    usecase_id, admin = admin_setup
    record = adj_env.settled_incompatible_import(admin, usecase_id)
    return admin, record


class TestRevisionAdjustmentBugExploration:
    """Bug condition exploration (Property 1, exploratory bugfix
    workflow). EXPECTED TO FAIL on unfixed code.

    **Validates: Requirements 1.1, 1.2, 1.3, 1.4**
    """

    @given(revision=adjustable_revisions())
    @settings(max_examples=15, deadline=None,
              suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_adjust_revision_route_answers_202_with_a_fetch_slot(
            self, adj_env, settled_incompatible, revision):
        """For any requested revision satisfying the bug condition,
        POST /plugins/{id}/versions/{v}/adjust-revision answers 202 and
        the record carries a `fetches` slot recording that revision.

        Unfixed code: plugin_importer.handler routes only
        /plugins/import, /plugin-modules, and .../select-plugins — the
        adjust call returns 404 NOT_FOUND (no API path exists to change
        a platform's revision after import, bug 1.3)."""
        admin, record = settled_incompatible

        status, body = adj_env.adjust_revision(
            admin, record["plugin_id"], 1, "arm64_jp4", revision)

        assert status == 202, (
            f"adjust-revision answered {status} ({body}): no post-import "
            "adjustment path exists")
        updated = adj_env.get_record(record["plugin_id"])
        fetches = updated.get("fetches") or {}
        assert any((entry or {}).get("revision") == revision
                   for entry in fetches.values()), (
            f"no fetches slot records the requested revision {revision!r}")

    def test_adjustment_changes_the_effective_source_prefix(
            self, adj_env, settled_incompatible):
        """Applying the suggested revision to arm64_jp4 changes
        arch_source_prefix(item, 'arm64_jp4') to the adjusted entry's
        rev-{slug}/ prefix.

        Unfixed code: every reachable operation (including a plain
        retry) re-uses the identical flat source_s3_prefix — nothing
        changes the platform's effective revision (bug 1.2, 1.4)."""
        admin, record = settled_incompatible
        plugin_id = record["plugin_id"]
        builds_mod = adj_env.stack.plugin_builds
        flat_prefix = builds_mod.arch_source_prefix(record, "arm64_jp4")
        assert flat_prefix == record["source_s3_prefix"]  # flat layout

        status, body = adj_env.adjust_revision(
            admin, plugin_id, 1, "arm64_jp4", "1.14")
        assert status == 202, (
            f"adjust-revision answered {status} ({body}): no operation "
            "exists that changes the effective revision")

        updated = adj_env.get_record(plugin_id)
        fetches = updated.get("fetches") or {}
        slugs = [s for s, e in fetches.items()
                 if (e or {}).get("revision") == "1.14"]
        assert slugs, "the adjustment must record a fetches slot for 1.14"
        slug = slugs[0]

        # Settle a still-running adjustment fetch (SUCCEEDED) so the
        # arch_revisions mapping flips.
        if fetches[slug].get("status") == "fetching":
            detail = adj_env.fetch_result_detail_with_slug(
                {"plugin_id": plugin_id, "version": 1,
                 "usecase_id": record["usecase_id"]},
                fetches[slug].get("fetch_build_id"), slug)
            adj_env.deliver_fetch_result(detail)
            updated = adj_env.get_record(plugin_id)

        adjusted_prefix = builds_mod.arch_source_prefix(updated, "arm64_jp4")
        assert adjusted_prefix == f"{record['source_s3_prefix']}rev-{slug}/"
        assert adjusted_prefix != flat_prefix

    def test_settled_record_adjustment_fetch_result_is_processed(
            self, adj_env, settled_incompatible):
        """A fetch result for a settled record (import_status ==
        'imported') carrying an adjustment marker (a fetches entry with
        pending_archs) is processed: the slot settles 'succeeded' and
        the pending arch maps through arch_revisions.

        Unfixed code: handle_fetch_result guards on import_status ==
        'fetching' and skips the delivery as 'already recorded' (root
        cause 2 — adjustment fetches for settled records are dropped,
        bug 1.3)."""
        admin, record = settled_incompatible
        plugin_id = record["plugin_id"]
        slug = adj_env.module.revision_slug("1.14")
        fetch_build_id = "dda-plugin-fetch:adjustment-1"
        # Seed the adjustment marker exactly as the (expected) endpoint
        # writes it: a 'fetching' fetches entry with pending_archs, the
        # affected arch queued.
        adj_env.stack.tables.plugin_records.update_item(
            Key={"plugin_id": plugin_id, "version": 1},
            UpdateExpression="SET fetches = :f, artifacts.arm64_jp4 = :q",
            ExpressionAttributeValues={
                ":f": {slug: {
                    "revision": "1.14",
                    "source_prefix":
                        f"{record['source_s3_prefix']}rev-{slug}/",
                    "status": "fetching",
                    "fetch_build_id": fetch_build_id,
                    "pending_archs": ["arm64_jp4"],
                }},
                ":q": {"buildStatus": "queued"},
            })

        result = adj_env.deliver_fetch_result(
            adj_env.fetch_result_detail_with_slug(
                {"plugin_id": plugin_id, "version": 1,
                 "usecase_id": record["usecase_id"]},
                fetch_build_id, slug))

        assert result.get("recorded") is True, (
            f"the adjustment fetch result was dropped: {result}")
        updated = adj_env.get_record(plugin_id)
        assert updated["fetches"][slug]["status"] == "succeeded"
        assert (updated.get("arch_revisions") or {}).get("arm64_jp4") == slug


# =====================================================================
# Preservation property tests: non-adjusted flows are unchanged
# (imported-plugin-revision-adjustment-fix, Property 2)
# =====================================================================
#
# **Validates: Requirements 3.1, 3.3, 3.4, 3.5, 3.6**
#
# Observation-first: these properties capture the behavior OBSERVED on
# the UNFIXED code for inputs where the bug condition does NOT hold —
# import-time multi-revision plans and persistence (3.1), plain build
# retries (3.3), the flat single-revision source layout (3.4), other
# architectures' artifact entries and builds_view rows (3.5), and the
# once-per-round component auto-packaging trigger (3.6). They MUST
# PASS on the unfixed code (that run is the baseline the fix must
# preserve) and MUST STILL PASS after tasks 3.1-3.5 land (task 3.7
# re-runs them unchanged). The compatible-platform display baseline
# (3.2) lives in the frontend suite (importFlow.test.ts).

import copy

PRESERVATION_ARCHS = ["x86_64", "x86_64_nvidia", "arm64_jp4",
                      "arm64_jp5", "arm64_jp6"]

PRESERVATION_BASE_PREFIX = "plugin-sources/uc-pres/p-pres/1/"


def preservation_revisions():
    """Revision strings as the import accepts them (non-empty once
    trimmed), biased toward release branches and slug-collision-prone
    shapes ('a/b' and 'a b' both slug to 'a-b')."""
    return st.one_of(
        st.sampled_from(["main", "1.14", "1.16", "1.24.2",
                         "a/b", "a b", "a-b"]),
        st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789._-/ ",
                min_size=1, max_size=12).filter(lambda s: s.strip()),
    )


def preservation_slugs():
    """S3-key-safe revision slugs as revision_slug produces them."""
    return st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789._-",
                   min_size=1, max_size=8).map(
        lambda s: s.strip("-.")).filter(bool)


def artifact_entries():
    """One per-arch artifact entry exactly as plugin_builds records
    them: succeeded (s3Key + checksum + signature), failed (logTail),
    or still in flight (queued/building)."""
    succeeded = st.fixed_dictionaries({
        "buildStatus": st.just("succeeded"),
        "s3Key": preservation_slugs().map(
            lambda s: f"workflow-plugins/custom/uc-pres/{s}.so"),
        "checksum": st.text("0123456789abcdef", min_size=8, max_size=8),
        "signature": st.text("ABCDEFab0123", min_size=8, max_size=8),
        "logTail": st.just(""),
    })
    failed = st.fixed_dictionaries({
        "buildStatus": st.just("failed"),
        "logTail": st.text(max_size=30),
    })
    in_flight = st.fixed_dictionaries({
        "buildStatus": st.sampled_from(["queued", "building"]),
        "logTail": st.just(""),
    })
    return st.one_of(succeeded, failed, in_flight)


@st.composite
def preservation_records(draw):
    """(Plugin_Record version item, adjusted_arch): a settled imported
    record — flat single-revision (3.4) or multi-revision with a
    fetches map and a partial arch_revisions mapping (3.1) — plus the
    one architecture an adjustment would target. Every OTHER
    architecture is a non-adjusted input whose behavior these
    properties pin down (3.5)."""
    archs = draw(st.lists(st.sampled_from(PRESERVATION_ARCHS),
                          min_size=2, max_size=5, unique=True))
    item = {
        "plugin_id": "p-pres", "version": 1, "usecase_id": "uc-pres",
        "kind": "imported", "import_status": "imported",
        "name": "gst-plugins-good",
        "requested_architectures": list(archs),
        "provenance": {"repoUrl": REPO_URL, "revision": "main"},
        "artifacts": {arch: draw(artifact_entries()) for arch in archs},
    }
    if draw(st.booleans()):
        item["components_triggered"] = 1234
    if draw(st.booleans()):
        # Multi-revision record: fetches map + partial arch mapping.
        slugs = draw(st.lists(preservation_slugs(),
                              min_size=1, max_size=3, unique=True))
        item["fetches"] = {
            slug: {"revision": draw(preservation_revisions()),
                   "source_prefix": f"{PRESERVATION_BASE_PREFIX}rev-{slug}/",
                   "status": "succeeded"}
            for slug in slugs
        }
        mapped_archs = draw(st.lists(st.sampled_from(archs),
                                     unique=True, max_size=len(archs)))
        if mapped_archs:
            item["arch_revisions"] = {
                arch: draw(st.sampled_from(slugs)) for arch in mapped_archs}
        item["source_s3_prefix"] = f"{PRESERVATION_BASE_PREFIX}rev-{slugs[0]}/"
        item["default_fetch_slug"] = slugs[0]
    else:
        # Flat single-revision record (3.4).
        item["source_s3_prefix"] = PRESERVATION_BASE_PREFIX
    adjusted = draw(st.sampled_from(archs))
    return item, adjusted


def apply_adjustment_record_shape(importer, item, arch, revision):
    """The record mutation the adjust-revision fix is designed to
    persist for `arch` (design 'Persist + act', settled on fetch
    success): a fetches slot for the requested revision under a fresh
    slug (numeric-suffix collision disambiguation like
    revision_fetch_plan), arch_revisions[arch] mapped to it, the arch
    re-queued, components_triggered REMOVEd (new build round). Applied
    to a deep copy — source_s3_prefix, default_fetch_slug, and every
    other architecture's artifact entry are NOT written, which is
    exactly the preservation contract (3.4, 3.5) these properties
    check against the UNFIXED pure functions."""
    updated = copy.deepcopy(item)
    fetches = dict(updated.get("fetches") or {})
    base = slug = importer.revision_slug(revision)
    suffix = 2
    while slug in fetches:
        slug = f"{base}-{suffix}"
        suffix += 1
    fetches[slug] = {
        "revision": revision,
        "source_prefix": f"{PRESERVATION_BASE_PREFIX}rev-{slug}/",
        "status": "succeeded",
    }
    updated["fetches"] = fetches
    arch_revisions = dict(updated.get("arch_revisions") or {})
    arch_revisions[arch] = slug
    updated["arch_revisions"] = arch_revisions
    updated["artifacts"] = dict(updated["artifacts"])
    updated["artifacts"][arch] = {"buildStatus": "queued"}
    updated.pop("components_triggered", None)
    return updated


class TestPreservationArchSourcePrefix:
    """arch_source_prefix preservation (Property 2).

    **Validates: Requirements 3.1, 3.4, 3.5**
    """

    @given(record=preservation_records(), revision=preservation_revisions())
    @settings(max_examples=25, deadline=None,
              suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_non_adjusted_archs_resolve_the_same_prefix(
            self, aws_stack, record, revision):
        """For any generated record (with/without a fetches map and
        arch_revisions) and any adjustment-shaped mutation of one
        architecture, every NON-adjusted architecture resolves to the
        identical source prefix, and the record's flat
        source_s3_prefix is never rewritten (3.1, 3.4, 3.5)."""
        item, adjusted = record
        builds = aws_stack.plugin_builds
        before = {arch: builds.arch_source_prefix(item, arch)
                  for arch in item["requested_architectures"]}

        updated = apply_adjustment_record_shape(
            aws_stack.plugin_importer, item, adjusted, revision)

        for arch in item["requested_architectures"]:
            if arch != adjusted:
                assert builds.arch_source_prefix(updated, arch) == \
                    before[arch]
        assert updated["source_s3_prefix"] == item["source_s3_prefix"]
        assert updated.get("default_fetch_slug") == \
            item.get("default_fetch_slug")

    @given(record=preservation_records())
    @settings(max_examples=25, deadline=None,
              suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_flat_records_resolve_the_flat_prefix_for_every_arch(
            self, aws_stack, record):
        """Flat single-revision records (no fetches / arch_revisions)
        keep the flat source_s3_prefix layout for every architecture
        (3.4)."""
        item, _ = record
        flat = {k: v for k, v in item.items()
                if k not in ("fetches", "arch_revisions",
                             "default_fetch_slug")}
        flat["source_s3_prefix"] = PRESERVATION_BASE_PREFIX
        for arch in flat["requested_architectures"]:
            assert aws_stack.plugin_builds.arch_source_prefix(flat, arch) \
                == PRESERVATION_BASE_PREFIX

    @given(record=preservation_records())
    @settings(max_examples=25, deadline=None,
              suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_unmapped_archs_fall_back_to_the_records_flat_tree(
            self, aws_stack, record):
        """Architectures without an arch_revisions mapping resolve to
        the record's source_s3_prefix — the fallback multi-revision
        records rely on (3.1, 3.4)."""
        item, _ = record
        mapped = set(item.get("arch_revisions") or {})
        for arch in item["requested_architectures"]:
            if arch not in mapped:
                assert aws_stack.plugin_builds.arch_source_prefix(
                    item, arch) == item["source_s3_prefix"]


class TestPreservationUntouchedEntries:
    """Untouched-entry preservation (Property 2).

    **Validates: Requirements 3.5**
    """

    @given(record=preservation_records(), revision=preservation_revisions())
    @settings(max_examples=25, deadline=None,
              suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_other_archs_entries_and_builds_view_rows_are_identical(
            self, aws_stack, record, revision):
        """For any generated record, an adjustment-shaped mutation of
        one architecture leaves every other architecture's artifact
        entry (status, s3Key, checksum, signature, logTail) and its
        builds_view row byte-identical (3.5)."""
        item, adjusted = record
        builds = aws_stack.plugin_builds
        snapshot = copy.deepcopy(item)
        view_before = builds.builds_view(item)

        updated = apply_adjustment_record_shape(
            aws_stack.plugin_importer, item, adjusted, revision)

        assert item == snapshot  # the mutation is pure (copy only)
        view_after = builds.builds_view(updated)
        for arch in item["requested_architectures"]:
            if arch == adjusted:
                continue
            assert updated["artifacts"][arch] == item["artifacts"][arch]
            assert view_after["builds"][arch] == view_before["builds"][arch]
        assert view_after["requested_architectures"] == \
            view_before["requested_architectures"]


class TestPreservationRevisionFetchPlan:
    """Import-flow preservation: revision_fetch_plan output (Property 2).

    **Validates: Requirements 3.1, 3.4**
    """

    BASE = PRESERVATION_BASE_PREFIX

    @given(revision=st.one_of(st.none(), preservation_revisions()),
           data=st.data())
    @settings(max_examples=25, deadline=None,
              suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_plan_fetches_each_distinct_revision_once_and_maps_archs(
            self, aws_stack, revision, data):
        """For any top-level revision and any arch_revisions overrides:
        a single distinct effective revision collapses to today's flat
        single-fetch layout (3.4); multiple distinct revisions plan one
        fetch per DISTINCT revision into rev-{slug}/ prefixes with
        every architecture mapped to the slug recording its effective
        revision, slug collisions disambiguated without clobbering
        (3.1)."""
        mod = aws_stack.plugin_importer
        archs = data.draw(st.lists(st.sampled_from(PRESERVATION_ARCHS),
                                   min_size=1, max_size=5, unique=True),
                          label="architectures")
        overrides = data.draw(st.dictionaries(
            st.sampled_from(archs), preservation_revisions(),
            max_size=len(archs)), label="arch_revisions")

        plan = mod.revision_fetch_plan(revision, overrides, archs, self.BASE)

        effective = {arch: (overrides.get(arch) or "").strip()
                     or (revision or "") for arch in archs}
        distinct = set(effective.values())
        if len(distinct) <= 1:
            single = next(iter(distinct)) if distinct else (revision or "")
            assert plan == {"mode": "single", "revision": single or None}
            return

        assert plan["mode"] == "multi"
        fetches = plan["fetches"]
        # One fetch per DISTINCT effective revision; unique slugs.
        assert len(fetches) == len(distinct)
        assert sorted(e["revision"] for e in fetches.values()) == \
            sorted((rev or mod.DEFAULT_REVISION) for rev in distinct)
        for slug, entry in fetches.items():
            assert entry["source_prefix"] == f"{self.BASE}rev-{slug}/"
            assert entry["status"] == "fetching"
        # Every arch maps to the slug recording its effective revision.
        for arch in archs:
            slug = plan["arch_revisions"][arch]
            assert fetches[slug]["revision"] == \
                (effective[arch] or mod.DEFAULT_REVISION)
        # The default tree is the top-level revision's when fetched,
        # deterministic otherwise.
        assert plan["default_slug"] in fetches
        if (revision or "") in distinct:
            assert fetches[plan["default_slug"]]["revision"] == \
                ((revision or "") or mod.DEFAULT_REVISION)


class PreservationEnv(ImporterEnv):
    """ImporterEnv plus POST .../build and per-arch build result
    delivery, for the plain-retry (3.3) and auto-packaging (3.6)
    baselines."""

    def post_build(self, user, plugin_id, version, body=None):
        event = {
            "httpMethod": "POST",
            "resource": "/plugins/{id}/versions/{v}/build",
            "path": f"/plugins/{plugin_id}/versions/{version}/build",
            "pathParameters": {"id": plugin_id, "v": str(version)},
            "queryStringParameters": None,
            "body": json.dumps(body or {}),
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
        response = self.stack.plugin_builds.handler(event, None)
        return response["statusCode"], json.loads(response["body"])

    def deliver_build_result(self, plugin, arch, build_id, status,
                             plugin_name):
        detail = {
            "build-status": status,
            "project-name": f"dda-plugin-build-{arch}",
            "build-id": ("arn:aws:codebuild:us-east-1:123456789012:build/"
                         + build_id),
            "additional-information": {
                "environment": {
                    "environment-variables": [
                        {"name": "USECASE_ID", "value": plugin["usecase_id"]},
                        {"name": "PLUGIN_ID", "value": plugin["plugin_id"]},
                        {"name": "PLUGIN_VERSION",
                         "value": str(plugin["version"])},
                        {"name": "PLUGIN_NAME", "value": plugin_name},
                        {"name": "TARGET_ARCH", "value": arch},
                    ],
                },
                "logs": {},
            },
        }
        return self.stack.plugin_builds.handler({
            "source": "aws.codebuild",
            "detail-type": "CodeBuild Build State Change",
            "detail": detail,
        }, None)


@pytest.fixture
def pres_env(aws_stack):
    return PreservationEnv(aws_stack)


@pytest.fixture
def settled_flat_import(pres_env, admin_setup):
    """(admin, settled flat single-revision record): builds building
    for three architectures, no fetches map, no arch_revisions — a
    non-bug-condition record for the retry baseline."""
    usecase_id, admin = admin_setup
    _, result, record = pres_env.complete_import(admin, {
        "usecase_id": usecase_id,
        "repo_url": REPO_URL,
        "revision": "main",
        "architectures": ["x86_64", "arm64_jp4", "arm64_jp5"],
    }, files={"meson.build": MESON_PLUGIN, "gstmyfilter.c": None})
    assert result == {"recorded": True, "import_status": "imported"}
    return admin, record


class TestPreservationPlainRetry:
    """Plain retry preservation (Property 2).

    **Validates: Requirements 3.3, 3.4, 3.5**
    """

    ARCHS = ["x86_64", "arm64_jp4", "arm64_jp5"]

    @given(data=st.data())
    @settings(max_examples=10, deadline=None,
              suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_retry_re_submits_the_same_tree_touching_only_retried_archs(
            self, pres_env, settled_flat_import, monkeypatch, data):
        """For any subset of architectures, POST .../build WITHOUT an
        adjustment StartBuilds each retried architecture from the
        IDENTICAL flat source tree (3.3, 3.4) and rewrites only the
        retried entries — non-retried architectures' artifact entries
        are byte-identical, and no fetches map or arch_revisions
        appears on the record (3.5)."""
        admin, record = settled_flat_import
        builds_module = pres_env.stack.plugin_builds
        if not isinstance(builds_module.codebuild, RecordingCodeBuild):
            monkeypatch.setattr(builds_module, "codebuild",
                                RecordingCodeBuild(builds_module.codebuild))
        recorder = builds_module.codebuild
        plugin_id = record["plugin_id"]
        flat_prefix = record["source_s3_prefix"]
        retried = data.draw(st.lists(st.sampled_from(self.ARCHS),
                                     min_size=1, max_size=3, unique=True),
                            label="retried architectures")
        before = pres_env.get_record(plugin_id)
        calls_before = len(recorder.calls)

        status, body = pres_env.post_build(admin, plugin_id, 1,
                                           {"architectures": retried})

        assert status == 202, body
        calls = recorder.calls[calls_before:]
        # One StartBuild per retried arch, sourcing the identical flat
        # tree — the retry never changes the effective revision (3.3).
        assert sorted(c["projectName"] for c in calls) == \
            sorted(f"dda-plugin-build-{a}" for a in retried)
        bucket = TEST_ENV["PORTAL_ARTIFACTS_BUCKET"]
        for call in calls:
            assert call["sourceLocationOverride"] == f"{bucket}/{flat_prefix}"

        after = pres_env.get_record(plugin_id)
        # The record writes touch only the retried arch entries; the
        # flat layout stays flat (3.4) and no adjustment state appears.
        assert after["source_s3_prefix"] == flat_prefix
        assert "fetches" not in after
        assert "arch_revisions" not in after
        assert "components_triggered" not in after
        for arch in self.ARCHS:
            if arch in retried:
                entry = after["artifacts"][arch]
                assert entry["buildStatus"] == "building"
                assert entry["buildId"].startswith(
                    f"dda-plugin-build-{arch}:")
            else:
                assert after["artifacts"][arch] == before["artifacts"][arch]
        # Today's write records the retried round's architectures.
        assert after["requested_architectures"] == sorted(retried)


class TestPreservationImportFlow:
    """Import-time multi-revision persistence baseline (Property 2).

    **Validates: Requirements 3.1, 3.4**
    """

    @given(data=st.data())
    @settings(max_examples=8, deadline=None,
              suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_import_fetches_each_distinct_revision_once_and_maps_archs(
            self, ienv, admin_setup, monkeypatch, data):
        """For any generated arch_revisions overrides on POST
        /plugins/import: distinct effective revisions fetch once each
        (REVISION env override per fetch) and the record maps every
        architecture to the fetches slot recording its revision (3.1);
        a single distinct revision keeps today's flat single-fetch
        layout with no fetches map at all (3.4)."""
        usecase_id, admin = admin_setup
        module = ienv.module
        if not isinstance(module.codebuild, RecordingCodeBuild):
            monkeypatch.setattr(module, "codebuild",
                                RecordingCodeBuild(module.codebuild))
        recorder = module.codebuild
        archs = data.draw(st.lists(st.sampled_from(PRESERVATION_ARCHS),
                                   min_size=2, max_size=4, unique=True),
                          label="architectures")
        overrides = data.draw(st.dictionaries(
            st.sampled_from(archs),
            st.sampled_from(["main", "1.14", "1.16", "1.22"]),
            min_size=1, max_size=len(archs)), label="arch_revisions")
        calls_before = len(recorder.calls)

        status, body = ienv.import_plugin(admin, {
            "usecase_id": usecase_id,
            "repo_url": REPO_URL,
            "revision": "main",
            "architectures": archs,
            "arch_revisions": overrides,
        })

        assert status == 202, body
        effective = {arch: overrides.get(arch) or "main" for arch in archs}
        distinct = sorted(set(effective.values()))
        calls = recorder.calls[calls_before:]
        record = ienv.get_record(body["plugin"]["plugin_id"])
        if len(distinct) == 1:
            # Collapses to today's flat single-fetch layout (3.4).
            assert len(calls) == 1
            assert "fetches" not in record
            assert "arch_revisions" not in record
            assert "/rev-" not in record["source_s3_prefix"]
            return
        # One fetch per DISTINCT effective revision (3.1).
        assert len(calls) == len(distinct)
        envs = [{v["name"]: v["value"]
                 for v in c["environmentVariablesOverride"]} for c in calls]
        assert sorted(e["REVISION"] for e in envs) == distinct
        # The record maps every arch to its revision's fetches slot.
        for arch in archs:
            slug = record["arch_revisions"][arch]
            assert record["fetches"][slug]["revision"] == effective[arch]
            assert record["fetches"][slug]["status"] == "fetching"


class TestPreservationComponentPackaging:
    """Component auto-packaging preservation (Property 2).

    **Validates: Requirements 3.6**
    """

    ARCHS = ["x86_64", "arm64_jp5"]

    def _settled_import(self, pres_env, admin_setup):
        usecase_id, admin = admin_setup
        _, result, record = pres_env.complete_import(admin, {
            "usecase_id": usecase_id,
            "repo_url": REPO_URL,
            "revision": "main",
            "architectures": self.ARCHS,
        }, files={"meson.build": MESON_PLUGIN, "gstmyfilter.c": None})
        assert result == {"recorded": True, "import_status": "imported"}
        return usecase_id, admin, record

    def _stub_packaging(self, pres_env, monkeypatch):
        invocations = []
        monkeypatch.setattr(
            pres_env.stack.plugin_builds, "lambda_client",
            type("Stub", (), {"invoke": staticmethod(
                lambda **kw: invocations.append(kw))})())
        return invocations

    def test_import_build_round_triggers_packaging_exactly_once(
            self, pres_env, admin_setup, monkeypatch):
        """The build round an import opens triggers auto-packaging
        exactly once when all requested builds settle with >= 1
        success, and a duplicate result delivery never re-triggers
        (3.6) — the once-per-round baseline the adjustment's round
        reopening must not disturb."""
        usecase_id, admin, record = self._settled_import(
            pres_env, admin_setup)
        invocations = self._stub_packaging(pres_env, monkeypatch)
        builds_module = pres_env.stack.plugin_builds
        plugin_id = record["plugin_id"]
        plugin_name = builds_module.sanitize_plugin_name(
            record.get("name"), plugin_id)
        plugin = {"plugin_id": plugin_id, "version": 1,
                  "usecase_id": usecase_id}

        # First arch fails: the round is not settled — no trigger.
        r1 = pres_env.deliver_build_result(
            plugin, "x86_64", record["artifacts"]["x86_64"]["buildId"],
            "FAILED", plugin_name)
        assert r1["component_packaging_triggered"] is False
        assert invocations == []

        # Second arch succeeds: settled with one success -> exactly one
        # trigger for the round.
        pres_env.s3.put_object(
            Bucket=pres_env.bucket,
            Key=f"workflow-plugins/custom/{usecase_id}/arm64_jp5/"
                f"{plugin_name}.so",
            Body=b"\x7fELF-shared-object")
        r2 = pres_env.deliver_build_result(
            plugin, "arm64_jp5", record["artifacts"]["arm64_jp5"]["buildId"],
            "SUCCEEDED", plugin_name)
        assert r2["component_packaging_triggered"] is True
        assert len(invocations) == 1
        assert pres_env.get_record(plugin_id).get("components_triggered")

        # Duplicate delivery of the settled result never re-triggers.
        duplicate = pres_env.deliver_build_result(
            plugin, "arm64_jp5", record["artifacts"]["arm64_jp5"]["buildId"],
            "SUCCEEDED", plugin_name)
        assert duplicate.get("recorded") is False
        assert len(invocations) == 1

    def test_plain_retry_reopens_the_round_and_triggers_once_more(
            self, pres_env, admin_setup, monkeypatch):
        """A plain retry REMOVEs components_triggered (new build
        round); settling the retried round triggers auto-packaging
        exactly once more (3.6)."""
        usecase_id, admin, record = self._settled_import(
            pres_env, admin_setup)
        invocations = self._stub_packaging(pres_env, monkeypatch)
        builds_module = pres_env.stack.plugin_builds
        plugin_id = record["plugin_id"]
        plugin_name = builds_module.sanitize_plugin_name(
            record.get("name"), plugin_id)
        plugin = {"plugin_id": plugin_id, "version": 1,
                  "usecase_id": usecase_id}

        # Settle the first round (both succeed): one trigger.
        for arch in self.ARCHS:
            pres_env.s3.put_object(
                Bucket=pres_env.bucket,
                Key=f"workflow-plugins/custom/{usecase_id}/{arch}/"
                    f"{plugin_name}.so",
                Body=b"\x7fELF-shared-object")
            pres_env.deliver_build_result(
                plugin, arch, record["artifacts"][arch]["buildId"],
                "SUCCEEDED", plugin_name)
        assert len(invocations) == 1

        # A plain retry reopens the round: the marker is REMOVEd.
        status, _ = pres_env.post_build(admin, plugin_id, 1,
                                        {"architectures": ["arm64_jp5"]})
        assert status == 202
        retried = pres_env.get_record(plugin_id)
        assert "components_triggered" not in retried

        # Settling the retried round triggers exactly once more.
        pres_env.deliver_build_result(
            plugin, "arm64_jp5", retried["artifacts"]["arm64_jp5"]["buildId"],
            "SUCCEEDED", plugin_name)
        assert len(invocations) == 2


# =====================================================================
# Unit tests for the adjust-revision fix specifics
# (imported-plugin-revision-adjustment-fix, task 4)
# =====================================================================
#
# The exploration tests above (TestRevisionAdjustmentBugExploration)
# already cover the 202-with-a-fetch-slot property, the effective-
# prefix flip, and the settled-record fetch-result success mapping.
# These tests pin down the remaining fix specifics: the handler's four
# paths (fetch / reuse / failed-slot re-fetch / concurrent join), the
# rejection matrix, the adjustment fetch-result failure and idempotency
# branches, and adjustment_fetch_slot's slug resolution.


class TestAdjustmentFetchSlot:
    """adjustment_fetch_slot slug resolution (pure).

    **Validates: Requirements 2.2**
    """

    def _slot(self, aws_stack, fetches, revision):
        return aws_stack.plugin_importer.adjustment_fetch_slot(
            {"fetches": fetches}, revision)

    def test_reuses_a_succeeded_slot_recording_the_same_revision(
            self, aws_stack):
        mod = aws_stack.plugin_importer
        fetches = {"1.16": {"revision": "1.16", "status": "succeeded"}}
        assert self._slot(aws_stack, fetches, "1.16") == \
            ("1.16", mod.ADJUST_REUSE)

    def test_joins_a_concurrent_fetch_of_the_same_revision(self, aws_stack):
        mod = aws_stack.plugin_importer
        fetches = {"1.16": {"revision": "1.16", "status": "fetching",
                            "pending_archs": ["x86_64"]}}
        assert self._slot(aws_stack, fetches, "1.16") == \
            ("1.16", mod.ADJUST_JOIN)

    def test_failed_slot_for_the_same_revision_is_reset_in_place(
            self, aws_stack):
        mod = aws_stack.plugin_importer
        fetches = {"1.16": {"revision": "1.16", "status": "failed"}}
        assert self._slot(aws_stack, fetches, "1.16") == \
            ("1.16", mod.ADJUST_FETCH)

    def test_fresh_slug_on_a_flat_record(self, aws_stack):
        mod = aws_stack.plugin_importer
        # No fetches map at all (flat single-revision record) and an
        # empty one both allocate the plain revision_slug.
        assert mod.adjustment_fetch_slot({}, "1.14") == \
            ("1.14", mod.ADJUST_FETCH)
        assert self._slot(aws_stack, {}, "feature/x") == \
            ("feature-x", mod.ADJUST_FETCH)

    def test_numeric_suffix_collision_never_clobbers_another_revision(
            self, aws_stack):
        mod = aws_stack.plugin_importer
        # The slug '1.16' already records a DIFFERENT revision: the
        # adjustment disambiguates with a numeric suffix exactly like
        # revision_fetch_plan instead of clobbering the entry.
        fetches = {"1.16": {"revision": "release/1.16",
                            "status": "succeeded"}}
        assert self._slot(aws_stack, fetches, "1.16") == \
            ("1.16-2", mod.ADJUST_FETCH)
        fetches["1.16-2"] = {"revision": "another", "status": "failed"}
        assert self._slot(aws_stack, fetches, "1.16") == \
            ("1.16-3", mod.ADJUST_FETCH)

    @given(
        fetches=st.dictionaries(
            preservation_slugs(),
            st.fixed_dictionaries({
                "revision": preservation_revisions(),
                "status": st.sampled_from(["succeeded", "fetching",
                                           "failed"]),
            }),
            max_size=5),
        revision=preservation_revisions(),
    )
    @settings(max_examples=50, deadline=None,
              suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_never_clobbers_an_entry_recording_a_different_revision(
            self, aws_stack, fetches, revision):
        """Property: for any set of existing fetches slugs and any
        revision, adjustment_fetch_slot never resolves to a slug whose
        entry records a different revision — an in-map slug always
        records the requested revision; otherwise the slug is fresh
        and the action is a fetch.

        **Validates: Requirements 2.2**
        """
        mod = aws_stack.plugin_importer
        slug, action = mod.adjustment_fetch_slot({"fetches": fetches},
                                                 revision)
        if slug in fetches:
            assert fetches[slug]["revision"] == revision
            expected = {"succeeded": mod.ADJUST_REUSE,
                        "fetching": mod.ADJUST_JOIN,
                        "failed": mod.ADJUST_FETCH}
            assert action == expected[fetches[slug]["status"]]
        else:
            assert action == mod.ADJUST_FETCH


class TestAdjustRevisionHandler:
    """adjust_revision handler paths (task 4 unit tests).

    **Validates: Requirements 2.1, 2.2, 2.5**
    """

    def _seed_record(self, adj_env, plugin_id, expression, values):
        adj_env.stack.tables.plugin_records.update_item(
            Key={"plugin_id": plugin_id, "version": 1},
            UpdateExpression=expression,
            ExpressionAttributeValues=values)

    def _fetch_calls(self, recorder):
        return [c for c in recorder.calls
                if c["projectName"] == TEST_ENV["FETCH_PROJECT_NAME"]]

    def _build_calls(self, recorder):
        return [c for c in recorder.calls
                if c["projectName"].startswith("dda-plugin-build-")]

    def test_new_revision_writes_the_fetching_slot_and_starts_the_fetch(
            self, adj_env, settled_incompatible, monkeypatch):
        """Happy path with a NEW revision: the fetches entry is written
        with pending_archs, the arch re-queued, the fetch started with
        the REVISION_SLUG attribution, and components_triggered REMOVEd
        (a new build round). arch_revisions flips only on fetch
        success."""
        admin, record = settled_incompatible
        plugin_id = record["plugin_id"]
        # A settled build round left its packaging marker: the
        # adjustment must open a new round.
        self._seed_record(adj_env, plugin_id,
                          "SET components_triggered = :m", {":m": 1234})
        recorder = RecordingCodeBuild(adj_env.module.codebuild)
        monkeypatch.setattr(adj_env.module, "codebuild", recorder)

        status, body = adj_env.adjust_revision(
            admin, plugin_id, 1, "arm64_jp4", "1.14")

        assert status == 202, body
        assert set(body) == {"plugin", "builds"}
        updated = adj_env.get_record(plugin_id)
        entry = updated["fetches"]["1.14"]
        assert entry["revision"] == "1.14"
        assert entry["status"] == "fetching"
        assert entry["pending_archs"] == ["arm64_jp4"]
        assert entry["source_prefix"] == \
            f"{record['source_s3_prefix']}rev-1.14/"
        assert entry["fetch_build_id"].startswith(
            TEST_ENV["FETCH_PROJECT_NAME"] + ":")
        # The adjusted arch re-queued; the packaging marker REMOVEd.
        assert updated["artifacts"]["arm64_jp4"] == {"buildStatus": "queued"}
        assert "components_triggered" not in updated
        # arch_revisions is NOT written on the fetch path (it flips on
        # fetch success only, 2.4).
        assert "arch_revisions" not in updated
        # The fetch StartBuild carries the REVISION_SLUG attribution.
        fetch_calls = self._fetch_calls(recorder)
        assert len(fetch_calls) == 1
        env = {v["name"]: v["value"]
               for v in fetch_calls[0]["environmentVariablesOverride"]}
        assert env["REVISION_SLUG"] == "1.14"
        assert env["REVISION"] == "1.14"
        assert env["DEST_PREFIX"].endswith("rev-1.14")
        assert env["PLUGIN_ID"] == plugin_id
        # Nothing else on the record was touched.
        assert updated["artifacts"]["x86_64"] == record["artifacts"]["x86_64"]
        assert updated["source_s3_prefix"] == record["source_s3_prefix"]

    def test_reuse_of_a_succeeded_slot_maps_the_arch_and_starts_the_build(
            self, adj_env, settled_incompatible, monkeypatch):
        """Reuse path: an existing succeeded slot recording the same
        revision — no fetch, arch_revisions mapped immediately, and
        the build started from the adjusted tree."""
        admin, record = settled_incompatible
        plugin_id = record["plugin_id"]
        prefix = f"{record['source_s3_prefix']}rev-1.16/"
        self._seed_record(adj_env, plugin_id, "SET fetches = :f", {
            ":f": {"1.16": {"revision": "1.16", "source_prefix": prefix,
                            "status": "succeeded"}}})
        importer_recorder = RecordingCodeBuild(adj_env.module.codebuild)
        monkeypatch.setattr(adj_env.module, "codebuild", importer_recorder)
        builds_module = adj_env.stack.plugin_builds
        builds_recorder = RecordingCodeBuild(builds_module.codebuild)
        monkeypatch.setattr(builds_module, "codebuild", builds_recorder)

        status, body = adj_env.adjust_revision(
            admin, plugin_id, 1, "arm64_jp4", "1.16")

        assert status == 202, body
        # No fetch was started: the synced tree is reused (2.2).
        assert self._fetch_calls(importer_recorder) == []
        updated = adj_env.get_record(plugin_id)
        assert updated["arch_revisions"] == {"arm64_jp4": "1.16"}
        assert updated["fetches"]["1.16"]["status"] == "succeeded"
        # The adjusted arch's build started from the adjusted tree.
        entry = updated["artifacts"]["arm64_jp4"]
        assert entry["buildStatus"] == "building"
        assert entry["buildId"].startswith("dda-plugin-build-arm64_jp4:")
        build_calls = self._build_calls(builds_recorder)
        assert len(build_calls) == 1
        bucket = TEST_ENV["PORTAL_ARTIFACTS_BUCKET"]
        assert build_calls[0]["sourceLocationOverride"] == \
            f"{bucket}/{prefix}"
        # The other architecture's entry is untouched (3.5).
        assert updated["artifacts"]["x86_64"] == record["artifacts"]["x86_64"]

    def test_failed_slot_is_re_fetched_in_place(
            self, adj_env, settled_incompatible, monkeypatch):
        """A previously failed entry for the same revision is reset in
        place: same slug, status back to fetching, a new fetch build id
        recorded."""
        admin, record = settled_incompatible
        plugin_id = record["plugin_id"]
        self._seed_record(adj_env, plugin_id, "SET fetches = :f", {
            ":f": {"1.14": {
                "revision": "1.14",
                "source_prefix": f"{record['source_s3_prefix']}rev-1.14/",
                "status": "failed",
                "fetch_build_id": "dda-plugin-fetch:old-attempt"}}})
        recorder = RecordingCodeBuild(adj_env.module.codebuild)
        monkeypatch.setattr(adj_env.module, "codebuild", recorder)

        status, body = adj_env.adjust_revision(
            admin, plugin_id, 1, "arm64_jp4", "1.14")

        assert status == 202, body
        updated = adj_env.get_record(plugin_id)
        assert sorted(updated["fetches"]) == ["1.14"]  # reset, not added
        entry = updated["fetches"]["1.14"]
        assert entry["status"] == "fetching"
        assert entry["pending_archs"] == ["arm64_jp4"]
        assert entry["fetch_build_id"] != "dda-plugin-fetch:old-attempt"
        assert entry["fetch_build_id"].startswith(
            TEST_ENV["FETCH_PROJECT_NAME"] + ":")
        assert len(self._fetch_calls(recorder)) == 1

    def test_concurrent_fetch_join_appends_the_arch(
            self, adj_env, settled_incompatible, monkeypatch):
        """A concurrent adjustment already fetching the revision: the
        arch joins its pending_archs — no second fetch, no build start,
        no arch_revisions write."""
        admin, record = settled_incompatible
        plugin_id = record["plugin_id"]
        self._seed_record(adj_env, plugin_id, "SET fetches = :f", {
            ":f": {"1.14": {
                "revision": "1.14",
                "source_prefix": f"{record['source_s3_prefix']}rev-1.14/",
                "status": "fetching",
                "fetch_build_id": "dda-plugin-fetch:concurrent",
                "pending_archs": ["x86_64"]}}})
        importer_recorder = RecordingCodeBuild(adj_env.module.codebuild)
        monkeypatch.setattr(adj_env.module, "codebuild", importer_recorder)
        builds_module = adj_env.stack.plugin_builds
        builds_recorder = RecordingCodeBuild(builds_module.codebuild)
        monkeypatch.setattr(builds_module, "codebuild", builds_recorder)

        status, body = adj_env.adjust_revision(
            admin, plugin_id, 1, "arm64_jp4", "1.14")

        assert status == 202, body
        updated = adj_env.get_record(plugin_id)
        entry = updated["fetches"]["1.14"]
        assert entry["pending_archs"] == ["x86_64", "arm64_jp4"]
        assert entry["status"] == "fetching"
        assert entry["fetch_build_id"] == "dda-plugin-fetch:concurrent"
        assert updated["artifacts"]["arm64_jp4"] == {"buildStatus": "queued"}
        assert "arch_revisions" not in updated
        assert self._fetch_calls(importer_recorder) == []
        assert self._build_calls(builds_recorder) == []


class TestAdjustRevisionRejections:
    """adjust_revision rejection matrix.

    **Validates: Requirements 2.5**
    """

    def _seed_record(self, adj_env, plugin_id, expression, values=None):
        kwargs = {}
        if values:
            kwargs["ExpressionAttributeValues"] = values
        adj_env.stack.tables.plugin_records.update_item(
            Key={"plugin_id": plugin_id, "version": 1},
            UpdateExpression=expression, **kwargs)

    def test_denied_without_node_designer_manage(
            self, adj_env, settled_incompatible):
        _, record = settled_incompatible
        plugin_id = record["plugin_id"]
        scientist = adj_env.make_user(role="Viewer")
        adj_env.assign_role(scientist, record["usecase_id"], "DataScientist")
        before = adj_env.get_record(plugin_id)

        status, body = adj_env.adjust_revision(
            scientist, plugin_id, 1, "arm64_jp4", "1.14")

        assert status == 403
        assert body["error"]["code"] == "FORBIDDEN"
        assert adj_env.get_record(plugin_id) == before

    @pytest.mark.parametrize("kind", ["scaffold", "generated"])
    def test_rejected_for_non_imported_records(
            self, adj_env, settled_incompatible, kind):
        admin, record = settled_incompatible
        plugin_id = record["plugin_id"]
        self._seed_record(adj_env, plugin_id, "SET kind = :k", {":k": kind})

        status, body = adj_env.adjust_revision(
            admin, plugin_id, 1, "arm64_jp4", "1.14")

        assert status == 409
        assert body["error"]["code"] == "REVISION_ADJUSTMENT_NOT_AVAILABLE"

    def test_rejected_without_a_recorded_repo_url(
            self, adj_env, settled_incompatible):
        admin, record = settled_incompatible
        plugin_id = record["plugin_id"]
        self._seed_record(adj_env, plugin_id, "REMOVE provenance.repoUrl")

        status, body = adj_env.adjust_revision(
            admin, plugin_id, 1, "arm64_jp4", "1.14")

        assert status == 409
        assert body["error"]["code"] == "REVISION_ADJUSTMENT_NOT_AVAILABLE"

    @pytest.mark.parametrize("import_status",
                             ["fetching", "pending_selection", "failed"])
    def test_rejected_for_unsettled_imports(
            self, adj_env, settled_incompatible, import_status):
        admin, record = settled_incompatible
        plugin_id = record["plugin_id"]
        self._seed_record(adj_env, plugin_id, "SET import_status = :s",
                          {":s": import_status})
        before = adj_env.get_record(plugin_id)

        status, body = adj_env.adjust_revision(
            admin, plugin_id, 1, "arm64_jp4", "1.14")

        assert status == 409
        assert body["error"]["code"] == "REVISION_ADJUSTMENT_NOT_AVAILABLE"
        assert body["error"]["details"]["import_status"] == import_status
        assert adj_env.get_record(plugin_id) == before

    def test_unknown_architecture_rejected(
            self, adj_env, settled_incompatible):
        admin, record = settled_incompatible

        status, body = adj_env.adjust_revision(
            admin, record["plugin_id"], 1, "riscv", "1.14")

        assert status == 400
        assert body["error"]["code"] == "INVALID_ARCHITECTURE"

    @pytest.mark.parametrize("revision", ["", "   ", None])
    def test_empty_revision_rejected(
            self, adj_env, settled_incompatible, revision):
        admin, record = settled_incompatible

        status, body = adj_env.adjust_revision(
            admin, record["plugin_id"], 1, "arm64_jp4", revision)

        assert status == 400
        assert body["error"]["code"] == "INVALID_REVISION"


class TestAdjustmentFetchResult:
    """_handle_adjustment_fetch_result specifics beyond the exploration
    coverage: the build start on success, the failure branch, and the
    duplicate/superseded idempotency guards.

    **Validates: Requirements 2.3, 2.4**
    """

    def _adjusted(self, adj_env, settled_incompatible):
        """Apply an adjustment on the fetch path; returns
        (record, slug, fetch_build_id)."""
        admin, record = settled_incompatible
        status, body = adj_env.adjust_revision(
            admin, record["plugin_id"], 1, "arm64_jp4", "1.14")
        assert status == 202, body
        updated = adj_env.get_record(record["plugin_id"])
        return record, "1.14", updated["fetches"]["1.14"]["fetch_build_id"]

    def _plugin_ref(self, record):
        return {"plugin_id": record["plugin_id"], "version": 1,
                "usecase_id": record["usecase_id"]}

    def test_success_maps_the_arch_and_starts_the_queued_build(
            self, adj_env, settled_incompatible, monkeypatch):
        record, slug, build_id = self._adjusted(adj_env,
                                                settled_incompatible)
        builds_module = adj_env.stack.plugin_builds
        recorder = RecordingCodeBuild(builds_module.codebuild)
        monkeypatch.setattr(builds_module, "codebuild", recorder)

        result = adj_env.deliver_fetch_result(
            adj_env.fetch_result_detail_with_slug(
                self._plugin_ref(record), build_id, slug))

        assert result["recorded"] is True
        updated = adj_env.get_record(record["plugin_id"])
        assert updated["fetches"][slug]["status"] == "succeeded"
        assert "pending_archs" not in updated["fetches"][slug]
        assert updated["arch_revisions"] == {"arm64_jp4": slug}
        # The queued build started, sourcing the adjusted tree (2.3).
        entry = updated["artifacts"]["arm64_jp4"]
        assert entry["buildStatus"] == "building"
        assert entry["buildId"].startswith("dda-plugin-build-arm64_jp4:")
        calls = [c for c in recorder.calls
                 if c["projectName"].startswith("dda-plugin-build-")]
        assert len(calls) == 1
        bucket = TEST_ENV["PORTAL_ARTIFACTS_BUCKET"]
        assert calls[0]["sourceLocationOverride"] == \
            f"{bucket}/{record['source_s3_prefix']}rev-{slug}/"

    def test_failure_records_the_log_tail_on_the_affected_arch_only(
            self, adj_env, settled_incompatible):
        record, slug, build_id = self._adjusted(adj_env,
                                                settled_incompatible)
        before = adj_env.get_record(record["plugin_id"])

        result = adj_env.deliver_fetch_result(
            adj_env.fetch_result_detail_with_slug(
                self._plugin_ref(record), build_id, slug, status="FAILED"))

        assert result["recorded"] is True
        updated = adj_env.get_record(record["plugin_id"])
        assert updated["fetches"][slug]["status"] == "failed"
        assert "pending_archs" not in updated["fetches"][slug]
        # The fetch failure surfaces on the affected arch only (2.4).
        assert updated["artifacts"]["arm64_jp4"] == {
            "buildStatus": "failed",
            "logTail":
                adj_env.module.adjustment_fetch_failure_log_tail("1.14"),
        }
        # The prior mapping (none: flat record) and everything else are
        # untouched (3.5).
        assert "arch_revisions" not in updated
        assert updated["artifacts"]["x86_64"] == before["artifacts"]["x86_64"]
        assert updated["import_status"] == "imported"
        assert updated["source_s3_prefix"] == record["source_s3_prefix"]

    def test_duplicate_and_superseded_deliveries_change_nothing(
            self, adj_env, settled_incompatible):
        record, slug, build_id = self._adjusted(adj_env,
                                                settled_incompatible)
        plugin = self._plugin_ref(record)

        # A result for a superseded build id is skipped.
        superseded = adj_env.deliver_fetch_result(
            adj_env.fetch_result_detail_with_slug(
                plugin, "dda-plugin-fetch:someone-else", slug,
                status="FAILED"))
        assert superseded == {"recorded": False,
                              "reason": "superseded build"}
        assert adj_env.get_record(
            record["plugin_id"])["fetches"][slug]["status"] == "fetching"

        # The real result settles the slot...
        detail = adj_env.fetch_result_detail_with_slug(plugin, build_id,
                                                       slug)
        first = adj_env.deliver_fetch_result(detail)
        assert first["recorded"] is True
        settled_record = adj_env.get_record(record["plugin_id"])

        # ...and a duplicate delivery of the same result changes nothing.
        duplicate = adj_env.deliver_fetch_result(detail)
        assert duplicate == {"recorded": False,
                             "reason": "already recorded"}
        assert adj_env.get_record(record["plugin_id"]) == settled_record


# =====================================================================
# Integration tests: end-to-end revision-adjustment flows
# (imported-plugin-revision-adjustment-fix, task 5)
# =====================================================================
#
# Full sequences through the real handlers (moto-backed), beyond the
# per-step coverage above: import -> incompatible platform -> adjust ->
# fetch result -> rebuild from the adjusted tree -> once-per-round
# auto-packaging (2.2, 2.3, 3.6), and adjust -> fetch FAILED -> only
# the affected arch touched -> plain retry from the prior tree
# (2.4, 3.3, 3.4, 3.5).


class AdjustmentE2EEnv(AdjustRevisionEnv, PreservationEnv):
    """AdjustRevisionEnv (adjust-revision + slugged fetch-result
    delivery) plus PreservationEnv (POST .../build + per-arch build
    result delivery) for the end-to-end flows."""


@pytest.fixture
def e2e_env(aws_stack):
    return AdjustmentE2EEnv(aws_stack)


class TestAdjustmentEndToEnd:
    """End-to-end integration flows for the revision adjustment.

    **Validates: Requirements 2.2, 2.3, 2.4, 3.3, 3.4, 3.5, 3.6**
    """

    def _stub_packaging(self, env, monkeypatch):
        invocations = []
        monkeypatch.setattr(
            env.stack.plugin_builds, "lambda_client",
            type("Stub", (), {"invoke": staticmethod(
                lambda **kw: invocations.append(kw))})())
        return invocations

    def _promote_artifact(self, env, usecase_id, arch, plugin_name):
        """Place the built .so where the build's promotion step leaves
        it, so a SUCCEEDED result records the artifact fields."""
        env.s3.put_object(
            Bucket=env.bucket,
            Key=f"workflow-plugins/custom/{usecase_id}/{arch}/"
                f"{plugin_name}.so",
            Body=b"\x7fELF-shared-object")

    def _settle_first_round(self, env, record, plugin_name):
        """Settle the import's build round like CodeBuild would: x86_64
        succeeds, arm64_jp4 (the incompatible platform) fails. Returns
        the plugin attribution ref for result deliveries."""
        plugin = {"plugin_id": record["plugin_id"], "version": 1,
                  "usecase_id": record["usecase_id"]}
        self._promote_artifact(env, record["usecase_id"], "x86_64",
                               plugin_name)
        env.deliver_build_result(
            plugin, "x86_64", record["artifacts"]["x86_64"]["buildId"],
            "SUCCEEDED", plugin_name)
        env.deliver_build_result(
            plugin, "arm64_jp4", record["artifacts"]["arm64_jp4"]["buildId"],
            "FAILED", plugin_name)
        return plugin

    def test_full_flow_rebuilds_from_the_adjusted_tree_and_packages_once(
            self, e2e_env, admin_setup, monkeypatch):
        """Full flow: import (flat, single revision) -> the platform
        scan records the incompatible arm64_jp4 entry with
        suggestedRevision '1.14' -> adjust via the endpoint -> fetch
        SUCCEEDED via handle_fetch_result -> the arch's StartBuild
        sources the rev-{slug}/ prefix (2.2, 2.3) -> build SUCCEEDED ->
        auto-packaging triggers exactly once for the round (3.6)."""
        usecase_id, admin = admin_setup
        invocations = self._stub_packaging(e2e_env, monkeypatch)
        builds_module = e2e_env.stack.plugin_builds

        # Import settles 'imported' with the incompatible arm64_jp4
        # entry (asserted inside settled_incompatible_import).
        record = e2e_env.settled_incompatible_import(admin, usecase_id)
        plugin_id = record["plugin_id"]
        plugin_name = builds_module.sanitize_plugin_name(
            record.get("name"), plugin_id)
        plugin = self._settle_first_round(e2e_env, record, plugin_name)
        # The import's round settled with one success: packaged once.
        assert len(invocations) == 1
        assert e2e_env.get_record(plugin_id).get("components_triggered")

        # Adjust arm64_jp4 to the suggested revision via the endpoint.
        status, body = e2e_env.adjust_revision(
            admin, plugin_id, 1, "arm64_jp4", "1.14")
        assert status == 202, body
        assert body["builds"]["settled"] is False
        adjusted = e2e_env.get_record(plugin_id)
        (slug,) = [s for s, e in adjusted["fetches"].items()
                   if e["revision"] == "1.14"]
        assert adjusted["fetches"][slug]["status"] == "fetching"
        assert adjusted["artifacts"]["arm64_jp4"] == {"buildStatus": "queued"}
        # The adjustment opened a new build round (3.6).
        assert "components_triggered" not in adjusted

        # The adjustment fetch SUCCEEDED: the arch maps to the slot and
        # its build re-runs from the adjusted tree (2.2, 2.3).
        recorder = RecordingCodeBuild(builds_module.codebuild)
        monkeypatch.setattr(builds_module, "codebuild", recorder)
        result = e2e_env.deliver_fetch_result(
            e2e_env.fetch_result_detail_with_slug(
                plugin, adjusted["fetches"][slug]["fetch_build_id"], slug))
        assert result["recorded"] is True
        mapped = e2e_env.get_record(plugin_id)
        assert mapped["arch_revisions"] == {"arm64_jp4": slug}
        calls = [c for c in recorder.calls
                 if c["projectName"].startswith("dda-plugin-build-")]
        assert [c["projectName"] for c in calls] == \
            ["dda-plugin-build-arm64_jp4"]
        bucket = TEST_ENV["PORTAL_ARTIFACTS_BUCKET"]
        assert calls[0]["sourceLocationOverride"] == \
            f"{bucket}/{record['source_s3_prefix']}rev-{slug}/"
        assert mapped["artifacts"]["arm64_jp4"]["buildStatus"] == "building"

        # The adjusted build SUCCEEDED: the round settles and
        # auto-packaging triggers exactly once for it (3.6).
        self._promote_artifact(e2e_env, usecase_id, "arm64_jp4",
                               plugin_name)
        build_id = mapped["artifacts"]["arm64_jp4"]["buildId"]
        settled = e2e_env.deliver_build_result(
            plugin, "arm64_jp4", build_id, "SUCCEEDED", plugin_name)
        assert settled["component_packaging_triggered"] is True
        assert len(invocations) == 2  # import round + adjusted round
        final = e2e_env.get_record(plugin_id)
        assert final["artifacts"]["arm64_jp4"]["buildStatus"] == "succeeded"
        assert final["artifacts"]["x86_64"]["buildStatus"] == "succeeded"
        # A duplicate delivery of the settled result never re-triggers.
        duplicate = e2e_env.deliver_build_result(
            plugin, "arm64_jp4", build_id, "SUCCEEDED", plugin_name)
        assert duplicate.get("recorded") is False
        assert len(invocations) == 2

    def test_fetch_failure_flow_touches_only_the_affected_arch(
            self, e2e_env, admin_setup, monkeypatch):
        """Fetch-failure flow: adjust -> fetch FAILED -> the failure
        surfaces as the affected arch's logTail only (2.4); the other
        arch's succeeded entry and the record's flat source_s3_prefix
        are untouched (3.4, 3.5); a plain retry afterwards still
        StartBuilds from the prior (flat) tree (3.3)."""
        usecase_id, admin = admin_setup
        self._stub_packaging(e2e_env, monkeypatch)
        builds_module = e2e_env.stack.plugin_builds

        record = e2e_env.settled_incompatible_import(admin, usecase_id)
        plugin_id = record["plugin_id"]
        plugin_name = builds_module.sanitize_plugin_name(
            record.get("name"), plugin_id)
        plugin = self._settle_first_round(e2e_env, record, plugin_name)
        before = e2e_env.get_record(plugin_id)
        flat_prefix = record["source_s3_prefix"]

        # Adjust arm64_jp4; the adjustment fetch then FAILS.
        status, body = e2e_env.adjust_revision(
            admin, plugin_id, 1, "arm64_jp4", "1.14")
        assert status == 202, body
        fetch_build_id = e2e_env.get_record(
            plugin_id)["fetches"]["1.14"]["fetch_build_id"]
        result = e2e_env.deliver_fetch_result(
            e2e_env.fetch_result_detail_with_slug(
                plugin, fetch_build_id, "1.14", status="FAILED"))
        assert result["recorded"] is True

        after = e2e_env.get_record(plugin_id)
        # The fetch failure surfaces on the affected arch only (2.4).
        assert after["artifacts"]["arm64_jp4"] == {
            "buildStatus": "failed",
            "logTail":
                e2e_env.module.adjustment_fetch_failure_log_tail("1.14"),
        }
        # The other arch's succeeded entry is byte-identical (3.5) and
        # the record's flat tree was never rewritten (3.4).
        assert after["artifacts"]["x86_64"] == before["artifacts"]["x86_64"]
        assert after["source_s3_prefix"] == flat_prefix
        assert "arch_revisions" not in after
        assert after["import_status"] == "imported"
        assert after["fetches"]["1.14"]["status"] == "failed"

        # A plain retry still builds from the prior (flat) tree (3.3).
        recorder = RecordingCodeBuild(builds_module.codebuild)
        monkeypatch.setattr(builds_module, "codebuild", recorder)
        status, _ = e2e_env.post_build(admin, plugin_id, 1,
                                       {"architectures": ["arm64_jp4"]})
        assert status == 202
        calls = [c for c in recorder.calls
                 if c["projectName"].startswith("dda-plugin-build-")]
        assert [c["projectName"] for c in calls] == \
            ["dda-plugin-build-arm64_jp4"]
        bucket = TEST_ENV["PORTAL_ARTIFACTS_BUCKET"]
        assert calls[0]["sourceLocationOverride"] == f"{bucket}/{flat_prefix}"
        retried = e2e_env.get_record(plugin_id)
        assert retried["artifacts"]["x86_64"] == before["artifacts"]["x86_64"]
