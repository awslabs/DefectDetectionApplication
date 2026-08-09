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
"""Version-range and pattern property tests for ``edgemlsdk-cmake-pin-failure``
(task 5.3) — **Property 3: No-4.x — Pinned Version Range**.

For any pinned CMake version string parsed from the CMake install steps of the
fixed files (including the untouched ``Dockerfile.jp6``), the version v SHALL
satisfy 3.21 <= v < 4.0 — preserving the Triton-compatible major/minor range
the original 3.21.3 pin provided and guaranteeing no 4.x can be installed
(CMake 4.0 removed ``cmake_minimum_required(VERSION < 3.5)`` support, which
breaks Triton's transitively fetched dependencies).

Three layers (design "Property-Based Tests"):

1. A Hypothesis property over generated version strings (including edge
   shapes like ``3.9``, ``3.21.0``, ``4.0.0-rc1``) validating the
   3.21 <= v < 4.0 comparator against a tuple-based oracle.
2. Direct assertions that every concrete ``CMAKE_VER=<x.y.z>`` parsed from
   the three fixed files AND ``Dockerfile.jp6`` satisfies the range, and that
   NO residual apt ``cmake=<ver>`` pin remains anywhere in those files.
3. The arch-selection property: for any generated ``uname -m`` value, the
   modeled arch selection yields ``x86_64`` for ``x86_64`` and ``aarch64``
   otherwise — matching JP6's behavior exactly — and each fixed file's CMake
   install step carries exactly that conditional shape.

Import-light (helpers from ``_cmake_support`` only) so the tests run under
``pytest ... --noconftest``.

Run (finite, non-watch):
    PYTHONPATH=src/backend:test/backend-test \
        pytest test/backend-test/edgemlsdk_cmake/ --noconftest
"""
import re

from hypothesis import example, given, settings
from hypothesis import strategies as st

from _cmake_support import (
    AFFECTED_FILES,
    DOCKERFILE_JP6_REL,
    MACHINE_SETUP_REL,
    cmake_install_steps,
    extract_function,
    read_repo_file,
)

# The four files whose pinned CMake versions Property 3 governs: the three
# fixed files plus the untouched JP6 anchor.
VERSIONED_FILES = AFFECTED_FILES + (DOCKERFILE_JP6_REL,)

# ``CMAKE_VER=3.31.6`` — the pinned version assignment in the JP6 pattern.
CMAKE_VER_RE = re.compile(r"\bCMAKE_VER=(\S+)")

# A residual apt version pin such as ``cmake=3.21.3-0kitware1ubuntu20.04.1``
# or ``cmake-data=...`` — there must be none after the fix.
APT_CMAKE_PIN_RE = re.compile(r"\bcmake(?:-data)?=(\S+)")

# Leading numeric components of a version string; any pre-release/build
# suffix (``-rc1``, ``+build5``) begins at the first non-digit/non-dot char.
VERSION_SHAPE_RE = re.compile(r"^(\d+)\.(\d+)(?:\.(\d+))?(?:[-+.].*)?$")


# --------------------------------------------------------------------------- #
# The comparator under test (models design fix-checking: 3.21 <= v < 4.0)
# --------------------------------------------------------------------------- #
def in_triton_compatible_range(version):
    """True iff version v satisfies 3.21 <= v < 4.0.

    Only the numeric major/minor components decide the range. A pre-release
    suffix never rescues an out-of-range major: ``4.0.0-rc1`` orders before
    4.0.0 in semver terms, but it IS a CMake 4.x and must be rejected — the
    property is "no 4.x can be installed", not semver ordering.
    """
    m = VERSION_SHAPE_RE.match(version)
    if not m:
        return False
    major, minor = int(m.group(1)), int(m.group(2))
    return major == 3 and minor >= 21


# --------------------------------------------------------------------------- #
# The arch-selection model under test (JP6 behavior, exactly)
# --------------------------------------------------------------------------- #
def select_cmake_arch(uname_m):
    """Model of the JP6 arch selection: ``x86_64`` for ``x86_64``, ``aarch64``
    for everything else."""
    return "x86_64" if uname_m == "x86_64" else "aarch64"


