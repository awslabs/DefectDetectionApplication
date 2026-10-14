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
``backend-jammy-retired-packages`` (task 2) — **Property 2: Preservation —
All Other Lines, Old-Base Behavior, and Sibling Files Unchanged**.

Methodology: capture the UNFIXED tree's bytes as goldens BEFORE any
production edit (first run: golden absent -> capture; subsequent runs:
byte-for-byte assert). These tests MUST PASS on the unfixed tree; the
goldens under ``backend_jammy_pkgs/baselines/`` are FROZEN from that point —
task 5.2 re-runs these SAME tests unchanged against the fixed tree to prove
that ONLY the line-70 libssl install step changed (F(X) = F'(X) for every
non-bug-condition input).

Diff-scoping goldens (design "Preservation Checking" pseudocode):

(a) ``backend_Dockerfile_libssl_masked.txt`` — the libssl-step-masked view
    of ``src/backend/Dockerfile``. The mask matches the target step by its
    apt package token (``libssl1.1``, content-matched) and absorbs any
    contiguous comment header directly above the target RUN, so the SAME
    frozen golden matches BOTH shapes: the unfixed single line
    ``RUN apt-get install libssl1.1 -y`` AND the fixed
    comment-block-plus-``/etc/os-release``-conditional form (design
    Change 1). The masked view proves the Python 3.11 source build, the
    awscrt vendored-link workaround, lines 69/71/72, the inert lines-73-75
    conditional, the CVE block, and all COPY/script invocation lines
    survive verbatim (Req 3.1);
(b) full-file sha256 goldens of the 8 untouched files:
    ``src/frontend/Dockerfile``, ``src/docker-compose.yaml``,
    ``src/backend/Dockerfile.jp5``, ``.jp6``, ``.x86_64_nvidia`` (Req 3.3),
    and the three install scripts ``prereqs_install.sh``,
    ``install_aravis.sh``, ``install_edgemlsdk.sh`` (design Decision 3).

Plus a mask-exactness assertion on the real file (the masked view differs
from the raw file by exactly the ONE target step, count and admissible
content asserted for BOTH shapes — the mask cannot hide collateral edits)
and the four Hypothesis properties from design: (i) retired-token
classifier exactness, (ii) masking preservation, (iii) apt-line
tokenization totality, (iv) the reachability model.

All parsing is TEXT only — no ``docker``, ``subprocess``, or shell-out
anywhere in this package.

**Validates: Requirements 3.1, 3.2, 3.3**

Run (finite, non-watch):
    PYTHONPATH=src/backend:test/backend-test \
        pytest test/backend-test/backend_jammy_pkgs/ --noconftest
