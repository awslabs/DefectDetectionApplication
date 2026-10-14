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
"""Observation-first preservation baseline for
``build-docker-save-stdout-failure`` (task 2) — **Property 2: Preservation —
All Other Lines, Tar Paths, and Neighbor Scripts Unchanged**.

Methodology: capture the UNFIXED tree's bytes as goldens BEFORE any
production edit (first run: golden absent -> capture; subsequent runs:
byte-for-byte assert). These tests MUST PASS on the unfixed tree; the
goldens under ``build_save_pkgs/baselines/`` are FROZEN from that point —
task 5.2 re-runs these SAME tests unchanged against the fixed tree to prove
that ONLY the one contiguous save block changed (F(X) = F'(X) for every
non-bug-condition input).

Diff-scoping goldens (design "Preservation Checking" pseudocode):

(a) ``build_custom_save_masked.txt`` — the save-block-masked view of
    ``build-custom.sh``: everything strictly between the two unchanged
    content anchors (the ``echo "save docker images as tarvballs"``
    log-anchor line above, the compose-``cp`` staging line below) removed.
    Shape-agnostic: both the unfixed 5-comment-lines-plus-2-save-lines
    block and the fixed comment-block-plus-helper-plus-2-call-lines form
    are fully contained between those anchors, so the SAME frozen golden
    asserts on both trees. The masked view proves the audit guard, the
    edgemlsdk build and deb extraction, the compose builds, the gate block,
    the staging-dir population, the ``.tmp-*`` cleanup, the diagnostics,
    the ``ZIP_MEMBERS`` list, the zip + ``zip -T``, and the greengrass copy
    survive verbatim (Req 3.1, 3.5);
(b) full-file sha256 goldens of the four scanned neighbor scripts —
    ``scripts/portal-build-agent.sh``, ``publish-ecr-only.sh``,
    ``com.dda.InferenceUploader/build-and-publish.sh``,
    ``src/edgemlsdk/build.sh`` — the class boundary enforced mechanically
    (Req 3.5).

Plus a mask-exactness assertion on the real file (the masked view differs
from the raw file by exactly the one contiguous save block in one of the
two admissible shapes, followed by the pinned two-line trailer — the mask
cannot hide collateral edits in either shape), a ZIP_MEMBERS-intact
assertion (both tar paths verbatim inside the masked/unchanged region AND
as the save sites' final destinations — producer and consumer agree,
Req 3.2), and the three Hypothesis helper properties from design:
save-form classifier, masking preservation, and tokenization totality.

All parsing is TEXT only — no ``docker``, ``subprocess``, or shell-out
anywhere in this package.

**Validates: Requirements 3.1, 3.2, 3.5**

Run (finite, non-watch):
    PYTHONPATH=src/backend:test/backend-test \
        pytest test/backend-test/build_save_pkgs/ --noconftest
"""
from hypothesis import given, settings
from hypothesis import strategies as st

from _save_preservation_support import (
    BLOCK_TRAILER,
    COMPOSE_CP_LINE,
    GOLDEN_MASKED,
    UNFIXED_BLOCK_TOTAL_LINES,
    anchor_indices,
    capture_or_assert_text,
    image_save_final_destinations,
    is_fixed_block,
    is_unfixed_block,
    mask_save_block,
    masked_region,
    neighbor_golden_name,
    sha256_hex,
)
from _save_support import (
    BUILD_CUSTOM_REL,
    FLASK_IMAGE,
    FLASK_TAR,
    LOG_ANCHOR_LINE,
    NEIGHBOR_SCRIPT_RELS,
    OTHER,
    OUTPUT_PARTIAL_MV,
    REACT_IMAGE,
    REACT_TAR,
    STDOUT_REDIRECT,
    docker_sites,
    read_repo_file,
    save_or_export_sites,
    stdout_redirect_sites,
    zip_members,
)


