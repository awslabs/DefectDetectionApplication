"""
Import through a Git_Connection (private-repo-plugin-import tasks 2, 3, 5;
Requirements 1.1-1.8, 2.1, 2.5, 3.1-3.5, 5.1-5.3, 6.1-6.3, 7.1-7.3).

Runs POST /plugins/import against the moto-backed stack with a
Git_Connection row in the connections table, a recorder wrapped around
`codebuild.start_build` (moto does not persist environmentVariablesOverride),
and synthetic EventBridge fetch results plus the result.json the fetch
buildspec writes.
"""
import json
import uuid

import pytest

from conftest import TEST_ENV
from test_plugin_importer import MESON_PLUGIN, ImporterEnv

GIT_CONNECTIONS_TABLE = TEST_ENV["GIT_CONNECTIONS_TABLE"]
TOKEN = "ghp_SECRETTOKENVALUE0123456789abcdef"


class PrivateImportEnv(ImporterEnv):
    """ImporterEnv plus Git_Connection rows and a StartBuild recorder."""

    def __init__(self, stack, monkeypatch):
        super().__init__(stack)
        self.start_calls = []
        real_start = self.module.codebuild.start_build

        def recording_start(**kwargs):
            self.start_calls.append(kwargs)
            return real_start(**kwargs)

        monkeypatch.setattr(self.module.codebuild, "start_build",
                            recording_start)

    def put_connection(self, usecase_id, status="verified", provider="github",
                       repo_url="https://github.com/acme/private-plugins.git",
                       default_branch="main"):
        connection_id = f"c-{uuid.uuid4()}"
        secret_arn = (f"arn:aws:secretsmanager:us-east-1:123456789012:secret:"
                      f"dda-portal/git-connections/{usecase_id}/{connection_id}-AbCdEf")
        self.stack.tables.git_connections.put_item(Item={
            "connection_id": connection_id,
            "usecase_id": usecase_id,
            "name": "private-plugins",
            "provider": provider,
            "repo_url": repo_url,
            "default_branch": default_branch,
            "status": status,
            "secret_arn": secret_arn,
            "created_by": "user-1",
            "created_at": 1,
            "updated_at": 1,
        })
        return {"connection_id": connection_id, "secret_arn": secret_arn,
                "repo_url": repo_url, "default_branch": default_branch,
                "provider": provider}

    def admin_for(self, usecase_id):
        admin = self.make_user(role="Viewer")
        self.assign_role(admin, usecase_id, "UseCaseAdmin")
        return admin

    def last_overrides(self):
        return self.start_calls[-1]["environmentVariablesOverride"]

    def put_fetch_result(self, plugin, result, slug=None):
        """Write the result.json the fetch buildspec leaves next to the tree
        (for a multi-revision import, next to the slug's rev- tree)."""
        dest_prefix = plugin["source_s3_prefix"]
        if slug:
            record = self.get_record(plugin["plugin_id"], plugin["version"])
            dest_prefix = record["fetches"][slug]["source_prefix"]
        key = self.module.fetch_result_key(dest_prefix.rstrip("/"))
        self.s3.put_object(Bucket=self.bucket, Key=key,
                           Body=json.dumps(result).encode("utf-8"))
        return key

    def settle_fetch(self, response, status="SUCCEEDED", files=None,
                     result=None):
        """Sync `files` like the runner, drop its result document, deliver
        the EventBridge fetch result; returns (handler_result, record)."""
        plugin = response["plugin"]
        if files:
            self.sync_source(plugin, files)
        if result is not None:
            self.put_fetch_result(plugin, result)
        handled = self.deliver_fetch_result(self.fetch_result_detail(
            plugin, response["import"]["buildId"], status=status))
        return handled, self.get_record(plugin["plugin_id"], plugin["version"])

    def invoke_records(self, method, resource, user, plugin_id, version=None):
        """Call the plugin_records API (GET version detail, DELETE record)."""
        path_params = {"id": plugin_id}
        if version is not None:
            path_params["v"] = str(version)
        event = {
            "httpMethod": method,
            "resource": resource,
            "path": resource.replace("{id}", plugin_id).replace(
                "{v}", str(version or "")),
            "pathParameters": path_params,
            "queryStringParameters": None,
            "body": None,
            "requestContext": {"authorizer": {"claims": {
                "sub": user["user_id"], "email": user["email"],
                "cognito:username": user["username"],
                "custom:role": user["role"]}}},
        }
        response = self.stack.plugin_records.handler(event, None)
        return response["statusCode"], json.loads(response["body"])

    def s3_keys(self, prefix):
        page = self.s3.list_objects_v2(Bucket=self.bucket, Prefix=prefix)
        return sorted(o["Key"] for o in page.get("Contents", []))


@pytest.fixture
def penv(aws_stack, monkeypatch):
    return PrivateImportEnv(aws_stack, monkeypatch)


def base_body(usecase_id, **extra):
    body = {"usecase_id": usecase_id, "architectures": ["x86_64"]}
    body.update(extra)
    return body


def by_name(overrides):
    return {v["name"]: v for v in overrides}


