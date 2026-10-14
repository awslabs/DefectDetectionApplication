# Copyright 2025 Amazon Web Services, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""
Repository and ref validation/normalization properties
(build-source-selection, task 7).

**Property 8: Expected Behavior** - Repository validation is total and
containment-safe.

**Validates: Requirements 1.3, 1.4, 2.7, 3.5**

The rule, restated independently of the implementation:

* ``normalize_repository_url`` is TOTAL: for every input — arbitrary text,
  a hostile corpus, and non-string junk — it returns either
  ``(normalized, None)`` or ``(None, error)`` where the error names its
  ``rule`` and its ``field``; it never raises (Req 1.4).
* Every ACCEPTED value satisfies the Normalized_Repository invariants:
  HTTPS, a host in ``ALLOWED_REPOSITORY_HOSTS``, a path of exactly
  ``<owner>/<repo>``, and no userinfo, port, query, or fragment
  (Req 1.3, 1.4).
* Discovery URLs are built ONLY from ``parse_owner_repo()`` of the
  normalized form against the fixed API host — never from raw input — so
  no input can direct a request at a non-repository endpoint (Req 3.5).
  ``parse_owner_repo`` refuses anything that is not a
  Normalized_Repository, so there is no path from a rejected string to an
  outbound URL. Task 10.1's extension: the REAL ``discover_branches`` is
  run against every accepted value with a recording injected fetch, and
  every recorded outbound URL must start with
  ``https://api.github.com/repos/<owner>/<repo>`` derived from the parse.
* ``normalize_source_ref`` is TOTAL the same way, accepts branch names,
  tags, and 40-hex commit SHAs verbatim (Req 2.7), and rejects control
  characters, whitespace, a leading ``-``, and ``..``.

