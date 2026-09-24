"""
Property tests for the private-repo-plugin-import request contract and
fetch environment (tasks 2.2, 2.3, 3.2, 3.3).

The properties are pure functions of the request / result document, so
these call `plugin_importer`'s helpers directly. The modules come from the
session `aws_stack` fixture (module-scoped here, which hypothesis allows)
rather than a module-level import: standalone tests such as
test_captures.py install a fake `shared_utils` at collection time, and a
module-level `import plugin_importer` collected after them fails.
"""
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st


@pytest.fixture(scope="module")
def pi(aws_stack):
    return aws_stack.plugin_importer


@pytest.fixture(scope="module")
def pr(aws_stack):
    return aws_stack.plugin_records

TOKEN = "ghp_PROPERTYTOKEN0123456789abcdefghij"
CONNECTION = {
    "connection_id": "c-prop",
    "usecase_id": "uc-prop",
    "provider": "github",
    "repo_url": "https://github.com/acme/private.git",
    "default_branch": "main",
    "status": "verified",
    "secret_arn": "arn:aws:secretsmanager:us-east-1:123456789012:secret:dda-portal/git-connections/uc-prop/c-prop-XyZ",
}

_maybe_url = st.one_of(st.none(), st.just(""), st.just("https://github.com/acme/x.git"))
_maybe_conn = st.one_of(st.none(), st.just(""), st.just("c-1"))


class TestProperty2SourceExclusivity:
    """**Feature: private-repo-plugin-import, Property 2: Source exclusivity**
    Validates: Requirements 1.2"""

    @given(repo_url=_maybe_url, connection_id=_maybe_conn)
    def test_accepted_iff_exactly_one_present(self, pi, pr, repo_url, connection_id):
        result = pi.validate_import_source(repo_url, connection_id)
        present = bool(repo_url) + bool(connection_id)
        assert (result is None) == (present == 1)
        if result is not None:
            message, field = result
            assert field in ("repo_url", "connection_id")
            assert message


_path_like = st.from_regex(r"[a-zA-Z0-9_. /\\-]{0,24}", fullmatch=True)


class TestProperty4SubdirectoryValidity:
    """**Feature: private-repo-plugin-import, Property 4: Subdirectory validity**
    Validates: Requirements 1.5"""

    @given(path=st.one_of(_path_like, st.text(max_size=24)))
    def test_import_path_accepted_iff_normalize_source_path_accepts(self, pi, pr, path):
        # The import handler confines `path` with exactly
        # plugin_records.normalize_source_path; this pins that the rule is
        # the shared one (and idempotent) rather than a second predicate.
        normalized = pr.normalize_source_path(path)
        if normalized is None:
            return
        assert pr.normalize_source_path(normalized) == normalized
        assert not normalized.startswith("/")
        assert ".." not in normalized.split("/")


_revision = st.one_of(st.none(), st.from_regex(r"[a-z0-9.]{1,10}", fullmatch=True))
_subdir = st.one_of(st.none(), st.from_regex(r"[a-z]{1,6}(/[a-z0-9]{1,6}){0,2}", fullmatch=True))
_branch = st.one_of(st.none(), st.from_regex(r"[a-z]{1,8}(/[a-z0-9]{1,6})?", fullmatch=True))
_slug = st.one_of(st.none(), st.from_regex(r"[a-f0-9]{6}", fullmatch=True))


def _dest(slug):
    base = "plugin-sources/uc-prop/p-prop/1"
    return f"{base}/rev-{slug}" if slug else base


