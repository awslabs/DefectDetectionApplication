# Copyright 2026 Amazon Web Services, Inc.
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
"""Bug-condition exploration static tests for
``edgemlsdk-python-dev-ubuntu2204`` (task 1) — **Property 1: Bug Condition —
Line 286 Resolves on the 22.04 Base**.

These tests assert the FIXED predicate from the design's fix-checking
pseudocode: no retired transitional Python package token (``python-dev``,
``python``, ``python-pip``, ``python-setuptools``) in any apt install step of
``src/edgemlsdk/Dockerfile``, and the Triton-section single-package install
step exactly ``RUN apt-get install python-dev-is-python3 -y``.

On the UNFIXED tree cases 1 and 2 MUST FAIL — failure confirms bug condition
C(X) and surfaces the counterexample: line 286's ``python-dev`` token
(``RUN apt-get install python-dev -y``, Docker build step 61/83, immediately
after the ``rapidjson-dev libre2-dev`` step), matching the live evidence:
portal build job ``08a1e2bd-45f9-4521-ac4a-b41b52222e2e`` (AMD64, dedicated,
source_ref ``feature/workflow-triggers``, 2026-08-09) logged
``E: Package 'python-dev' has no installation candidate ... replaced by:
python2-dev python2 python-dev-is-python3``, apt exit code 100, settled
``BUILD_FAILED``.

The scoping check (case 3) and the ¬C cases (JP6 anchor, JP5 out-of-scope,
downstream rm-precondition — cases 4-6) MUST PASS on the unfixed tree.

These are NOT throwaway inverted tests: they encode the expected behavior and
are re-run unchanged as the fix check in task 5.1.

Counterexamples found on the unfixed tree are recorded in
``.kiro/specs/edgemlsdk-python-dev-ubuntu2204/exploration-notes.md``.

All parsing is TEXT only — no ``docker``, ``subprocess``, or shell-out
anywhere in this package.
"""
from _pythondev_support import (
    BUG_SITE_LINE,
    BUG_SITE_TOKEN,
    DOCKERFILE_JP5_REL,
    DOCKERFILE_JP6_REL,
    DOCKERFILE_REL,
    FIXED_TRITON_STEP,
    find_step_with_packages,
    format_sites,
    read_repo_file,
    retired_token_sites,
    rm_usr_bin_python_command,
    triton_python_install_step,
)


class TestNoRetiredTokensInAptInstallSteps:
    """Exploration case 1: no retired transitional Python package token in
    any apt install step of ``src/edgemlsdk/Dockerfile``.

    EXPECTED ON UNFIXED TREE: FAIL — counterexample: line 286's
    ``python-dev`` (apt exit 100 on the 22.04 base, job 08a1e2bd step 61/83).
    """

    def test_no_retired_transitional_python_package_tokens(self):
        """**Validates: Requirements 1.1, 1.3, 2.1**"""
        text = read_repo_file(DOCKERFILE_REL)
        sites = retired_token_sites(text)
        assert not sites, (
            f"bug condition C(X) present — {DOCKERFILE_REL} requests retired "
            f"transitional Python package(s) with no installation candidate "
            f"on the Ubuntu 22.04 base (apt exit 100; live evidence: portal "
            f"build job 08a1e2bd-45f9-4521-ac4a-b41b52222e2e, Docker step "
            f"61/83). Counterexample sites:\n{format_sites(DOCKERFILE_REL, sites)}"
        )


class TestTritonSectionFixedLine:
    """Exploration case 2: the Triton-section single-package install step
    (the RUN immediately after the ``rapidjson-dev libre2-dev`` step) is
    exactly ``RUN apt-get install python-dev-is-python3 -y``.

    EXPECTED ON UNFIXED TREE: FAIL — the unfixed step is
    ``RUN apt-get install python-dev -y`` (line 286).
    """

    def test_triton_python_step_is_exact_fixed_form(self):
        """**Validates: Requirements 1.1, 2.1, 2.2, 2.3**

        ``python-dev-is-python3`` is apt's own named replacement: Depends
        ``python3-dev`` (the JP6 anchor's dev-headers package) and
        ``python-is-python3`` (provides ``/usr/bin/python``, preserving the
        downstream ``rm /usr/bin/python`` (no ``-f``) precondition)."""
        text = read_repo_file(DOCKERFILE_REL)
        lineno, run_text = triton_python_install_step(text)
        assert run_text.strip() == FIXED_TRITON_STEP, (
            f"bug condition C(X) present — {DOCKERFILE_REL}:{lineno}: the "
            f"Triton-section single-package install step is\n"
            f"  {run_text.strip()!r}\nexpected exactly\n"
            f"  {FIXED_TRITON_STEP!r}\n"
            f"(live evidence: apt exit 100 at Docker step 61/83 in portal "
            f"build job 08a1e2bd-45f9-4521-ac4a-b41b52222e2e)."
        )


class TestCounterexampleInventoryScoping:
    """Exploration case 3: counterexample inventory scoping — the
    retired-token scan over ``src/edgemlsdk/Dockerfile`` finds AT MOST one
    site, and any site found is exactly line 286's ``python-dev``.

    Confirms the bugfix.md repo scan (single-line fix scope). If additional
    retired-package sites existed, the root cause analysis would be
    incomplete and we would re-hypothesize before fixing.

    EXPECTED: PASS pre-fix (exactly one site: line 286) AND post-fix (zero
    sites — the design's inverted meaning).
    """

    def test_retired_scan_finds_at_most_the_line_286_site(self):
        """**Validates: Requirements 1.1, 1.2, 2.1**"""
        text = read_repo_file(DOCKERFILE_REL)
        sites = retired_token_sites(text)
        assert len(sites) <= 1, (
            f"the retired-token scan found MORE than the single site "
            f"identified by bugfix.md — the single-line fix scope is wrong; "
            f"re-hypothesize before fixing. Sites:\n"
            f"{format_sites(DOCKERFILE_REL, sites)}"
        )
        for lineno, token, run_text in sites:
            assert (lineno, token) == (BUG_SITE_LINE, BUG_SITE_TOKEN), (
                f"the retired-token scan found a site OTHER than line "
                f"{BUG_SITE_LINE}'s {BUG_SITE_TOKEN!r} — the root cause "
                f"analysis is incomplete; re-hypothesize before fixing. "
                f"Found: {DOCKERFILE_REL}:{lineno}: token {token!r} in step: "
                f"{run_text.strip()}"
            )


