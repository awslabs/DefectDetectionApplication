"""
Unit tests for git_sync.py (custom-node-source-lifecycle task 5.9).

Covers Git_Connection CRUD with Secrets Manager (token stored once, never
returned, never read by the Lambda, rolled back on a failed item write -
2.3, 2.4, 2.7, 2.8), the verify flow, Git_Link management (3.1), the
push/pull guards (CONNECTION_NOT_VERIFIED, GIT_LINK_REQUIRED, SOURCE_LOCKED,
SYNC_IN_PROGRESS - 2.6, 3.12, 4.2), StartBuild environment (2.4), the
StartBuild-failure settlement, result handling for verify/push/pull incl.
no_changes, diverged, push_rejected, not_found, invalid_source, the
in_place and new_version installs (4.4-4.8), missing result.json -> log
tail internal, RBAC (9.1, 9.3), and the disconnected link (10.5).

Runs against the moto-backed stack (moto implements Secrets Manager,
CodeBuild StartBuild, DynamoDB, S3).
"""
import json
import uuid
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

from conftest import TEST_ENV
from test_plugin_records import PluginRecordsEnv, make_scaffold_declaration

TOKEN = "ghp_TESTTOKEN_abcdefghijklmnop"
HOOK = "plugin/frame_processing_hook.py"


class GitSyncEnv(PluginRecordsEnv):
    """Facade for the Git_Sync_Service API."""

    def __init__(self, stack):
        super().__init__(stack)
        self.git = stack.git_sync
        self.codebuild = stack.codebuild
        self.connections = stack.tables.git_connections
        self.operations = stack.tables.git_sync_operations

    def invoke_git(self, method, resource, user, body=None, query=None, **path_params):
        event = {
            "httpMethod": method,
            "resource": resource,
            "path": resource,
            "pathParameters": {k: str(v) for k, v in path_params.items()} or None,
            "queryStringParameters": query,
            "body": json.dumps(body) if body is not None else None,
            "requestContext": {"authorizer": {"claims": {
                "sub": user["user_id"], "email": user["email"],
                "cognito:username": user["username"], "custom:role": user["role"]}}},
        }
        response = self.git.handler(event, None)
        return response["statusCode"], json.loads(response["body"])

    def create_connection(self, user, usecase_id, **overrides):
        body = {"usecase_id": usecase_id, "name": "team repo", "provider": "github",
                "repo_url": "https://github.com/acme/plugins.git",
                "default_branch": "main", "token": TOKEN}
        body.update(overrides)
        return self.invoke_git("POST", "/git-connections", user, body=body)

    def verified_connection(self, user, usecase_id, **overrides):
        status, body = self.create_connection(user, usecase_id, **overrides)
        assert status == 202, body
        cid = body["connection"]["connection_id"]
        self.settle(body["operation"]["operation_id"], {"ok": True, "kind": "verify",
                                                          "default_branch": "main"})
        return cid

    def link(self, user, plugin_id, version, cid, **extra):
        return self.invoke_git("PUT", "/plugins/{id}/versions/{v}/git", user,
                               body={"connection_id": cid, **extra}, id=plugin_id, v=version)

    def put_result(self, operation_id, payload):
        self.s3.put_object(Bucket=self.bucket,
                           Key=f"plugin-git-sync/{operation_id}/result.json",
                           Body=json.dumps(payload).encode())

    def codebuild_event(self, operation_id, status="SUCCEEDED", build_id=None,
                        project=None, logs=None):
        op = self.git.get_operation(operation_id) or {}
        env = [{"name": "OPERATION_ID", "value": operation_id}]
        if op.get("plugin_id"):
            env += [{"name": "PLUGIN_ID", "value": op["plugin_id"]},
                    {"name": "PLUGIN_VERSION", "value": str(op["version"])}]
        bid = build_id or op.get("build_id") or f"{TEST_ENV['GIT_SYNC_PROJECT_NAME']}:{uuid.uuid4()}"
        return {
            "source": "aws.codebuild",
            "detail-type": "CodeBuild Build State Change",
            "detail": {
                "build-status": status,
                "project-name": project or TEST_ENV["GIT_SYNC_PROJECT_NAME"],
                "build-id": f"arn:aws:codebuild:us-east-1:123456789012:build/{bid}",
                "additional-information": {
                    "environment": {"environment-variables": env},
                    "logs": logs or {},
                },
            },
        }

    def settle(self, operation_id, result=None, status="SUCCEEDED", **kwargs):
        if result is not None:
            self.put_result(operation_id, result)
        return self.git.handler(self.codebuild_event(operation_id, status=status, **kwargs), None)

    def stage_tree(self, operation_id, files):
        for path, content in files.items():
            self.s3.put_object(Bucket=self.bucket,
                               Key=f"plugin-git-sync/{operation_id}/tree/{path}",
                               Body=content.encode())

    def source_tree(self, usecase_id, plugin_id, version):
        prefix = f"plugin-sources/{usecase_id}/{plugin_id}/{version}/"
        response = self.s3.list_objects_v2(Bucket=self.bucket, Prefix=prefix)
        return {o["Key"][len(prefix):]: self.s3.get_object(Bucket=self.bucket, Key=o["Key"])["Body"].read().decode()
                for o in response.get("Contents", [])}

    def record(self, plugin_id, version):
        return self.stack.plugin_records.get_version_item(plugin_id, version)

    def secret_calls(self):
        return self.secretsmanager_calls


