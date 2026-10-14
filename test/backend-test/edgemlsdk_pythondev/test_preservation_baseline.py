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
``edgemlsdk-python-dev-ubuntu2204`` (task 2) — **Property 2: Preservation —
All Other Lines and Sibling Dockerfiles Unchanged**.

Methodology: capture the UNFIXED tree's bytes as goldens BEFORE any
production edit (first run: golden absent -> capture; subsequent runs:
byte-for-byte assert). These tests MUST PASS on the unfixed tree; the goldens
under ``edgemlsdk_pythondev/baselines/`` are FROZEN from that point —
task 5.2 re-runs these SAME tests unchanged against the fixed tree to prove
that ONLY line 286's python-dev install line changed (F(X) = F'(X) for every
non-bug-condition input).

Diff-scoping goldens (design "Preservation Checking" pseudocode):

(a) python-dev-line-masked view of ``src/edgemlsdk/Dockerfile`` — masks ONLY
    the retired-Python-package install line (line 286's logical RUN); the
    mask matches the target step by its apt package tokens (``python-dev``
    unfixed OR ``python-dev-is-python3`` fixed) so the SAME frozen golden
    works on both sides of the fix. The masked view proves the prior spec's
    CMake block, the Python 3.11 source build, the neighboring
    ``rapidjson-dev libre2-dev`` install, and the ``rm /usr/bin/python``
    step survive verbatim (Req 3.1);
(b) full-file sha256 of ``src/edgemlsdk/Dockerfile.jp5`` — bit-identical
    post-fix (Req 3.2, design Decision 2);
(c) full-file sha256 of ``src/edgemlsdk/Dockerfile.jp6`` — bit-identical
    post-fix (Req 3.3).

Plus a mask-exactness assertion on the real file (the masked view differs
from the raw file by exactly the one target line — count and content
asserted, so the mask cannot hide collateral edits) and the three Hypothesis
helper properties from design: masking preservation, retired-token
classifier, and apt-line tokenization.

All parsing is TEXT only — no ``docker``, ``subprocess``, or shell-out
anywhere in this package.

**Validates: Requirements 3.1, 3.2, 3.3**

Run (finite, non-watch):
    PYTHONPATH=src/backend:test/backend-test \
        pytest test/backend-test/edgemlsdk_pythondev/ --noconftest
"""
from hypothesis import given, settings
from hypothesis import strategies as st

from _pythondev_preservation_support import (
    FIXED_TRITON_STEP_SHAPE,
    GOLDEN_DOCKERFILE_MASKED,
    GOLDEN_JP5_SHA256,
    GOLDEN_JP6_SHA256,
    TARGET_STEP_TOKENS,
    UNFIXED_TRITON_STEP,
    capture_or_assert_text,
    is_retired_transitional_python_package,
    mask_pythondev_install_line,
    pythondev_install_line_ranges,
    sha256_hex,
)
from _pythondev_support import (
    DOCKERFILE_JP5_REL,
    DOCKERFILE_JP6_REL,
    DOCKERFILE_REL,
    REPO_ROOT,
    RETIRED_TRANSITIONAL_PYTHON_PACKAGES,
    apt_install_steps,
    read_repo_file,
    retired_token_sites,
)


# --------------------------------------------------------------------------- #
# Goldens a–c: capture on the unfixed tree, byte-for-byte assert thereafter
# --------------------------------------------------------------------------- #
class TestFrozenGoldens:
    """The three diff-scoping goldens. Captured once from the UNFIXED tree
    and frozen; task 5.2 re-runs these unchanged against the fixed tree."""

    def test_dockerfile_pythondev_masked_golden(self):
        """**Validates: Requirements 3.1** — golden (a): every line of
        src/edgemlsdk/Dockerfile EXCEPT the retired-Python-package install
        line (line 286's logical RUN) is byte-for-byte frozen — including
        the prior spec's CMake block, the Python 3.11 source build, the
        rapidjson-dev/libre2-dev step, and the rm /usr/bin/python step."""
        lines = read_repo_file(DOCKERFILE_REL).splitlines()
        masked = mask_pythondev_install_line(lines)
        capture_or_assert_text(
            GOLDEN_DOCKERFILE_MASKED, "\n".join(masked) + "\n"
        )

    def test_jp5_dockerfile_sha256_golden(self):
        """**Validates: Requirements 3.2** — golden (b): Dockerfile.jp5 must
        remain bit-identical post-fix (design Decision 2: its python-dev
        token resolves on the digest-pinned focal base; untouched)."""
        digest = sha256_hex(REPO_ROOT, DOCKERFILE_JP5_REL)
        capture_or_assert_text(GOLDEN_JP5_SHA256, digest + "\n")

    def test_jp6_dockerfile_sha256_golden(self):
        """**Validates: Requirements 3.3** — golden (c): Dockerfile.jp6 must
        remain bit-identical post-fix (the ¬C JP6 anchor, never touched)."""
        digest = sha256_hex(REPO_ROOT, DOCKERFILE_JP6_REL)
        capture_or_assert_text(GOLDEN_JP6_SHA256, digest + "\n")


# --------------------------------------------------------------------------- #
# Mask exactness on the REAL file — the mask cannot hide collateral edits
# --------------------------------------------------------------------------- #
class TestMaskExactnessOnRealFile:
    """The masked view differs from the raw file by exactly the ONE target
    line, whose content is one of the two admissible shapes (unfixed
    ``python-dev`` / fixed ``python-dev-is-python3``). Passes on both trees."""

    def test_mask_removes_exactly_one_line_with_admissible_content(self):
        """**Validates: Requirements 3.1**"""
        full = read_repo_file(DOCKERFILE_REL).splitlines()
        ranges = pythondev_install_line_ranges(full)
        assert len(ranges) == 1, (
            f"{DOCKERFILE_REL}: expected exactly ONE target python-dev "
            f"install step, found {len(ranges)}: {ranges}"
        )
        (a, b), = ranges
        assert a == b, (
            f"{DOCKERFILE_REL}: the target install step spans physical "
            f"lines {a + 1}-{b + 1}; the fix scope is a single physical "
            f"line (design Change 1)."
        )
        masked = mask_pythondev_install_line(full)
        # Count: exactly the one physical line dropped, nothing else.
        assert len(masked) == len(full) - 1
        assert masked == full[:a] + full[a + 1 :]
        # Content: the dropped line is one of the two admissible shapes.
        dropped = full[a].strip()
        assert dropped in (UNFIXED_TRITON_STEP, FIXED_TRITON_STEP_SHAPE), (
            f"{DOCKERFILE_REL}:{a + 1}: the masked line is {dropped!r} — "
            f"expected the unfixed {UNFIXED_TRITON_STEP!r} or the fixed "
            f"{FIXED_TRITON_STEP_SHAPE!r}"
        )

    def test_masked_view_has_no_target_or_retired_tokens(self):
        """**Validates: Requirements 3.1** — the mask caught every target
        line: the masked view contains no retired transitional Python
        package token and no target-step token in any apt install step."""
        full = read_repo_file(DOCKERFILE_REL).splitlines()
        masked_text = "\n".join(mask_pythondev_install_line(full)) + "\n"
        assert not retired_token_sites(masked_text), (
            f"{DOCKERFILE_REL}: a retired-token apt install step survived "
            f"the python-dev-line mask"
        )
        for lineno, run_text, packages in apt_install_steps(masked_text):
            leaked = TARGET_STEP_TOKENS.intersection(packages)
            assert not leaked, (
                f"masked view line {lineno}: target token(s) {leaked} "
                f"survived the mask in step: {run_text.strip()}"
            )


# --------------------------------------------------------------------------- #
# Property (i): masking preservation over generated line sequences
# --------------------------------------------------------------------------- #
# Smart generators: benign lines never end in a continuation backslash (so a
# preceding logical line cannot absorb an injected target block) and never
# request a target-step token; adversarial neighbors exercise the
# python-dev / python-dev-is-python3 prefix trap through the token-boundary
# scan (python3-dev and python-dev-tools lines are NOT masked).
_BENIGN_DOCKER_LINES = st.sampled_from(
    [
        "ENV DEBIAN_FRONTEND noninteractive",
        "ARG OS",
        "RUN apt-get update",
        "RUN apt-get install rapidjson-dev libre2-dev -y",
        "RUN apt-get install python3-dev -y",
        "RUN apt-get install python-dev-tools-not-really -y",
        "RUN apt-get install ffmpeg -y",
        "RUN pip3 install meson",
        "# Install Triton Server and it's dependencies",
        "RUN rm /usr/bin/python && ln -s /usr/bin/python3.11 /usr/bin/python",
        "COPY src ./src",
        "",
    ]
)

# Both the UNFIXED (python-dev) and FIXED (python-dev-is-python3) shapes of
# the target install step — the helper must mask either, so the same frozen
# golden works before and after the fix. Continuation shapes exercise
# logical-RUN reconstruction.
_TARGET_BLOCKS = st.sampled_from(
    [
        ["RUN apt-get install python-dev -y"],
        ["RUN apt-get install python-dev-is-python3 -y"],
        ["RUN apt-get update && \\", "    apt-get install python-dev -y"],
        ["RUN apt-get install \\", "    python-dev-is-python3 -y"],
    ]
)

_CHUNKS = st.lists(
    st.one_of(
        st.tuples(st.just("benign"), _BENIGN_DOCKER_LINES),
        st.tuples(st.just("target"), _TARGET_BLOCKS),
    ),
    max_size=12,
)


class TestMaskingPreservationProperty:
    """Property (i): for generated Dockerfile line sequences containing zero
    or more marked target lines, ``mask_pythondev_install_line`` removes
    exactly the target line(s) and nothing else (mirrors the
    ``edgemlsdk_cmake`` masking-helper property pattern)."""

    @settings(max_examples=200, deadline=None)
    @given(chunks=_CHUNKS)
    def test_mask_removes_exactly_the_target_lines(self, chunks):
        """**Validates: Requirements 3.1**"""
        lines = []
        expected = []
        for kind, payload in chunks:
            if kind == "benign":
                lines.append(payload)
                expected.append(payload)
            else:
                lines.extend(payload)
        assert mask_pythondev_install_line(lines) == expected

    @settings(max_examples=200, deadline=None)
    @given(lines=st.lists(_BENIGN_DOCKER_LINES, max_size=20))
    def test_mask_is_identity_without_a_target_line(self, lines):
        """**Validates: Requirements 3.1** — zero target lines means nothing
        is removed (all non-bug-condition lines preserved)."""
        assert mask_pythondev_install_line(lines) == lines


# --------------------------------------------------------------------------- #
# Property (ii): retired-token classifier over generated package tokens
# --------------------------------------------------------------------------- #
_NAME_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789+.-"

_RANDOM_PKG_NAMES = st.text(
    alphabet=_NAME_ALPHABET, min_size=1, max_size=24
).filter(lambda s: not s.startswith("-"))

# Adversarial prefix/suffix mutations of the retired names (the
# python-dev / python-dev-is-python3 prefix trap and friends).
_ADVERSARIAL_TOKENS = st.sampled_from(
    [
        "python-dev-is-python3",
        "libpython-dev-foo",
        "python-devtools",
        "python-dev-tools",
        "python3-dev",
        "python3",
        "python-is-python3",
        "python2-dev",
        "python-pip3",
        "python-setuptools-doc",
        "libpython-dev",
        "mypython-dev",
    ]
)

_RETIRED_TOKENS = st.sampled_from(sorted(RETIRED_TRANSITIONAL_PYTHON_PACKAGES))

_TOKENS = st.one_of(_RETIRED_TOKENS, _ADVERSARIAL_TOKENS, _RANDOM_PKG_NAMES)


class TestRetiredTokenClassifierProperty:
    """Property (ii): the classifier flags a token iff it is EXACTLY a member
    of the retired set — never by prefix/suffix/substring."""

    @settings(max_examples=300, deadline=None)
    @given(token=_TOKENS)
    def test_classifier_flags_iff_exact_member(self, token):
        """**Validates: Requirements 3.1** (token-boundary discipline)"""
        expected = token in RETIRED_TRANSITIONAL_PYTHON_PACKAGES
        assert is_retired_transitional_python_package(token) == expected

    @settings(max_examples=300, deadline=None)
    @given(token=_TOKENS)
    def test_scan_pipeline_flags_iff_exact_member(self, token):
        """**Validates: Requirements 3.1** — end-to-end through the real
        apt-scan pipeline: an install line requesting the token yields a
        retired-token site iff the token is exactly in the retired set."""
        text = f"RUN apt-get install {token} -y"
        sites = retired_token_sites(text)
        if token in RETIRED_TRANSITIONAL_PYTHON_PACKAGES:
            assert [(n, tok) for n, tok, _ in sites] == [(1, token)]
        else:
            assert sites == []


# --------------------------------------------------------------------------- #
# Property (iii): apt-line tokenization over generated install lines
# --------------------------------------------------------------------------- #
_APT_FLAGS = st.sampled_from(
    ["-y", "-qq", "--no-install-recommends", "--fix-missing", "--assume-yes"]
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
        """**Validates: Requirements 3.1** (parser totality underpinning both
        the bug-condition scan and the preservation mask)"""
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
        steps = apt_install_steps(text)
        assert len(steps) == 1, (
            f"tokenization not total: expected exactly one apt install "
            f"step, got {len(steps)} for {text!r}"
        )
        _, _, packages = steps[0]
        expected_pkgs = [value for kind, value in items if kind == "pkg"]
        assert packages == expected_pkgs
        assert not any(p.startswith("-") for p in packages), (
            f"a flag classified as a package in {packages}"
        )
