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
``backend-jammy-retired-packages`` (task 1) — **Property 1: Bug Condition —
No Jammy-Unresolvable Package in Any AMD64-Reachable Apt Step**.

These tests assert the FIXED predicate from the design's fix-checking
pseudocode: no jammy-retired package token (``RETIRED_JAMMY_PACKAGES =
{"libssl1.1"}``) in any 22.04-reachable apt install step of
``src/backend/Dockerfile`` or its three invoked install scripts, and the
libssl install step exactly the ``/etc/os-release``-gated conditional with
allowlist {"18.04", "20.04"} and body ``apt-get install libssl1.1 -y``.

On the UNFIXED tree cases 1, 2, and 6 MUST FAIL — failure confirms bug
condition C(X) and surfaces the counterexample: line 70's unconditional
``RUN apt-get install libssl1.1 -y`` (target ``backend_generic``, Docker
build step 24/63, between the ``apt update -y`` lines 69/71), matching the
live evidence: portal build job ``3d18ba88-9c17-490a-811b-8c21360216f4``
(AMD64, dedicated X86 server, source_ref ``feature/workflow-triggers``,
commit ``4e1ce8c``, 2026-08-09) logged ``E: Unable to locate package
libssl1.1``, apt exit code 100, settled ``BUILD_FAILED`` at ~21m51s.

The scoping check (case 3), class-closure verdict inventory (case 4),
ARG-scoping structural pin (case 5), and frontend sanity (case 7) MUST PASS
on the unfixed tree.

These are NOT throwaway inverted tests: they encode the expected behavior
and are re-run unchanged as the fix check in task 5.1.

Counterexamples found on the unfixed tree are recorded in
``.kiro/specs/backend-jammy-retired-packages/exploration-notes.md``.