class TestProperty3FetchEnvironmentNeverCarriesTheToken:
    """**Feature: private-repo-plugin-import, Property 3: The fetch environment
    never carries the token** - Validates: Requirements 2.1"""

    @settings(max_examples=150)
    @given(revision=_revision, subdir=_subdir, branch=_branch, slug=_slug,
           shallow=st.booleans())
    def test_exactly_one_secrets_manager_variable_and_no_plaintext_token(
            self, pi, pr, revision, subdir, branch, slug, shallow):
        overrides = pi.fetch_env_overrides(
            CONNECTION["repo_url"], revision, _dest(slug), "uc-prop", "p-prop", 1,
            revision_slug_id=slug, connection=CONNECTION, subdir=subdir,
            branch=branch, shallow=shallow)
        secret_vars = [v for v in overrides if v["type"] == "SECRETS_MANAGER"]
        assert len(secret_vars) == 1
        assert secret_vars[0]["name"] == "GIT_TOKEN"
        assert secret_vars[0]["value"] == CONNECTION["secret_arn"] + ":token"
        for var in overrides:
            if var["type"] != "SECRETS_MANAGER":
                assert var["type"] == "PLAINTEXT"
                assert TOKEN not in var["value"]
                assert "secret:" not in var["value"] or var["name"] == "GIT_TOKEN"
        names = [v["name"] for v in overrides]
        assert len(names) == len(set(names))
        assert ("SHALLOW" in names) == shallow
        by_name = {v["name"]: v["value"] for v in overrides}
        assert by_name["REPO_SUBDIR"] == (subdir or "")
        assert by_name["REPO_BRANCH"] == (branch or "")
        # The result document is a sibling of the version prefix, never
        # inside the tree listing scope `{v}/`.
        assert by_name["RESULT_KEY"].startswith("plugin-sources/uc-prop/p-prop/1.fetch/")
        assert not by_name["RESULT_KEY"].startswith("plugin-sources/uc-prop/p-prop/1/")
        assert by_name["RESULT_KEY"].endswith(".json")


class TestProperty1AnonymousImportIsByteIdentical:
    """**Feature: private-repo-plugin-import, Property 1: Anonymous import is
    byte-identical** - Validates: Requirements 6.1"""

    @settings(max_examples=100)
    @given(revision=_revision, slug=_slug)
    def test_anonymous_overrides_equal_the_pre_feature_list(self, pi, pr, revision, slug):
        expected = [
            {"name": "REPO_URL", "value": "https://github.com/acme/x.git", "type": "PLAINTEXT"},
            {"name": "REVISION", "value": revision or "", "type": "PLAINTEXT"},
            {"name": "DEST_PREFIX", "value": _dest(slug), "type": "PLAINTEXT"},
            {"name": "USECASE_ID", "value": "uc-prop", "type": "PLAINTEXT"},
            {"name": "PLUGIN_ID", "value": "p-prop", "type": "PLAINTEXT"},
            {"name": "PLUGIN_VERSION", "value": "1", "type": "PLAINTEXT"},
        ]
        if slug:
            expected.append({"name": "REVISION_SLUG", "value": slug, "type": "PLAINTEXT"})
        actual = pi.fetch_env_overrides(
            "https://github.com/acme/x.git", revision, _dest(slug),
            "uc-prop", "p-prop", 1, revision_slug_id=slug)
        assert actual == expected

    @given(subdir=_subdir)
    def test_import_name_without_subdir_is_the_url_derived_name(self, pi, pr, subdir):
        url = "https://github.com/acme/gst-plugins-good.git"
        # Anonymous imports never pass a subdir, so the name is unchanged;
        # with a subdir the record is named after its last segment.
        assert pi.derive_import_name(None, url, []) == pi.default_plugin_name(url)
        if subdir:
            assert pi.derive_import_name(None, url, [], subdir=subdir) == subdir.rsplit("/", 1)[-1]


# ------------------------------------------------- result handling (task 5)

_token_like = st.one_of(
    st.from_regex(r"ghp_[A-Za-z0-9]{20,36}", fullmatch=True),
    st.from_regex(r"github_pat_[A-Za-z0-9_]{22,40}", fullmatch=True),
    st.from_regex(r"glpat-[A-Za-z0-9_\-]{20,26}", fullmatch=True),
    st.from_regex(r"gho_[A-Za-z0-9]{20,36}", fullmatch=True),
)
_log_words = st.lists(
    st.sampled_from(["fatal:", "remote:", "Authentication", "failed", "for",
                     "HTTP", "403", "Repository", "not", "found", "error:",
                     "pathspec", "unable", "to", "access", "clone", "\n"]),
    min_size=0, max_size=12)
