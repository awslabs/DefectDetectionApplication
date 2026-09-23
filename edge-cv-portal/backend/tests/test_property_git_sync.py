"""
Property tests for git_sync.py (custom-node-source-lifecycle tasks 5.4-5.8).

Property 9:  Sync request validation (connection payload + link path).
Property 10: Start-build environment never carries the token.
Property 11: Failure classification is total and redaction is complete.
Property 12: Sync settlement is idempotent and releases the lock.
Property 13: Pull installation is all-or-nothing.
"""
import json
import uuid

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from test_git_sync import GitSyncEnv, TOKEN  # noqa: F401
from test_plugin_records import make_scaffold_declaration
from test_property_source_tree import reference_valid


@pytest.fixture(scope="module")
def module(aws_stack):
    return aws_stack.git_sync


# ---------------------------------------------------------------- Property 9

_hosts = st.sampled_from(["github.com", "gitlab.com", "git.example.internal:8443", ""])
_schemes = st.sampled_from(["https", "http", "ssh", "git", ""])
_paths = st.sampled_from(["/acme/plugins.git", "/group/sub/repo", "/", ""])
_creds = st.sampled_from(["", "user:pw@"])
_text = st.text(max_size=12)


def reference_url_ok(url):
    from urllib.parse import urlparse
    if not isinstance(url, str) or not url.strip():
        return False
    p = urlparse(url.strip())
    return (p.scheme == "https" and bool(p.netloc) and "@" not in p.netloc
            and bool(p.path) and p.path != "/")


class TestSyncRequestValidation:
    """**Feature: custom-node-source-lifecycle, Property 9: Sync request
    validation** — Validates: Requirements 2.1, 2.2, 3.1"""

    @given(scheme=_schemes, creds=_creds, host=_hosts, path=_paths,
           provider=st.sampled_from(["github", "gitlab", "bitbucket", "", None]),
           name=_text, branch=_text, token=_text)
    def test_connection_accepted_iff_all_fields_valid(self, module, scheme, creds, host,
                                                      path, provider, name, branch, token):
        url = f"{scheme}://{creds}{host}{path}" if scheme else f"{creds}{host}{path}"
        body = {"provider": provider, "repo_url": url, "name": name,
                "default_branch": branch, "token": token}
        err = module.validate_connection(body)
        expected_ok = (provider in ("github", "gitlab") and reference_url_ok(url)
                       and bool(name.strip()) and bool(branch.strip()) and bool(token.strip()))
        assert (err is None) == expected_ok

    @given(path=st.one_of(_text, st.sampled_from(["../x", "/abs", "a/../../b", ".", "plugins/blur/"])))
    def test_repo_path_follows_source_path_rule(self, module, path):
        result = module.normalize_repo_path(path)
        assert (result is not None) == reference_valid(path)
        if result is not None:
            assert not result.startswith("/") and ".." not in result.split("/")


# --------------------------------------------------------------- Property 10

_token_strings = st.text(alphabet=st.characters(min_codepoint=33, max_codepoint=126),
                         min_size=12, max_size=40)


class TestStartEnvironment:
    """**Feature: custom-node-source-lifecycle, Property 10: Start-build
    environment never carries the token** — Validates: Requirements 2.3, 2.4"""

    @given(kind=st.sampled_from(["verify", "push", "pull"]),
           provider=st.sampled_from(["github", "gitlab"]),
           branch=st.text(min_size=1, max_size=10), path=st.text(min_size=1, max_size=10),
           force=st.booleans(), token=_token_strings)
    def test_exactly_one_secrets_manager_variable(self, module, kind, provider, branch,
                                                  path, force, token):
        connection = {"connection_id": "c1", "usecase_id": "uc", "provider": provider,
                      "repo_url": "https://github.com/a/b.git", "default_branch": "main",
                      "secret_arn": "arn:aws:secretsmanager:us-east-1:1:secret:dda-portal/git-connections/uc/c1-AbCdEf"}
        item = {"plugin_id": "p1", "version": 3, "usecase_id": "uc", "name": "blur",
                "source_s3_prefix": "plugin-sources/uc/p1/3/", "provenance": {}}
        target = {"branch": branch, "path": path, "force": force, "ref": branch,
                  "last_sync_commit": "abc"}
        env = module.build_start_environment(kind, "op-1", connection, item, target,
                                             manifest={"pluginId": "p1"}, message="m")
        secrets = [v for v in env if v["type"] == "SECRETS_MANAGER"]
        assert len(secrets) == 1
        assert secrets[0]["name"] == "GIT_TOKEN"
        assert secrets[0]["value"] == f"{connection['secret_arn']}:token"
        assert all(v["type"] == "PLAINTEXT" for v in env if v is not secrets[0])
        assert all(token not in v["value"] for v in env)
        names = [v["name"] for v in env]
        assert len(names) == len(set(names))
        assert ("SOURCE_PREFIX" in names) == (kind == "push")
        assert ("STAGING_PREFIX" in names) == (kind == "pull")