# --------------------------------------------------------------------------- #
# Goldens a-b: capture on the unfixed tree, byte-for-byte assert thereafter
# --------------------------------------------------------------------------- #
class TestFrozenGoldens:
    """The diff-scoping goldens. Captured once from the UNFIXED tree and
    frozen; task 5.2 re-runs these unchanged against the fixed tree."""

    def test_build_custom_save_masked_golden(self):
        """**Validates: Requirements 3.1, 3.5** — golden (a): every line of
        build-custom.sh EXCEPT the maskable region between the log-anchor
        echo and the compose-cp staging line is byte-for-byte frozen —
        including the audit guard, edgemlsdk build, compose builds, gate
        block, staging-dir population, .tmp-* cleanup, diagnostics,
        ZIP_MEMBERS, zip + zip -T, and the greengrass copy."""
        lines = read_repo_file(BUILD_CUSTOM_REL).split("\n")
        masked = mask_save_block(lines)
        capture_or_assert_text(GOLDEN_MASKED, "\n".join(masked))

    def test_neighbor_script_sha256_goldens(self):
        """**Validates: Requirements 3.5** — golden (b): the four scanned
        neighbor scripts must remain bit-identical post-fix (the Req 2.2
        class boundary enforced mechanically)."""
        for rel in NEIGHBOR_SCRIPT_RELS:
            digest = sha256_hex(rel)
            capture_or_assert_text(neighbor_golden_name(rel), digest + "\n")


# --------------------------------------------------------------------------- #
# Mask exactness on the REAL file — the mask cannot hide collateral edits
# --------------------------------------------------------------------------- #
class TestMaskExactnessOnRealFile:
    """The masked view differs from the raw file by exactly the one
    contiguous maskable region: the save block in one of the two admissible
    shapes (unfixed: 5 comment lines + 2 stdout-redirect saves; fixed:
    comment block + save_image_tar helper + 2 call lines) followed by the
    pinned two-line trailer. Passes on both trees."""

    def test_anchors_unique_and_mask_reconstructs_raw_file(self):
        """**Validates: Requirements 3.1**"""
        lines = read_repo_file(BUILD_CUSTOM_REL).split("\n")
        echo_count = sum(1 for l in lines if l.strip() == LOG_ANCHOR_LINE)
        cp_count = sum(1 for l in lines if l.strip() == COMPOSE_CP_LINE)
        assert echo_count == 1, (
            f"{BUILD_CUSTOM_REL}: expected exactly ONE log-anchor line "
            f"{LOG_ANCHOR_LINE!r}, found {echo_count}"
        )
        assert cp_count == 1, (
            f"{BUILD_CUSTOM_REL}: expected exactly ONE compose-cp anchor "
            f"line {COMPOSE_CP_LINE!r}, found {cp_count}"
        )
        found = anchor_indices(lines)
        assert found is not None, (
            f"{BUILD_CUSTOM_REL}: the two mask anchors were not found in "
            f"order (echo above, compose-cp below)"
        )
        echo_idx, cp_idx = found
        assert echo_idx < cp_idx
        masked = mask_save_block(lines)
        region = masked_region(lines)
        # The masked view + the region reconstruct the raw file exactly —
        # exactly ONE contiguous region differs, nothing else.
        assert masked == lines[: echo_idx + 1] + lines[cp_idx:]
        assert region == lines[echo_idx + 1 : cp_idx]
        assert masked[: echo_idx + 1] + region + masked[echo_idx + 1 :] == lines

    def test_masked_region_is_exactly_the_save_block_plus_pinned_trailer(self):
        """**Validates: Requirements 3.1** — the region the mask removes is
        exactly: the save block in ONE of the two admissible shapes, then
        the pinned trailer bytes (a blank line and the include-comment) —
        so masking can hide no collateral edit in either shape."""
        lines = read_repo_file(BUILD_CUSTOM_REL).split("\n")
        region = masked_region(lines)
        trailer = list(BLOCK_TRAILER)
        assert len(region) > len(trailer) and region[-len(trailer):] == trailer, (
            f"{BUILD_CUSTOM_REL}: the masked region no longer ends with the "
            f"pinned trailer {trailer!r} — a collateral edit is hiding "
            f"inside the mask; region: {region!r}"
        )
        block = region[: -len(trailer)]
        unfixed = is_unfixed_block(block)
        fixed = is_fixed_block(block)
        assert unfixed or fixed, (
            f"{BUILD_CUSTOM_REL}: the masked save block matches NEITHER "
            f"admissible shape (unfixed: exactly "
            f"{UNFIXED_BLOCK_TOTAL_LINES} lines — 5 comment lines + the 2 "
            f"known stdout-redirect saves; fixed: rewritten comment block + "
            f"save_image_tar helper + the 2 call sites on the exact "
            f"ZIP_MEMBERS paths) — a collateral edit is hiding inside the "
            f"mask; block: {block!r}"
        )

    def test_masked_view_contains_no_image_save_site(self):
        """**Validates: Requirements 3.1** — the mask caught every
        image-save site: the masked (unchanged) view contains no docker
        save/export invocation site of any form."""
        lines = read_repo_file(BUILD_CUSTOM_REL).split("\n")
        masked_text = "\n".join(mask_save_block(lines))
        assert not save_or_export_sites(masked_text), (
            f"{BUILD_CUSTOM_REL}: a docker save/export invocation site "
            f"survived the save-block mask — the fix scope escaped the "
            f"anchored region"
        )