"""
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from _jammy_preservation_support import (
    GOLDEN_BACKEND_MASKED,
    UNTOUCHED_FILES,
    apt_steps_in_text,
    capture_or_assert_text,
    is_retired_jammy_package,
    libssl_step_line_ranges,
    mask_libssl_install_step,
    masked_block_shape,
    retired_sites_in_text,
    sha256_hex,
)
from _jammy_support import (
    BACKEND_DOCKERFILE_REL,
    BUG_SITE_TOKEN,
    REPO_ROOT,
    RETIRED_JAMMY_PACKAGES,
    AptStep,
    guard_allowlist,
    is_reachable,
    read_repo_file,
)


# --------------------------------------------------------------------------- #
# Goldens a-b: capture on the unfixed tree, byte-for-byte assert thereafter
# --------------------------------------------------------------------------- #
class TestFrozenGoldens:
    """The diff-scoping goldens. Captured once from the UNFIXED tree and
    frozen; task 5.2 re-runs these unchanged against the fixed tree."""

    def test_backend_dockerfile_libssl_masked_golden(self):
        """**Validates: Requirements 3.1** — golden (a): every line of
        src/backend/Dockerfile EXCEPT the libssl install step (line 70's
        logical RUN, plus any contiguous comment header directly above it)
        is byte-for-byte frozen — including the Python 3.11 source build,
        the awscrt vendored-link workaround, the apt update neighbors
        (lines 69/71), line 72's six-package install, the inert lines-73-75
        conditional, the CVE block, and all COPY/script invocations."""
        lines = read_repo_file(BACKEND_DOCKERFILE_REL).splitlines()
        masked = mask_libssl_install_step(lines)
        capture_or_assert_text(
            GOLDEN_BACKEND_MASKED, "\n".join(masked) + "\n"
        )

    @pytest.mark.parametrize("rel_path,golden_name", UNTOUCHED_FILES)
    def test_untouched_file_sha256_golden(self, rel_path, golden_name):
        """**Validates: Requirements 3.2, 3.3** — golden (b): the 8 files
        the fix must leave byte-for-byte untouched (frontend Dockerfile,
        compose, jp5/jp6/x86_64_nvidia variants — Req 3.3 — and the three
        install scripts, design Decision 3), each pinned by a full-file
        sha256 golden captured on the unfixed tree."""
        digest = sha256_hex(REPO_ROOT, rel_path)
        capture_or_assert_text(golden_name, digest + "\n")


# --------------------------------------------------------------------------- #
# Mask exactness on the REAL file — the mask cannot hide collateral edits
# --------------------------------------------------------------------------- #
class TestMaskExactnessOnRealFile:
    """The masked view differs from the raw file by exactly the ONE target
    step, whose content is one of the two admissible shapes (unfixed single
    line / fixed comment-block-plus-conditional). Passes on both trees."""

    def test_mask_removes_exactly_one_step_with_admissible_content(self):
        """**Validates: Requirements 3.1, 3.2**"""
        full = read_repo_file(BACKEND_DOCKERFILE_REL).splitlines()
        ranges = libssl_step_line_ranges(full)
        assert len(ranges) == 1, (
            f"{BACKEND_DOCKERFILE_REL}: expected exactly ONE target libssl "
            f"install step, found {len(ranges)}: {ranges}"
        )
        (a, b), = ranges
        masked = mask_libssl_install_step(full)
        # Count: exactly the one contiguous block dropped, nothing else.
        assert masked == full[:a] + full[b + 1 :], (
            f"{BACKEND_DOCKERFILE_REL}: the masked view is not the raw file "
            f"minus exactly physical lines {a + 1}-{b + 1}"
        )
        # Content: the dropped block is one of the two admissible shapes.
        block = full[a : b + 1]
        shape = masked_block_shape(block)
        assert shape in ("unfixed", "fixed"), (
            f"{BACKEND_DOCKERFILE_REL}: the masked block (physical lines "
            f"{a + 1}-{b + 1}) is neither the unfixed single line "
            f"'RUN apt-get install libssl1.1 -y' nor the fixed "
            f"comment-block-plus-/etc/os-release-conditional form "
            f"(design Change 1). Block:\n" + "\n".join(block)
        )
        # No accidental comment absorption: the line directly above the
        # masked region (when one exists) is not a comment — on the unfixed
        # tree that is line 69's 'RUN apt update -y'.
        if a > 0:
            assert not full[a - 1].lstrip().startswith("#"), (
                f"{BACKEND_DOCKERFILE_REL}:{a}: an unabsorbed comment sits "
                f"directly above the masked region — absorption is "
                f"inconsistent"
            )

    def test_masked_view_has_no_retired_or_target_tokens(self):
        """**Validates: Requirements 3.1** — the mask caught every target
        step: the masked view contains no 22.04-reachable retired-token apt
        step and no apt step requesting the ``libssl1.1`` token at all."""
        full = read_repo_file(BACKEND_DOCKERFILE_REL).splitlines()
        masked_text = "\n".join(mask_libssl_install_step(full)) + "\n"
        assert not retired_sites_in_text(masked_text), (
            f"{BACKEND_DOCKERFILE_REL}: a retired-token apt install step "
            f"survived the libssl-step mask"
        )
        for lineno, text, packages, _allow in apt_steps_in_text(masked_text):
            assert BUG_SITE_TOKEN not in packages, (
                f"masked view line {lineno}: target token "
                f"{BUG_SITE_TOKEN!r} survived the mask in step: "
                f"{text.strip()}"
            )

    def test_masked_view_preserves_key_noncondition_anchors(self):
        """**Validates: Requirements 3.1** — the preserved (¬C) anchors are
        inside the masked view: both ``apt update -y`` neighbors (lines
        69/71), line 72's six-package install, and the inert lines-73-75
        ``$OS`` conditional with its typo'd package (design Decision 4)."""
        full = read_repo_file(BACKEND_DOCKERFILE_REL).splitlines()
        masked = mask_libssl_install_step(full)
        stripped = [line.strip() for line in masked]
        assert stripped.count("RUN apt update -y") >= 2, (
            "the apt update neighbors (lines 69/71) did not survive the mask"
        )
        assert (
            "RUN apt install libexif12 libcurl4 libarchive13 "
            "gstreamer1.0-tools gstreamer1.0-libav ffmpeg -y" in stripped
        ), "line 72's six-package install did not survive the mask"
        masked_text = "\n".join(masked)
        assert '[ "$OS" = "18.04" ]' in masked_text, (
            "the inert lines-73-75 $OS conditional did not survive the mask"
        )
        assert "libavcodec-extra57i" in masked_text, (
            "the inert conditional's body did not survive the mask"
        )