# ------------------------------------------------- request contract (1.x)

class TestImportSourceContract:
    def test_connection_import_starts_an_authenticated_fetch(self, penv):
        usecase_id = penv.create_usecase()
        admin = penv.admin_for(usecase_id)
        conn = penv.put_connection(usecase_id)

        status, body = penv.import_plugin(admin, base_body(
            usecase_id, connection_id=conn["connection_id"],
            path="plugins/resize", branch="release/1", revision="v1.2.0"))

        assert status == 202, body
        plugin = body["plugin"]
        assert plugin["kind"] == "imported"
        assert plugin["import_status"] == "fetching"
        # Import_Source recorded without any credential material (1.7).
        assert plugin["import_source"] == {
            "kind": "git_connection",
            "connection_id": conn["connection_id"],
            "path": "plugins/resize",
            "branch": "release/1",
            "revision": "v1.2.0",
            "shallow": False,
        }
        assert plugin["provenance"]["repoUrl"] == conn["repo_url"]
        # Subdirectory imports are named after the directory (1.5).
        assert plugin["name"] == "resize"
        assert "secret" not in json.dumps(body).lower()
        assert TOKEN not in json.dumps(body)

        env = by_name(penv.last_overrides())
        assert env["GIT_TOKEN"] == {"name": "GIT_TOKEN", "type": "SECRETS_MANAGER",
                                    "value": conn["secret_arn"] + ":token"}
        assert env["REPO_URL"]["value"] == conn["repo_url"]
        assert env["REPO_BRANCH"]["value"] == "release/1"
        assert env["REPO_SUBDIR"]["value"] == "plugins/resize"
        assert env["REVISION"]["value"] == "v1.2.0"
        assert env["GIT_USERNAME"]["value"] == "x-access-token"
        assert env["RESULT_KEY"]["value"] == (
            plugin["source_s3_prefix"].rstrip("/") + ".fetch/result.json")
        assert "SHALLOW" not in env

    def test_branch_defaults_to_the_connections_default_branch(self, penv):
        usecase_id = penv.create_usecase()
        admin = penv.admin_for(usecase_id)
        conn = penv.put_connection(usecase_id, default_branch="develop",
                                   provider="gitlab")

        status, body = penv.import_plugin(admin, base_body(
            usecase_id, connection_id=conn["connection_id"]))

        assert status == 202, body
        assert body["plugin"]["import_source"]["branch"] == "develop"
        assert "path" not in body["plugin"]["import_source"]
        env = by_name(penv.last_overrides())
        assert env["REPO_BRANCH"]["value"] == "develop"
        assert env["REPO_SUBDIR"]["value"] == ""
        assert env["GIT_USERNAME"]["value"] == "oauth2"
        # Repository-named when no subdirectory is given.
        assert body["plugin"]["name"] == "private-plugins"

    def test_both_sources_rejected(self, penv):
        usecase_id = penv.create_usecase()
        admin = penv.admin_for(usecase_id)
        status, body = penv.import_plugin(admin, base_body(
            usecase_id, repo_url="https://github.com/acme/x.git",
            connection_id="c-1"))
        assert status == 400
        assert body["error"]["code"] == "INVALID_IMPORT_SOURCE"
        assert body["error"]["details"]["field"] == "connection_id"
        assert penv.start_calls == []

    def test_neither_source_rejected(self, penv):
        usecase_id = penv.create_usecase()
        admin = penv.admin_for(usecase_id)
        status, body = penv.import_plugin(admin, base_body(usecase_id))
        assert status == 400
        assert body["error"]["code"] == "INVALID_IMPORT_SOURCE"
        assert body["error"]["details"]["field"] == "repo_url"

    def test_path_and_branch_need_a_connection(self, penv):
        usecase_id = penv.create_usecase()
        admin = penv.admin_for(usecase_id)
        status, body = penv.import_plugin(admin, base_body(
            usecase_id, repo_url="https://github.com/acme/x.git",
            path="plugins/x"))
        assert status == 400
        assert body["error"]["code"] == "INVALID_IMPORT_SOURCE"
        assert body["error"]["details"]["field"] == "path"

    def test_unknown_connection_is_404(self, penv):
        usecase_id = penv.create_usecase()
        admin = penv.admin_for(usecase_id)
        status, body = penv.import_plugin(admin, base_body(
            usecase_id, connection_id="c-does-not-exist"))
        assert status == 404
        # The same code git_sync answers for a missing connection (1.3).
        assert body["error"]["code"] == "CONNECTION_NOT_FOUND"
        assert penv.start_calls == []

    def test_cross_usecase_connection_is_indistinguishable_from_unknown(self, penv):
        usecase_a = penv.create_usecase()
        usecase_b = penv.create_usecase()
        admin_b = penv.admin_for(usecase_b)
        conn_a = penv.put_connection(usecase_a)
        status, body = penv.import_plugin(admin_b, base_body(
            usecase_b, connection_id=conn_a["connection_id"]))
        assert status == 404
        assert body["error"]["code"] == "CONNECTION_NOT_FOUND"
        # Byte-identical to the unknown case apart from the echoed id the
        # caller supplied: nothing about connection A leaks (7.2).
        _, unknown = penv.import_plugin(admin_b, base_body(
            usecase_b, connection_id="c-does-not-exist"))
        assert json.dumps(body).replace(conn_a["connection_id"], "X") == \
            json.dumps(unknown).replace("c-does-not-exist", "X")
        assert conn_a["repo_url"] not in json.dumps(body)
        assert penv.start_calls == []

    def test_unauthorized_caller_learns_nothing_about_connections(self, penv):
        """RBAC before resolution (7.1): a viewer gets 403 whether or not
        the connection exists, so a missing permission never turns into a
        connection probe."""
        usecase_id = penv.create_usecase()
        viewer = penv.make_user(role="Viewer")
        penv.assign_role(viewer, usecase_id, "Viewer")
        conn = penv.put_connection(usecase_id)
        status_known, body_known = penv.import_plugin(viewer, base_body(
            usecase_id, connection_id=conn["connection_id"]))
        status_unknown, body_unknown = penv.import_plugin(viewer, base_body(
            usecase_id, connection_id="c-does-not-exist"))
        assert status_known == status_unknown == 403
        assert body_known["error"]["code"] == body_unknown["error"]["code"]
        assert penv.start_calls == []

    def test_revision_may_not_look_like_a_git_option(self, penv):
        usecase_id = penv.create_usecase()
        admin = penv.admin_for(usecase_id)
        conn = penv.put_connection(usecase_id)
        for body in (base_body(usecase_id, connection_id=conn["connection_id"],
                               revision="--upload-pack=evil"),
                     base_body(usecase_id, repo_url="https://github.com/acme/pub.git",
                               revision="-x")):
            status, response = penv.import_plugin(admin, body)
            assert status == 400, response
            assert response["error"]["code"] == "INVALID_REVISION"
        assert penv.start_calls == []

    @pytest.mark.parametrize("connection_status", ["verifying", "failed"])
    def test_unverified_connection_is_409(self, penv, connection_status):
        usecase_id = penv.create_usecase()
        admin = penv.admin_for(usecase_id)
        conn = penv.put_connection(usecase_id, status=connection_status)
        status, body = penv.import_plugin(admin, base_body(
            usecase_id, connection_id=conn["connection_id"]))
        assert status == 409
        assert body["error"]["code"] == "CONNECTION_NOT_VERIFIED"
        assert body["error"]["details"]["status"] == connection_status
        assert penv.start_calls == []

    @pytest.mark.parametrize("bad_path", ["../escape", "/abs/path", "a/../../b",
                                          ".", "  "])
    def test_bad_subdirectory_rejected(self, penv, bad_path):
        usecase_id = penv.create_usecase()
        admin = penv.admin_for(usecase_id)
        conn = penv.put_connection(usecase_id)
        status, body = penv.import_plugin(admin, base_body(
            usecase_id, connection_id=conn["connection_id"], path=bad_path))
        assert status == 400, body
        assert body["error"]["code"] == "INVALID_FILE_PATH"
        assert penv.start_calls == []

    @pytest.mark.parametrize("bad_branch", ["-flag", "has space", "a..b", ""])
    def test_bad_branch_rejected(self, penv, bad_branch):
        usecase_id = penv.create_usecase()
        admin = penv.admin_for(usecase_id)
        conn = penv.put_connection(usecase_id)
        body = base_body(usecase_id, connection_id=conn["connection_id"])
        body["branch"] = bad_branch
        status, response = penv.import_plugin(admin, body)
        if bad_branch == "":
            # Empty means "use the default branch", not an error.
            assert status == 202, response
        else:
            assert status == 400, response
            assert response["error"]["code"] == "INVALID_BRANCH"

    def test_shallow_must_be_boolean(self, penv):
        usecase_id = penv.create_usecase()
        admin = penv.admin_for(usecase_id)
        conn = penv.put_connection(usecase_id)
        status, body = penv.import_plugin(admin, base_body(
            usecase_id, connection_id=conn["connection_id"], shallow="yes"))
        assert status == 400
        assert body["error"]["code"] == "INVALID_SHALLOW"

    def test_shallow_adds_exactly_one_flag_for_either_source(self, penv):
        usecase_id = penv.create_usecase()
        admin = penv.admin_for(usecase_id)
        conn = penv.put_connection(usecase_id)

        status, body = penv.import_plugin(admin, base_body(
            usecase_id, connection_id=conn["connection_id"], shallow=True))
        assert status == 202, body
        assert by_name(penv.last_overrides())["SHALLOW"]["value"] == "1"
        assert body["plugin"]["import_source"]["shallow"] is True

        status, body = penv.import_plugin(admin, base_body(
            usecase_id, repo_url="https://github.com/acme/pub.git", shallow=True))
        assert status == 202, body
        env = by_name(penv.last_overrides())
        assert env["SHALLOW"]["value"] == "1"
        assert "GIT_TOKEN" not in env and "RESULT_KEY" not in env
        assert body["plugin"]["provenance"]["shallow"] is True
        assert "import_source" not in body["plugin"]

    def test_import_permission_required(self, penv):
        usecase_id = penv.create_usecase()
        viewer = penv.make_user(role="Viewer")
        penv.assign_role(viewer, usecase_id, "Viewer")
        conn = penv.put_connection(usecase_id)
        status, body = penv.import_plugin(viewer, base_body(
            usecase_id, connection_id=conn["connection_id"]))
        assert status == 403
        assert penv.start_calls == []

    def test_audit_names_the_connection_never_the_token(self, penv):
        usecase_id = penv.create_usecase()
        admin = penv.admin_for(usecase_id)
        conn = penv.put_connection(usecase_id)
        status, body = penv.import_plugin(admin, base_body(
            usecase_id, connection_id=conn["connection_id"],
            path="plugins/x", shallow=True))
        assert status == 202
        entries = [e for e in penv.stack.tables.audit_log.scan()["Items"]
                   if e["action"] == "import_plugin_record"
                   and e["resource_id"] == body["plugin"]["plugin_id"]]
        assert len(entries) == 1
        details = entries[0]["details"]
        assert details["connection_id"] == conn["connection_id"]
        assert details["path"] == "plugins/x"
        assert details["branch"] == "main"
        assert details["shallow"] is True
        dumped = json.dumps(entries[0], default=str)
        assert "secret" not in dumped.lower() and TOKEN not in dumped


