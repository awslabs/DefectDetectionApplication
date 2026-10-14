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
"""Shared helpers for the ``edgemlsdk-cmake-pin-failure`` spec tests.

Bug condition C(X): a CMake installation step X in an edgemlsdk Dockerfile or
setup script resolves CMake through the Kitware apt repository
(``apt.kitware.com``), so the outcome depends on what that repo currently
serves — the pinned ``3.21.3-0kitware1ubuntu{18,20}.04.1`` package versions are
no longer published (apt exit 100; verified live on portal build job
``40b036fc-c379-4a70-b00e-e6d6b0d46ecb``, AMD64, 2026-08-08), and the unpinned
host-script path installs whatever the repo serves today (CMake 4.x,
Triton-incompatible).

These helpers parse the affected files as TEXT (no Docker builds — the spec's
validation constraint) and provide the FIXED-predicate assertion from the
design's fix-checking pseudocode. Import-light so the tests run under
``pytest ... --noconftest`` without pulling in the backend package.
"""
import os
import re

# edgemlsdk_cmake/ -> backend-test/ -> test/ -> repo root (3 up).
REPO_ROOT = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..")
)

# The three files with the bug condition plus the already-fixed JP6 anchor.
DOCKERFILE_REL = "src/edgemlsdk/Dockerfile"
DOCKERFILE_JP5_REL = "src/edgemlsdk/Dockerfile.jp5"
DOCKERFILE_JP6_REL = "src/edgemlsdk/Dockerfile.jp6"
MACHINE_SETUP_REL = "src/edgemlsdk/src/utilities/machine_setup.sh"

AFFECTED_FILES = (DOCKERFILE_REL, DOCKERFILE_JP5_REL, MACHINE_SETUP_REL)

KITWARE_APT_HOST = "apt.kitware.com"
KITWARE_PIN_MARKER = "0kitware"  # matches cmake=3.21.3-0kitware1ubuntu*.04.1
BIONIC_PIN = "cmake=3.21.3-0kitware1ubuntu18.04.1"
FOCAL_PIN = "cmake=3.21.3-0kitware1ubuntu20.04.1"

# The JP6 pattern's self-contained GitHub-release installer, e.g.
# https://github.com/Kitware/CMake/releases/download/v${CMAKE_VER}/cmake-${CMAKE_VER}-linux-${CM_ARCH}.sh
GITHUB_RELEASE_INSTALLER_RE = re.compile(
    r"github\.com/Kitware/CMake/releases/download/v\S+/cmake-\S+-linux-\S+\.sh"
)


def read_repo_file(rel_path):
    with open(os.path.join(REPO_ROOT, rel_path), encoding="utf-8") as fh:
        return fh.read()


# --------------------------------------------------------------------------- #
# Dockerfile parsing
# --------------------------------------------------------------------------- #
def parse_run_instructions(dockerfile_text):
    """Return every ``RUN`` instruction of a Dockerfile as a single logical
    string (backslash continuation lines joined), in file order."""
    logical_lines = []
    pending = ""
    for raw in dockerfile_text.splitlines():
        line = pending + raw
        # A trailing backslash continues the logical line (Dockerfile syntax).
        if line.rstrip().endswith("\\"):
            pending = line.rstrip()[:-1] + "\n"
            continue
        logical_lines.append(line)
        pending = ""
    if pending:
        logical_lines.append(pending)
    return [l for l in logical_lines if l.lstrip().upper().startswith("RUN ")]


def is_cmake_install_step(run_text):
    """True when a RUN instruction INSTALLS CMake (as opposed to merely
    invoking ``cmake`` as a build tool): an apt install of the ``cmake``
    package (pinned or unpinned), any Kitware apt repo involvement, or the
    GitHub-release installer download."""
    if KITWARE_APT_HOST in run_text or KITWARE_PIN_MARKER in run_text:
        return True
    if GITHUB_RELEASE_INSTALLER_RE.search(run_text):
        return True
    return bool(re.search(r"apt-get\s+install[^&|;]*[\s=]cmake\b", run_text))