@pytest.fixture
def genv(aws_stack, monkeypatch):
    env = GitSyncEnv(aws_stack)
    # Record every Secrets Manager call the Lambda makes and forbid reads.
    real = env.git.secretsmanager
    calls = []

    class Recording:
        def __getattr__(self, name):
            if name == "get_secret_value":
                raise AssertionError("the Lambda must never read a Git_Credential")
            attr = getattr(real, name)
            if callable(attr):
                def wrapper(*args, **kwargs):
                    calls.append((name, kwargs))
                    return attr(*args, **kwargs)
                return wrapper
            return attr

    monkeypatch.setattr(env.git, "secretsmanager", Recording())
    env.secretsmanager_calls = calls

    # moto does not persist environmentVariablesOverride on builds: record
    # every StartBuild call so tests can inspect the environment.
    real_cb = env.git.codebuild
    starts = []

    class RecordingCodeBuild:
        def start_build(self, **kwargs):
            starts.append(kwargs)
            return real_cb.start_build(**kwargs)

        def __getattr__(self, name):
            return getattr(real_cb, name)

    monkeypatch.setattr(env.git, "codebuild", RecordingCodeBuild())
    env.start_calls = starts
    return env


def start_env(genv, operation_id):
    """{name: {value, type}} of the recorded StartBuild of one operation."""
    for call in genv.start_calls:
        env = {v["name"]: v for v in call["environmentVariablesOverride"]}
        if env.get("OPERATION_ID", {}).get("value") == operation_id:
            assert call["projectName"] == TEST_ENV["GIT_SYNC_PROJECT_NAME"]
            return env
    raise AssertionError(f"no StartBuild recorded for {operation_id}")


@pytest.fixture
def setup(genv):
    usecase_id = genv.create_usecase()
    admin = genv.make_user(role="Viewer")
    genv.assign_role(admin, usecase_id, "UseCaseAdmin")
    status, body = genv.create_plugin(admin, usecase_id,
                                      declaration=make_scaffold_declaration())
    assert status == 201
    return {"usecase_id": usecase_id, "admin": admin,
            "plugin_id": body["plugin"]["plugin_id"], "files": body["files"]}


def audit(genv, action):
    return [i for i in genv.stack.tables.audit_log.scan()["Items"] if i["action"] == action]


# ------------------------------------------------------------ connections