# ------------------------------------------------ preservation (6.1-6.3)

class TestAnonymousPreservation:
    ANON = {"REPO_URL", "REVISION", "DEST_PREFIX", "USECASE_ID", "PLUGIN_ID",
            "PLUGIN_VERSION"}

    def test_anonymous_import_overrides_and_record_are_unchanged(self, penv):
        usecase_id = penv.create_usecase()
        admin = penv.admin_for(usecase_id)
        status, body = penv.import_plugin(admin, base_body(
            usecase_id, repo_url="https://github.com/acme/pub.git",
            revision="v2"))
        assert status == 202, body
        overrides = penv.last_overrides()
        assert {v["name"] for v in overrides} == self.ANON
        assert all(v["type"] == "PLAINTEXT" for v in overrides)
        plugin = body["plugin"]
        assert "import_source" not in plugin
        assert "shallow" not in plugin["provenance"]
        assert plugin["provenance"]["repoUrl"] == "https://github.com/acme/pub.git"

    def test_anonymous_url_rule_still_rejects_ssh_and_files(self, penv):
        usecase_id = penv.create_usecase()
        admin = penv.admin_for(usecase_id)
        for url in ("git@github.com:acme/x.git", "file:///tmp/x", "/tmp/x"):
            status, body = penv.import_plugin(admin, base_body(
                usecase_id, repo_url=url))
            assert status == 400, (url, body)
            assert body["error"]["code"] == "INVALID_REPO_URL"


