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
"""Bug-condition exploration static tests for ``edgemlsdk-cmake-pin-failure``
(task 1) — **Property 1: Bug Condition — Pinned Upstream Release-Binary CMake
Install**.

These tests assert the FIXED predicate from the design's fix-checking
pseudocode: no ``apt.kitware.com`` CMake resolution and no
``cmake=3.21.3-0kitware*`` pin anywhere in the affected files; each CMake
install step uses the pinned GitHub-release installer with ``uname -m`` arch
selection and ``--skip-license --prefix=/usr/local``; each Dockerfile CMake
step ends with an in-build ``cmake --version``.

On the UNFIXED tree they MUST FAIL for ``src/edgemlsdk/Dockerfile`` (both OS
branches), ``src/edgemlsdk/Dockerfile.jp5``, and ``machine_setup.sh`` —
failure confirms bug condition C(X) and surfaces the exact Kitware apt lines
and dead 3.21.3 pins as counterexamples, matching the live evidence: portal
build job ``40b036fc-c379-4a70-b00e-e6d6b0d46ecb`` (AMD64, 2026-08-08,
BUILD_FAILED exit 1, apt exit code 100 on
``cmake=3.21.3-0kitware1ubuntu20.04.1``). The ``Dockerfile.jp6`` ¬C anchor
MUST PASS on the unfixed tree, anchoring the target JP6 pattern.

These are NOT throwaway inverted tests: they encode the expected behavior and
are re-run unchanged as the fix check in task 5.1.

Counterexamples found on the unfixed tree are recorded in
``.kiro/specs/edgemlsdk-cmake-pin-failure/exploration-notes.md``.
"""
from _cmake_support import (
    AFFECTED_FILES,
    BIONIC_PIN,
    DOCKERFILE_JP5_REL,
    DOCKERFILE_JP6_REL,
    DOCKERFILE_REL,
    FOCAL_PIN,
    KITWARE_APT_HOST,
    KITWARE_PIN_MARKER,
    MACHINE_SETUP_REL,
    REPO_ROOT,
    assert_fixed_cmake_step,
    assert_no_kitware_apt_resolution,
    cmake_install_steps,
    extract_function,
    kitware_lines,
    read_repo_file,
)
import os


def _assert_dockerfile_fixed(rel_path):
    """The full FIXED predicate for one Dockerfile."""
    text = read_repo_file(rel_path)
    assert_no_kitware_apt_resolution(rel_path, text)
    steps = cmake_install_steps(text)
    assert steps, f"{rel_path}: no CMake install step found"
    for step in steps:
        assert_fixed_cmake_step(rel_path, step, dockerfile=True)


class TestDockerfileAmd64:
    """Exploration cases 1 and 2: ``src/edgemlsdk/Dockerfile`` (AMD64 builds).

    EXPECTED ON UNFIXED TREE: FAIL — the single if/else CMake ``RUN`` block
    adds ``apt.kitware.com`` and pins ``cmake=3.21.3-0kitware1ubuntu18.04.1``
    (bionic branch) / ``cmake=3.21.3-0kitware1ubuntu20.04.1`` (focal branch),
    neither of which the Kitware repo still serves (apt exit 100).
    """

    def test_bionic_branch_uses_pinned_upstream_release_binary(self):
        """**Validates: Requirements 1.2, 2.1, 2.3**

        The 18.04/bionic CMake install must not pin the dead
        ``...0kitware1ubuntu18.04.1`` apt package and must use the JP6-pattern
        GitHub-release installer."""
        text = read_repo_file(DOCKERFILE_REL)
        hits = [
            (n, l)
            for n, l in enumerate(text.splitlines(), start=1)
            if BIONIC_PIN in l
        ]
        assert not hits, (
            f"bug condition C(X): {DOCKERFILE_REL} 18.04 branch pins the dead "
            f"Kitware apt package (apt exit 100). Counterexample lines:\n"
            + "\n".join(f"  {DOCKERFILE_REL}:{n}: {l.strip()}" for n, l in hits)
        )
        _assert_dockerfile_fixed(DOCKERFILE_REL)

    def test_focal_branch_uses_pinned_upstream_release_binary(self):
        """**Validates: Requirements 1.1, 2.1, 2.3**

        The 20.04/focal CMake install must not pin the dead
        ``...0kitware1ubuntu20.04.1`` apt package (the exact failure logged by
        job 40b036fc) and must use the JP6-pattern GitHub-release installer."""
        text = read_repo_file(DOCKERFILE_REL)
        hits = [
            (n, l)
            for n, l in enumerate(text.splitlines(), start=1)
            if FOCAL_PIN in l
        ]
        assert not hits, (
            f"bug condition C(X): {DOCKERFILE_REL} 20.04 branch pins the dead "
            f"Kitware apt package — the exact apt exit 100 failure verified "
            f"live on portal build job 40b036fc-c379-4a70-b00e-e6d6b0d46ecb. "
            f"Counterexample lines:\n"
            + "\n".join(f"  {DOCKERFILE_REL}:{n}: {l.strip()}" for n, l in hits)
        )
        _assert_dockerfile_fixed(DOCKERFILE_REL)


