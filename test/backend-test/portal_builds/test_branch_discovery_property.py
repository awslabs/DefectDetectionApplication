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
Branch discovery classification properties
(build-source-selection, task 10.1).

**Property 10: Expected Behavior** - Discovery result classification.

**Validates: Requirements 3.1, 3.2, 3.3, 3.5**

The rule, restated independently of the implementation: _for any_ upstream
outcome — success, empty repository, 404, rate-limited 403, non-rate-limit
403, 429, timeout, 5xx, malformed payload — ``discover_branches`` returns

* on success: the branch list with EXACTLY ONE branch identified as the
  default (present in the list), and a ``truncated`` flag at the page cap
  (Req 3.1);
* otherwise: a distinct, actionable error code per condition (Req 3.3) —
  ``REPOSITORY_NOT_FOUND`` (404), ``REPOSITORY_FORBIDDEN`` (403 without
  rate-limit indication), ``DISCOVERY_RATE_LIMITED`` (403/429 with it),
  ``DISCOVERY_TIMEOUT``, ``DISCOVERY_UPSTREAM_ERROR`` (5xx or malformed),
  ``REPOSITORY_EMPTY`` (reachable, no branches);
* NEVER a failure shaped as a success with an empty branch list;
* with every outbound URL built only from ``parse_owner_repo()`` against
  the fixed ``https://api.github.com`` host, carrying no credentials
  (Req 3.2, 3.5).