# The literal conditional shape shared by all four files (whitespace-tolerant;
# the tested subject is "$ARCH_TAG" in the Dockerfiles and $(uname -m) in the
# host script — either way a `uname -m` derived value):
#   if [ "$ARCH_TAG" = "x86_64" ]; then CM_ARCH=x86_64; else CM_ARCH=aarch64; fi
#   if [ $(uname -m) = "x86_64" ]; then CM_ARCH=x86_64; else CM_ARCH=aarch64; fi
ARCH_CONDITIONAL_RE = re.compile(
    r'if\s+\[\s+(?:"\$ARCH_TAG"|\$\(uname -m\))\s+=\s+"x86_64"\s+\];\s*'
    r"then\s+CM_ARCH=x86_64;\s*else\s+CM_ARCH=aarch64;\s*fi"
)


# --------------------------------------------------------------------------- #
# Layer 1 — Hypothesis property over generated version strings
# --------------------------------------------------------------------------- #
_SUFFIXES = st.sampled_from(["", "-rc1", "-rc2", "-beta1", "+build5", ".post1"])


@st.composite
def version_strings(draw):
    """major.minor[.patch][suffix] shapes spanning both sides of the range
    boundaries (majors 0..9, minors 0..99 cover 3.9, 3.20/3.21, 3.99, 4.0)."""
    major = draw(st.integers(min_value=0, max_value=9))
    minor = draw(st.integers(min_value=0, max_value=99))
    patch = draw(st.one_of(st.none(), st.integers(min_value=0, max_value=99)))
    suffix = draw(_SUFFIXES)
    base = f"{major}.{minor}" if patch is None else f"{major}.{minor}.{patch}"
    return (major, minor, base + suffix)


class TestVersionRangeComparatorProperty:
    """Property 3 comparator: v is in range iff major == 3 and minor >= 21
    (the tuple-based oracle), across generated version strings including the
    design's edge shapes ``3.9``, ``3.21.0``, and ``4.0.0-rc1``."""

    @settings(max_examples=300, deadline=None)
    @example((3, 9, "3.9"))        # two-component, minor 9 < 21 numerically-as-version
    @example((3, 21, "3.21.0"))    # inclusive lower bound
    @example((3, 21, "3.21"))      # inclusive lower bound, no patch
    @example((4, 0, "4.0.0-rc1"))  # pre-release of 4.0 is still a 4.x — rejected
    @example((3, 31, "3.31.6"))    # the actual pin in all four files
    @example((3, 99, "3.99.9"))    # top of the 3.x range
    @example((4, 0, "4.0.0"))      # exclusive upper bound
    @example((2, 99, "2.99.0"))    # below-3 major
    @given(version_strings())
    def test_comparator_matches_tuple_oracle(self, generated):
        """**Validates: Requirements 2.3, 3.3**"""
        major, minor, version = generated
        expected = major == 3 and minor >= 21
        assert in_triton_compatible_range(version) == expected, (
            f"comparator disagrees with the (major, minor) oracle for "
            f"{version!r}: expected in-range={expected}"
        )

    @settings(max_examples=100, deadline=None)
    @given(
        st.text(min_size=0, max_size=12).filter(
            lambda s: not VERSION_SHAPE_RE.match(s)
        )
    )
    def test_unparseable_strings_are_never_in_range(self, garbage):
        """**Validates: Requirements 2.3, 3.3** — a string that is not a
        ``major.minor[.patch]`` version can never be accepted as a valid
        Triton-compatible pin."""
        assert in_triton_compatible_range(garbage) is False