def cmake_install_steps(dockerfile_text):
    return [r for r in parse_run_instructions(dockerfile_text) if is_cmake_install_step(r)]


# --------------------------------------------------------------------------- #
# Shell-script parsing
# --------------------------------------------------------------------------- #
def extract_function(sh_text, name):
    """Return the full text of shell function ``name`` (from ``name()`` through
    its closing ``}`` at column 0)."""
    lines = sh_text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if re.match(rf"^{re.escape(name)}\s*\(\)", line):
            start = i
            break
    assert start is not None, f"function {name}() not found"
    for j in range(start + 1, len(lines)):
        if lines[j].startswith("}"):
            return "\n".join(lines[start : j + 1])
    raise AssertionError(f"closing brace of {name}() not found")


# --------------------------------------------------------------------------- #
# Counterexample reporting
# --------------------------------------------------------------------------- #
def kitware_lines(text):
    """The exact (lineno, line) counterexamples: every line mentioning the
    Kitware apt host or a 0kitware package pin."""
    return [
        (n, line)
        for n, line in enumerate(text.splitlines(), start=1)
        if KITWARE_APT_HOST in line or KITWARE_PIN_MARKER in line
    ]


def _format_counterexamples(rel_path, hits):
    return "\n".join(f"  {rel_path}:{n}: {line.strip()}" for n, line in hits)


# --------------------------------------------------------------------------- #
# The FIXED predicate (design "Fix Checking" pseudocode)
# --------------------------------------------------------------------------- #
def assert_no_kitware_apt_resolution(rel_path, text):
    """No apt.kitware.com CMake resolution and no cmake=3.21.3-0kitware* pin
    anywhere in the file. On the unfixed tree this surfaces the exact offending
    lines as counterexamples (live evidence: apt exit 100 on job 40b036fc)."""
    hits = kitware_lines(text)
    assert not hits, (
        f"bug condition C(X) present — {rel_path} resolves CMake via the "
        f"Kitware apt repository (apt exit 100 on the dead 3.21.3 pins, or an "
        f"unpinned 4.x install). Counterexample lines:\n"
        f"{_format_counterexamples(rel_path, hits)}"
    )


def assert_fixed_cmake_step(rel_path, step_text, dockerfile=True, sudo_var=None):
    """The JP6-pattern predicate for one CMake install step: GitHub-release
    installer, ``uname -m`` arch selection, ``--skip-license
    --prefix=/usr/local``, and (Dockerfiles) a trailing in-build
    ``cmake --version`` verification."""
    assert GITHUB_RELEASE_INSTALLER_RE.search(step_text), (
        f"{rel_path}: CMake install step does not download the pinned "
        f"GitHub-release installer "
        f"(github.com/Kitware/CMake/releases/download/v<ver>/"
        f"cmake-<ver>-linux-<arch>.sh). Step:\n{step_text}"
    )
    assert "uname -m" in step_text, (
        f"{rel_path}: CMake install step lacks 'uname -m' arch selection "
        f"(x86_64/aarch64). Step:\n{step_text}"
    )
    assert "--skip-license" in step_text and "--prefix=/usr/local" in step_text, (
        f"{rel_path}: CMake install step lacks "
        f"'--skip-license --prefix=/usr/local'. Step:\n{step_text}"
    )
    if dockerfile:
        assert step_text.rstrip().rstrip(";").endswith("cmake --version"), (
            f"{rel_path}: Dockerfile CMake install step must end with an "
            f"in-build 'cmake --version' verification. Step:\n{step_text}"
        )
    if sudo_var:
        assert re.search(
            rf"\{sudo_var}\s+sh\s+\S*cmake\.sh\s+--skip-license", step_text
        ), (
            f"{rel_path}: host-script CMake install must run the installer via "
            f"'{sudo_var} sh /tmp/cmake.sh --skip-license ...' (mirroring "
            f"install_powershell's $do_sudo convention). Step:\n{step_text}"
        )