class TestConnections:
    def test_create_stores_secret_once_and_starts_verification(self, genv, setup):
        s = setup
        status, body = genv.create_connection(s["admin"], s["usecase_id"])
        assert status == 202, body
        conn = body["connection"]
        assert conn["status"] == "verifying"
        assert "secret_arn" not in conn and TOKEN not in json.dumps(body)
        assert body["operation"]["kind"] == "verify"
        assert body["operation"]["status"] == "running"

        creates = [c for c in genv.secret_calls() if c[0] == "create_secret"]
        assert len(creates) == 1
        assert creates[0][1]["Name"] == f"dda-portal/git-connections/{s['usecase_id']}/{conn['connection_id']}"
        assert json.loads(creates[0][1]["SecretString"]) == {"token": TOKEN}
        stored = genv.connections.get_item(Key={"connection_id": conn["connection_id"]})["Item"]
        assert stored["secret_arn"].startswith("arn:aws:secretsmanager:")
        assert TOKEN not in json.dumps(stored, default=str)

        # Audit entries never carry the token.
        entries = audit(genv, "create_git_connection")
        assert entries and all(TOKEN not in json.dumps(e, default=str) for e in entries)

        # StartBuild carried the token only as a SECRETS_MANAGER reference.
        env = start_env(genv, body["operation"]["operation_id"])
        assert env["GIT_TOKEN"]["type"] == "SECRETS_MANAGER"
        assert env["GIT_TOKEN"]["value"] == f"{stored['secret_arn']}:token"
        assert env["SYNC_KIND"]["value"] == "verify"
        assert all(TOKEN not in v["value"] for v in env.values())

    @pytest.mark.parametrize("overrides, code", [
        ({"repo_url": "http://github.com/a/b.git"}, "INVALID_REPO_URL"),
        ({"repo_url": "https://user:pw@github.com/a/b.git"}, "INVALID_REPO_URL"),
        ({"repo_url": "https://github.com/"}, "INVALID_REPO_URL"),
        ({"provider": "bitbucket"}, "INVALID_PROVIDER"),
        ({"token": "  "}, "MISSING_FIELDS"),
        ({"name": ""}, "MISSING_FIELDS"),
    ])
    def test_validation(self, genv, setup, overrides, code):
        s = setup
        status, body = genv.create_connection(s["admin"], s["usecase_id"], **overrides)
        assert status == 400
        assert body["error"]["code"] == code
        assert not [c for c in genv.secret_calls() if c[0] == "create_secret"]

    def test_verify_result_marks_verified_or_failed(self, genv, setup):
        s = setup
        _, body = genv.create_connection(s["admin"], s["usecase_id"])
        cid, op_id = body["connection"]["connection_id"], body["operation"]["operation_id"]
        genv.settle(op_id, {"ok": True, "kind": "verify", "default_branch": "develop"})
        _, body = genv.invoke_git("GET", "/git-connections/{cid}", s["admin"], cid=cid)
        assert body["connection"]["status"] == "verified"
        assert body["connection"]["verification"]["default_branch_detected"] == "develop"

        _, body = genv.invoke_git("POST", "/git-connections/{cid}/verify", s["admin"], cid=cid)
        assert body["connection"]["status"] == "verifying"
        genv.settle(body["operation"]["operation_id"],
                    {"ok": False, "kind": "verify", "category": "authentication",
                     "message": "fatal: Authentication failed"})
        _, body = genv.invoke_git("GET", "/git-connections/{cid}", s["admin"], cid=cid)
        assert body["connection"]["status"] == "failed"
        assert body["connection"]["verification"]["category"] == "authentication"

    def test_update_token_rotates_secret_and_reverifies(self, genv, setup):
        s = setup
        cid = genv.verified_connection(s["admin"], s["usecase_id"])
        status, body = genv.invoke_git("PUT", "/git-connections/{cid}", s["admin"],
                                       body={"token": "glpat-NEWTOKEN12345678"}, cid=cid)
        assert status == 202, body
        assert body["connection"]["status"] == "verifying"
        puts = [c for c in genv.secret_calls() if c[0] == "put_secret_value"]
        assert len(puts) == 1 and json.loads(puts[0][1]["SecretString"]) == {"token": "glpat-NEWTOKEN12345678"}
        assert "glpat-NEWTOKEN12345678" not in json.dumps(body)
        # A name-only change does not re-verify.
        genv.settle(body["operation"]["operation_id"], {"ok": True, "kind": "verify"})
        status, body = genv.invoke_git("PUT", "/git-connections/{cid}", s["admin"],
                                       body={"name": "renamed"}, cid=cid)
        assert status == 200 and body["connection"]["status"] == "verified"
        assert body["connection"]["name"] == "renamed"

    def test_delete_schedules_secret_deletion_and_disconnects_links(self, genv, setup):
        s = setup
        cid = genv.verified_connection(s["admin"], s["usecase_id"])
        genv.link(s["admin"], s["plugin_id"], 1, cid)
        status, body = genv.invoke_git("DELETE", "/git-connections/{cid}", s["admin"], cid=cid)
        assert status == 200
        deletes = [c for c in genv.secret_calls() if c[0] == "delete_secret"]
        assert len(deletes) == 1 and "ForceDeleteWithoutRecovery" not in deletes[0][1]
        status, _ = genv.invoke_git("GET", "/git-connections/{cid}", s["admin"], cid=cid)
        assert status == 404
        # The version keeps its link but sync is unavailable (10.5).
        record = genv.record(s["plugin_id"], 1)
        assert record["git"]["connection_id"] == cid
        status, body = genv.invoke_git("POST", "/plugins/{id}/versions/{v}/git/push", s["admin"],
                                       body={}, id=s["plugin_id"], v=1)
        assert status == 409 and body["error"]["code"] == "CONNECTION_NOT_VERIFIED"
        assert body["error"]["details"]["status"] == "missing"

    def test_item_write_failure_rolls_back_the_secret(self, genv, setup, monkeypatch):
        s = setup
        table = genv.git.connections_table()
        real_put = table.put_item

        def boom(**kwargs):
            raise RuntimeError("dynamo down")
        monkeypatch.setattr(genv.git, "connections_table",
                            lambda: type("T", (), {"put_item": staticmethod(boom)})())
        status, body = genv.create_connection(s["admin"], s["usecase_id"])
        assert status == 500
        deletes = [c for c in genv.secret_calls() if c[0] == "delete_secret"]
        assert len(deletes) == 1 and deletes[0][1]["ForceDeleteWithoutRecovery"] is True
        monkeypatch.undo()

    def test_list_and_rbac(self, genv, setup):
        s = setup
        cid = genv.verified_connection(s["admin"], s["usecase_id"])
        viewer = genv.make_user(role="Viewer")
        genv.assign_role(viewer, s["usecase_id"], "Viewer")
        status, body = genv.invoke_git("GET", "/git-connections", viewer,
                                       query={"usecase_id": s["usecase_id"]})
        assert status == 200
        assert [c["connection_id"] for c in body["connections"]] == [cid]
        assert "secret_arn" not in body["connections"][0]
        for method, resource, payload in (
                ("POST", "/git-connections", {"usecase_id": s["usecase_id"], "name": "x",
                                              "provider": "github",
                                              "repo_url": "https://github.com/a/b.git",
                                              "default_branch": "main", "token": "t"}),
                ("PUT", "/git-connections/{cid}", {"name": "y"}),
                ("DELETE", "/git-connections/{cid}", None),
                ("POST", "/git-connections/{cid}/verify", None)):
            status, body = genv.invoke_git(method, resource, viewer, body=payload, cid=cid)
            assert status == 403, (method, resource)
        assert audit(genv, "unauthorized_access")