# --------------------------------------------------------------- Property 11

MARKERS = {
    "authentication": ["Authentication failed", "could not read Username", "HTTP 403",
                       "Invalid username or password", "Permission denied"],
    "not_found": ["Repository not found", "couldn't find remote ref", "HTTP 404",
                  "does not appear to be a git repository"],
    "unreachable": ["Could not resolve host", "Connection timed out", "unable to access",
                    "Connection refused"],
}
_filler = st.text(alphabet=st.characters(blacklist_categories=("Cs",)), max_size=30)


class TestClassificationAndRedaction:
    """**Feature: custom-node-source-lifecycle, Property 11: Failure
    classification is total and redaction is complete** — Validates:
    Requirements 4.11, 9.5"""

    @given(text=_filler)
    def test_classification_is_total(self, module, text):
        assert module.classify_failure(text) in ("authentication", "not_found",
                                                 "unreachable", "internal")

    @given(category=st.sampled_from(sorted(MARKERS)), index=st.integers(0, 4),
           prefix=_filler, suffix=_filler)
    def test_markers_land_in_their_category(self, module, category, index, prefix, suffix):
        marker = MARKERS[category][index % len(MARKERS[category])]
        text = f"{prefix} {marker} {suffix}"
        result = module.classify_failure(text)
        # An earlier-precedence category may win if the filler happens to
        # contain one of its markers; otherwise the planted marker decides.
        lowered = text.lower()
        earlier = [c for c, _ in module._CLASSIFY_RULES]
        for cat, markers in module._CLASSIFY_RULES:
            if any(m in lowered for m in markers):
                assert result == cat
                break
        assert result != "internal"

    @given(kind=st.sampled_from(["ghp", "gho", "ghu", "ghs", "ghr", "github_pat", "glpat"]),
           body=st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789", min_size=8, max_size=30),
           prefix=st.text(alphabet="abcdef ghij\n", max_size=20),
           suffix=st.text(alphabet="klmno pqrs\n", max_size=20),
           user=st.text(alphabet="abcxyz", min_size=1, max_size=6))
    def test_redaction_removes_every_secret_and_keeps_clean_text(self, module, kind, body,
                                                                 prefix, suffix, user):
        secret = f"{kind}_{body}" if kind != "glpat" else f"glpat-{body}"
        url_secret = f"https://{user}:{secret}@github.com/a/b.git"
        text = f"{prefix} {secret} {suffix} {url_secret}"
        redacted = module.redact(text)
        assert secret not in redacted
        assert f"{user}:{secret}@" not in redacted
        assert "https://***@github.com/a/b.git" in redacted
        clean = f"{prefix} plain words {suffix}"
        assert module.redact(clean) == clean


# ---------------------------------------------------------- Property 12 / 13

@pytest.fixture
def genv(aws_stack, monkeypatch):
    env = GitSyncEnv(aws_stack)
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


def new_linked_plugin(genv):
    usecase_id = genv.create_usecase()
    admin = genv.make_user(role="Viewer")
    genv.assign_role(admin, usecase_id, "UseCaseAdmin")
    status, body = genv.create_plugin(admin, usecase_id, declaration=make_scaffold_declaration())
    assert status == 201
    plugin_id, files = body["plugin"]["plugin_id"], body["files"]
    cid = genv.verified_connection(admin, usecase_id)
    genv.link(admin, plugin_id, 1, cid, path="blur")
    return usecase_id, admin, plugin_id, files