class TestJp6Anchor:
    """Exploration case 4 (¬C anchor): ``Dockerfile.jp6``'s system-packages
    block requests ``python3-dev`` (token-boundary) and the file has no
    retired token in any apt install step — anchoring the target dev-headers
    package for this dependency group on a 22.04-generation base.

    EXPECTED: PASS on the unfixed tree (and after the fix).
    """

    def test_jp6_system_packages_block_requests_python3_dev(self):
        """**Validates: Requirements 2.2, 3.3** (target-package anchor)"""
        text = read_repo_file(DOCKERFILE_JP6_REL)
        _, found = find_step_with_packages(
            text, ("rapidjson-dev", "libre2-dev", "python3-dev")
        )
        assert found is not None, (
            f"{DOCKERFILE_JP6_REL}: system-packages block does not request "
            f"'python3-dev' alongside 'rapidjson-dev libre2-dev' — the JP6 "
            f"anchor is refuted; re-hypothesize before fixing."
        )
        sites = retired_token_sites(text)
        assert not sites, (
            f"{DOCKERFILE_JP6_REL}: retired transitional Python package "
            f"token(s) found in the JP6 anchor file:\n"
            f"{format_sites(DOCKERFILE_JP6_REL, sites)}"
        )


class TestJp5OutOfScope:
    """Exploration case 5 (¬C out-of-scope): ``Dockerfile.jp5`` contains the
    ``python-dev`` token in its single system-packages block. Its base is
    digest-pinned to ``l4t-jetpack:r35.4.1`` (focal), where the token still
    resolves — design Decision 2 leaves it untouched. This test asserts
    PRESENCE, guarding against accidental modification.

    EXPECTED: PASS pre-fix AND post-fix.
    """

    def test_jp5_system_packages_block_contains_python_dev(self):
        """**Validates: Requirements 3.2** (Decision 2's untouched scope)"""
        text = read_repo_file(DOCKERFILE_JP5_REL)
        sites = retired_token_sites(text)
        assert len(sites) == 1, (
            f"{DOCKERFILE_JP5_REL}: expected exactly ONE retired-token site "
            f"(the 'python-dev' token in the single system-packages block, "
            f"resolving on the pinned focal base — design Decision 2), found "
            f"{len(sites)}:\n{format_sites(DOCKERFILE_JP5_REL, sites)}\n"
            f"The JP5 file must remain byte-for-byte untouched by this fix."
        )
        lineno, token, run_text = sites[0]
        assert token == "python-dev", (
            f"{DOCKERFILE_JP5_REL}:{lineno}: expected the 'python-dev' "
            f"token, found {token!r}"
        )
        pkgs = run_text.split()
        assert "rapidjson-dev" in pkgs and "libre2-dev" in pkgs, (
            f"{DOCKERFILE_JP5_REL}:{lineno}: the retired token is not inside "
            f"the system-packages block (the step also installing "
            f"rapidjson-dev and libre2-dev). Step: {run_text.strip()}"
        )


class TestDownstreamRmPrecondition:
    """Exploration case 6 (¬C structural precondition): the
    ``rm /usr/bin/python`` step exists in ``src/edgemlsdk/Dockerfile``, uses
    no ``-f``, and follows the Triton-section install site (line 286) — the
    structural fact forcing design Decision 1's ``python-dev-is-python3``
    choice (options (a) drop and (c) bare ``python3-dev`` would relocate the
    failure to this later, preserved-byte-for-byte step, because ``rm``
    without ``-f`` exits 1 when ``/usr/bin/python`` does not exist).

    EXPECTED: PASS pre-fix AND post-fix.
    """

    def test_rm_usr_bin_python_no_dash_f_follows_the_install_site(self):
        """**Validates: Requirements 2.2, 3.1**"""
        text = read_repo_file(DOCKERFILE_REL)
        found = rm_usr_bin_python_command(text)
        assert found is not None, (
            f"{DOCKERFILE_REL}: no 'rm /usr/bin/python' step found — the "
            f"downstream-precondition analysis behind Decision 1 is refuted; "
            f"re-hypothesize before fixing."
        )
        rm_lineno, rm_tokens = found
        flags = [t for t in rm_tokens[1:] if t.startswith("-")]
        assert not flags, (
            f"{DOCKERFILE_REL}:{rm_lineno}: the 'rm /usr/bin/python' command "
            f"uses flag(s) {flags} — Decision 1 assumed no '-f'; "
            f"re-hypothesize (a bare 'python3-dev' fix may become viable)."
        )
        triton_lineno, _ = triton_python_install_step(text)
        assert rm_lineno > triton_lineno, (
            f"{DOCKERFILE_REL}: the 'rm /usr/bin/python' step (line "
            f"{rm_lineno}) does not follow the Triton-section install site "
            f"(line {triton_lineno}) — the ordering assumption behind "
            f"Decision 1 is refuted; re-hypothesize before fixing."
        )