# --------------------------------------------------------------------------- #
# Property (i): retired-token classifier over generated package tokens
# --------------------------------------------------------------------------- #
_NAME_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789+.-"

_RANDOM_PKG_NAMES = st.text(
    alphabet=_NAME_ALPHABET, min_size=1, max_size=24
).filter(lambda s: not s.startswith("-"))

# Adversarial prefix/suffix mutations around libssl1.1 / libssl-dev (the
# task-mandated adversarial set plus near-misses) — none is retired.
_ADVERSARIAL_TOKENS = st.sampled_from(
    [
        "libssl1.1-foo",
        "libssl-dev",
        "libssl3",
        "zlib1g-dev",
        "libssl1.1-dbg",
        "libssl1.10",
        "libssl1.1.1",
        "liblibssl1.1",
        "libssl1",
        "ssl1.1",
        "libssl",
        "libssl2.1",
        "xlibssl1.1",
    ]
)

_RETIRED_TOKENS = st.sampled_from(sorted(RETIRED_JAMMY_PACKAGES))

_TOKENS = st.one_of(_RETIRED_TOKENS, _ADVERSARIAL_TOKENS, _RANDOM_PKG_NAMES)


class TestRetiredTokenClassifierProperty:
    """Property (i): the classifier flags a token iff it is EXACTLY a member
    of the retired set — never by prefix/suffix/substring (token-boundary
    discipline; ``libssl-dev``/``libssl3`` must never classify as retired)."""

    @settings(max_examples=300, deadline=None)
    @given(token=_TOKENS)
    def test_classifier_flags_iff_exact_member(self, token):
        """**Validates: Requirements 3.1**"""
        expected = token in RETIRED_JAMMY_PACKAGES
        assert is_retired_jammy_package(token) == expected

    @settings(max_examples=300, deadline=None)
    @given(token=_TOKENS)
    def test_scan_pipeline_flags_iff_exact_member(self, token):
        """**Validates: Requirements 3.1** — end-to-end through the real
        apt-scan pipeline: an unconditional install line requesting the
        token yields a retired-token site iff the token is exactly in the
        retired set."""
        text = f"RUN apt-get install {token} -y"
        sites = retired_sites_in_text(text)
        if token in RETIRED_JAMMY_PACKAGES:
            assert sites == [(1, token)]
        else:
            assert sites == []


# --------------------------------------------------------------------------- #
# Property (ii): masking preservation over generated line sequences
# --------------------------------------------------------------------------- #
# Smart generators: benign lines never end in a continuation backslash (so a
# preceding logical line cannot absorb an injected target block) and never
# request the libssl1.1 token; adversarial neighbors exercise the
# token-boundary discipline (libssl-dev / libssl3 / libssl1.1-dbg lines are
# NOT masked). Comment lines are included to prove unrelated comments
# survive; the assembly inserts a non-comment separator before a target
# block whenever the previous line is a comment, because contiguous comment
# headers directly above a target ARE deliberately absorbed.
_BENIGN_DOCKER_LINES = st.sampled_from(
    [
        "ENV DEBIAN_FRONTEND noninteractive",
        "ARG OS",
        "RUN apt update -y",
        "RUN apt-get install libssl-dev zlib1g-dev -y",
        "RUN apt-get install libssl3 -y",
        "RUN apt-get install libssl1.1-dbg -y",
        "RUN apt install libexif12 libcurl4 ffmpeg -y",
        'RUN if [ "$OS" = "18.04" ] ; then echo old ; fi',
        "RUN pip3 install meson",
        "# unrelated comment: must survive the mask",
        "RUN apt clean -y",
        "COPY app.py ./",
        "",
    ]
)