class TestDockerfileJp5:
    """Exploration case 3: ``src/edgemlsdk/Dockerfile.jp5`` (JP5/arm64).

    EXPECTED ON UNFIXED TREE: FAIL — the "── 2. CMake 3.21.3 from Kitware ──"
    step adds the Kitware apt list and pins the dead focal 3.21.3 packages.
    """

    def test_jp5_cmake_step_uses_pinned_upstream_release_binary(self):
        """**Validates: Requirements 1.3, 2.2, 2.3**"""
        _assert_dockerfile_fixed(DOCKERFILE_JP5_REL)


class TestMachineSetupInstallCmake:
    """Exploration case 4: ``machine_setup.sh``'s ``install_cmake``.

    EXPECTED ON UNFIXED TREE: FAIL — the 18.04 branch adds ``apt.kitware.com``
    and installs an UNPINNED ``cmake``, so whatever the repo currently serves
    gets installed (CMake 4.x, Triton-incompatible) or the install fails
    outright on retired distributions.
    """

    def test_install_cmake_uses_pinned_upstream_release_binary(self):
        """**Validates: Requirements 1.5, 2.4**"""
        text = read_repo_file(MACHINE_SETUP_REL)
        body = extract_function(text, "install_cmake")
        assert_no_kitware_apt_resolution(MACHINE_SETUP_REL, body)
        # The whole file must be Kitware-apt free, not just the function.
        assert_no_kitware_apt_resolution(MACHINE_SETUP_REL, text)
        assert_fixed_cmake_step(
            MACHINE_SETUP_REL, body, dockerfile=False, sudo_var="$do_sudo"
        )


class TestDockerfileJp6Anchor:
    """Exploration case 5 (¬C anchor): ``src/edgemlsdk/Dockerfile.jp6`` is
    already fixed — pinned CMake 3.31.6 from the GitHub release binary, no
    Kitware apt CMake resolution.

    EXPECTED ON UNFIXED TREE: PASS — anchoring the target pattern the three
    affected files must replicate.
    """

    def test_jp6_already_satisfies_fixed_predicate(self):
        """**Validates: Requirements 2.1, 2.3** (target-pattern anchor)"""
        _assert_dockerfile_fixed(DOCKERFILE_JP6_REL)


class TestRepoWideScanGuard:
    """Root-cause scoping guard: the ONLY files under ``src/edgemlsdk``
    involving the Kitware apt repo are the three identified affected files.
    If additional CMake install sites existed, the root cause analysis would
    be incomplete and we would re-hypothesize.

    EXPECTED: PASS on both the unfixed tree (hits confined to the three known
    files) and the fixed tree (no hits at all).
    """

    def test_no_additional_kitware_cmake_sites(self):
        """**Validates: Requirements 1.4**"""
        edgemlsdk_root = os.path.join(REPO_ROOT, "src", "edgemlsdk")
        offenders = []
        for dirpath, dirnames, filenames in os.walk(edgemlsdk_root):
            dirnames[:] = [d for d in dirnames if d not in (".git", "build")]
            for fname in filenames:
                path = os.path.join(dirpath, fname)
                try:
                    with open(path, encoding="utf-8", errors="ignore") as fh:
                        content = fh.read()
                except OSError:
                    continue
                if KITWARE_APT_HOST in content or KITWARE_PIN_MARKER in content:
                    rel = os.path.relpath(path, REPO_ROOT).replace(os.sep, "/")
                    if rel not in AFFECTED_FILES:
                        offenders.append(rel)
        assert not offenders, (
            "additional Kitware-apt CMake install sites found outside the "
            f"three files identified by isBugCondition: {offenders} — "
            "re-hypothesize the root cause before fixing."
        )

    def test_unfixed_counterexample_inventory_is_scoped(self):
        """**Validates: Requirements 1.1, 1.2, 1.3, 1.5**

        Sanity on the exploration itself: every Kitware hit in the affected
        files sits inside a CMake install step / the install_cmake function —
        i.e. the bug condition is exactly the four identified install steps.
        PASSES on both trees (on the fixed tree there are no hits at all)."""
        for rel in (DOCKERFILE_REL, DOCKERFILE_JP5_REL):
            text = read_repo_file(rel)
            in_steps = "\n".join(cmake_install_steps(text))
            for n, line in kitware_lines(text):
                # The RUN parser strips line-continuation backslashes, so
                # normalize the raw line the same way before matching.
                needle = line.strip().rstrip("\\").strip()
                assert needle in in_steps, (
                    f"{rel}:{n}: Kitware reference outside any CMake install "
                    f"step: {line.strip()} — widen the fix scope."
                )
        text = read_repo_file(MACHINE_SETUP_REL)
        body = extract_function(text, "install_cmake")
        for n, line in kitware_lines(text):
            assert line.strip() in body, (
                f"{MACHINE_SETUP_REL}:{n}: Kitware reference outside "
                f"install_cmake: {line.strip()} — widen the fix scope."
            )