# ------------------------------------ result handling (2.4, 3.1-3.4, 5.1-5.3)

COMMIT = "0123456789abcdef0123456789abcdef01234567"
AUTHENTICATED_URL = (f"https://x-access-token:{TOKEN}@github.com/acme/"
                     "private-plugins.git/")


def failed_result(marker, stderr_tail):
    return {"status": "failed", "commit": None, "branch": None,
            "failure_marker": marker, "stderr_tail": stderr_tail}


def succeeded_result(commit=COMMIT, branch="main"):
    return {"status": "succeeded", "commit": commit, "branch": branch,
            "failure_marker": None, "stderr_tail": ""}


AUTH_FAILURE = failed_result("CLONE_FAILED", (
    "remote: Invalid username or token. Password authentication is not "
    f"supported for Git operations.\nfatal: Authentication failed for "
    f"'{AUTHENTICATED_URL}'\nCLONE_FAILED: git clone failed\n"))


class TestFetchFailureReporting:
    def _start(self, penv, **extra):
        usecase_id = penv.create_usecase()
        admin = penv.admin_for(usecase_id)
        conn = penv.put_connection(usecase_id)
        body = base_body(usecase_id, connection_id=conn["connection_id"],
                         path="plugins/resize", **extra)
        status, response = penv.import_plugin(admin, body)
        assert status == 202, response
        return usecase_id, admin, conn, response

    def test_rejected_token_is_an_authentication_finding(self, penv):
        """3.1, 3.2, 2.4: the shared classifier names the category, the
        finding points at the Git connections page, and the token in
        git's authenticated URL never reaches the record."""
        _, admin, _, response = self._start(penv)
        handled, record = penv.settle_fetch(response, status="FAILED",
                                            result=AUTH_FAILURE)
        assert handled == {"recorded": True, "import_status": "failed"}
        assert record["import_status"] == "failed"
        assert record["import_error_code"] == "REPO_FETCH_FAILED"
        assert record["import_finding_category"] == "authentication"
        finding = record["import_finding"]
        assert "rejected the Git connection's token" in finding
        assert "Git connections page" in finding
        assert "Authentication failed" in finding  # the redacted excerpt
        assert TOKEN not in finding
        assert "https://***@github.com" in finding
        assert "git" not in record  # no link on failure
        assert record["artifacts"] == {}

    def test_version_detail_exposes_category_and_never_the_token(self, penv):
        """2.5: GET version detail carries import_finding_category and
        import_source, and no token / secret ARN / authenticated URL."""
        _, admin, conn, response = self._start(penv)
        penv.settle_fetch(response, status="FAILED", result=AUTH_FAILURE)
        plugin = response["plugin"]
        status, body = penv.invoke_records(
            "GET", "/plugins/{id}/versions/{v}", admin,
            plugin["plugin_id"], plugin["version"])
        assert status == 200, body
        detail = body["plugin"]
        assert detail["import_status"] == "failed"
        assert detail["import_finding_category"] == "authentication"
        assert detail["import_source"]["connection_id"] == conn["connection_id"]
        dumped = json.dumps(body)
        assert TOKEN not in dumped
        assert conn["secret_arn"] not in dumped
        assert "x-access-token:" not in dumped

    @pytest.mark.parametrize("marker,tail,expected", [
        ("PATH_NOT_FOUND",
         "PATH_NOT_FOUND: subdirectory 'plugins/resize' not found in the "
         "repository",
         "subdirectory 'plugins/resize' does not exist"),
        ("BRANCH_NOT_FOUND",
         "fatal: Remote branch release/9 not found in upstream origin\n"
         "BRANCH_NOT_FOUND: branch 'release/9' not found in the repository",
         "branch 'release/9' does not exist"),
        ("REVISION_NOT_FOUND",
         "error: pathspec 'v9.9.9' did not match any file(s) known to git\n"
         "REVISION_NOT_FOUND: revision 'v9.9.9' not found",
         "revision 'v9.9.9' does not exist"),
    ])
    def test_missing_path_branch_or_revision_is_not_found(
            self, penv, marker, tail, expected):
        """3.3: the runner's marker wins over whatever git printed."""
        _, _, _, response = self._start(penv, branch="release/9",
                                        revision="v9.9.9")
        _, record = penv.settle_fetch(response, status="FAILED",
                                      result=failed_result(marker, tail))
        assert record["import_status"] == "failed"
        assert record["import_finding_category"] == "not_found"
        assert expected in record["import_finding"]
        assert record["import_finding"].startswith(
            "The repository, branch, revision, or subdirectory could not be "
            "found")

    def test_unreachable_host_is_classified(self, penv):
        _, _, _, response = self._start(penv)
        _, record = penv.settle_fetch(response, status="FAILED",
                                      result=failed_result("CLONE_FAILED", (
                                          "fatal: unable to access 'https://github.com/acme/"
                                          "private-plugins.git/': Could not resolve host: "
                                          "github.com\nCLONE_FAILED: git clone failed")))
        assert record["import_finding_category"] == "unreachable"
        assert record["import_finding"].startswith(
            "The repository host could not be reached")

    def test_failure_without_a_result_document_is_internal(self, penv):
        """A build that died before the runner ran (image pull failure,
        timeout) leaves no result.json: the record still settles, with
        the generic text and category internal."""
        _, _, _, response = self._start(penv)
        handled, record = penv.settle_fetch(response, status="FAULT")
        assert handled == {"recorded": True, "import_status": "failed"}
        assert record["import_finding_category"] == "internal"
        assert record["import_finding"] == "The repository could not be fetched"
        assert record["import_error_code"] == "REPO_FETCH_FAILED"

    def test_unbuildable_tree_keeps_the_existing_finding(self, penv):
        """3.4: no recognizable plugin under `path` is the existing
        failed-import finding, distinct from a clone failure - no
        category, no fetch text, and no Git_Link."""
        _, _, _, response = self._start(penv)
        _, record = penv.settle_fetch(
            response, files={"README.md": "docs only", "src/main.c": None},
            result=succeeded_result())
        assert record["import_status"] == "failed"
        assert "import_finding_category" not in record
        assert "import_error_code" not in record
        assert "could not be fetched" not in record["import_finding"]
        assert "git" not in record

    def test_anonymous_fetch_failure_is_unchanged(self, penv):
        """6.1: the public-URL path keeps the generic finding, never a
        category, and reads no result document."""
        usecase_id = penv.create_usecase()
        admin = penv.admin_for(usecase_id)
        status, response = penv.import_plugin(admin, base_body(
            usecase_id, repo_url="https://github.com/acme/pub.git"))
        assert status == 202
        _, record = penv.settle_fetch(response, status="FAILED")
        assert record["import_finding"] == penv.module.FETCH_FAILURE_FINDING
        assert "import_finding_category" not in record