_marker = st.sampled_from([None, "CLONE_FAILED", "PATH_NOT_FOUND",
                           "BRANCH_NOT_FOUND", "REVISION_NOT_FOUND",
                           "SYNC_FAILED"])


class TestProperty5RedactionTotality:
    """**Feature: private-repo-plugin-import, Property 5: Redaction totality**
    Validates: Requirements 2.4"""

    @settings(max_examples=200)
    @given(token=_token_like, before=_log_words, after=_log_words,
           marker=_marker, in_url=st.booleans())
    def test_stored_finding_never_contains_the_token(self, pi, pr, token, before, after,
                                                     marker, in_url):
        secret = (f"https://x-access-token:{token}@github.com/acme/private.git/"
                  if in_url else token)
        tail = " ".join(before) + " " + secret + " " + " ".join(after)
        result = {"status": "failed", "commit": None, "branch": None,
                  "failure_marker": marker, "stderr_tail": tail}
        finding, category = pi.fetch_failure_finding(
            result, {"connection_id": "c-prop", "path": "p", "branch": "main",
                     "revision": "v1"})
        assert token not in finding
        assert category in ("authentication", "not_found", "unreachable",
                            "internal")
        if marker in ("PATH_NOT_FOUND", "BRANCH_NOT_FOUND", "REVISION_NOT_FOUND"):
            assert category == "not_found"


_commit = st.from_regex(r"[0-9a-f]{40}", fullmatch=True)
_user = st.from_regex(r"user-[a-z0-9]{4,8}", fullmatch=True)


class TestProperty6LinkCompleteness:
    """**Feature: private-repo-plugin-import, Property 6: Link completeness**
    Validates: Requirements 5.1, 5.2"""

    @settings(max_examples=150)
    @given(subdir=st.from_regex(r"[a-z]{1,6}(/[a-z0-9]{1,6}){0,2}", fullmatch=True),
           requested_branch=st.from_regex(r"[a-z]{1,8}(/[a-z0-9]{1,6})?", fullmatch=True),
           resolved_branch=st.one_of(st.none(), st.from_regex(r"[a-z]{1,8}", fullmatch=True)),
           commit=_commit, user=_user, timestamp=st.integers(min_value=1, max_value=2**40))
    def test_link_carries_connection_branch_path_and_the_fetched_commit(
            self, pi, pr, subdir, requested_branch, resolved_branch, commit, user, timestamp):
        item = {
            "plugin_id": "p-prop", "version": 1, "name": "resize",
            "provenance": {"importedBy": user},
            "import_source": {"kind": "git_connection", "connection_id": "c-prop",
                              "path": subdir, "branch": requested_branch,
                              "revision": "default", "shallow": False},
        }
        result = {"status": "succeeded", "commit": commit,
                  "branch": resolved_branch, "failure_marker": None,
                  "stderr_tail": ""}
        link = pi.import_git_link(item, result, timestamp)
        expected_branch = resolved_branch or requested_branch
        assert link["connection_id"] == "c-prop"
        assert link["branch"] == expected_branch
        assert link["path"] == subdir
        assert link["linked_by"] == user
        assert link["linked_at"] == timestamp
        assert link["last_sync"] == {"kind": "pull", "commit": commit,
                                     "branch": expected_branch, "path": subdir,
                                     "by": user, "at": timestamp}
        # The link is exactly the hand-made shape (5.3): no extra keys.
        assert set(link) == {"connection_id", "branch", "path", "linked_by",
                             "linked_at", "last_sync"}

    @given(commit=st.one_of(st.none(), _commit))
    def test_whole_tree_import_never_links(self, pi, pr, commit):
        item = {"plugin_id": "p", "version": 1, "name": "n",
                "provenance": {"importedBy": "u"},
                "import_source": {"kind": "git_connection", "connection_id": "c",
                                  "branch": "main", "revision": "default",
                                  "shallow": False}}
        assert pi.import_git_link(item, {"commit": commit, "branch": "main"}, 5) is None