# --------------------------------------------------------------------------- #
# ZIP_MEMBERS intact — producer and consumer agree (Req 3.2)
# --------------------------------------------------------------------------- #
class TestZipMembersIntact:
    """Both tar paths appear verbatim inside the masked (unchanged) region
    AND as the save sites' final destinations. Passes on both trees."""

    def test_tar_paths_verbatim_in_masked_region_and_as_save_destinations(self):
        """**Validates: Requirements 3.2**"""
        text = read_repo_file(BUILD_CUSTOM_REL)
        masked_text = "\n".join(mask_save_block(text.split("\n")))
        members = zip_members(masked_text)
        assert members is not None, (
            f"{BUILD_CUSTOM_REL}: no explicit ZIP_MEMBERS=( ... ) array in "
            f"the masked (unchanged) region"
        )
        for tar_path in (FLASK_TAR, REACT_TAR):
            assert tar_path in members, (
                f"{BUILD_CUSTOM_REL}: ZIP_MEMBERS in the masked region is "
                f"missing the tar path {tar_path!r}; members: {members!r}"
            )
        dests = image_save_final_destinations(text)
        expected = {(FLASK_IMAGE, FLASK_TAR), (REACT_IMAGE, REACT_TAR)}
        assert dests == expected, (
            f"{BUILD_CUSTOM_REL}: the image-save sites' final destinations "
            f"{dests!r} do not agree with the ZIP_MEMBERS tar paths "
            f"{expected!r} (Req 3.2 producer/consumer agreement)"
        )


# --------------------------------------------------------------------------- #
# Property (i): save-form classifier over generated docker save variants
# --------------------------------------------------------------------------- #
# Smart generators: image names are docker-ish identifier tokens (never
# flag-shaped, never all-digits — an adjacent all-digit token would merge
# into an fd redirect like `2>`, a different shell construct); destinations
# are safe path tokens (no quotes, no `$`, so string wrapping stays valid).
_IMG_NAMES = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz0123456789-", min_size=1, max_size=12
).filter(lambda s: not s.startswith("-") and not s.isdigit())

_PATH_PARTS = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz0123456789_-", min_size=1, max_size=8
).filter(lambda s: not s.startswith("-"))

_DESTS = st.builds(
    lambda a, b: f"./{a}/{b}.tar", _PATH_PARTS, _PATH_PARTS
)

_SPACES = st.sampled_from([" ", "  ", " \t "])


@st.composite
def _redirect_invocations(draw):
    """A real `docker save <image> > <file>` stdout-redirect variant:
    random spacing, `>` or `>>`, tight or spaced redirect target."""
    img = draw(_IMG_NAMES)
    dest = draw(_DESTS)
    s1 = draw(_SPACES)
    op = draw(st.sampled_from([">", ">>"]))
    tight = draw(st.booleans())
    if tight:
        line = f"docker save{s1}{img} {op}{dest}"
    else:
        line = f"docker save{s1}{img}{draw(_SPACES)}{op} {dest}"
    return line, img, dest


@st.composite
def _output_invocations(draw):
    """A `docker save --output/-o/--output= <dest>[.partial] <image>`
    variant with random flag spelling, ordering, and spacing."""
    img = draw(_IMG_NAMES)
    dest = draw(_DESTS)
    partial = draw(st.booleans())
    full_dest = dest + ".partial" if partial else dest
    spelling = draw(st.sampled_from(["--output", "-o", "--output="]))
    if spelling == "--output=":
        flag_part = f"--output={full_dest}"
    else:
        flag_part = f"{spelling} {full_dest}"
    if draw(st.booleans()):
        line = f"docker save {img}{draw(_SPACES)}{flag_part}"
    else:
        line = f"docker save {flag_part}{draw(_SPACES)}{img}"
    expected_form = OUTPUT_PARTIAL_MV if partial else OTHER
    return line, img, full_dest, expected_form