class TestSettlementIdempotency:
    """**Feature: custom-node-source-lifecycle, Property 12: Sync settlement
    is idempotent and releases the lock** — Validates: Requirements 3.10,
    3.12, 4.9"""

    @given(kind=st.sampled_from(["push", "pull"]), ok=st.booleans(),
           duplicates=st.integers(min_value=1, max_value=3))
    @settings(max_examples=20, deadline=None,
              suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow])
    def test_first_delivery_settles_later_ones_are_noops(self, genv, kind, ok, duplicates):
        usecase_id, admin, plugin_id, files = new_linked_plugin(genv)
        route = f"/plugins/{{id}}/versions/{{v}}/git/{kind}"
        status, body = genv.invoke_git("POST", route, admin, body={}, id=plugin_id, v=1)
        assert status == 202, body
        op_id = body["operation"]["operation_id"]
        assert genv.record(plugin_id, 1)["active_sync_operation"] == op_id

        if kind == "pull" and ok:
            genv.stage_tree(op_id, files)
        result = ({"ok": True, "kind": kind, "commit": "e" * 40, "files": len(files),
                   "ref": "main", "no_changes": False}
                  if ok else {"ok": False, "kind": kind, "category": "not_found", "message": "x"})
        first = genv.settle(op_id, result)
        assert first["recorded"] is True
        op_after_first = genv.git.get_operation(op_id)
        record_after_first = genv.record(plugin_id, 1)
        assert op_after_first["status"] == ("succeeded" if ok else "failed")
        assert "active_sync_operation" not in record_after_first
        if ok:
            assert record_after_first["git"]["last_sync"]["commit"] == "e" * 40
        else:
            assert "last_sync" not in record_after_first["git"]

        for _ in range(duplicates):
            again = genv.settle(op_id, None)
            assert again["recorded"] is False
            assert genv.git.get_operation(op_id) == op_after_first
            assert genv.record(plugin_id, 1) == record_after_first


class TestPullInstallation:
    """**Feature: custom-node-source-lifecycle, Property 13: Pull
    installation is all-or-nothing** — Validates: Requirements 4.3, 4.6,
    4.7, 4.8"""

    _extra = st.dictionaries(
        st.from_regex(r"(docs|extra)/[a-z]{1,6}\.(md|txt)", fullmatch=True),
        st.text(min_size=1, max_size=20), max_size=4)

    @given(mode=st.sampled_from(["in_place", "new_version"]),
           staged_extra=_extra, target_extra=_extra, valid=st.booleans())
    @settings(max_examples=20, deadline=None,
              suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow])
    def test_install_outcomes(self, genv, mode, staged_extra, target_extra, valid):
        usecase_id, admin, plugin_id, files = new_linked_plugin(genv)
        # Extra files on the current tree that a valid in_place pull removes.
        for path, content in target_extra.items():
            genv.s3.put_object(Bucket=genv.bucket,
                               Key=f"plugin-sources/{usecase_id}/{plugin_id}/1/{path}",
                               Body=content.encode())
        before_tree = genv.source_tree(usecase_id, plugin_id, 1)
        before_revision = genv.record(plugin_id, 1)["source_revision"]

        staged = {**files, **staged_extra}
        if not valid:
            staged.pop("plugin/frame_processing_hook.py")
        status, body = genv.invoke_git("POST", "/plugins/{id}/versions/{v}/git/pull", admin,
                                       body={"mode": mode}, id=plugin_id, v=1)
        assert status == 202, body
        op_id = body["operation"]["operation_id"]
        genv.stage_tree(op_id, staged)
        genv.settle(op_id, {"ok": True, "kind": "pull", "commit": "f" * 40, "ref": "main"})
        op = genv.git.get_operation(op_id)

        versions = [v["version"] for v in
                    genv.invoke("GET", "/plugins/{id}", admin, plugin_id)[1]["versions"]]
        if not valid:
            assert op["status"] == "failed" and op["failure"]["category"] == "invalid_source"
            assert genv.source_tree(usecase_id, plugin_id, 1) == before_tree
            assert genv.record(plugin_id, 1)["source_revision"] == before_revision
            assert versions == [1]
        elif mode == "in_place":
            assert op["status"] == "succeeded", op
            assert genv.source_tree(usecase_id, plugin_id, 1) == staged
            assert genv.record(plugin_id, 1)["source_revision"] == before_revision + 1
            assert versions == [1]
        else:
            assert op["status"] == "succeeded", op
            assert genv.source_tree(usecase_id, plugin_id, 1) == before_tree
            assert genv.record(plugin_id, 1)["source_revision"] == before_revision
            assert sorted(versions) == [1, 2]
            assert genv.source_tree(usecase_id, plugin_id, 2) == staged
            assert genv.record(plugin_id, 2)["source_revision"] == 1
        assert "active_sync_operation" not in genv.record(plugin_id, 1)
        # Staging never lingers.
        listed = genv.s3.list_objects_v2(Bucket=genv.bucket, Prefix=f"plugin-git-sync/{op_id}/tree/")
        assert listed.get("KeyCount", 0) == 0