The upstream is an INJECTED fetch (the ``vllm_fit_check._default_hf_fetch``
pattern): no real network call is ever made. Pure module test — run with
``--noconftest`` like the rest of the ``portal_builds`` suite.
"""
import email.message
import io
import os
import socket
import sys
import urllib.error
from urllib.parse import parse_qs, urlsplit

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

API_PREFIX = "https://api.github.com/repos/"
DDA_URL = "https://github.com/awslabs/DefectDetectionApplication"

PER_PAGE = build_source.BRANCHES_PER_PAGE          # 100
MAX_PAGES = build_source.MAX_BRANCH_PAGES          # 3
PAGE_CAP = PER_PAGE * MAX_PAGES                    # 300


def _http_error(status, headers=None, url="https://api.github.com/x"):
    """A urllib HTTPError with the given status and headers."""
    message = email.message.Message()
    for name, value in (headers or {}).items():
        message[name] = value
    return urllib.error.HTTPError(url, status, f"HTTP {status}", message,
                                  io.BytesIO(b"{}"))


# ---------------------------------------------------------------------------
# The scripted upstream: an injected fetch that records every outbound URL
# and plays one generated outcome.
# ---------------------------------------------------------------------------

class FakeGitHub:
    """An injected fetch playing one scripted upstream outcome.

    ``script`` is a dict:
      kind        one of 'success', 'empty', 'not_found', 'forbidden',
                  'rate_limited_403', 'rate_limited_429', 'timeout',
                  'timeout_wrapped', 'server_error', 'malformed'
      fail_at     'meta' or 'branches' (where an HTTP/transport failure
                  strikes)
      names       branch names for 'success'
      default     the default branch name for 'success'/'empty'
      status      the 5xx status for 'server_error'
      malformed   which malformed shape to play
      rl_headers  the rate-limit indication headers for 'rate_limited_403'
    """

    def __init__(self, script):
        self.script = script
        self.urls = []

    def _fail(self, url):
        kind = self.script["kind"]
        if kind == "not_found":
            raise _http_error(404, url=url)
        if kind == "forbidden":
            # 403 with NO rate-limit indication (remaining budget left).
            raise _http_error(
                403, headers={"X-RateLimit-Remaining": "37"}, url=url)
        if kind == "rate_limited_403":
            raise _http_error(403, headers=self.script["rl_headers"],
                              url=url)
        if kind == "rate_limited_429":
            raise _http_error(429, url=url)
        if kind == "timeout":
            raise socket.timeout("timed out")
        if kind == "timeout_wrapped":
            raise urllib.error.URLError(socket.timeout("timed out"))
        if kind == "server_error":
            raise _http_error(self.script["status"], url=url)
        raise AssertionError(f"not a failure kind: {kind}")

    def __call__(self, url):
        self.urls.append(url)
        parts = urlsplit(url)
        is_branches = parts.path.endswith("/branches")
        kind = self.script["kind"]

        failure_kinds = {"not_found", "forbidden", "rate_limited_403",
                         "rate_limited_429", "timeout", "timeout_wrapped",
                         "server_error"}
        if kind in failure_kinds:
            fail_at = self.script.get("fail_at", "meta")
            if fail_at == "meta" and not is_branches:
                self._fail(url)
            if fail_at == "branches" and is_branches:
                self._fail(url)

        if kind == "malformed":
            shape = self.script["malformed"]
            if not is_branches:
                if shape == "meta_not_dict":
                    return ["not", "a", "dict"]
                if shape == "meta_no_default":
                    return {"full_name": "x/y"}
                if shape == "meta_not_json":
                    raise ValueError("Expecting value: line 1 column 1")
            else:
                if shape == "branches_not_list":
                    return {"message": "moved"}
                if shape == "branches_entry_no_name":
                    return [{"commit": {"sha": "abc"}}]

        # The healthy metadata / branch pages.
        if not is_branches:
            return {"default_branch": self.script.get("default", "main")}
        page = int(parse_qs(parts.query)["page"][0])
        per_page = int(parse_qs(parts.query)["per_page"][0])
        names = self.script.get("names", [])
        start = (page - 1) * per_page
        return [{"name": name} for name in names[start:start + per_page]]


# ---------------------------------------------------------------------------
# The upstream-outcome domain (Property 10).
# ---------------------------------------------------------------------------

_EXPECTED_CODE = {
    "not_found": build_source.REPOSITORY_NOT_FOUND,
    "forbidden": build_source.REPOSITORY_FORBIDDEN,
    "rate_limited_403": build_source.DISCOVERY_RATE_LIMITED,
    "rate_limited_429": build_source.DISCOVERY_RATE_LIMITED,
    "timeout": build_source.DISCOVERY_TIMEOUT,
    "timeout_wrapped": build_source.DISCOVERY_TIMEOUT,
    "server_error": build_source.DISCOVERY_UPSTREAM_ERROR,
    "malformed": build_source.DISCOVERY_UPSTREAM_ERROR,
    "empty": build_source.REPOSITORY_EMPTY,
}


@st.composite
def upstream_outcomes(draw):
    """One scripted upstream outcome covering the whole Property 10 domain.

    Branch counts are drawn around the pagination boundaries (page edges
    and the cap) as well as small values, so the ``truncated`` flag and
    the page walk are both exercised without generating thousands of
    names.
    """
    kind = draw(st.sampled_from([
        "success", "empty", "not_found", "forbidden", "rate_limited_403",
        "rate_limited_429", "timeout", "timeout_wrapped", "server_error",
        "malformed",
    ]))
    script = {"kind": kind}
    if kind in ("not_found", "forbidden", "rate_limited_403",
                "rate_limited_429", "timeout", "timeout_wrapped",
                "server_error"):
        script["fail_at"] = draw(st.sampled_from(["meta", "branches"]))
    if kind == "rate_limited_403":
        script["rl_headers"] = draw(st.sampled_from([
            {"X-RateLimit-Remaining": "0"},
            {"Retry-After": "60"},
            {"X-RateLimit-Remaining": "0", "Retry-After": "60"},
        ]))
    if kind == "server_error":
        script["status"] = draw(st.sampled_from([500, 502, 503, 504, 599]))
    if kind == "malformed":
        script["malformed"] = draw(st.sampled_from([
            "meta_not_dict", "meta_no_default", "meta_not_json",
            "branches_not_list", "branches_entry_no_name",
        ]))
    if kind in ("success", "empty"):
        script["default"] = draw(st.sampled_from(
            ["main", "master", "develop", "release/2.0"]))
    if kind == "success":
        count = draw(st.one_of(
            st.integers(min_value=1, max_value=8),
            st.sampled_from([PER_PAGE - 1, PER_PAGE, PER_PAGE + 1,
                             PAGE_CAP - 1, PAGE_CAP, PAGE_CAP + 25]),
        ))
        names = [f"branch-{i}" for i in range(count)]
        # The default branch is either one of the listed names or (the
        # truncated-out case) absent from the listing entirely.
        default_listed = draw(st.booleans())
        if default_listed:
            names[draw(st.integers(0, count - 1))] = script["default"]
        script["names"] = names
        script["default_listed"] = default_listed
    return script


class TestProperty10DiscoveryResultClassification:
    """**Validates: Requirements 3.1, 3.2, 3.3, 3.5**"""

    @settings(max_examples=250, deadline=None)
    @given(script=upstream_outcomes())
    def test_every_upstream_outcome_is_distinctly_classified(self, script):
        fake = FakeGitHub(script)
        result = build_source.discover_branches(DDA_URL, fetch=fake)

        # Containment (Req 3.5): every recorded outbound URL is built from
        # the parsed <owner>/<repo> against the fixed API host.
        owner, repo = build_source.parse_owner_repo(DDA_URL)
        prefix = f"{API_PREFIX}{owner}/{repo}"
        assert fake.urls, "discovery made no outbound call"
        for url in fake.urls:
            assert url.startswith(prefix), url
            assert url[len(prefix):len(prefix) + 1] in ("", "/", "?"), url
            # No credentials in any URL (Req 3.2).
            assert "@" not in urlsplit(url).netloc

        kind = script["kind"]
        if kind == "success":
            # Success: the branch list, exactly one default branch —
            # present in the list — and the truncation flag (Req 3.1).
            assert "error" not in result, result
            assert result["default_branch"] == script["default"]
            assert result["branches"]
            assert result["default_branch"] in result["branches"]
            assert result["branches"].count(script["default"]) == 1
            listed = set(script["names"]) | {script["default"]}
            assert set(result["branches"]) <= listed
            # A listing that filled every allowed page is flagged
            # truncated — at exactly the cap the walk cannot know whether
            # more branches exist, so it must not promise completeness.
            expected_truncated = len(script["names"]) >= PAGE_CAP
            assert result["truncated"] is expected_truncated
            if not expected_truncated and script["default_listed"]:
                assert result["branches"] == script["names"]
            return

        # Failure: the distinct code for this condition (Req 3.3), and
        # never a success shape with an empty list.
        assert "branches" not in result, result
        error = result["error"]
        assert error["code"] == _EXPECTED_CODE[kind], (kind, error)
        assert error["message"]
        # Actionable: the failure names the repository it is about.
        assert DDA_URL in error["message"]

    @settings(max_examples=100, deadline=None)
    @given(script=upstream_outcomes())
    def test_result_shape_is_exactly_one_of_success_or_error(self, script):
        """No outcome yields both shapes, neither shape, or an empty
        success — the "empty list presented as success" bug class is
        structurally impossible (Req 3.3)."""
        result = build_source.discover_branches(
            DDA_URL, fetch=FakeGitHub(script))
        is_success = "branches" in result
        is_error = "error" in result
        assert is_success != is_error
        if is_success:
            assert len(result["branches"]) >= 1
            assert set(result) == {"branches", "default_branch",
                                   "truncated"}
        else:
            assert set(result["error"]) == {"code", "message"}


# ---------------------------------------------------------------------------
# Example cases — one per error code, the success shape, and the page cap
# (the design's named unit cases for discover_branches).
# ---------------------------------------------------------------------------

class TestDiscoveryExamples:
    """**Validates: Requirements 3.1, 3.2, 3.3, 3.5**"""

    def test_success_flags_the_default_branch(self):
        fake = FakeGitHub({"kind": "success", "default": "main",
                           "names": ["dev", "main", "release/2.0"],
                           "default_listed": True})
        result = build_source.discover_branches(DDA_URL, fetch=fake)
        assert result == {"branches": ["dev", "main", "release/2.0"],
                          "default_branch": "main", "truncated": False}

    def test_truncation_at_the_page_cap(self):
        names = [f"branch-{i}" for i in range(PAGE_CAP + 1)]
        fake = FakeGitHub({"kind": "success", "default": "branch-0",
                           "names": names, "default_listed": True})
        result = build_source.discover_branches(DDA_URL, fetch=fake)
        assert result["truncated"] is True
        assert len(result["branches"]) == PAGE_CAP
        # The page walk stopped at the cap: 1 metadata + 3 branch pages.
        assert len(fake.urls) == 1 + MAX_PAGES

    def test_exactly_full_listing_is_flagged_truncated_not_lied_about(self):
        """A repository with exactly page-cap branches: the walk cannot
        know whether more exist, so ``truncated`` is flagged rather than
        promising completeness."""
        names = [f"branch-{i}" for i in range(PAGE_CAP)]
        fake = FakeGitHub({"kind": "success", "default": "branch-0",
                           "names": names, "default_listed": True})
        result = build_source.discover_branches(DDA_URL, fetch=fake)
        assert result["branches"] == names
        assert result["truncated"] is True

    def test_a_truncated_out_default_branch_is_still_identified(self):
        names = [f"zz-{i}" for i in range(PAGE_CAP + 10)]
        fake = FakeGitHub({"kind": "success", "default": "main",
                           "names": names, "default_listed": False})
        result = build_source.discover_branches(DDA_URL, fetch=fake)
        assert result["default_branch"] == "main"
        assert result["branches"].count("main") == 1

    def test_404_is_repository_not_found(self):
        result = build_source.discover_branches(
            DDA_URL, fetch=FakeGitHub({"kind": "not_found",
                                       "fail_at": "meta"}))
        assert result["error"]["code"] == build_source.REPOSITORY_NOT_FOUND

    def test_plain_403_is_repository_forbidden(self):
        result = build_source.discover_branches(
            DDA_URL, fetch=FakeGitHub({"kind": "forbidden",
                                       "fail_at": "meta"}))
        assert result["error"]["code"] == build_source.REPOSITORY_FORBIDDEN

    def test_rate_limited_403_is_discovery_rate_limited(self):
        result = build_source.discover_branches(
            DDA_URL, fetch=FakeGitHub({
                "kind": "rate_limited_403", "fail_at": "meta",
                "rl_headers": {"X-RateLimit-Remaining": "0"}}))
        assert result["error"]["code"] == \
            build_source.DISCOVERY_RATE_LIMITED

    def test_429_is_discovery_rate_limited(self):
        result = build_source.discover_branches(
            DDA_URL, fetch=FakeGitHub({"kind": "rate_limited_429",
                                       "fail_at": "branches"}))
        assert result["error"]["code"] == \
            build_source.DISCOVERY_RATE_LIMITED

    def test_timeout_is_discovery_timeout(self):
        result = build_source.discover_branches(
            DDA_URL, fetch=FakeGitHub({"kind": "timeout",
                                       "fail_at": "meta"}))
        assert result["error"]["code"] == build_source.DISCOVERY_TIMEOUT

    def test_5xx_is_discovery_upstream_error(self):
        result = build_source.discover_branches(
            DDA_URL, fetch=FakeGitHub({"kind": "server_error",
                                       "fail_at": "branches",
                                       "status": 502}))
        assert result["error"]["code"] == \
            build_source.DISCOVERY_UPSTREAM_ERROR

    def test_malformed_payload_is_discovery_upstream_error(self):
        result = build_source.discover_branches(
            DDA_URL, fetch=FakeGitHub({"kind": "malformed",
                                       "malformed": "meta_no_default"}))
        assert result["error"]["code"] == \
            build_source.DISCOVERY_UPSTREAM_ERROR

    def test_no_branches_is_repository_empty_not_an_empty_success(self):
        result = build_source.discover_branches(
            DDA_URL, fetch=FakeGitHub({"kind": "empty",
                                       "default": "main", "names": []}))
        assert "branches" not in result
        assert result["error"]["code"] == build_source.REPOSITORY_EMPTY

    def test_github_409_empty_repository_is_repository_empty(self):
        """GitHub answers 409 ("Git Repository is empty") on git-data
        endpoints of a commit-less repository: reachable, no branches."""
        def fetch(url):
            if url.rstrip("/").endswith(("awslabs/DefectDetectionApplication",)):
                return {"default_branch": "main"}
            raise _http_error(409, url=url)
        result = build_source.discover_branches(DDA_URL, fetch=fetch)
        assert result["error"]["code"] == build_source.REPOSITORY_EMPTY

    def test_raw_input_never_reaches_the_wire(self):
        """Req 3.5: a non-normalized value raises before ANY outbound
        call is composed."""
        fake = FakeGitHub({"kind": "success", "default": "main",
                           "names": ["main"], "default_listed": True})
        with pytest.raises(ValueError):
            build_source.discover_branches(
                "https://evil.com/owner/repo", fetch=fake)
        assert fake.urls == []

    def test_the_default_fetch_sends_no_credentials(self, monkeypatch):
        """Req 3.2: the default transport composes a request carrying no
        Authorization header and the 5-second timeout (captured by a
        stubbed opener — nothing is sent)."""
        captured = {}

        class _Response:
            def read(self):
                return b'{"default_branch": "main"}'

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        def fake_urlopen(request, timeout=None):
            captured["request"] = request
            captured["timeout"] = timeout
            return _Response()

        monkeypatch.setattr(build_source.urllib.request, "urlopen",
                            fake_urlopen)
        payload = build_source._default_github_fetch(
            "https://api.github.com/repos/awslabs/DefectDetectionApplication")
        assert payload == {"default_branch": "main"}
        request = captured["request"]
        assert not request.has_header("Authorization")
        assert captured["timeout"] == 5
        assert build_source.DISCOVERY_TIMEOUT_SECONDS == 5
