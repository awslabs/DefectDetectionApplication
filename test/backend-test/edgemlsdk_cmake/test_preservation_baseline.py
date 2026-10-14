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
"""Observation-first preservation baseline for ``edgemlsdk-cmake-pin-failure``
(task 2) — **Property 2: Preservation — Non-CMake Lines and JP6 Unchanged**.

Methodology: capture the UNFIXED tree's bytes as goldens BEFORE any production
edit (first run: golden absent -> capture; subsequent runs: byte-for-byte
assert). These tests MUST PASS on the unfixed tree; the goldens under
``edgemlsdk_cmake/baselines/`` are FROZEN from that point — task 5.2 re-runs
these SAME tests unchanged against the fixed tree to prove that ONLY the CMake
install block/function lines changed (F(X) = F'(X) for every non-bug-condition
input).

Diff-scoping goldens (design "Preservation Checking" pseudocode):

(a) full-file sha256 of ``src/edgemlsdk/Dockerfile.jp6`` — bit-identical
    post-fix (Req 3.1);
(b) CMake-block-masked view of ``src/edgemlsdk/Dockerfile`` — masks only the
    if/else Kitware CMake ``RUN`` block plus its comment header (Req 3.2);
(c) CMake-block-masked view of ``src/edgemlsdk/Dockerfile.jp5`` — masks only
    the "── 2. CMake ──" ``RUN`` block plus its comment header (Req 3.2);
(d) ``install_cmake``-masked view of
    ``src/edgemlsdk/src/utilities/machine_setup.sh`` — masks only the
    ``install_cmake`` function (Req 3.5).

Plus a Hypothesis masking-helper sanity property (the helper removes exactly
the CMake-install block / function lines and nothing else — mirrors the
``test_masking_removed_the_from_lines`` pattern of the docker security suite)
and verbatim assertions that ``install_cmake``'s "already installed" skip path
and its non-18.04 ``check_and_install_package cmake`` else-branch exist in the
tree (Req 3.5 semantics survive the fix).

**Validates: Requirements 3.1, 3.2, 3.4, 3.5**

Run (finite, non-watch):
    PYTHONPATH=src/backend:test/backend-test \
        pytest test/backend-test/edgemlsdk_cmake/ --noconftest
"""
import re

from hypothesis import assume, given, settings
from hypothesis import strategies as st

from _cmake_preservation_support import (
    GOLDEN_DOCKERFILE_JP5_MASKED,
    GOLDEN_DOCKERFILE_MASKED,
    GOLDEN_JP6_SHA256,
    GOLDEN_MACHINE_SETUP_MASKED,
    capture_or_assert_text,
    cmake_install_block_ranges,
    mask_cmake_install_blocks,
    mask_shell_function,
    sha256_hex,
    shell_function_range,
)
from _cmake_support import (
    DOCKERFILE_JP5_REL,
    DOCKERFILE_JP6_REL,
    DOCKERFILE_REL,
    KITWARE_APT_HOST,
    KITWARE_PIN_MARKER,
    MACHINE_SETUP_REL,
    REPO_ROOT,
    extract_function,
    read_repo_file,
)


# --------------------------------------------------------------------------- #
# Goldens a–d: capture on the unfixed tree, byte-for-byte assert thereafter
# --------------------------------------------------------------------------- #
class TestFrozenGoldens:
    """The four diff-scoping goldens. Captured once from the UNFIXED tree and
    frozen; task 5.2 re-runs these unchanged against the fixed tree."""

    def test_jp6_dockerfile_sha256_golden(self):
        """**Validates: Requirements 3.1** — golden (a): Dockerfile.jp6 must
        remain bit-identical post-fix (JP6 is the ¬C anchor, never touched)."""
        digest = sha256_hex(REPO_ROOT, DOCKERFILE_JP6_REL)
        capture_or_assert_text(GOLDEN_JP6_SHA256, digest + "\n")

    def test_dockerfile_cmake_masked_golden(self):
        """**Validates: Requirements 3.2** — golden (b): every line of
        src/edgemlsdk/Dockerfile EXCEPT the if/else Kitware CMake RUN block
        (and its comment header) is byte-for-byte frozen."""
        lines = read_repo_file(DOCKERFILE_REL).splitlines()
        masked = mask_cmake_install_blocks(lines)
        capture_or_assert_text(GOLDEN_DOCKERFILE_MASKED, "\n".join(masked) + "\n")

    def test_dockerfile_jp5_cmake_masked_golden(self):
        """**Validates: Requirements 3.2** — golden (c): every line of
        src/edgemlsdk/Dockerfile.jp5 EXCEPT the "── 2. CMake ──" RUN block
        (and its comment header) is byte-for-byte frozen — including the
        Python step's Kitware residue cleanup lines, which are explicitly
        preserved."""
        lines = read_repo_file(DOCKERFILE_JP5_REL).splitlines()
        masked = mask_cmake_install_blocks(lines)
        capture_or_assert_text(
            GOLDEN_DOCKERFILE_JP5_MASKED, "\n".join(masked) + "\n"
        )

    def test_machine_setup_install_cmake_masked_golden(self):
        """**Validates: Requirements 3.5** — golden (d): every line of
        machine_setup.sh EXCEPT the install_cmake function is byte-for-byte
        frozen (all other functions and the top-level package installs are
        preserved)."""
        lines = read_repo_file(MACHINE_SETUP_REL).splitlines()
        masked = mask_shell_function(lines, "install_cmake")
        capture_or_assert_text(
            GOLDEN_MACHINE_SETUP_MASKED, "\n".join(masked) + "\n"
        )


