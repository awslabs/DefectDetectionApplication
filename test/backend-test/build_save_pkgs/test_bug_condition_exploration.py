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
``build-docker-save-stdout-failure`` (task 1) — **Property 1: Bug Condition —
Both Save Sites Use the Output+Rename Form with an Integrity Guard**.

These tests assert the FIXED predicate from the design's fix-checking
pseudocode: zero ``docker save <image> > <file>`` stdout-redirect invocation
sites anywhere in ``build-custom.sh``; exactly two image-save call sites
(flask-app, react-webapp), both invoking the shared ``save_image_tar``
helper with final destinations exactly the two ``ZIP_MEMBERS`` tar paths;
and the helper containing, in order, ``docker save --output`` to a
``"$dest.partial"`` destination, an atomic ``mv`` to the final name, a size
guard with threshold 1048576, a ``tar -tf`` structural check, and non-zero
exits with diagnostics on both failure paths.

On the UNFIXED tree cases 1, 2 and 3 MUST FAIL — failure confirms bug
condition C(X) and surfaces the counterexamples: the two stdout-redirect
saves between the ``echo "save docker images as tarvballs"`` log anchor and
the compose-``cp`` staging line, matching the live evidence: portal build
job ``d844a5fb-81d5-4294-956d-d6d6ae1f000e`` (AMD64, dedicated X86 server,
source_ref ``feature/workflow-triggers``, commit ``ab900d9``, settled
``failed`` 2026-08-09 at ~9m32s) logged ``save docker images as tarvballs``
then ``write /dev/stdout: bad file descriptor`` (EBADF); the 0-byte
``flask-app.tar`` was confirmed by read-only SSM inspection (29 GB free
disk, flask-app image 4.78 GB — disk was not the cause).

The scoping check (case 4) and the ¬C cases (class boundary over the four
neighbor scripts, temp-hazard guard coexistence, log-anchor preservation —
cases 5-7) MUST PASS on the unfixed tree.

These are NOT throwaway inverted tests: they encode the expected behavior
and are re-run unchanged as the fix check in task 5.1.

Counterexamples found on the unfixed tree are recorded in
``.kiro/specs/build-docker-save-stdout-failure/exploration-notes.md``.