class TestFetchFailureFindingPure:
    """fetch_failure_finding without AWS."""

    def test_no_document_is_internal(self, aws_stack):
        module = aws_stack.plugin_importer
        assert module.fetch_failure_finding(None, {"path": "p"}) == (
            "The repository could not be fetched", "internal")

    def test_excerpt_is_capped_and_redacted(self, aws_stack):
        module = aws_stack.plugin_importer
        tail = ("x" * 2000) + f" token glpat-ABCDEFGHIJKLMNOP here"
        finding, category = module.fetch_failure_finding(
            failed_result("CLONE_FAILED", tail), {})
        assert category == "internal"
        assert "glpat-ABCDEFGHIJKLMNOP" not in finding
        assert "***" in finding
        assert len(finding) < 700
        assert "Fetch output: ..." in finding

    def test_marker_detail_uses_the_multi_revision_override(self, aws_stack):
        module = aws_stack.plugin_importer
        finding, category = module.fetch_failure_finding(
            failed_result("REVISION_NOT_FOUND", ""),
            {"revision": "v1", "path": "p"}, revision="v2")
        assert category == "not_found"
        assert "revision 'v2' does not exist" in finding
        assert "Fetch output" not in finding


class TestAutomaticGitLink:
    def _start(self, penv, **extra):
        usecase_id = penv.create_usecase()
        admin = penv.admin_for(usecase_id)
        conn = penv.put_connection(usecase_id)
        status, response = penv.import_plugin(admin, base_body(
            usecase_id, connection_id=conn["connection_id"], **extra))
        assert status == 202, response
        return usecase_id, admin, conn, response

    def test_successful_import_records_the_link_and_its_pull_baseline(
            self, penv):
        """5.1, 5.2: connection + resolved branch + subdirectory as the
        Repository_Path, linked by the importer, last_sync of kind pull
        at the fetched commit."""
        _, admin, conn, response = self._start(
            penv, path="plugins/resize", branch="release/1", revision="v1.2.0")
        handled, record = penv.settle_fetch(
            response, files={"meson.build": MESON_PLUGIN, "gstresize.c": None},
            result=succeeded_result(branch="release/1"))
        assert handled == {"recorded": True, "import_status": "imported"}
        link = record["git"]
        assert link["connection_id"] == conn["connection_id"]
        assert link["branch"] == "release/1"
        assert link["path"] == "plugins/resize"
        assert link["linked_by"] == admin["user_id"]
        assert link["linked_at"] > 0
        assert link["last_sync"] == {
            "kind": "pull", "commit": COMMIT, "branch": "release/1",
            "path": "plugins/resize", "by": admin["user_id"],
            "at": link["linked_at"],
        }
        # The link behaves like a hand-made one: the records API shows it
        # (5.3) and nothing in the response is credential material (2.5).
        plugin = response["plugin"]
        status, body = penv.invoke_records(
            "GET", "/plugins/{id}/versions/{v}", admin,
            plugin["plugin_id"], plugin["version"])
        assert status == 200
        detail = body["plugin"]
        assert detail["git"]["connection_id"] == conn["connection_id"]
        assert detail["git"]["last_sync"]["commit"] == COMMIT
        assert TOKEN not in json.dumps(body)
        assert conn["secret_arn"] not in json.dumps(body)

    def test_link_falls_back_to_the_requested_branch_without_a_document(
            self, penv):
        """No result.json (upload failed) still links on the requested
        branch - just without a pull baseline."""
        _, _, conn, response = self._start(penv, path="plugins/resize")
        _, record = penv.settle_fetch(
            response, files={"meson.build": MESON_PLUGIN, "gstresize.c": None})
        assert record["import_status"] == "imported"
        assert record["git"]["branch"] == conn["default_branch"]
        assert record["git"]["path"] == "plugins/resize"
        assert "last_sync" not in record["git"]

    def test_whole_tree_import_gets_no_link(self, penv):
        """A Repository_Path is a subdirectory (the sync runner refuses
        '.'), so importing the repository root records the origin in
        import_source but no Git_Link."""
        _, _, conn, response = self._start(penv)
        _, record = penv.settle_fetch(
            response, files={"meson.build": MESON_PLUGIN, "gstresize.c": None},
            result=succeeded_result())
        assert record["import_status"] == "imported"
        assert "git" not in record
        assert record["import_source"]["connection_id"] == conn["connection_id"]

    def test_anonymous_success_records_no_link(self, penv):
        usecase_id = penv.create_usecase()
        admin = penv.admin_for(usecase_id)
        status, response = penv.import_plugin(admin, base_body(
            usecase_id, repo_url="https://github.com/acme/pub.git"))
        assert status == 202
        _, record = penv.settle_fetch(
            response, files={"meson.build": MESON_PLUGIN, "gstpub.c": None})
        assert record["import_status"] == "imported"
        assert "git" not in record

    def test_completion_audit_names_the_link_never_the_token(self, penv):
        _, _, conn, response = self._start(penv, path="plugins/resize")
        penv.settle_fetch(
            response, files={"meson.build": MESON_PLUGIN, "gstresize.c": None},
            result=succeeded_result())
        entries = [e for e in penv.stack.tables.audit_log.scan()["Items"]
                   if e["action"] == "complete_plugin_import"
                   and e["resource_id"] == response["plugin"]["plugin_id"]]
        assert len(entries) == 1
        details = entries[0]["details"]
        assert details["git_link"] == {"connection_id": conn["connection_id"],
                                       "branch": "main", "path": "plugins/resize"}
        dumped = json.dumps(entries[0], default=str)
        assert TOKEN not in dumped and conn["secret_arn"] not in dumped