# --------------------------------------------------------------------------- #
# Layer 2 — direct assertions on the concrete pins in the four files
# --------------------------------------------------------------------------- #
class TestConcretePinnedVersions:
    """Every concrete ``CMAKE_VER=<x.y.z>`` in the three fixed files and
    ``Dockerfile.jp6`` satisfies 3.21 <= v < 4.0, and no residual apt
    ``cmake=<ver>`` pin remains anywhere in them."""

    def test_every_cmake_ver_pin_is_in_range(self):
        """**Validates: Requirements 2.3, 3.3**"""
        for rel in VERSIONED_FILES:
            text = read_repo_file(rel)
            pins = CMAKE_VER_RE.findall(text)
            assert pins, (
                f"{rel}: no CMAKE_VER=<x.y.z> pin found — the JP6-pattern "
                f"install step is missing its pinned version assignment"
            )
            for pin in pins:
                version = pin.strip("\"'")
                assert in_triton_compatible_range(version), (
                    f"{rel}: pinned CMAKE_VER={version} violates "
                    f"3.21 <= v < 4.0 (Triton-compatible range; no 4.x)"
                )

    def test_no_residual_apt_cmake_pin_remains(self):
        """**Validates: Requirements 2.3, 3.3** — the fix removed every apt
        ``cmake=<ver>`` / ``cmake-data=<ver>`` pin; none may remain in the
        three fixed files or ``Dockerfile.jp6``."""
        for rel in VERSIONED_FILES:
            hits = [
                (n, line.strip())
                for n, line in enumerate(
                    read_repo_file(rel).splitlines(), start=1
                )
                if APT_CMAKE_PIN_RE.search(line)
            ]
            assert not hits, (
                f"{rel}: residual apt cmake version pin(s) found — the CMake "
                f"install must not resolve cmake via apt:\n"
                + "\n".join(f"  {rel}:{n}: {line}" for n, line in hits)
            )

    def test_machine_setup_pin_sits_inside_install_cmake(self):
        """**Validates: Requirements 2.3, 3.3** — the host script's pin is the
        install_cmake function's own (scoping sanity for the parser)."""
        text = read_repo_file(MACHINE_SETUP_REL)
        body = extract_function(text, "install_cmake")
        assert CMAKE_VER_RE.findall(text) == CMAKE_VER_RE.findall(body), (
            "machine_setup.sh: CMAKE_VER pin(s) found outside install_cmake"
        )


# --------------------------------------------------------------------------- #
# Layer 3 — arch-selection property (JP6 behavior, exactly)
# --------------------------------------------------------------------------- #
_KNOWN_MACHINES = st.sampled_from(
    ["x86_64", "aarch64", "arm64", "armv7l", "i686", "ppc64le", "s390x", "riscv64"]
)


class TestArchSelectionProperty:
    """For any generated ``uname -m`` value, the modeled arch selection yields
    ``x86_64`` for ``x86_64`` and ``aarch64`` otherwise — matching JP6 — and
    each file's CMake install step carries exactly that conditional shape."""

    @settings(max_examples=300, deadline=None)
    @example("x86_64")
    @example("aarch64")
    @given(st.one_of(_KNOWN_MACHINES, st.text(min_size=0, max_size=12)))
    def test_model_yields_x86_64_only_for_x86_64(self, uname_m):
        """**Validates: Requirements 2.3, 3.3**"""
        selected = select_cmake_arch(uname_m)
        if uname_m == "x86_64":
            assert selected == "x86_64"
        else:
            assert selected == "aarch64"
        # The selected arch is always one of the two published installer archs.
        assert selected in ("x86_64", "aarch64")

    def test_every_cmake_step_encodes_the_modeled_selection(self):
        """**Validates: Requirements 2.3, 3.3** — the literal conditional in
        each of the four files is the modeled two-way selection (test against
        "x86_64", then-branch x86_64, else-branch aarch64), so the model and
        the shipped shell logic agree by construction."""
        for rel in VERSIONED_FILES:
            text = read_repo_file(rel)
            if rel == MACHINE_SETUP_REL:
                step = extract_function(text, "install_cmake")
            else:
                steps = cmake_install_steps(text)
                assert len(steps) == 1, (
                    f"{rel}: expected exactly one CMake install step, "
                    f"found {len(steps)}"
                )
                step = steps[0]
            assert ARCH_CONDITIONAL_RE.search(step), (
                f"{rel}: CMake install step lacks the JP6 arch-selection "
                f"conditional (x86_64 -> x86_64, else aarch64). Step:\n{step}"
            )