# Both the UNFIXED and FIXED shapes of the target libssl install step — the
# helper must mask either (with the fixed form's comment header absorbed),
# so the same frozen golden works before and after the fix. Continuation
# shapes exercise logical-RUN reconstruction.
_TARGET_BLOCKS = st.sampled_from(
    [
        ["RUN apt-get install libssl1.1 -y"],
        ["RUN apt-get install \\", "    libssl1.1 -y"],
        [
            'RUN . /etc/os-release && if [ "$VERSION_ID" = "18.04" ] || '
            '[ "$VERSION_ID" = "20.04" ] ; then \\',
            "    apt-get install libssl1.1 -y; \\",
            "    fi",
        ],
        [
            "# libssl1.1 (OpenSSL 1.1 runtime) exists only through focal; on",
            "# jammy the base already ships libssl3 and the edgemlsdk debs "
            "carry their",
            "# own OpenSSL 3.x (openssl.deb). Gate on the base's own "
            "/etc/os-release:",
            "# the OS build-arg is out of scope in RUN (declared only "
            "before FROM).",
            'RUN . /etc/os-release && if [ "$VERSION_ID" = "18.04" ] || '
            '[ "$VERSION_ID" = "20.04" ] ; then \\',
            "    apt-get install libssl1.1 -y; \\",
            "    fi",
        ],
    ]
)

_CHUNKS = st.lists(
    st.one_of(
        st.tuples(st.just("benign"), _BENIGN_DOCKER_LINES),
        st.tuples(st.just("target"), _TARGET_BLOCKS),
    ),
    max_size=12,
)

_SEPARATOR = "RUN apt update -y"


class TestMaskingPreservationProperty:
    """Property (ii): for generated Dockerfile line sequences containing
    zero or more marked target steps (both shapes), the masking helper
    removes exactly the target step(s) — including any contiguous comment
    header directly above them — and nothing else (mirrors the
    ``edgemlsdk_pythondev`` masking-helper property pattern)."""

    @settings(max_examples=200, deadline=None)
    @given(chunks=_CHUNKS)
    def test_mask_removes_exactly_the_target_blocks(self, chunks):
        """**Validates: Requirements 3.1**"""
        lines = []
        expected = []
        for kind, payload in chunks:
            if kind == "benign":
                lines.append(payload)
                expected.append(payload)
            else:
                # A benign comment directly above a target would be
                # (deliberately) absorbed; keep the expectation exact by
                # separating them with a non-comment line.
                if lines and lines[-1].lstrip().startswith("#"):
                    lines.append(_SEPARATOR)
                    expected.append(_SEPARATOR)
                lines.extend(payload)
        assert mask_libssl_install_step(lines) == expected

    @settings(max_examples=200, deadline=None)
    @given(lines=st.lists(_BENIGN_DOCKER_LINES, max_size=20))
    def test_mask_is_identity_without_a_target_step(self, lines):
        """**Validates: Requirements 3.1** — zero target steps means nothing
        is removed (all non-bug-condition lines preserved, comments
        included: absorption only ever happens above a target step)."""
        assert mask_libssl_install_step(lines) == lines

    def test_comment_header_directly_above_target_is_absorbed(self):
        """**Validates: Requirements 3.1** — the fixed form's traveling
        comment block is masked with the step (the both-shapes subtlety)."""
        lines = [
            "RUN apt update -y",
            "# gate rationale line 1",
            "# gate rationale line 2",
            "RUN apt-get install libssl1.1 -y",
            "RUN apt update -y",
        ]
        assert mask_libssl_install_step(lines) == [
            "RUN apt update -y",
            "RUN apt update -y",
        ]

    def test_unrelated_comment_not_contiguous_with_target_survives(self):
        """**Validates: Requirements 3.1** — a comment separated from the
        target step by any non-comment line is NOT absorbed."""
        lines = [
            "# unrelated header comment",
            "RUN apt update -y",
            "RUN apt-get install libssl1.1 -y",
            "# trailing unrelated comment",
        ]
        assert mask_libssl_install_step(lines) == [
            "# unrelated header comment",
            "RUN apt update -y",
            "# trailing unrelated comment",
        ]


# --------------------------------------------------------------------------- #
# Property (iii): apt-line tokenization over generated install lines
# --------------------------------------------------------------------------- #
_APT_FLAGS = st.sampled_from(
    [
        "-y",
        "-qq",
        "--no-install-recommends",
        "--only-upgrade",
        "--fix-missing",
        "--assume-yes",
    ]
)

_APT_ITEMS = st.lists(
    st.one_of(
        st.tuples(st.just("flag"), _APT_FLAGS),
        st.tuples(st.just("pkg"), _RANDOM_PKG_NAMES),
    ),
    min_size=1,
    max_size=10,
).filter(lambda items: any(kind == "pkg" for kind, _ in items))