# ----------------------------------------------------------------- links

class TestGitLink:
    def test_link_defaults_and_path_validation(self, genv, setup):
        s = setup
        cid = genv.verified_connection(s["admin"], s["usecase_id"])
        status, body = genv.link(s["admin"], s["plugin_id"], 1, cid)
        assert status == 200, body
        assert body["git"]["branch"] == "main"
        assert body["git"]["path"] == "blur-regions"
        assert body["git"]["connection_id"] == cid
        status, body = genv.link(s["admin"], s["plugin_id"], 1, cid, path="../escape")
        assert status == 400 and body["error"]["code"] == "INVALID_REPO_PATH"
        status, body = genv.link(s["admin"], s["plugin_id"], 1, cid, path="plugins/blur/",
                                 branch="feature/x")
        assert status == 200 and body["git"]["path"] == "plugins/blur"
        assert body["git"]["branch"] == "feature/x"
        detail = genv.invoke("GET", "/plugins/{id}/versions/{v}", s["admin"], s["plugin_id"], 1)[1]
        assert detail["plugin"]["git"]["path"] == "plugins/blur"

    def test_link_rejects_foreign_connection(self, genv, setup):
        s = setup
        other_uc = genv.create_usecase()
        other_admin = genv.make_user(role="Viewer")
        genv.assign_role(other_admin, other_uc, "UseCaseAdmin")
        cid = genv.verified_connection(other_admin, other_uc)
        status, body = genv.link(s["admin"], s["plugin_id"], 1, cid)
        assert status == 404 and body["error"]["code"] == "CONNECTION_NOT_FOUND"

    def test_unlink(self, genv, setup):
        s = setup
        cid = genv.verified_connection(s["admin"], s["usecase_id"])
        genv.link(s["admin"], s["plugin_id"], 1, cid)
        status, body = genv.invoke_git("DELETE", "/plugins/{id}/versions/{v}/git", s["admin"],
                                       id=s["plugin_id"], v=1)
        assert status == 200 and body["git"] is None
        assert "git" not in genv.record(s["plugin_id"], 1)