class TestSaveFormClassifierProperty:
    """Property (i) — save-form classifier (design Property 1): the
    classifier returns STDOUT_REDIRECT iff the invocation is a real
    (non-comment, non-string) docker save whose image tar goes through a
    shell stdout redirect, and OUTPUT_PARTIAL_MV iff it is the fixed form —
    token discipline: `--output` never matches `--output-foo`."""

    @settings(max_examples=200, deadline=None)
    @given(case=_redirect_invocations())
    def test_redirect_variants_classify_stdout_redirect(self, case):
        """**Validates: Requirements 2.1, 2.2** (Property 1)"""
        line, img, dest = case
        sites = docker_sites(line)
        assert len(sites) == 1, f"expected one site in {line!r}, got {sites!r}"
        site = sites[0]
        assert site.form == STDOUT_REDIRECT
        assert site.image == img
        assert site.dest == dest

    @settings(max_examples=200, deadline=None)
    @given(case=_output_invocations())
    def test_output_variants_classify_partial_mv_iff_partial_dest(self, case):
        """**Validates: Requirements 2.1, 2.2** (Property 1)"""
        line, img, full_dest, expected_form = case
        sites = docker_sites(line)
        assert len(sites) == 1, f"expected one site in {line!r}, got {sites!r}"
        site = sites[0]
        assert site.form == expected_form, (
            f"{line!r}: classified {site.form}, expected {expected_form}"
        )
        assert site.image == img
        assert site.dest == full_dest

    @settings(max_examples=200, deadline=None)
    @given(img=_IMG_NAMES, dest=_DESTS)
    def test_output_foo_never_matches_output_token(self, img, dest):
        """**Validates: Requirements 2.1** (token discipline: `--output-foo`
        is some other flag, never the fixed form's output flag)"""
        line = f"docker save --output-foo {dest}.partial {img}"
        sites = docker_sites(line)
        assert len(sites) == 1
        assert sites[0].form == OTHER, (
            f"{line!r}: `--output-foo` classified as the fixed form — "
            f"token discipline violated"
        )

    @settings(max_examples=200, deadline=None)
    @given(
        case=_redirect_invocations(),
        wrapper=st.sampled_from(["comment", "double_quote", "single_quote"]),
    )
    def test_comment_or_string_wrapped_never_classifies_as_site(
        self, case, wrapper
    ):
        """**Validates: Requirements 2.1** (comment/string content never
        classifies as an invocation site)"""
        line, _, _ = case
        if wrapper == "comment":
            wrapped = f"# {line}"
        elif wrapper == "double_quote":
            wrapped = f'echo "{line}"'
        else:
            wrapped = f"echo '{line}'"
        assert docker_sites(wrapped) == [], (
            f"{wrapped!r}: comment/string-wrapped docker save classified "
            f"as a real invocation site"
        )


# --------------------------------------------------------------------------- #
# Property (ii): masking preservation over generated line sequences
# --------------------------------------------------------------------------- #
# Benign lines mirror real build-custom.sh content and never strip to
# either anchor line.
_BENIGN_LINES = st.sampled_from(
    [
        "set -e",
        "set -o pipefail",
        "docker compose --profile amd64 build",
        "mkdir -p ./custom-build/$COMPONENT_NAME/backend",
        'echo "Packaging artifact: $ARCHIVE"',
        "rm -f ./custom-build/$COMPONENT_NAME/.tmp-* 2>/dev/null || true",
        "cp -r src/host_scripts ./custom-build/$COMPONENT_NAME/",
        "# save Docker images as tar",
        "",
        "ls -lh ./custom-build/$COMPONENT_NAME/ || true",
        'zip -r -X "$ARCHIVE" "${ZIP_MEMBERS[@]}" -x \'*/.tmp-*\'',
    ]
)

# Marked save blocks in both admissible shapes (and a degenerate comment-only
# block) — the mask must remove any of them, and any number of them.
_SAVE_BLOCKS = st.sampled_from(
    [
        [
            "docker save flask-app > ./custom-build/$COMPONENT_NAME/flask-app.tar",
            "docker save react-webapp > ./custom-build/$COMPONENT_NAME/react-webapp.tar",
        ],
        [
            "# Use stdout redirection rather than `docker save --output`.",
            "docker save flask-app > ./custom-build/$COMPONENT_NAME/flask-app.tar",
        ],
        [
            "save_image_tar() {",
            '  local image=$1 dest=$2',
            '  docker save --output "$dest.partial" "$image"',
            '  mv "$dest.partial" "$dest"',
            "}",
            "save_image_tar flask-app ./custom-build/$COMPONENT_NAME/flask-app.tar",
            "save_image_tar react-webapp ./custom-build/$COMPONENT_NAME/react-webapp.tar",
        ],
        ["# a lone comment block"],
    ]
)