Pure module test: no AWS clients, no network, no compute. Run with
``--noconftest`` like the rest of the ``portal_builds`` suite.
"""
import os
import re
import sys

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

# Import the pure source module from the portal Lambda bundle.
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.normpath(os.path.join(_HERE, "..", "..", ".."))
_FUNCTIONS_DIR = os.path.join(_REPO_ROOT, "edge-cv-portal", "backend",
                              "functions")
if _FUNCTIONS_DIR not in sys.path:
    sys.path.insert(0, _FUNCTIONS_DIR)

import build_source  # noqa: E402

# ---------------------------------------------------------------------------
# The invariants, restated here rather than imported, so the test cannot
# drift with the implementation.
# ---------------------------------------------------------------------------

#: The one fixed discovery API host (task 10.1 speaks the GitHub API).
API_HOST = "api.github.com"

#: A Normalized_Repository: https, allowlisted host, exactly
#: ``<owner>/<repo>``, nothing else (Req 1.4, 3.5).
NORMALIZED_RE = re.compile(
    r"^https://(github\.com)/"
    r"[A-Za-z0-9][A-Za-z0-9-]*"          # owner: no '/', '@', ':', '?', '#'
    r"/[A-Za-z0-9_][A-Za-z0-9_.-]*$"     # repo: no '/', '@', ':', '?', '#'
)

#: What an owner / repo segment out of the parse may contain: nothing that
#: could re-shape an outbound URL (no separator, authority, query or
#: fragment characters, no traversal).
OWNER_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*$")
REPO_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")

#: An accepted ref: git-safe charset, no whitespace, no control characters,
#: no leading '-', no '..' (Req 2.7 and the injection containment).
ACCEPTED_REF_RE = re.compile(r"^[A-Za-z0-9._/+-]+$")

DDA_URL = "https://github.com/awslabs/DefectDetectionApplication"

#: Hostile repository corpus: every known way to smuggle a non-repository
#: endpoint, another host, credentials, or shell/URL metacharacters past a
#: naive parser (Req 1.4, 3.5).
HOSTILE_REPOSITORIES = [
    "http://github.com/owner/repo",                    # non-https scheme
    "git@github.com:owner/repo.git",                   # scp-style remote
    "ssh://git@github.com/owner/repo",                 # ssh scheme
    "ftp://github.com/owner/repo",                     # other scheme
    "file:///etc/passwd",                              # local file scheme
    "javascript:alert(1)",                             # non-remote scheme
    "https://user:pass@github.com/owner/repo",         # userinfo
    "https://github.com@evil.com/owner/repo",          # host-in-userinfo
    "https://github.com:443/owner/repo",               # port (even https')
    "https://github.com:8443/owner/repo",              # port
    "https://github.com/owner/repo?x=1",               # query
    "https://github.com/owner/repo#frag",              # fragment
    "https://github.com/owner/repo/tree/main",         # extra segments
    "https://github.com/owner",                        # missing repo
    "https://github.com/",                             # no path
    "https://github.com",                              # no path at all
    "https://github.com//owner/repo",                  # empty segment
    "https://github.com/owner//repo",                  # empty segment
    "https://github.com/../repo",                      # traversal owner
    "https://github.com/owner/..",                     # traversal repo
    "https://github.com/owner/.git",                   # bare .git repo
    "https://evil.com/owner/repo",                     # other host
    "https://gitlab.com/owner/repo",                   # other forge
    "https://github.com.evil.com/owner/repo",          # host suffix trick
    "https://evilgithub.com/owner/repo",               # host lookalike
    "https://github.com./owner/repo",                  # trailing-dot host
    "https://[::1]/owner/repo",                        # IPv6 literal
    "https://[::1/owner/repo",                         # malformed authority
    "https://github.com/owner/repo%2Fextra",           # encoded separator
    "https://github.com/own er/repo",                  # embedded space
    "https://github.com/owner/repo\n.evil.com",        # newline splice
    "https://github.com/owner/re\tpo",                 # tab
    "https://github.com/owner/repo\x00",               # NUL
    "https://github.com/owner/$(rm -rf /)",            # shell substitution
    "https://github.com/owner/`id`",                   # backtick
    "https://github.com/owner/repo;ls",                # separator char
    "",                                                # empty
    "   ",                                             # blank
    "not a url at all",                                # free text
    "https://" + "a" * 600 + "/owner/repo",            # over-long
]

#: Hostile ref corpus: option injection, range/traversal operators, shell
#: metacharacters, whitespace, control characters, and git's own forbidden
#: forms.
HOSTILE_REFS = [
    "-rf",                          # leading '-': git reads an option
    "--upload-pack=/bin/sh",        # the classic argument injection
    "main..dev",                    # '..' range operator
    "a/../b",                       # '..' traversal
    "..",                           # bare '..'
    "ref with space",               # whitespace
    "ref\tname",                    # tab
    "ref\nname",                    # newline
    "ref\rname",                    # carriage return
    "ref\x00name",                  # NUL
    "ref\x1bname",                  # escape
    "$(rm -rf /)",                  # shell substitution
    "`id`",                         # backtick
    "ref;ls",                       # separator
    "ref|ls",                       # pipe
    "ref&bg",                       # background
    "HEAD^",                        # git-forbidden '^'
    "ref~1",                        # git-forbidden '~'
    "ref:path",                     # git-forbidden ':'
    "ref?",                         # git-forbidden '?'
    "ref*",                         # git-forbidden '*'
    "ref[",                         # git-forbidden '['
    "ref\\name",                    # git-forbidden backslash
    "@{upstream}",                  # git-forbidden '@{'
    "/leading",                     # leading '/'
    "trailing/",                    # trailing '/'
    "a//b",                         # empty component
    ".hidden",                      # component starting '.'
    "dir/.hidden",                  # nested component starting '.'
    "name.lock",                    # '.lock' suffix
    "ends.",                        # trailing '.'
    "r" * 300,                      # over-long
]

#: Non-string junk both functions must reject (or, for the ref, treat None
#: as "no selection") without raising.
NON_STRINGS = [None, 17, 3.5, True, False, [], ["x"], {}, {"url": "y"},
               b"https://github.com/owner/repo", object()]


def _record_discovery_urls(normalized):
    """Every outbound URL the real ``discover_branches`` composes for one
    accepted repository, captured by an injected fetch (task 10.1: no
    real network call is ever made)."""
    recorded = []

    def recording_fetch(url):
        recorded.append(url)
        # The branches listing is the only paged (query-carrying) URL; the
        # substring check alone would misfire on a repo named 'branches'.
        if "/branches?" in url:
            return [{"name": "main"}]
        return {"default_branch": "main"}

    result = build_source.discover_branches(normalized,
                                            fetch=recording_fetch)
    assert "error" not in result, result
    assert recorded, "discovery made no outbound call"
    return recorded


def _assert_repository_rejection(error):
    """A rejection names its rule and its field (Req 1.4)."""
    assert isinstance(error, dict)
    assert error["rule"] == build_source.RULE_REPOSITORY_INVALID
    assert error["field"] == "repository"
    assert error["message"]


def _assert_ref_rejection(error):
    assert isinstance(error, dict)
    assert error["rule"] == build_source.RULE_SOURCE_REF_INVALID
    assert error["field"] == "source_ref"
    assert error["message"]


def _assert_repository_outcome(value):
    """The Property 8 totality + invariant check for ONE input."""
    normalized, error = build_source.normalize_repository_url(value)
    # Total: exactly one of the two, for every input.
    assert (normalized is None) != (error is None)
    if error is not None:
        _assert_repository_rejection(error)
        # No path from a rejected string to an outbound URL (Req 3.5).
        with pytest.raises(ValueError):
            build_source.parse_owner_repo(value)
        return
    # Accepted: the Normalized_Repository invariants hold (Req 1.3, 1.4).
    assert NORMALIZED_RE.match(normalized), normalized
    assert not normalized.endswith("/")
    # Re-normalizing an accepted form accepts it again unchanged enough to
    # keep parsing stable: same owner/repo out of the parse.
    owner, repo = build_source.parse_owner_repo(normalized)
    assert OWNER_SEGMENT_RE.match(owner), owner
    assert REPO_SEGMENT_RE.match(repo), repo
    # The only discovery URL constructible from the parse hits the fixed
    # API host at a /repos/<owner>/<repo> path (Req 3.5).
    discovery = f"https://{API_HOST}/repos/{owner}/{repo}"
    assert discovery.startswith(f"https://{API_HOST}/repos/")
    assert discovery == f"https://{API_HOST}/repos/{owner}/{repo}"
    assert "@" not in discovery.split("//", 1)[1].split("/", 1)[0]
    # Task 10.1 extension: run the REAL discovery against a recording
    # injected fetch — every outbound URL it composes starts with
    # https://api.github.com/repos/<owner>/<repo> from the parse, and
    # carries no credentials (Req 3.5, 3.2).
    for url in _record_discovery_urls(normalized):
        assert url.startswith(discovery), (normalized, url)
        assert url[len(discovery):len(discovery) + 1] in ("", "/", "?"), url
        assert "@" not in url.split("//", 1)[1].split("/", 1)[0]


def _assert_ref_outcome(value):
    """The Property 8 totality + invariant check for one ref input."""
    ref, error = build_source.normalize_source_ref(value)
    if error is not None:
        assert ref is None
        _assert_ref_rejection(error)
        return
    # Accepted: either "no selection" (None) or a value satisfying the
    # accepted-ref invariants verbatim apart from surrounding whitespace.
    if ref is None:
        assert value is None or (isinstance(value, str)
                                 and not value.strip())
        return
    assert ACCEPTED_REF_RE.match(ref), ref
    assert not ref.startswith("-")
    assert ".." not in ref
    assert isinstance(value, str) and ref == value.strip()


# ---------------------------------------------------------------------------
# Property 8 — hypothesis over arbitrary text plus the hostile corpus
# ---------------------------------------------------------------------------

class TestProperty8RepositoryValidationIsTotal:
    """**Validates: Requirements 1.3, 1.4, 3.5**"""

    @settings(max_examples=300)
    @given(st.text())
    def test_arbitrary_text_is_totally_classified(self, value):
        _assert_repository_outcome(value)

    @settings(max_examples=200)
    @given(st.one_of(
        st.none(), st.integers(), st.floats(allow_nan=True), st.booleans(),
        st.binary(), st.lists(st.text(), max_size=2),
        st.dictionaries(st.text(max_size=4), st.text(max_size=4),
                        max_size=2),
    ))
    def test_non_string_input_is_rejected_not_raised(self, value):
        normalized, error = build_source.normalize_repository_url(value)
        assert normalized is None
        _assert_repository_rejection(error)

    @pytest.mark.parametrize("value", HOSTILE_REPOSITORIES)
    def test_hostile_corpus_is_rejected_with_the_field_named(self, value):
        normalized, error = build_source.normalize_repository_url(value)
        assert normalized is None, (value, normalized)
        _assert_repository_rejection(error)

    @pytest.mark.parametrize("value", NON_STRINGS)
    def test_non_string_junk_is_rejected_with_the_field_named(self, value):
        normalized, error = build_source.normalize_repository_url(value)
        assert normalized is None
        _assert_repository_rejection(error)

    @settings(max_examples=200)
    @given(
        owner=st.from_regex(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}",
                            fullmatch=True),
        repo=st.from_regex(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,99}",
                           fullmatch=True),
        git_suffix=st.booleans(),
        trailing_slash=st.booleans(),
        pad=st.sampled_from(["", " ", "  ", "\t"]),
    )
    def test_every_wellformed_url_is_accepted_and_normalized(
            self, owner, repo, git_suffix, trailing_slash, pad):
        """The accept side is not vacuous: any real ``<owner>/<repo>``
        (optional ``.git``, optional trailing slash, padding) normalizes
        to the canonical form, and the parse round-trips (Req 1.3)."""
        candidate = f"https://github.com/{owner}/{repo}"
        if git_suffix:
            candidate += ".git"
        if trailing_slash:
            candidate += "/"
        candidate = f"{pad}{candidate}{pad}"
        normalized, error = build_source.normalize_repository_url(candidate)
        if repo.endswith(".git") or (git_suffix and repo.endswith(".")):
            # 'x.git' loses its suffix and 'x..git' would need 'x.' — the
            # generator can produce shapes whose stripped form differs;
            # totality still holds either way.
            _assert_repository_outcome(candidate)
            return
        assert error is None, (candidate, error)
        assert normalized == f"https://github.com/{owner}/{repo}"
        assert build_source.parse_owner_repo(normalized) == (owner, repo)
        # Idempotent: normalizing the normalized form returns it unchanged.
        again, err2 = build_source.normalize_repository_url(normalized)
        assert err2 is None and again == normalized


class TestProperty8SourceRefValidationIsTotal:
    """**Validates: Requirements 1.4, 2.7**"""

    @settings(max_examples=300)
    @given(st.text())
    def test_arbitrary_text_is_totally_classified(self, value):
        _assert_ref_outcome(value)

    @settings(max_examples=100)
    @given(st.one_of(st.integers(), st.floats(allow_nan=True),
                     st.booleans(), st.binary(),
                     st.lists(st.text(), max_size=2)))
    def test_non_string_non_none_input_is_rejected_not_raised(self, value):
        ref, error = build_source.normalize_source_ref(value)
        assert ref is None
        _assert_ref_rejection(error)

    @pytest.mark.parametrize("value", HOSTILE_REFS)
    def test_hostile_corpus_is_rejected_with_the_field_named(self, value):
        ref, error = build_source.normalize_source_ref(value)
        assert ref is None, (value, ref)
        _assert_ref_rejection(error)

    @settings(max_examples=200)
    @given(st.from_regex(r"[0-9a-fA-F]{40}", fullmatch=True))
    def test_forty_hex_shas_are_accepted_verbatim(self, sha):
        """Req 2.7: non-branch refs stay valid, unchanged."""
        ref, error = build_source.normalize_source_ref(sha)
        assert error is None
        assert ref == sha


# ---------------------------------------------------------------------------
# Unit tests — the exact examples the task names
# ---------------------------------------------------------------------------

class TestRepositoryUnitExamples:
    """**Validates: Requirements 1.3, 1.4**"""

    @pytest.mark.parametrize("candidate", [
        DDA_URL,
        DDA_URL + ".git",
        DDA_URL + "/",
        DDA_URL + ".git/",
    ])
    def test_the_dda_url_normalizes_in_all_its_spellings(self, candidate):
        normalized, error = build_source.normalize_repository_url(candidate)
        assert error is None
        assert normalized == DDA_URL

    def test_a_fork_is_accepted(self):
        normalized, error = build_source.normalize_repository_url(
            "https://github.com/some-user/DefectDetectionApplication.git")
        assert error is None
        assert normalized == \
            "https://github.com/some-user/DefectDetectionApplication"

    @pytest.mark.parametrize("candidate,why", [
        ("http://github.com/awslabs/DefectDetectionApplication", "http"),
        ("git@github.com:awslabs/DefectDetectionApplication.git", "git@"),
        ("https://user:token@github.com/awslabs/DDA", "userinfo"),
        ("https://github.com:8443/awslabs/DDA", "port"),
        ("https://github.com/awslabs/DDA?ref=main", "query"),
        ("https://github.com/awslabs/DDA#readme", "fragment"),
        ("https://github.com/awslabs/DDA/tree/main", "extra segment"),
        ("https://gitlab.com/awslabs/DDA", "other host"),
    ])
    def test_each_named_rejection_form(self, candidate, why):
        normalized, error = build_source.normalize_repository_url(candidate)
        assert normalized is None, why
        _assert_repository_rejection(error)

    def test_non_string_is_rejected(self):
        normalized, error = build_source.normalize_repository_url(
            ["https://github.com/awslabs/DDA"])
        assert normalized is None
        _assert_repository_rejection(error)

    def test_parse_owner_repo_of_the_dda_url(self):
        assert build_source.parse_owner_repo(DDA_URL) == \
            ("awslabs", "DefectDetectionApplication")

    def test_parse_owner_repo_refuses_raw_input(self):
        """Discovery must never be built from raw input (Req 3.5)."""
        with pytest.raises(ValueError):
            build_source.parse_owner_repo(
                "https://evil.com/awslabs/DefectDetectionApplication")

    def test_allowed_hosts_constant(self):
        assert build_source.ALLOWED_REPOSITORY_HOSTS == ("github.com",)


class TestSourceRefUnitExamples:
    """**Validates: Requirements 1.4, 2.7**"""

    @pytest.mark.parametrize("candidate", [
        "main",
        "feature/portal-build-fleet-and-workflow-gates",
        "v1.2.3",
        "479ab7f0479ab7f0479ab7f0479ab7f0479ab7f0",   # 40-hex SHA
    ])
    def test_each_named_accepted_form_survives_verbatim(self, candidate):
        ref, error = build_source.normalize_source_ref(candidate)
        assert error is None
        assert ref == candidate

    @pytest.mark.parametrize("candidate,why", [
        ("release\x07bell", "control character"),
        ("my branch", "whitespace"),
        ("-option-looking", "leading '-'"),
        ("main..dev", "'..'"),
    ])
    def test_each_named_rejected_form(self, candidate, why):
        ref, error = build_source.normalize_source_ref(candidate)
        assert ref is None, why
        _assert_ref_rejection(error)

    def test_none_and_blank_mean_no_selection_not_an_error(self):
        """The existing ``source_ref = None`` meaning is preserved."""
        assert build_source.normalize_source_ref(None) == (None, None)
        assert build_source.normalize_source_ref("") == (None, None)
        assert build_source.normalize_source_ref("   ") == (None, None)