class TestMultiRevisionConnectionImport:
    """arch_revisions imports through a connection: one authenticated
    fetch per distinct revision, per-slug result documents."""

    def _start(self, penv):
        usecase_id = penv.create_usecase()
        admin = penv.admin_for(usecase_id)
        conn = penv.put_connection(usecase_id)
        status, response = penv.import_plugin(admin, {
            "usecase_id": usecase_id,
            "architectures": ["x86_64", "arm64_jp5"],
            "connection_id": conn["connection_id"],
            "path": "plugins/resize",
            "revision": "v1",
            "arch_revisions": {"arm64_jp5": "v2"},
        })
        assert status == 202, response
        return admin, conn, response

    def _deliver(self, penv, response, slug, status="SUCCEEDED"):
        plugin = response["plugin"]
        detail = penv.fetch_result_detail(
            plugin, response["import"]["fetchBuildIds"][slug], status=status)
        detail["additional-information"]["environment"][
            "environment-variables"].append(
            {"name": "REVISION_SLUG", "value": slug})
        return penv.deliver_fetch_result(detail)

    def test_each_fetch_authenticates_with_its_own_result_key(self, penv):
        _, conn, response = self._start(penv)
        by_slug = {}
        for call in penv.start_calls:
            env = by_name(call["environmentVariablesOverride"])
            by_slug[env["REVISION_SLUG"]["value"]] = env
        assert sorted(by_slug) == ["v1", "v2"]
        for slug, env in by_slug.items():
            assert env["GIT_TOKEN"]["type"] == "SECRETS_MANAGER"
            assert env["REPO_SUBDIR"]["value"] == "plugins/resize"
            assert env["RESULT_KEY"]["value"].endswith(
                f".fetch/result-rev-{slug}.json")
            assert env["REVISION"]["value"] == slug

    def test_all_succeeded_links_the_default_tree(self, penv):
        admin, conn, response = self._start(penv)
        plugin = response["plugin"]
        record = penv.get_record(plugin["plugin_id"])
        default_slug = record["default_fetch_slug"]
        # The record's source_s3_prefix is the default revision's tree.
        penv.sync_source(plugin, {"meson.build": MESON_PLUGIN, "a.c": None})
        for slug in record["fetches"]:
            penv.put_fetch_result(plugin, succeeded_result(
                commit=f"{slug}-commit", branch="main"), slug=slug)
        self._deliver(penv, response, "v2")
        self._deliver(penv, response, "v1")
        record = penv.get_record(plugin["plugin_id"])
        assert record["import_status"] == "imported"
        assert record["git"]["connection_id"] == conn["connection_id"]
        assert record["git"]["path"] == "plugins/resize"
        assert record["git"]["last_sync"]["commit"] == f"{default_slug}-commit"

    def test_one_failed_fetch_is_classified_from_its_document(self, penv):
        _, _, response = self._start(penv)
        plugin = response["plugin"]
        penv.put_fetch_result(plugin, failed_result(
            "REVISION_NOT_FOUND",
            "error: pathspec 'v2' did not match any file(s) known to git\n"
            "REVISION_NOT_FOUND: revision 'v2' not found"), slug="v2")
        penv.put_fetch_result(plugin, succeeded_result(commit="v1-commit"),
                              slug="v1")
        self._deliver(penv, response, "v1")
        self._deliver(penv, response, "v2", status="FAILED")
        record = penv.get_record(plugin["plugin_id"])
        assert record["import_status"] == "failed"
        assert record["import_finding_category"] == "not_found"
        assert record["import_finding"].startswith(
            "Could not retrieve the repository at revision v2")
        assert "revision 'v2' does not exist" in record["import_finding"]
        assert "git" not in record