# --------------------------------------------------------------------------- #
# Masking sanity on the REAL files (mirrors test_masking_removed_the_from_lines)
# --------------------------------------------------------------------------- #
class TestMaskingSanityOnRealFiles:
    """The masked goldens are genuine non-CMake views, not whole-file copies,
    and the mask caught every Kitware line. Passes on both trees (on the fixed
    tree there are no Kitware lines anywhere, and the fixed CMake block is
    masked by its GitHub-release-installer signature)."""

    def _assert_masked_dockerfile(self, rel_path):
        full = read_repo_file(rel_path).splitlines()
        ranges = cmake_install_block_ranges(full)
        assert len(ranges) == 1, (
            f"{rel_path}: expected exactly ONE CMake install block, found "
            f"{len(ranges)}: {ranges}"
        )
        masked = mask_cmake_install_blocks(full)
        (a, b), = ranges
        # Masking dropped exactly the block's physical lines and nothing else.
        assert len(masked) == len(full) - (b - a + 1)
        assert masked == full[:a] + full[b + 1 :]
        # Every Kitware apt reference sat inside the masked block.
        assert not any(
            KITWARE_APT_HOST in ln or KITWARE_PIN_MARKER in ln for ln in masked
        ), f"{rel_path}: Kitware line survived the CMake-block mask"

    def test_dockerfile_mask_is_exact(self):
        """**Validates: Requirements 3.2**"""
        self._assert_masked_dockerfile(DOCKERFILE_REL)

    def test_dockerfile_jp5_mask_is_exact(self):
        """**Validates: Requirements 3.2**"""
        self._assert_masked_dockerfile(DOCKERFILE_JP5_REL)

    def test_machine_setup_mask_is_exact(self):
        """**Validates: Requirements 3.5**"""
        full = read_repo_file(MACHINE_SETUP_REL).splitlines()
        start, end = shell_function_range(full, "install_cmake")
        masked = mask_shell_function(full, "install_cmake")
        assert masked == full[:start] + full[end + 1 :]
        # Every Kitware apt reference sat inside install_cmake.
        assert not any(KITWARE_APT_HOST in ln for ln in masked), (
            "machine_setup.sh: apt.kitware.com line survived the "
            "install_cmake mask"
        )


# --------------------------------------------------------------------------- #
# Req 3.5 semantics: skip path and non-18.04 else-branch survive the fix
# --------------------------------------------------------------------------- #
class TestInstallCmakePreservedSemantics:
    """The two install_cmake behaviors the fix must keep verbatim (design
    Change 3 keeps both lines): the "already installed" skip path and the
    non-18.04 ``check_and_install_package cmake`` else-branch (Ubuntu archive,
    not Kitware — not part of C). Passes on both the unfixed and fixed tree."""

    def test_already_installed_skip_path_exists_verbatim(self):
        """**Validates: Requirements 3.5**"""
        body = extract_function(read_repo_file(MACHINE_SETUP_REL), "install_cmake")
        assert 'echo "CMake is already installed"' in body, (
            "install_cmake lost its 'already installed' skip path (Req 3.5)"
        )

    def test_non_1804_else_branch_exists_verbatim(self):
        """**Validates: Requirements 3.5**"""
        body = extract_function(read_repo_file(MACHINE_SETUP_REL), "install_cmake")
        assert "check_and_install_package cmake;" in body, (
            "install_cmake lost its non-18.04 'check_and_install_package "
            "cmake' else-branch (Req 3.5)"
        )


# --------------------------------------------------------------------------- #
# Masking-helper sanity PROPERTY tests (generated line sequences)
# --------------------------------------------------------------------------- #
# Smart generators: benign lines never end in a continuation backslash (so a
# preceding logical line cannot absorb the injected block), never carry a
# CMake-install signature, and the line adjacent to the injected block is
# never a comment (comments directly above a CMake RUN are its header by the
# masking semantics — that absorption is exercised by the header strategy).
_BENIGN_DOCKER_LINES = st.sampled_from(
    [
        "ENV DEBIAN_FRONTEND noninteractive",
        "ARG PLATFORM",
        "RUN apt-get update",
        "RUN apt-get install -y curl wget gpg",
        "RUN pip3 install meson",
        "COPY src ./src",
        "# a benign comment about something else",
        "RUN echo done",
        "",
        "ENV LD_LIBRARY_PATH=/usr/local/lib/",
    ]
)