All parsing is TEXT only — no ``docker``, ``subprocess``, or shell-out
anywhere in this package. Textual anchors use content matching, never line
numbers (the fix shifts later line numbers).
"""
from _jammy_support import (
    BACKEND_DOCKERFILE_REL,
    BUG_SITE_TOKEN,
    FIXED_LIBSSL_ALLOWLIST,
    FIXED_LIBSSL_BODY,
    FRONTEND_DOCKERFILE_REL,
    JAMMY_RESOLVABLE_INVENTORY,
    RETIRED_JAMMY_PACKAGES,
    UNFIXED_BUG_STEP,
    amd64_build_path_apt_steps,
    arg_declarations,
    dockerfile_apt_steps,
    format_sites,
    is_reachable,
    libssl_install_step,
    normalized_apt_bodies,
    read_repo_file,
    retired_reachable_sites,
)

LIVE_EVIDENCE = (
    "live evidence: portal build job 3d18ba88-9c17-490a-811b-8c21360216f4, "
    "backend Docker step 24/63, apt exit 100 "
    "('E: Unable to locate package libssl1.1')"
)


class TestNoRetiredTokensInReachableAptSteps:
    """Exploration case 1: no jammy-retired package token in any
    AMD64-reachable (22.04-reachable) apt install step of
    ``src/backend/Dockerfile`` + the three install scripts.

    EXPECTED ON UNFIXED TREE: FAIL — counterexample: line 70's unconditional
    ``libssl1.1`` request ({LIVE_EVIDENCE}).
    """

    def test_no_jammy_retired_tokens_in_amd64_reachable_apt_steps(self):
        """**Validates: Requirements 1.1, 1.3, 1.4, 2.1, 2.4**"""
        sites = retired_reachable_sites(base="22.04")
        assert not sites, (
            f"bug condition C(X) present — the docker-compose AMD64 build "
            f"path requests jammy-retired package(s) with no installation "
            f"candidate on the Ubuntu 22.04 base in a 22.04-reachable apt "
            f"step ({LIVE_EVIDENCE}). Counterexample sites:\n"
            f"{format_sites(sites)}"
        )


class TestLibsslStepFixedForm:
    """Exploration case 2: the libssl install step of
    ``src/backend/Dockerfile`` is the exact ``/etc/os-release``-gated
    conditional — allowlist exactly {"18.04", "20.04"}, apt body exactly
    ``apt-get install libssl1.1 -y``.

    EXPECTED ON UNFIXED TREE: FAIL — the unfixed step is the unconditional
    ``RUN apt-get install libssl1.1 -y`` (line 70).
    """

    def test_libssl_step_is_os_release_gated_conditional(self):
        """**Validates: Requirements 2.1, 2.2, 2.3**

        The gate reads the base image's ground truth
        (``. /etc/os-release`` + ``"$VERSION_ID"`` comparisons), NOT the
        ``$OS`` build-arg, which is out of scope in every RUN (``ARG OS``
        declared only before FROM — design Decision 1)."""
        step = libssl_install_step()
        assert step is not None, (
            f"{BACKEND_DOCKERFILE_REL}: no apt install step requests the "
            f"{BUG_SITE_TOKEN!r} token at all — the root cause analysis is "
            f"refuted (line already changed?); re-hypothesize before fixing."
        )
        assert step.allowlist == FIXED_LIBSSL_ALLOWLIST, (
            f"bug condition C(X) present — "
            f"{BACKEND_DOCKERFILE_REL}:{step.lineno}: the libssl install "
            f"step is not the /etc/os-release-gated conditional with "
            f"allowlist {sorted(FIXED_LIBSSL_ALLOWLIST)}; found guard "
            f"allowlist {step.allowlist and sorted(step.allowlist)} in "
            f"step:\n  {step.text.strip()!r}\n({LIVE_EVIDENCE})."
        )
        assert ". /etc/os-release" in step.text, (
            f"{BACKEND_DOCKERFILE_REL}:{step.lineno}: the libssl step's "
            f"guard does not source /etc/os-release — the fixed form reads "
            f"the base's ground truth, not the out-of-scope $OS arg. Step: "
            f"{step.text.strip()!r}"
        )
        apt_bodies = normalized_apt_bodies(step.text.lstrip()[len("RUN "):])
        assert apt_bodies == [FIXED_LIBSSL_BODY], (
            f"{BACKEND_DOCKERFILE_REL}:{step.lineno}: the libssl step's apt "
            f"body is {apt_bodies!r}, expected exactly "
            f"[{FIXED_LIBSSL_BODY!r}] (identical old-base behavior, "
            f"Req 3.2). Step: {step.text.strip()!r}"
        )


class TestCounterexampleInventoryScoping:
    """Exploration case 3: counterexample inventory scoping — the
    retired-token scan over the tree finds AT MOST one 22.04-reachable
    site, and any site found is exactly the line-70 step
    (``RUN apt-get install libssl1.1 -y``, content-matched).

    Confirms the bugfix.md full-path scan (single-step fix scope). If
    additional retired-package sites existed, the root cause analysis would
    be incomplete and we would re-hypothesize before fixing.

    EXPECTED: PASS pre-fix (exactly one site: the line-70 step) AND
    post-fix (zero reachable sites — the design's post-fix meaning).
    """

    def test_retired_scan_finds_at_most_the_line_70_site(self):
        """**Validates: Requirements 1.1, 1.2, 1.4, 2.1**"""
        sites = retired_reachable_sites(base="22.04")
        assert len(sites) <= 1, (
            f"the retired-token scan found MORE than the single site "
            f"identified by bugfix.md — the single-step fix scope is wrong; "
            f"re-hypothesize before fixing. Sites:\n{format_sites(sites)}"
        )
        for step, token in sites:
            assert (
                step.path == BACKEND_DOCKERFILE_REL
                and token == BUG_SITE_TOKEN
                and step.text.strip() == UNFIXED_BUG_STEP
            ), (
                f"the retired-token scan found a site OTHER than the "
                f"line-70 step ({UNFIXED_BUG_STEP!r} in "
                f"{BACKEND_DOCKERFILE_REL}) — the root cause analysis is "
                f"incomplete; re-hypothesize before fixing. Found: "
                f"{step.path}:{step.lineno}: token {token!r} in step: "
                f"{step.text.strip()}"
            )


class TestClassClosureVerdictInventory:
    """Exploration case 4 (¬C class closure): every package token requested
    by an AMD64-reachable apt step in the Dockerfile and the three install
    scripts is a member of the design-verified jammy-resolvable inventory
    (the Decision 2 index-verification table plus the bugfix.md scan's
    live-green sites). Any unknown token fails until vetted — future
    package additions must be re-vetted against the jammy index.

    Retired-set members are excluded here: case 1 already isolates them.

    EXPECTED: PASS pre-fix (line 70's token is in the retired set, not
    unknown) AND post-fix (completely — the guarded step is no longer
    22.04-reachable).
    """

    def test_every_reachable_apt_token_is_jammy_vetted(self):
        """**Validates: Requirements 1.4, 2.4**"""
        unknown = []
        for step in amd64_build_path_apt_steps():
            if not is_reachable(step, "22.04"):
                continue
            for pkg in step.packages:
                if (
                    pkg not in JAMMY_RESOLVABLE_INVENTORY
                    and pkg not in RETIRED_JAMMY_PACKAGES
                ):
                    unknown.append((step, pkg))
        assert not unknown, (
            f"class-closure violation — AMD64-reachable apt step(s) request "
            f"package token(s) NOT in the design-verified jammy-resolvable "
            f"inventory (Decision 2 table + bugfix.md scan). Vet each "
            f"against the jammy index before adding it to the inventory:\n"
            f"{format_sites(unknown)}"
        )


class TestArgScopingStructuralPin:
    """Exploration case 5 (¬C structural pin): ``ARG OS`` appears only
    BEFORE ``FROM`` in ``src/backend/Dockerfile``, and no ``ARG OS``
    re-declaration exists after ``FROM`` — the structural fact that forces
    the ``/etc/os-release`` gate (``$OS`` is out of scope in every RUN) and
    keeps the dormant lines-73-75 conditional inert (re-declaring would
    activate it on JP4 and attempt the typo'd ``libavcodec-extra57i``
    install — design Decision 1).

    EXPECTED: PASS pre-fix AND post-fix.
    """

    def test_arg_os_only_before_from_no_redeclaration(self):
        """**Validates: Requirements 2.2, 2.4** (Decision 1's structural basis)"""
        text = read_repo_file(BACKEND_DOCKERFILE_REL)
        os_decls = [d for d in arg_declarations(text) if d[1] == "OS"]
        assert os_decls, (
            f"{BACKEND_DOCKERFILE_REL}: no 'ARG OS' declaration found — the "
            f"ARG-scoping analysis behind Decision 1 is refuted; "
            f"re-hypothesize before fixing."
        )
        after_from = [
            (lineno, name)
            for lineno, name, before in os_decls
            if not before
        ]
        assert not after_from, (
            f"{BACKEND_DOCKERFILE_REL}: 'ARG OS' is re-declared after FROM "
            f"at {after_from} — $OS would be IN scope in RUN, activating "
            f"the dormant lines-73-75 branch (typo'd libavcodec-extra57i on "
            f"JP4). The Decision 1 analysis is refuted; re-hypothesize "
            f"before fixing."
        )


class TestOldBaseAllowlistReachability:
    """Exploration case 6: old-base allowlist reachability — under the
    reachability model, the libssl install step is reachable when the base
    is 18.04 or 20.04 (old-base behavior preserved, Req 3.2's contract) and
    UNreachable when the base is 22.04 (jammy skips the retired request).

    EXPECTED ON UNFIXED TREE: FAIL — the unconditional step is reachable on
    all three bases, including 22.04 (where it dies with apt exit 100).
    """

    def test_libssl_step_reachable_old_bases_unreachable_jammy(self):
        """**Validates: Requirements 1.1, 2.1, 2.3**"""
        step = libssl_install_step()
        assert step is not None, (
            f"{BACKEND_DOCKERFILE_REL}: no apt install step requests the "
            f"{BUG_SITE_TOKEN!r} token at all — the root cause analysis is "
            f"refuted; re-hypothesize before fixing."
        )
        for old_base in ("18.04", "20.04"):
            assert is_reachable(step, old_base), (
                f"{BACKEND_DOCKERFILE_REL}:{step.lineno}: the libssl "
                f"install step is NOT reachable on the {old_base} base — "
                f"old-base behavior would be broken (Req 3.2). Step: "
                f"{step.text.strip()!r}"
            )
        assert not is_reachable(step, "22.04"), (
            f"bug condition C(X) present — "
            f"{BACKEND_DOCKERFILE_REL}:{step.lineno}: the libssl install "
            f"step IS reachable when the base is Ubuntu 22.04 (jammy), "
            f"where {BUG_SITE_TOKEN!r} has no installation candidate "
            f"({LIVE_EVIDENCE}). Step: {step.text.strip()!r}"
        )


class TestFrontendZeroAptSanity:
    """Exploration case 7 (¬C scan boundary): ``src/frontend/Dockerfile``
    contains zero apt install steps (node:18-alpine build stage +
    nginx:stable-alpine runtime, npm only) — anchoring the Req 2.4 scan
    boundary. The react-webapp image exported successfully in live build
    3d18ba88, as expected.

    EXPECTED: PASS pre-fix AND post-fix.
    """

    def test_frontend_dockerfile_has_zero_apt_install_steps(self):
        """**Validates: Requirements 2.4** (scan boundary anchor)"""
        steps = dockerfile_apt_steps(FRONTEND_DOCKERFILE_REL)
        assert not steps, (
            f"{FRONTEND_DOCKERFILE_REL}: expected ZERO apt install steps "
            f"(alpine/npm only), found:\n"
            + "\n".join(
                f"  {s.path}:{s.lineno}: {s.text.strip()}" for s in steps
            )
            + "\nThe Req 2.4 scan boundary is refuted — re-scan the "
            f"frontend build path before fixing."
        )