class TestFetchResultCleanup:
    def test_delete_removes_the_result_documents_with_the_tree(self, penv):
        usecase_id = penv.create_usecase()
        admin = penv.admin_for(usecase_id)
        conn = penv.put_connection(usecase_id)
        status, response = penv.import_plugin(admin, base_body(
            usecase_id, connection_id=conn["connection_id"], path="p"))
        assert status == 202
        plugin = response["plugin"]
        penv.settle_fetch(response, status="FAILED", result=AUTH_FAILURE)
        version_prefix = plugin["source_s3_prefix"].rstrip("/")
        assert penv.s3_keys(version_prefix + ".fetch/") == [
            version_prefix + ".fetch/result.json"]

        status, body = penv.invoke_records(
            "DELETE", "/plugins/{id}", admin, plugin["plugin_id"])
        assert status == 200, body
        assert penv.s3_keys(version_prefix + ".fetch/") == []
        assert penv.get_record(plugin["plugin_id"]) is None



class TestRevisionAdjustmentThroughTheConnection:
    """adjust_revision on a connection-sourced record re-fetches through
    the same Git_Connection with the import's subdirectory, branch, and
    clone depth (review finding: it used to clone anonymously)."""

    def _imported(self, penv, connection_status_after="verified", shallow=True):
        usecase_id = penv.create_usecase()
        admin = penv.admin_for(usecase_id)
        conn = penv.put_connection(usecase_id, default_branch="develop")
        status, response = penv.import_plugin(admin, {
            "usecase_id": usecase_id,
            "architectures": ["x86_64", "arm64_jp5"],
            "connection_id": conn["connection_id"],
            "path": "plugins/resize",
            "shallow": shallow,
        })
        assert status == 202, response
        _, record = penv.settle_fetch(
            response, files={"meson.build": MESON_PLUGIN, "gstresize.c": None},
            result=succeeded_result(branch="develop"))
        assert record["import_status"] == "imported"
        if connection_status_after != "verified":
            penv.stack.tables.git_connections.update_item(
                Key={"connection_id": conn["connection_id"]},
                UpdateExpression="SET #s = :s",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":s": connection_status_after})
        penv.start_calls.clear()
        return admin, conn, record

    def _adjust(self, penv, admin, record, architecture, revision):
        event = {
            "httpMethod": "POST",
            "resource": "/plugins/{id}/versions/{v}/adjust-revision",
            "path": f"/plugins/{record['plugin_id']}/versions/1/adjust-revision",
            "pathParameters": {"id": record["plugin_id"], "v": "1"},
            "queryStringParameters": None,
            "body": json.dumps({"architecture": architecture,
                                "revision": revision}),
            "requestContext": {"authorizer": {"claims": {
                "sub": admin["user_id"], "email": admin["email"],
                "cognito:username": admin["username"],
                "custom:role": admin["role"]}}},
        }
        response = penv.module.handler(event, None)
        return response["statusCode"], json.loads(response["body"])

    def test_adjustment_fetch_carries_the_connection_options(self, penv):
        admin, conn, record = self._imported(penv)
        status, body = self._adjust(penv, admin, record, "arm64_jp5", "1.16")
        assert status == 202, body
        assert len(penv.start_calls) == 1
        env = by_name(penv.last_overrides())
        assert env["GIT_TOKEN"] == {"name": "GIT_TOKEN", "type": "SECRETS_MANAGER",
                                    "value": conn["secret_arn"] + ":token"}
        assert env["REPO_URL"]["value"] == conn["repo_url"]
        assert env["REPO_SUBDIR"]["value"] == "plugins/resize"
        assert env["REPO_BRANCH"]["value"] == "develop"
        assert env["REVISION"]["value"] == "1.16"
        assert env["REVISION_SLUG"]["value"] == "1.16"
        assert env["SHALLOW"]["value"] == "1"
        assert env["RESULT_KEY"]["value"].endswith(".fetch/result-rev-1.16.json")
        assert TOKEN not in json.dumps(penv.last_overrides())

    def test_adjustment_is_refused_when_the_connection_lost_verification(self, penv):
        admin, conn, record = self._imported(penv, connection_status_after="failed")
        status, body = self._adjust(penv, admin, record, "arm64_jp5", "1.16")
        assert status == 409, body
        assert body["error"]["code"] == "CONNECTION_NOT_VERIFIED"
        assert body["error"]["details"]["status"] == "failed"
        assert penv.start_calls == []
        # Nothing on the record moved.
        after = penv.get_record(record["plugin_id"])
        assert after.get("fetches") is None
        assert after["artifacts"]["arm64_jp5"]["buildStatus"] == \
            record["artifacts"]["arm64_jp5"]["buildStatus"]

    def test_adjustment_revision_may_not_look_like_a_git_option(self, penv):
        admin, _, record = self._imported(penv)
        status, body = self._adjust(penv, admin, record, "arm64_jp5", "--bad")
        assert status == 400
        assert body["error"]["code"] == "INVALID_REVISION"
        assert penv.start_calls == []

    def test_failed_adjustment_fetch_is_classified_on_the_arch_entry(self, penv):
        admin, _, record = self._imported(penv)
        status, body = self._adjust(penv, admin, record, "arm64_jp5", "1.16")
        assert status == 202, body
        plugin = penv.get_record(record["plugin_id"])
        entry = plugin["fetches"]["1.16"]
        # The runner left its document beside the rev- tree.
        penv.s3.put_object(
            Bucket=penv.bucket,
            Key=penv.module.fetch_result_key(entry["source_prefix"].rstrip("/")),
            Body=json.dumps(AUTH_FAILURE).encode("utf-8"))
        detail = penv.fetch_result_detail(
            {"usecase_id": plugin["usecase_id"], "plugin_id": plugin["plugin_id"],
             "version": 1}, entry["fetch_build_id"], status="FAILED")
        detail["additional-information"]["environment"][
            "environment-variables"].append(
            {"name": "REVISION_SLUG", "value": "1.16"})
        penv.deliver_fetch_result(detail)
        after = penv.get_record(record["plugin_id"])
        tail = after["artifacts"]["arm64_jp5"]["logTail"]
        assert after["artifacts"]["arm64_jp5"]["buildStatus"] == "failed"
        assert "rejected the Git connection's token" in tail
        assert TOKEN not in tail
        # The prior mapping and the other architecture are untouched.
        assert after.get("arch_revisions") is None
        assert after["artifacts"]["x86_64"] == record["artifacts"]["x86_64"]