class TestAptLineTokenizationProperty:
    """Property (iii): for generated apt install lines with random
    flag/package orderings and backslash continuations, tokenization is
    total (parses to exactly one step with exactly the generated packages,
    order preserved) and flags never classify as packages."""

    @settings(max_examples=300, deadline=None)
    @given(
        apt_cmd=st.sampled_from(["apt-get", "apt"]),
        pre_flags=st.lists(_APT_FLAGS, max_size=3),
        items=_APT_ITEMS,
        breaks=st.lists(st.booleans(), min_size=10, max_size=10),
    )
    def test_tokenization_is_total_and_flags_never_packages(
        self, apt_cmd, pre_flags, items, breaks
    ):
        """**Validates: Requirements 3.1** (parser totality underpinning
        both the bug-condition scan and the preservation mask)"""
        tokens = [apt_cmd] + pre_flags + ["install"] + [
            value for _, value in items
        ]
        # Random backslash-continuation layout: break (or not) after each
        # token; the logical instruction must reconstruct identically.
        text = "RUN"
        for i, tok in enumerate(tokens):
            if breaks[i % len(breaks)] and i < len(tokens) - 1:
                text += " " + tok + " \\\n   "
            else:
                text += " " + tok
        steps = apt_steps_in_text(text)
        assert len(steps) == 1, (
            f"tokenization not total: expected exactly one apt install "
            f"step, got {len(steps)} for {text!r}"
        )
        _lineno, _text, packages, _allow = steps[0]
        expected_pkgs = [value for kind, value in items if kind == "pkg"]
        assert packages == expected_pkgs
        assert not any(p.startswith("-") for p in packages), (
            f"a flag classified as a package in {packages}"
        )


# --------------------------------------------------------------------------- #
# Property (iv): the reachability model over generated allowlists/bases
# --------------------------------------------------------------------------- #
_RELEASES = st.one_of(
    st.sampled_from(["16.04", "18.04", "20.04", "22.04", "24.04", "25.10"]),
    st.tuples(st.integers(4, 99), st.integers(0, 99)).map(
        lambda t: f"{t[0]}.{t[1]:02d}"
    ),
)

_ALLOWLISTS = st.frozensets(_RELEASES, min_size=1, max_size=4)


def _guarded_step_text(allowlist):
    guard = " || ".join(
        f'[ "$VERSION_ID" = "{v}" ]' for v in sorted(allowlist)
    )
    return (
        "RUN . /etc/os-release && if " + guard + " ; then \\\n"
        "    apt-get install some-pkg -y; \\\n"
        "    fi"
    )


class TestReachabilityModelProperty:
    """Property (iv): for generated release allowlists and base versions, a
    guarded step is reachable iff the base is in the allowlist, and an
    unconditional step is always reachable — so the fixed step is
    22.04-unreachable and 18.04/20.04-reachable by construction (Req 3.2's
    old-base contract)."""

    @settings(max_examples=300, deadline=None)
    @given(allowlist=_ALLOWLISTS, base=_RELEASES)
    def test_guarded_step_reachable_iff_base_in_allowlist(
        self, allowlist, base
    ):
        """**Validates: Requirements 3.2**"""
        text = _guarded_step_text(allowlist)
        steps = apt_steps_in_text(text)
        assert len(steps) == 1
        lineno, logical, packages, parsed_allowlist = steps[0]
        assert parsed_allowlist == allowlist, (
            f"guard parsing lost the allowlist: built {sorted(allowlist)}, "
            f"parsed {parsed_allowlist and sorted(parsed_allowlist)} from "
            f"{logical!r}"
        )
        step = AptStep("<generated>", lineno, logical, packages,
                       parsed_allowlist)
        assert is_reachable(step, base) == (base in allowlist)

    @settings(max_examples=300, deadline=None)
    @given(base=_RELEASES)
    def test_unconditional_step_reachable_on_every_base(self, base):
        """**Validates: Requirements 3.2**"""
        text = "RUN apt-get install some-pkg -y"
        steps = apt_steps_in_text(text)
        assert len(steps) == 1
        lineno, logical, packages, parsed_allowlist = steps[0]
        assert parsed_allowlist is None
        assert guard_allowlist(logical) is None
        step = AptStep("<generated>", lineno, logical, packages, None)
        assert is_reachable(step, base)