All parsing is TEXT only (comment/string-aware tokenization — the unfixed
comment block's literal ``docker save --output`` text never classifies as an
invocation site) — no ``docker``, ``subprocess``, or shell-out anywhere in
this package. Textual anchors are content matches, never line numbers,
except the deliberate structural pins on lines 2-3, which precede the fix
region and cannot shift.
"""
from _save_support import (
    BUILD_CUSTOM_REL,
    EXPECTED_UNFIXED_REDIRECT_SITES,
    FLASK_IMAGE,
    FLASK_TAR,
    HELPER_NAME,
    LOG_ANCHOR_LINE,
    NEIGHBOR_SCRIPT_RELS,
    OUTPUT_PARTIAL_MV,
    REACT_IMAGE,
    REACT_TAR,
    SIZE_THRESHOLD_LITERAL,
    docker_sites,
    find_helper_body,
    format_sites,
    has_tmp_cleanup,
    has_verbatim_line,
    helper_call_sites,
    mv_commands,
    norm_path,
    read_repo_file,
    save_or_export_sites,
    stdout_redirect_sites,
    zip_exclusion_present,
    zip_members,
    zip_uses_explicit_member_list,
)


class TestNoStdoutRedirectSaveSiteRemains:
    """Exploration case 1: classify every ``docker save`` invocation site in
    ``build-custom.sh``; zero sites of form STDOUT_REDIRECT.

    EXPECTED ON UNFIXED TREE: FAIL — counterexamples: the flask-app and
    react-webapp stdout-redirect saves, matching the live EBADF evidence
    from job d844a5fb-81d5-4294-956d-d6d6ae1f000e.
    """

    def test_zero_stdout_redirect_docker_save_sites(self):
        """**Validates: Requirements 1.1, 1.3, 2.1, 2.2**"""
        text = read_repo_file(BUILD_CUSTOM_REL)
        sites = stdout_redirect_sites(text)
        assert not sites, (
            f"bug condition C(X) present — {BUILD_CUSTOM_REL} streams image "
            f"tars through the snap-confined docker CLI's redirected stdout "
            f"(`docker save <image> > <file>`), which fails with "
            f"'write /dev/stdout: bad file descriptor' (EBADF) under the SSM "
            f"RunShellScript context, leaving a 0-byte tar and aborting via "
            f"set -e (live evidence: portal build job "
            f"d844a5fb-81d5-4294-956d-d6d6ae1f000e, AMD64 dedicated, "
            f"2026-08-09). Counterexample sites:\n"
            f"{format_sites(BUILD_CUSTOM_REL, sites)}"
        )


class TestBothSitesInFixedForm:
    """Exploration case 2: exactly two image-save call sites exist
    (flask-app, react-webapp), both invoking the shared ``save_image_tar``
    helper, with final destinations exactly the two ``ZIP_MEMBERS`` tar
    paths.

    EXPECTED ON UNFIXED TREE: FAIL — the unfixed tree has no helper call
    sites at all (the saves are bare stdout-redirect lines).
    """

    def test_exactly_two_helper_call_sites_with_zip_member_dests(self):
        """**Validates: Requirements 2.1, 2.2**"""
        text = read_repo_file(BUILD_CUSTOM_REL)
        calls = helper_call_sites(text)
        assert len(calls) == 2, (
            f"bug condition C(X) present — expected exactly TWO image-save "
            f"call sites invoking the shared {HELPER_NAME!r} helper "
            f"(flask-app, react-webapp), found {len(calls)}: {calls!r}. The "
            f"unfixed tree saves via bare stdout redirects instead (job "
            f"d844a5fb's EBADF failure mode)."
        )
        dest_by_image = {}
        for lineno, args in calls:
            assert len(args) == 2, (
                f"{BUILD_CUSTOM_REL}:{lineno}: {HELPER_NAME} call site must "
                f"pass exactly <image> <dest>, got args {args!r}"
            )
            dest_by_image[args[0]] = norm_path(args[1])
        expected = {FLASK_IMAGE: FLASK_TAR, REACT_IMAGE: REACT_TAR}
        assert dest_by_image == expected, (
            f"{HELPER_NAME} call sites do not land the tars at the exact "
            f"ZIP_MEMBERS paths — got {dest_by_image!r}, expected "
            f"{expected!r} (Req 3.2 producer/consumer agreement)."
        )


class TestFixedFormHelperStructure:
    """Exploration case 3: the ``save_image_tar`` helper contains, in order:
    ``docker save --output`` to a ``"$dest.partial"`` destination, an ``mv``
    from ``.partial`` to the final name, a size guard with threshold literal
    1048576, a ``tar -tf`` structural check, and non-zero exits with
    diagnostics on both failure paths.

    EXPECTED ON UNFIXED TREE: FAIL — the unfixed tree has no helper.
    """

    def test_helper_has_exact_fixed_form_structure(self):
        """**Validates: Requirements 2.1, 2.3, 2.4**"""
        text = read_repo_file(BUILD_CUSTOM_REL)
        found = find_helper_body(text)
        assert found is not None, (
            f"bug condition C(X) present — {BUILD_CUSTOM_REL} defines no "
            f"{HELPER_NAME}() helper: the saves are bare stdout-redirect "
            f"lines with no --output/.partial/mv discipline and no integrity "
            f"guard (a silent 0-byte tar can reach the zip — exactly job "
            f"d844a5fb's failure shape)."
        )
        start_lineno, body = found

        # (a) docker save --output to "$dest.partial" (never docker stdout)
        save_sites = docker_sites(body)
        partial_saves = [s for s in save_sites if s.form == OUTPUT_PARTIAL_MV]
        assert len(partial_saves) == 1 and len(save_sites) == 1, (
            f"{HELPER_NAME} (at line {start_lineno}) must contain exactly "
            f"one docker save, of the OUTPUT_PARTIAL_MV form; found: "
            f"{format_sites(BUILD_CUSTOM_REL, save_sites)}"
        )
        save = partial_saves[0]
        assert save.dest == "$dest.partial", (
            f"{HELPER_NAME}: docker save --output destination must be "
            f'"$dest.partial", got {save.dest!r}'
        )

        # (b) atomic mv "$dest.partial" -> "$dest"
        moves = [
            (lineno, args)
            for lineno, args in mv_commands(body)
            if args == ["$dest.partial", "$dest"]
        ]
        assert moves, (
            f'{HELPER_NAME}: no atomic `mv "$dest.partial" "$dest"` rename '
            f"found — the final tar name must only ever appear complete "
            f"(Req 2.3)."
        )

        # (c) size guard with the exact 1048576 threshold literal
        assert SIZE_THRESHOLD_LITERAL in body, (
            f"{HELPER_NAME}: size guard threshold literal "
            f"{SIZE_THRESHOLD_LITERAL} (1 MiB) not found in the helper body."
        )

        # (d) tar -tf structural check
        assert "tar -tf" in body, (
            f"{HELPER_NAME}: no `tar -tf` structural integrity check found."
        )

        # (e) in order: save, then mv, then the integrity guard
        save_pos = body.find("docker save")
        mv_pos = body.find("mv ")
        size_pos = body.find(SIZE_THRESHOLD_LITERAL)
        tar_pos = body.find("tar -tf")
        assert save_pos < mv_pos < size_pos and mv_pos < tar_pos, (
            f"{HELPER_NAME}: fixed-form steps out of order — expected "
            f"docker save --output, then mv, then the size guard and "
            f"tar -tf check (positions: save={save_pos}, mv={mv_pos}, "
            f"size={size_pos}, tar={tar_pos})."
        )

        # (f) non-zero exits with diagnostics on BOTH failure paths
        exit_count = body.count("exit 1")
        assert exit_count >= 2, (
            f"{HELPER_NAME}: expected non-zero exits on both failure paths "
            f"(save failure, integrity failure), found {exit_count} "
            f"`exit 1` occurrence(s)."
        )
        assert body.count("ERROR") >= 2, (
            f"{HELPER_NAME}: expected loud diagnostics (ERROR echoes) on "
            f"both failure paths."
        )


class TestCounterexampleInventoryScoping:
    """Exploration case 4: counterexample inventory scoping — the
    STDOUT_REDIRECT scan finds either exactly the TWO known sites (the
    flask-app and react-webapp saves — the unfixed tree, confirming the
    bugfix.md scan and that the fix scope is exactly the save block) or
    ZERO sites (the fixed tree).

    If the scan finds any OTHER site, or only one of the two, the root
    cause analysis is incomplete — re-hypothesize before fixing.

    EXPECTED: PASS pre-fix (exactly the two known sites) AND post-fix
    (zero sites).
    """

    def test_redirect_scan_finds_exactly_the_two_known_sites_or_none(self):
        """**Validates: Requirements 1.1, 1.2, 1.3**"""
        text = read_repo_file(BUILD_CUSTOM_REL)
        sites = stdout_redirect_sites(text)
        found = {(s.image, norm_path(s.dest)) for s in sites}
        assert len(sites) == len(found), (
            f"duplicate STDOUT_REDIRECT sites found — scoping refuted; "
            f"re-hypothesize before fixing:\n"
            f"{format_sites(BUILD_CUSTOM_REL, sites)}"
        )
        assert found in (set(), set(EXPECTED_UNFIXED_REDIRECT_SITES)), (
            f"the STDOUT_REDIRECT scan found site(s) OTHER than the two "
            f"identified by bugfix.md (flask-app -> {FLASK_TAR}, "
            f"react-webapp -> {REACT_TAR}) — the save-block fix scope is "
            f"wrong; re-hypothesize before fixing. Found:\n"
            f"{format_sites(BUILD_CUSTOM_REL, sites)}"
        )


class TestClassBoundaryNeighborScripts:
    """Exploration case 5 (¬C class boundary): the four scanned neighbor
    scripts contain zero ``docker save``/``docker export`` invocation
    sites — anchoring the Req 2.2 class closure (the class is exactly the
    two ``build-custom.sh`` sites).

    EXPECTED: PASS pre-fix AND post-fix.
    """

    def test_neighbor_scripts_have_no_save_or_export_sites(self):
        """**Validates: Requirements 2.2**"""
        for rel in NEIGHBOR_SCRIPT_RELS:
            text = read_repo_file(rel)
            sites = save_or_export_sites(text)
            assert not sites, (
                f"{rel}: unexpected docker save/export invocation site(s) — "
                f"the bugfix.md class scan is refuted; re-hypothesize before "
                f"fixing:\n{format_sites(rel, sites)}"
            )


class TestTempHazardGuardsCoexist:
    """Exploration case 6 (Req 2.3): the pre-zip
    ``rm -f ./custom-build/$COMPONENT_NAME/.tmp-*`` cleanup, the explicit
    ``ZIP_MEMBERS`` array with both tar paths, and the ``-x '*/.tmp-*'``
    zip exclusion are all present, and the transient names the fixed form
    can produce (``.tmp-*``, ``*.partial``) never appear in ``ZIP_MEMBERS``.

    EXPECTED: PASS pre-fix AND post-fix (the ``.partial`` non-membership
    assertion is trivially true pre-fix).
    """

    def test_zip_side_guards_present_and_transients_never_members(self):
        """**Validates: Requirements 1.4, 2.3**"""
        text = read_repo_file(BUILD_CUSTOM_REL)
        assert has_tmp_cleanup(text), (
            f"{BUILD_CUSTOM_REL}: the pre-zip "
            f"`rm -f ./custom-build/$COMPONENT_NAME/.tmp-*` cleanup is "
            f"missing — the zip-side guard layering documented at lines "
            f"385-421 is refuted; re-hypothesize before fixing."
        )
        members = zip_members(text)
        assert members is not None, (
            f"{BUILD_CUSTOM_REL}: no explicit ZIP_MEMBERS=( ... ) array "
            f"found — the explicit-member-list packaging is refuted; "
            f"re-hypothesize before fixing."
        )
        for tar_path in (FLASK_TAR, REACT_TAR):
            assert tar_path in members, (
                f"{BUILD_CUSTOM_REL}: ZIP_MEMBERS is missing the tar path "
                f"{tar_path!r}; members: {members!r}"
            )
        offenders = [
            m for m in members if ".tmp-" in m or ".partial" in m
        ]
        assert not offenders, (
            f"{BUILD_CUSTOM_REL}: transient names must never appear in "
            f"ZIP_MEMBERS, found: {offenders!r}"
        )
        assert zip_exclusion_present(text), (
            f"{BUILD_CUSTOM_REL}: the zip `-x '*/.tmp-*'` exclusion is "
            f"missing — the zip-side guard layering is refuted; "
            f"re-hypothesize before fixing."
        )
        assert zip_uses_explicit_member_list(text), (
            f"{BUILD_CUSTOM_REL}: the zip invocation does not package the "
            f'explicit "${{ZIP_MEMBERS[@]}}" list — a recursive dir scan '
            f"would reintroduce the transient-file race (exit 18)."
        )


class TestLogAnchorPreserved:
    """Exploration case 7: the ``echo "save docker images as tarvballs"``
    line survives verbatim — the live-log grep anchor for the verification
    build (job d844a5fb logged the EBADF immediately after it). Structural
    pins: ``set -e`` and ``set -o pipefail`` still at lines 2-3 (they
    precede the fix region and cannot shift).

    EXPECTED: PASS pre-fix AND post-fix.
    """

    def test_log_anchor_and_structural_pins(self):
        """**Validates: Requirements 1.1, 1.2**"""
        text = read_repo_file(BUILD_CUSTOM_REL)
        assert has_verbatim_line(text, LOG_ANCHOR_LINE), (
            f"{BUILD_CUSTOM_REL}: the live-log grep anchor "
            f"{LOG_ANCHOR_LINE!r} is missing — the verification build's "
            f"monitoring anchor (and job d844a5fb's evidence anchor) must "
            f"survive verbatim."
        )
        lines = text.split("\n")
        assert lines[1].strip() == "set -e", (
            f"{BUILD_CUSTOM_REL}: line 2 is {lines[1]!r}, expected 'set -e' "
            f"(the abort-on-failure discipline C(X) relies on)."
        )
        assert lines[2].strip() == "set -o pipefail", (
            f"{BUILD_CUSTOM_REL}: line 3 is {lines[2]!r}, expected "
            f"'set -o pipefail'."
        )