class TestMaskingPreservationProperty:
    """Property (ii) — masking preservation (design Property 2): for
    generated shell-line sequences containing zero or more marked save
    blocks between the two anchors, the masking helper removes exactly the
    between-anchor content (the block(s)) and nothing else."""

    @settings(max_examples=200, deadline=None)
    @given(
        prefix=st.lists(_BENIGN_LINES, max_size=8),
        blocks=st.lists(_SAVE_BLOCKS, max_size=3),
        suffix=st.lists(_BENIGN_LINES, max_size=8),
    )
    def test_mask_removes_exactly_the_between_anchor_blocks(
        self, prefix, blocks, suffix
    ):
        """**Validates: Requirements 3.1** (Property 2)"""
        between = [line for block in blocks for line in block]
        lines = (
            prefix + [LOG_ANCHOR_LINE] + between + [COMPOSE_CP_LINE] + suffix
        )
        masked = mask_save_block(lines)
        assert masked == prefix + [LOG_ANCHOR_LINE, COMPOSE_CP_LINE] + suffix
        assert masked_region(lines) == between

    @settings(max_examples=200, deadline=None)
    @given(lines=st.lists(_BENIGN_LINES, max_size=20))
    def test_mask_is_identity_without_the_anchors(self, lines):
        """**Validates: Requirements 3.1** — no anchors means nothing is
        removed (all non-bug-condition lines preserved)."""
        assert mask_save_block(lines) == lines
        assert masked_region(lines) == []


# --------------------------------------------------------------------------- #
# Property (iii): tokenization totality over chaotic shell lines
# --------------------------------------------------------------------------- #
_FRAGMENTS = st.sampled_from(
    [
        "docker", "save", "export", "--output", "-o", "--output=x.partial",
        "--output-foo", ">", ">>", "2>", "2>/dev/null", "|", "&&", "||",
        ";", "#", "'", '"', "\\", "img", "./custom-build/f.tar",
        "$dest.partial", "flask-app", "(", ")", "if", "then", "fi",
        "VAR=1", "1048576", "1", "2", "mv", "tar", "-tf",
    ]
)

_RANDOM_TEXT = st.text(
    alphabet="dockersave -o>#'\"\\$.{}()|&;\tx=12", max_size=40
)


@st.composite
def _chaotic_scripts(draw):
    lines = []
    for _ in range(draw(st.integers(min_value=0, max_value=6))):
        if draw(st.booleans()):
            tokens = draw(st.lists(_FRAGMENTS, max_size=8))
            lines.append(draw(_SPACES).join(tokens))
        else:
            lines.append(draw(_RANDOM_TEXT))
    return "\n".join(lines)


class TestTokenizationTotalityProperty:
    """Property (iii) — tokenization totality (design Properties 1-2): for
    generated shell lines with random comments, strings, redirects, and
    continuations, classification is total and never throws — unknown
    constructs classify OTHER, never crash and never silently classify as
    the fixed form."""

    @settings(max_examples=300, deadline=None)
    @given(text=_chaotic_scripts())
    def test_classification_is_total_and_never_silently_fixed_form(self, text):
        """**Validates: Requirements 2.1, 3.1** (Properties 1-2)"""
        sites = docker_sites(text)  # must never raise
        for site in sites:
            assert site.form in (STDOUT_REDIRECT, OUTPUT_PARTIAL_MV, OTHER)
            if site.form == OUTPUT_PARTIAL_MV:
                assert site.dest is not None and site.dest.endswith(
                    ".partial"
                ), (
                    f"a non-.partial destination silently classified as the "
                    f"fixed form in {text!r}: {site!r}"
                )
            if site.form == STDOUT_REDIRECT:
                assert site.dest is not None
        # The save/export class-boundary scan and the masking helper are
        # total over the same chaotic input.
        save_or_export_sites(text)
        lines = text.split("\n")
        masked = mask_save_block(lines)
        region = masked_region(lines)
        assert len(masked) + len(region) == len(lines)

    @settings(max_examples=300, deadline=None)
    @given(text=_RANDOM_TEXT)
    def test_single_chaotic_line_never_throws(self, text):
        """**Validates: Requirements 2.1, 3.1**"""
        for site in docker_sites(text):
            assert site.form in (STDOUT_REDIRECT, OUTPUT_PARTIAL_MV, OTHER)