# Both the UNFIXED (Kitware apt) and FIXED (GitHub release installer) shapes of
# a CMake install block — the helper must mask either, so the same frozen
# golden works before and after the fix.
_UNFIXED_CMAKE_BLOCK = [
    "RUN wget -O - https://apt.kitware.com/keys/kitware-archive-latest.asc | tee /etc/apt/trusted.gpg.d/kitware.gpg && \\",
    "    apt-get update && \\",
    "    apt-get install cmake=3.21.3-0kitware1ubuntu20.04.1 cmake-data=3.21.3-0kitware1ubuntu20.04.1 -y",
]
_FIXED_CMAKE_BLOCK = [
    "RUN CMAKE_VER=3.31.6 && \\",
    '    wget -q "https://github.com/Kitware/CMake/releases/download/v${CMAKE_VER}/cmake-${CMAKE_VER}-linux-x86_64.sh" -O /tmp/cmake.sh && \\',
    "    sh /tmp/cmake.sh --skip-license --prefix=/usr/local && \\",
    "    rm -f /tmp/cmake.sh && \\",
    "    cmake --version",
]
_CMAKE_BLOCKS = st.sampled_from([_UNFIXED_CMAKE_BLOCK, _FIXED_CMAKE_BLOCK])

_HEADER_COMMENTS = st.lists(
    st.sampled_from(
        [
            "# Install the latest versions of CMake",
            "# ── 2. CMake 3.21.3 from Kitware ──",
            "# Install pinned CMake 3.x from the official upstream release binary",
        ]
    ),
    min_size=0,
    max_size=3,
)


class TestMaskingHelperProperty:
    """Property: for generated line sequences, ``mask_cmake_install_blocks``
    removes exactly the CMake-install block (RUN instruction + its contiguous
    comment header) and nothing else; likewise ``mask_shell_function`` removes
    exactly the named function. Mirrors the
    ``test_masking_removed_the_from_lines`` sanity pattern."""

    @settings(max_examples=200, deadline=None)
    @given(
        prefix=st.lists(_BENIGN_DOCKER_LINES, max_size=8),
        header=_HEADER_COMMENTS,
        block=_CMAKE_BLOCKS,
        suffix=st.lists(_BENIGN_DOCKER_LINES, max_size=8),
    )
    def test_mask_removes_exactly_the_cmake_block(
        self, prefix, header, block, suffix
    ):
        """**Validates: Requirements 3.2, 3.4**"""
        # A benign comment directly above the injected header would be
        # absorbed into the header by design — exclude that layout so the
        # expectation is exact (header absorption itself is exercised via the
        # generated header lines).
        assume(not prefix or not prefix[-1].lstrip().startswith("#"))
        lines = prefix + header + block + suffix
        assert mask_cmake_install_blocks(lines) == prefix + suffix

    @settings(max_examples=200, deadline=None)
    @given(lines=st.lists(_BENIGN_DOCKER_LINES, max_size=20))
    def test_mask_is_identity_without_a_cmake_block(self, lines):
        """**Validates: Requirements 3.2, 3.4** — no CMake install block means
        nothing is removed (all non-bug-condition lines preserved)."""
        assert mask_cmake_install_blocks(lines) == lines

    @settings(max_examples=200, deadline=None)
    @given(
        prefix=st.lists(
            st.sampled_from(
                [
                    "#!/bin/bash",
                    "set -e",
                    "check_and_install_package curl",
                    'echo "setting up machine"',
                    "install_powershell",
                    "",
                ]
            ),
            max_size=8,
        ),
        body=st.lists(
            st.sampled_from(
                [
                    '    if ! dpkg -s "cmake" >/dev/null 2>&1; then',
                    "        apt-get install cmake -y;",
                    "        check_and_install_package cmake;",
                    '        echo "CMake is already installed"',
                    "    fi",
                ]
            ),
            min_size=1,
            max_size=8,
        ),
        suffix=st.lists(
            st.sampled_from(
                [
                    "install_swig()",
                    "{",
                    '    echo "swig"',
                    "}",
                    "check_and_install_package ninja-build",
                    "",
                ]
            ),
            max_size=8,
        ),
    )
    def test_mask_removes_exactly_the_named_function(self, prefix, body, suffix):
        """**Validates: Requirements 3.5**"""
        function = ["install_cmake()", "{"] + body + ["}"]
        lines = prefix + function + suffix
        assert mask_shell_function(lines, "install_cmake") == prefix + suffix