# ------------------------------------------------------------- push / pull

class TestPushPull:
    def _linked(self, genv, setup):
        s = setup
        cid = genv.verified_connection(s["admin"], s["usecase_id"])
        genv.link(s["admin"], s["plugin_id"], 1, cid, path="plugins/blur")
        return cid

    def test_push_requires_link_and_verified_connection(self, genv, setup):
        s = setup
        status, body = genv.invoke_git("POST", "/plugins/{id}/versions/{v}/git/push", s["admin"],
                                       body={}, id=s["plugin_id"], v=1)
        assert status == 409 and body["error"]["code"] == "GIT_LINK_REQUIRED"
        _, created = genv.create_connection(s["admin"], s["usecase_id"])  # still verifying
        genv.link(s["admin"], s["plugin_id"], 1, created["connection"]["connection_id"])
        status, body = genv.invoke_git("POST", "/plugins/{id}/versions/{v}/git/push", s["admin"],
                                       body={}, id=s["plugin_id"], v=1)
        assert status == 409 and body["error"]["code"] == "CONNECTION_NOT_VERIFIED"
        assert body["error"]["details"]["status"] == "verifying"

    def test_push_starts_operation_with_environment_and_settles(self, genv, setup):
        s = setup
        self._linked(genv, setup)
        status, body = genv.invoke_git("POST", "/plugins/{id}/versions/{v}/git/push", s["admin"],
                                       body={"message": "first push"}, id=s["plugin_id"], v=1)
        assert status == 202, body
        op = body["operation"]
        assert op["kind"] == "push" and op["status"] == "running"
        assert op["target"] == {"branch": "main", "path": "plugins/blur", "force": False,
                                "last_sync_commit": None}
        record = genv.record(s["plugin_id"], 1)
        assert record["active_sync_operation"] == op["operation_id"]

        env = start_env(genv, op["operation_id"])
        assert env["SYNC_KIND"]["value"] == "push"
        assert env["SOURCE_PREFIX"]["value"] == record["source_s3_prefix"]
        assert env["COMMIT_MESSAGE"]["value"] == "first push"
        assert env["FORCE"]["value"] == "0"
        manifest = json.loads(env["MANIFEST_JSON"]["value"])
        assert manifest["pluginId"] == s["plugin_id"] and manifest["sourceRevision"] == 1
        assert manifest["scaffoldDeclaration"]["typeId"] == "custom.blur_regions"
        assert env["GIT_TOKEN"]["type"] == "SECRETS_MANAGER"
        plaintext = [v for v in env.values() if v["type"] == "PLAINTEXT"]
        assert len(plaintext) == len(env) - 1

        # Second push while running -> SYNC_IN_PROGRESS.
        status, body = genv.invoke_git("POST", "/plugins/{id}/versions/{v}/git/push", s["admin"],
                                       body={}, id=s["plugin_id"], v=1)
        assert status == 409 and body["error"]["code"] == "SYNC_IN_PROGRESS"
        assert body["error"]["details"]["operation_id"] == op["operation_id"]

        genv.settle(op["operation_id"], {"ok": True, "kind": "push", "commit": "a" * 40,
                                         "files": 5, "no_changes": False,
                                         "branch_created": False})
        _, polled = genv.invoke_git("GET", "/git-sync-operations/{opId}", s["admin"],
                                    opId=op["operation_id"])
        assert polled["operation"]["status"] == "succeeded"
        assert polled["operation"]["result"]["commit"] == "a" * 40
        record = genv.record(s["plugin_id"], 1)
        assert "active_sync_operation" not in record
        assert record["git"]["last_sync"]["commit"] == "a" * 40
        assert record["git"]["last_sync"]["kind"] == "push"
        assert record["git"]["last_sync"]["source_revision"] == 1
        # Duplicate delivery is a no-op.
        assert genv.settle(op["operation_id"], status="SUCCEEDED")["reason"] == "already settled"

        # The next push carries the last sync commit for the Divergence_Guard.
        status, body = genv.invoke_git("POST", "/plugins/{id}/versions/{v}/git/push", s["admin"],
                                       body={"force": True}, id=s["plugin_id"], v=1)
        assert status == 202
        assert body["operation"]["target"]["last_sync_commit"] == "a" * 40
        assert body["operation"]["target"]["force"] is True

        _, history = genv.invoke_git("GET", "/plugins/{id}/versions/{v}/git/operations",
                                     s["admin"], id=s["plugin_id"], v=1)
        assert [o["kind"] for o in history["operations"]] == ["push", "push"]
        assert history["operations"][0]["status"] == "running"

    def test_push_is_allowed_in_any_lifecycle_state(self, genv, setup):
        s = setup
        self._linked(genv, setup)
        genv.stack.tables.plugin_records.update_item(
            Key={"plugin_id": s["plugin_id"], "version": 1},
            UpdateExpression="SET lifecycle_state = :s", ExpressionAttributeValues={":s": "prod"})
        status, _ = genv.invoke_git("POST", "/plugins/{id}/versions/{v}/git/push", s["admin"],
                                    body={}, id=s["plugin_id"], v=1)
        assert status == 202

    @pytest.mark.parametrize("result, category, extra_key", [
        ({"ok": False, "category": "diverged", "message": "changed",
          "changed_files": ["plugins/blur/plugin/gstx.c"]}, "diverged", "changed_files"),
        ({"ok": False, "category": "push_rejected", "message": "rejected"}, "push_rejected", None),
        ({"ok": False, "category": "authentication",
          "message": "fatal: Authentication failed for https://x:ghp_abcdefghijklmnop@github.com/"},
         "authentication", None),
    ])
    def test_push_failures_settle_with_category(self, genv, setup, result, category, extra_key):
        s = setup
        self._linked(genv, setup)
        _, body = genv.invoke_git("POST", "/plugins/{id}/versions/{v}/git/push", s["admin"],
                                  body={}, id=s["plugin_id"], v=1)
        op_id = body["operation"]["operation_id"]
        genv.settle(op_id, result)
        op = genv.git.get_operation(op_id)
        assert op["status"] == "failed"
        assert op["failure"]["category"] == category
        if extra_key:
            assert op["failure"][extra_key] == result[extra_key]
        assert "ghp_abcdefghijklmnop" not in json.dumps(op["failure"])
        record = genv.record(s["plugin_id"], 1)
        assert "active_sync_operation" not in record and "last_sync" not in record["git"]

    def test_missing_result_uses_log_tail_as_internal(self, genv, setup, monkeypatch):
        s = setup
        self._linked(genv, setup)
        _, body = genv.invoke_git("POST", "/plugins/{id}/versions/{v}/git/push", s["admin"],
                                  body={}, id=s["plugin_id"], v=1)
        op_id = body["operation"]["operation_id"]
        fake_logs = MagicMock()
        fake_logs.get_log_events.return_value = {"events": [
            {"message": "cloning https://x-access-token:ghp_ZZZZZZZZZZZZZZZZ@github.com/a/b"},
            {"message": "fatal: something exploded"}]}
        monkeypatch.setattr(genv.git, "logs_client", fake_logs)
        genv.settle(op_id, None, status="FAILED",
                    logs={"group-name": "/aws/codebuild/dda-plugin-git-sync", "stream-name": "s"})
        op = genv.git.get_operation(op_id)
        assert op["status"] == "failed" and op["failure"]["category"] == "internal"
        assert "ghp_ZZZZZZZZZZZZZZZZ" not in op["failure"]["log_excerpt"]
        assert "https://***@github.com" in op["failure"]["log_excerpt"]

    def test_startbuild_failure_settles_synchronously(self, genv, setup, monkeypatch):
        s = setup
        self._linked(genv, setup)

        def boom(**kwargs):
            raise ClientError({"Error": {"Code": "AccessDeniedException", "Message": "no"}},
                              "StartBuild")
        monkeypatch.setattr(genv.git.codebuild, "start_build", boom)
        status, body = genv.invoke_git("POST", "/plugins/{id}/versions/{v}/git/push", s["admin"],
                                       body={}, id=s["plugin_id"], v=1)
        assert status == 502
        assert body["operation"]["status"] == "failed"
        assert body["operation"]["failure"]["category"] == "internal"
        assert "active_sync_operation" not in genv.record(s["plugin_id"], 1)

    def test_pull_in_place_replaces_tree_and_bumps_revision(self, genv, setup):
        s = setup
        self._linked(genv, setup)
        genv.seed_artifact(s["plugin_id"], 1, arch="x86_64")
        genv.s3.put_object(Bucket=genv.bucket,
                           Key=f"plugin-sources/{s['usecase_id']}/{s['plugin_id']}/1/extra/old.txt",
                           Body=b"old")
        status, body = genv.invoke_git("POST", "/plugins/{id}/versions/{v}/git/pull", s["admin"],
                                       body={"ref": "feature/x"}, id=s["plugin_id"], v=1)
        assert status == 202, body
        op = body["operation"]
        assert op["target"]["mode"] == "in_place" and op["target"]["ref"] == "feature/x"
        env = {k: v["value"] for k, v in start_env(genv, op["operation_id"]).items()}
        assert env["REF"] == "feature/x"
        assert env["STAGING_PREFIX"] == f"plugin-git-sync/{op['operation_id']}/tree/"

        pulled = dict(s["files"])
        pulled[HOOK] = "def process_frame(frame, params):\n    return frame[::-1]\n"
        genv.stage_tree(op["operation_id"], pulled)
        genv.settle(op["operation_id"], {"ok": True, "kind": "pull", "commit": "b" * 40,
                                         "ref": "feature/x", "tree_files": len(pulled)})
        settled = genv.git.get_operation(op["operation_id"])
        assert settled["status"] == "succeeded", settled
        assert settled["result"]["version"] == 1 and settled["result"]["mode"] == "in_place"
        tree = genv.source_tree(s["usecase_id"], s["plugin_id"], 1)
        assert tree == pulled  # extra/old.txt is gone, hook replaced
        record = genv.record(s["plugin_id"], 1)
        assert record["source_revision"] == 2
        assert record["git"]["last_sync"]["kind"] == "pull"
        assert record["git"]["last_sync"]["commit"] == "b" * 40
        assert "active_sync_operation" not in record
        detail = genv.invoke("GET", "/plugins/{id}/versions/{v}", s["admin"], s["plugin_id"], 1)[1]
        assert detail["plugin"]["stale_architectures"] == ["x86_64"]
        # Staging is cleaned up.
        listed = genv.s3.list_objects_v2(Bucket=genv.bucket,
                                         Prefix=f"plugin-git-sync/{op['operation_id']}/tree/")
        assert listed.get("KeyCount", 0) == 0

    def test_pull_new_version_creates_dev_version_from_staged_tree(self, genv, setup):
        s = setup
        self._linked(genv, setup)
        genv.stack.tables.plugin_records.update_item(
            Key={"plugin_id": s["plugin_id"], "version": 1},
            UpdateExpression="SET lifecycle_state = :s", ExpressionAttributeValues={":s": "test"})
        status, body = genv.invoke_git("POST", "/plugins/{id}/versions/{v}/git/pull", s["admin"],
                                       body={"mode": "in_place"}, id=s["plugin_id"], v=1)
        assert status == 409 and body["error"]["code"] == "SOURCE_LOCKED"
        status, body = genv.invoke_git("POST", "/plugins/{id}/versions/{v}/git/pull", s["admin"],
                                       body={}, id=s["plugin_id"], v=1)
        assert status == 202
        op = body["operation"]
        assert op["target"]["mode"] == "new_version" and op["target"]["ref"] == "main"
        pulled = dict(s["files"])
        pulled["docs/NOTES.md"] = "from git\n"
        genv.stage_tree(op["operation_id"], pulled)
        genv.settle(op["operation_id"], {"ok": True, "kind": "pull", "commit": "c" * 40,
                                         "ref": "main"})
        settled = genv.git.get_operation(op["operation_id"])
        assert settled["status"] == "succeeded", settled
        assert settled["result"]["version"] == 2
        v2 = genv.record(s["plugin_id"], 2)
        assert v2["lifecycle_state"] == "dev" and v2["provenance"]["forkedFrom"] == 1
        assert v2["provenance"]["gitPull"]["commit"] == "c" * 40
        assert v2["git"]["connection_id"] == genv.record(s["plugin_id"], 1)["git"]["connection_id"]
        assert v2["git"]["last_sync"]["version"] == 2
        assert genv.source_tree(s["usecase_id"], s["plugin_id"], 2) == pulled
        # The original version's tree and revision are untouched.
        assert genv.source_tree(s["usecase_id"], s["plugin_id"], 1) == s["files"]
        assert genv.record(s["plugin_id"], 1)["source_revision"] == 1

    def test_pull_scaffold_defects_change_nothing(self, genv, setup):
        s = setup
        self._linked(genv, setup)
        _, body = genv.invoke_git("POST", "/plugins/{id}/versions/{v}/git/pull", s["admin"],
                                  body={}, id=s["plugin_id"], v=1)
        op = body["operation"]
        pulled = {k: v for k, v in s["files"].items() if k != HOOK}
        genv.stage_tree(op["operation_id"], pulled)
        genv.settle(op["operation_id"], {"ok": True, "kind": "pull", "commit": "d" * 40})
        settled = genv.git.get_operation(op["operation_id"])
        assert settled["status"] == "failed"
        assert settled["failure"]["category"] == "invalid_source"
        assert any("frame_processing_hook" in d for d in settled["failure"]["defects"])
        assert genv.source_tree(s["usecase_id"], s["plugin_id"], 1) == s["files"]
        assert genv.record(s["plugin_id"], 1)["source_revision"] == 1
        assert "active_sync_operation" not in genv.record(s["plugin_id"], 1)

    def test_pull_runner_failures_settle(self, genv, setup):
        s = setup
        self._linked(genv, setup)
        for result in ({"ok": False, "kind": "pull", "category": "not_found", "message": "no ref"},
                       {"ok": False, "kind": "pull", "category": "invalid_source",
                        "message": "too big", "limit": "bytes"}):
            _, body = genv.invoke_git("POST", "/plugins/{id}/versions/{v}/git/pull", s["admin"],
                                      body={}, id=s["plugin_id"], v=1)
            op_id = body["operation"]["operation_id"]
            genv.settle(op_id, result)
            op = genv.git.get_operation(op_id)
            assert op["status"] == "failed" and op["failure"]["category"] == result["category"]
            if "limit" in result:
                assert op["failure"]["limit"] == "bytes"

    def test_rbac_for_sync_routes(self, genv, setup):
        s = setup
        cid = self._linked(genv, setup)
        viewer = genv.make_user(role="Viewer")
        genv.assign_role(viewer, s["usecase_id"], "Viewer")
        for method, resource in (("PUT", "/plugins/{id}/versions/{v}/git"),
                                 ("DELETE", "/plugins/{id}/versions/{v}/git"),
                                 ("POST", "/plugins/{id}/versions/{v}/git/push"),
                                 ("POST", "/plugins/{id}/versions/{v}/git/pull")):
            status, _ = genv.invoke_git(method, resource, viewer,
                                        body={"connection_id": cid}, id=s["plugin_id"], v=1)
            assert status == 403, (method, resource)
        status, body = genv.invoke_git("GET", "/plugins/{id}/versions/{v}/git/operations", viewer,
                                       id=s["plugin_id"], v=1)
        assert status == 200

    def test_foreign_project_events_are_ignored(self, genv, setup):
        result = genv.git.handler({"source": "aws.codebuild",
                                   "detail": {"project-name": "dda-plugin-build-x86_64"}}, None)
        assert result == {"recorded": False, "reason": "not the git-sync project"}
